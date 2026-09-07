"""L'astrazione del core cognitivo, e l'UNICO file che vede `claude_agent_sdk`.

E' la regola piu' importante del progetto, e `loop/boundaries.json` la rende
verificabile: se l'SDK comparisse in dieci file, l'astrazione sarebbe gia'
morta e nessuno se ne sarebbe accorto.

La ragione non e' estetica. Il 14 maggio 2026 Anthropic ha annunciato che
Agent SDK e `claude -p` sarebbero usciti dal pool dell'abbonamento dal 15
giugno; il 15 giugno ha sospeso la modifica lo stesso giorno, dicendo che la
rielaborera' con preavviso. Un demone vocale personale e' esattamente il
carico programmatico che quella modifica voleva colpire. Finche' questo
Protocol resta pulito, cambiare rotta costa una riga di `config.toml`.

Dentro questo file stanno tre cose che l'SDK impone e che nessun altro deve
sapere: le opzioni, il ponte skill -> tool MCP in-process, e la callback dei
permessi. La policy vera vive in `brain/policy.py` e non conosce l'SDK.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import re
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol, runtime_checkable
from urllib.request import urlopen
# `urlopen` non e' piu' chiamato direttamente da questo file (D-24): resta
# importato qui perche' `brain/qwen_client.py:QwenClient.complete` lo
# richiama con `from brain.provider import urlopen`, un import DIFFERITO
# (a ogni chiamata, non al caricamento del modulo) proprio per restare il
# punto che `tests/test_local_server_provider.py` patcha con
# `patch("brain.provider.urlopen", ...)` — l'oracolo di non-regressione di
# questa estrazione, invariato per contratto di piano.

log = logging.getLogger(__name__)


# --------------------------------------------------------------- il contratto

@dataclass
class Delta:
    """Testo in streaming, da mandare al TTS mentre arriva."""
    text: str


@dataclass
class ToolCall:
    tool: str
    args: dict[str, Any]
    id: str | None = None


@dataclass
class Result:
    text: str = ""
    session: str | None = None
    cost_usd: float | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    turns: int | None = None
    model: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Provider(Protocol):
    """Un cervello. Claude, un modello locale, o qualsiasi altra cosa."""

    async def run(self, prompt: str, session: str | None = None
                  ) -> AsyncIterator[Delta | ToolCall | Result]:
        """Emette Delta durante la generazione, ToolCall quando esegue, e
        chiude con esattamente un Result."""
        ...


_JSON_TYPES = {
    bool: "boolean",
    int: "integer",
    float: "number",
    str: "string",
    dict: "object",
    list: "array",
}


def _input_schema(spec) -> dict[str, Any]:
    """Schema JSON esplicito per il tool dell'SDK.

    Gli argomenti delle skill sono opzionali per contratto (`validate_args`
    tratta None come assenza: e' la skill a esigere le sue combinazioni), ma
    l'SDK, davanti a un dict di tipi, li marcherebbe TUTTI `required` — e una
    skill come set_volume, che ne vuole esattamente uno, diventerebbe
    inchiamabile (trappola trovata chiudendo il C6, 2026-08-24).
    """
    return {
        "type": "object",
        "properties": {name: {"type": _JSON_TYPES.get(t, "string")}
                       for name, t in spec.args.items()},
        "required": list(spec.required_args),
    }


class _TaintState:
    """Stato di taint DENTRO una singola esecuzione (query SDK), piano 06-18
    Task 2: chiude il gap trovato da 06-VERIFICATION.md truth 8 — il tier
    Claude non aveva una barriera di taint INTRA-task equivalente a quella
    del tier locale (`AgentRunner._effective_names(tainted=True)`).

    Mai un campo condiviso sul provider (`self.xxx`): N `run_agent`
    concorrenti (D-23, Fase 6) condividono lo stesso `ClaudeProvider`, e un
    flag su `self` negherebbe i tool di un agente per cio' che ha letto un
    altro. Vive invece dentro il turno."""

    __slots__ = ("tainted",)

    def __init__(self) -> None:
        self.tainted = False


# ------------------------------------------------- implementazione: Claude

class ClaudeProvider:
    # Piano 06-17 (chiude il TO-DO di processo n. 6): quante negazioni
    # dell'hook PreToolUse restano in memoria senza mai essere reclamate da
    # un blocco di risultato (es. una richiesta di conferma la cui risposta
    # l'SDK non riporta mai come tool_result, o un run interrotto a meta').
    # Un demone che gira tutto il giorno non puo' lasciare crescere questo
    # buffer senza limite: e' un difetto di per se', non solo un dettaglio
    # implementativo (<behavior> del piano).
    _DENIAL_BUFFER_MAX = 128

    def __init__(self, config, registry, policy, system_prompt: str = "",
                 server_name: str = "vox", workspace=None) -> None:
        self.config = config
        self.registry = registry
        self.policy = policy
        self.system_prompt = system_prompt
        self.server_name = server_name
        # Callable senza argomenti -> str | None: la cartella di lavoro e'
        # stato applicativo (vive nello store, mutabile da /set-working-directory),
        # quindi si legge a ogni giro, non si congela alla costruzione.
        self.workspace = workspace
        self._server = None
        # Buffer delle negazioni dell'hook PreToolUse, correlato per
        # identificatore della tool call (piano 06-17): `_run_with_options`
        # lo consulta quando arriva il blocco di risultato corrispondente,
        # per distinguere un rifiuto della NOSTRA policy (denied=True, la
        # ragione dell'utente) da un fallimento logico della skill
        # (denied=False, la ragione dal contenuto del blocco). Condiviso fra
        # tutte le run_agent concorrenti sul provider: correlato per id,
        # mai per ordine di arrivo, quindi due run intrecciate non si
        # scambiano gli esiti (T-06-86) anche condividendolo.
        self._tool_denials: OrderedDict[str, tuple[str, str]] = OrderedDict()
        # Barriera di taint INTRA-task (piano 06-18, Task 2): `_build_options`
        # crea uno stato FRESCO a ogni chiamata (una per `run`/`run_agent`) e
        # lo propaga con un `contextvars.ContextVar` — asyncio isola il
        # contesto per Task, ed e' esattamente cosi' che N `run_agent`
        # concorrenti (D-23, ognuno nel proprio Task via
        # `AgentQueue.run`/`asyncio.gather`) non si contaminano a vicenda,
        # pur condividendo lo STESSO metodo `_pre_tool_use_hook` (l'hook
        # registrato in `_build_options` resta sempre `self._pre_tool_use_hook`
        # — nessun wrapper nuovo per chiamata, altrimenti la garanzia di
        # non-shadowing di 06-09 [RESEARCH Pitfall 7] cambierebbe identita' a
        # ogni run e i test che la confrontano [`matchers[0].hooks ==
        # [provider._pre_tool_use_hook]`] smetterebbero di avere senso).
        self._taint_state_var: contextvars.ContextVar[_TaintState | None] = (
            contextvars.ContextVar(f"claude_provider_taint_state_{id(self)}", default=None))
        # Stato di ripiego SOLO per chi chiama `_pre_tool_use_hook`
        # direttamente, fuori da una query costruita con `_build_options`
        # (i test di 06-09/06-17 esistenti, che restano verdi senza
        # modifiche): il percorso reale (`run`/`run_agent`) passa sempre da
        # `_build_options`, che valorizza il ContextVar per quel turno.
        self._fallback_taint_state = _TaintState()

    # -- ponte skill -> tool MCP in-process -------------------------------

    def _build_server(self):
        """Traduce le SkillSpec neutre del registry nei tool dell'SDK.

        Sta qui e non in `skills/registry.py` di proposito: il registry non
        deve sapere quale provider lo consumera'.
        """
        from claude_agent_sdk import ToolAnnotations, create_sdk_mcp_server, tool
        from core.trust import Risk

        tools = []
        for spec in self.registry.specs():
            tools.append(self._wrap(spec, tool, ToolAnnotations, Risk))
        return create_sdk_mcp_server(name=self.server_name, version="0.1.0", tools=tools)

    def _wrap(self, spec, tool_deco, Annotations, Risk):
        registry = self.registry
        policy = self.policy

        async def _handler(args: dict[str, Any]) -> dict[str, Any]:
            from brain.policy import wrap_untrusted
            from core.contracts import SkillResult
            try:
                log.info("tool provider %s args=%s", spec.name,
                         sorted(args) if isinstance(args, dict) else type(args).__name__)
                result = await registry.execute(spec.name, args)
                if isinstance(result, SkillResult):
                    from brain.presentation import present_skill_result
                    await present_skill_result(registry, spec.name, result)
                    # Piano 06-18 (Task 2, azione c): con `data` presente il
                    # modello riceve i dati strutturati, non solo la frase —
                    # senza `data` resta il solo `speech`, come oggi (nessuna
                    # regressione per le skill che non ne hanno).
                    if result.data is not None:
                        text = json.dumps({"speech": result.speech, "data": result.data},
                                          ensure_ascii=False, default=str)
                    else:
                        text = result.speech
                else:
                    text = str(result)
                if policy is not None and policy.taints_context(spec.name):
                    # Marcato UNA volta sola, qui: e' il solo punto in cui il
                    # testo grezzo della skill entra nel prompt del modello
                    # sul tier Claude.
                    text = wrap_untrusted(spec.name, text)
                if isinstance(result, SkillResult) and not result.ok:
                    # Un esito non-ok deve arrivare al modello come tale:
                    # e' cosi' che non nasce un "fatto" detto su un fallimento.
                    return {"content": [{"type": "text", "text": text}], "is_error": True}
                return {"content": [{"type": "text", "text": text}]}
            except Exception as exc:                       # noqa: BLE001
                log.exception("skill %s fallita", spec.name)
                return {"content": [{"type": "text", "text": f"Errore: {exc}"}],
                        "is_error": True}

        _handler.__name__ = spec.name
        return tool_deco(
            spec.name, spec.description, _input_schema(spec),
            annotations=Annotations(readOnlyHint=spec.risk == Risk.READ_ONLY,
                                    destructiveHint=spec.destructive),
        )(_handler)

    # Dall'SDK 0.2.x la CLI inclusa consegna i tool MCP come *deferred*: il
    # modello ne vede il nome ma non lo schema, e per poterli chiamare deve
    # prima caricarli con `ToolSearch`. Senza questa voce l'allowlist e'
    # perfetta e inutile — il modello conclude che il tool "non e' disponibile"
    # e risponde di no. Trovato dal vivo il 2026-08-29 sul ripiego Sonnet del
    # client: il log non mostrava nessuna negazione perche' negazione non ce
    # n'era, la skill non veniva proprio invocata.
    # Non allarga i permessi: `ToolSearch` espone gli schemi dei soli tool gia'
    # in allowlist, e OGNI chiamata vera continua a passare dall'hook
    # `PreToolUse` (cioe' dalla policy e dalla conferma riletta).
    TOOL_SEARCH = "ToolSearch"

    def allowed_tools(self) -> list[str]:
        """Allowlist esplicita, mai blocklist."""
        return [self.TOOL_SEARCH] + [
            f"mcp__{self.server_name}__{n}" for n in sorted(self.registry.names())]

    # -- permessi ----------------------------------------------------------

    async def _pre_tool_use_hook(self, input_data: dict, tool_use_id: str | None,
                                 context: dict,
                                 taint_state: "_TaintState | None" = None) -> dict:
        """Ultima linea prima dell'esecuzione. Traduce la Decision della policy
        nel formato dell'SDK, e non prende decisioni per conto proprio.

        Un `can_use_tool` NON basta: `allowed_tools()` produce, per ogni
        skill, una entry senza specifier ('mcp__vox__nome_skill', l'intero
        tool) — ed e' esattamente la forma che l'SDK auto-approva PRIMA di
        consultare `can_use_tool` (`CanUseToolShadowedWarning`, trovato
        2026-08-26): `policy.authorize` non girava MAI per i tool chiamati da
        Claude, in violazione diretta della conferma esplicita per le azioni
        irreversibili (CLAUDE.md). Un hook PreToolUse non e' soggetto allo
        shadowing: gira per OGNI tool call, qualunque sia `allowed_tools`.

        Barriera di taint INTRA-task (piano 06-18, Task 2, chiude il gap di
        06-VERIFICATION.md truth 8 — il tier locale la ha dal 06-12, questo
        tier no). `taint_state` e' lo stato di taint DI QUESTA esecuzione,
        opzionale: chiamato senza (come fanno i test di 06-09/06-17, e come
        arriva davvero dall'SDK, che non lo conosce) usa lo stato di
        ripiego del provider (`self._fallback_taint_state`) quando nessun
        `_build_options` ha valorizzato il ContextVar per questo turno —
        nessun test verde diventa rosso, la barriera vale comunque sul
        percorso reale (`run`/`run_agent`).

        Ordine dei controlli, lo stesso del tier locale
        (`AgentRunner._execute_tool`): PRIMA la barriera di taint (chi ha
        letto qualcosa di non fidato in questo turno puo' solo mostrarlo),
        POI `policy.authorize`. Il taint si alza DOPO che la policy ha
        ammesso un tool per cui `policy.taints_context(...)` e' vero — MAI
        prima: un tool negato non deve sporcare il contesto per cio' che
        avrebbe letto."""
        from brain.policy import Outcome

        state = taint_state if taint_state is not None else (
            self._taint_state_var.get() or self._fallback_taint_state)

        tool_name = input_data.get("tool_name", "") or ""
        tool_input = input_data.get("tool_input") or {}
        bare = tool_name.split("__")[-1]

        if state.tainted and bare != "show_panel":
            reason = ("tool negato: questo turno ha gia' letto contenuto non "
                      "fidato, puoi solo mostrarlo")
        elif bare == self.TOOL_SEARCH:
            # `ToolSearch` NON e' una skill: carica gli schemi dei tool gia' in
            # allowlist e non compie nessuna azione sul dispositivo. La policy
            # ne cerca il nome nel registro, non lo trova e — giustamente, per
            # come e' fatta — lo nega: il risultato e' un modello cieco su
            # TUTTO il catalogo, che risponde "il tool non e' disponibile".
            # L'esenzione sta QUI e non nella policy perche' la policy deve
            # continuare a non conoscere nomi che non siano skill.
            # Sta dopo la barriera di taint di proposito: quella resta assoluta.
            return {"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
            }}
        else:
            decision = await self.policy.authorize(bare, tool_input)
            if decision.outcome is Outcome.ALLOW:
                if self.policy.taints_context(bare):
                    state.tainted = True
                return {"hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                }}
            reason = decision.reason or "negato dalla policy"
        # Piano 06-17 (TO-DO di processo n. 6): la negazione si registra QUI,
        # correlata all'identificatore della tool call (l'argomento che
        # l'hook riceve, o il campo omonimo nel payload se l'argomento e'
        # assente) — mai per ordine di arrivo. `_run_with_options` la
        # consuma quando arriva il blocco di risultato corrispondente: e'
        # cosi' che `tool_attempts` riporta la ragione della NOSTRA policy
        # (l'utente o la barriera di taint), non il testo che l'SDK
        # confeziona per una chiamata negata nel blocco di risultato. La
        # risposta all'SDK non cambia: l'hook continua a tradurre la
        # decisione, non a prenderne una.
        # La negazione si dice anche nel log, non solo al modello (2026-08-30).
        # Prima l'unica traccia era la RISPOSTA del modello, che di un tool
        # negato parla in modo vago o non parla affatto: per capire perché una
        # ricerca non fosse partita bisognava contare i secondi fra due righe di
        # consumo token. Una riga qui costa niente e chiude l'indagine.
        log.warning("tool negato: %s — %s", bare or "?", reason)
        key = tool_use_id or input_data.get("tool_use_id")
        if key:
            self._tool_denials[key] = (bare, reason)
            while len(self._tool_denials) > self._DENIAL_BUFFER_MAX:
                self._tool_denials.popitem(last=False)
        return {"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }}

    # -- opzioni -----------------------------------------------------------

    def _build_options(self, *, session: str | None = None,
                       system_prompt: str | None = None,
                       allowed_tools: list[str] | None = None,
                       max_turns: int | None = None,
                       max_budget_usd: float | None = None):
        """Costruttore UNICO delle opzioni SDK: sia `_options` (percorso
        principale) sia `agent_options` (Task 1, 06-09, per-agente)
        chiamano SEMPRE questo metodo, mai i kwargs a mano — e' la
        contromisura a RESEARCH Pitfall 7 (`CanUseToolShadowedWarning`,
        gia' trovato e chiuso il 2026-08-26): l'hook `PreToolUse` nasce QUI,
        quindi nessun chiamante nuovo puo' dimenticarlo (verificato dal test
        con `-W error::UserWarning`, che tornerebbe rosso su un percorso
        nuovo che lo riaprisse).

        Il campo dei subagenti dell'SDK (`agents`) non viene MAI impostato,
        qui ne' altrove: il dispatch lo deciderebbe il modello tramite il
        tool "Agent" (D-13), violando la regola che la coda — mai il
        modello — decide quanti agenti girano e quando; gli eventi dei
        subagenti arriverebbero anche annidati sotto `parent_tool_use_id`,
        impossibile far combaciare 1:1 con la coda del tier Qwen (MA-05,
        T-06-48).

        Barriera di taint per-esecuzione (piano 06-18, Task 2): a OGNI
        chiamata crea uno stato di taint FRESCO (`_TaintState()`) e lo
        valorizza in `self._taint_state_var` (un `contextvars.ContextVar`,
        isolato per Task da asyncio) PRIMA di costruire le opzioni — cosi'
        il turno che sta per iniziare parte pulito e non eredita il taint
        di un turno precedente sullo stesso provider, ne' di un `run_agent`
        concorrente. L'hook resta letteralmente `self._pre_tool_use_hook`
        (nessun wrapper per-chiamata): legge lo stato dal ContextVar quando
        viene invocato piu' tardi dall'SDK, DENTRO lo stesso Task in cui
        questo metodo e' stato chiamato."""
        import claude_agent_sdk

        self._taint_state_var.set(_TaintState())

        if self._server is None:
            self._server = self._build_server()

        kwargs: dict[str, Any] = dict(
            model=self.config.get("claude.model", "claude-sonnet-5"),
            system_prompt=self.system_prompt if system_prompt is None else system_prompt,
            mcp_servers={self.server_name: self._server},
            allowed_tools=self.allowed_tools() if allowed_tools is None else allowed_tools,
            include_partial_messages=True,      # necessario per il TTS incrementale
            # NON can_use_tool: sarebbe shadowed dalle entry di allowed_tools
            # che coprono l'intero tool (vedi _pre_tool_use_hook). Il gate
            # sta in un hook PreToolUse, che gira per ogni chiamata.
            hooks={"PreToolUse": [claude_agent_sdk.HookMatcher(hooks=[self._pre_tool_use_hook])]},
            max_turns=(self.config.get("claude.max_turns", 12)
                      if max_turns is None else max_turns),
            # Circuit breaker su OGNI query, principale o per-agente (D-15,
            # CLAUDE.md): e' cosi' che NON si brucia la finestra da 5 ore.
            max_budget_usd=(self.config.get("claude.max_budget_usd", 0.25)
                            if max_budget_usd is None else max_budget_usd),
            # Comandi vocali: serve latenza bassa, non ragionamento profondo
            effort=self.config.get("claude.effort", "low"),
        )
        if session:
            kwargs["resume"] = session
        cwd = self.workspace() if self.workspace is not None else None
        if cwd:
            kwargs["cwd"] = cwd
        return claude_agent_sdk.ClaudeAgentOptions(**kwargs)

    def _options(self, session: str | None):
        return self._build_options(session=session)

    def agent_options(self, *, system_prompt: str, allowed_tools: frozenset[str],
                      max_turns: int, max_budget_usd: float, session: str | None = None):
        """Opzioni per UN agente del piano (Task 1, 06-09, D-15/D-23):
        `allowed_tools` e' l'allowlist RISTRETTA del task (mai quella intera
        del provider), tradotta qui nel formato con prefisso del server MCP
        — stesso principio di `allowed_tools()` sopra, applicato a un
        sottoinsieme esplicito invece che a tutto il registry. `max_turns`/
        `max_budget_usd` vengono dal ruolo (D-15), mai da un valore fisso
        nel codice: in loro assenza il chiamante deve passare i default di
        `[claude]`, questo metodo non ne inventa uno proprio. Nasce da
        `_build_options`: stesso hook, stessa garanzia di non-shadowing."""
        translated = sorted(f"mcp__{self.server_name}__{name}" for name in allowed_tools)
        return self._build_options(session=session, system_prompt=system_prompt,
                                   allowed_tools=translated, max_turns=max_turns,
                                   max_budget_usd=max_budget_usd)

    # -- il giro -----------------------------------------------------------

    async def run(self, prompt: str, session: str | None = None
                  ) -> AsyncIterator[Delta | ToolCall | Result]:
        """Il percorso principale: le opzioni "di sistema" (`_options`).
        Caso particolare di `_run_with_options` — vedi `run_agent` per il
        caso per-agente, stesso corpo, altre opzioni."""
        async for item in self._run_with_options(prompt, self._options(session)):
            yield item

    async def run_agent(self, prompt: str, *, system_prompt: str,
                        allowed_tools: frozenset[str], max_turns: int,
                        max_budget_usd: float, session: str | None = None
                        ) -> AsyncIterator[Delta | ToolCall | Result]:
        """Come `run`, con le opzioni per agente (Task 1, 06-09): stesso
        corpo (`_run_with_options`), non duplicato — `run` e' il caso
        particolare 'opzioni principali' di questo metodo. Produce lo
        stesso tipo di stream di `run` (`Delta`, `ToolCall`, `Result`):
        chi consuma l'uno sa gia' consumare l'altro."""
        options = self.agent_options(system_prompt=system_prompt, allowed_tools=allowed_tools,
                                     max_turns=max_turns, max_budget_usd=max_budget_usd,
                                     session=session)
        async for item in self._run_with_options(prompt, options):
            yield item

    @staticmethod
    def _bare_tool_name(name: str) -> str:
        """Nome NUDO del tool, senza il prefisso `mcp__<server>__` che l'SDK
        usa per i tool MCP — stesso taglio di `_pre_tool_use_hook` sopra,
        cosi' `tool_attempts["tool"]` e' comparabile 1:1 col nome che il
        tier locale produce (`AgentRunner`, dove il nome e' gia' quello del
        registry, mai prefissato)."""
        return (name or "").split("__")[-1]

    @staticmethod
    def _tool_result_text(content) -> str:
        """Estrae un testo leggibile dal contenuto di un `ToolResultBlock`,
        che l'SDK puo' consegnare come stringa o come lista di content
        block (`[{"type": "text", "text": "..."}]`, stessa forma dei blocchi
        assistente)."""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [str(block.get("text", "")) for block in content
                     if isinstance(block, dict) and block.get("type") == "text"]
            return "\n".join(p for p in parts if p)
        return ""

    async def _run_with_options(self, prompt: str, options
                                ) -> AsyncIterator[Delta | ToolCall | Result]:
        from claude_agent_sdk import query
        from claude_agent_sdk.types import ResultMessage, StreamEvent, ToolResultBlock, UserMessage

        chunks: list[str] = []
        # Mappa LOCALE a questo run (id tool call -> nome, cosi' come
        # arrivato dallo stream), popolata quando la tool call INIZIA e
        # consumata quando arriva il suo blocco di risultato: locale alla
        # generator instance di questa chiamata, mai condivisa fra run
        # concorrenti sullo stesso provider (solo `self._tool_denials`,
        # correlato per id, e' davvero condiviso — vedi sopra).
        pending_calls: dict[str, str] = {}
        # Esito di ogni tool call, nello stesso schema a quattro campi del
        # tier locale (piano 06-17, chiude il TO-DO di processo n. 6):
        # riportato in Result.meta["tool_attempts"] su OGNI Result emesso
        # da questo run, in ordine di esecuzione — mai una chiave assente,
        # anche quando resta vuota (nessuna tool call osservata).
        tool_attempts: list[dict[str, Any]] = []

        async for message in query(prompt=prompt, options=options):
            if isinstance(message, StreamEvent):
                event = message.event or {}
                event_type = event.get("type")
                if event_type == "content_block_delta":
                    delta = event.get("delta") or {}
                    if delta.get("type") == "text_delta":
                        text = delta.get("text", "")
                        if text:
                            chunks.append(text)
                            yield Delta(text)
                elif event_type == "content_block_start":
                    # Il demone resta THINKING per tutta l'esecuzione del tool
                    # se questo evento non diventa un ToolCall: e' la forma
                    # grezza dell'evento Anthropic passata tale e quale dal
                    # parser dell'SDK installato (vedi
                    # claude_agent_sdk._internal.message_parser, case
                    # "stream_event": StreamEvent.event = data["event"]),
                    # verificata sui tipi installati e non a memoria
                    # (trovato 2026-08-26).
                    block = event.get("content_block") or {}
                    if block.get("type") == "tool_use":
                        call_id = block.get("id")
                        if call_id:
                            pending_calls[call_id] = block.get("name", "")
                        yield ToolCall(tool=block.get("name", ""),
                                      args=block.get("input") or {},
                                      id=call_id)
            elif isinstance(message, UserMessage):
                # Il BLOCCO DI RISULTATO di una tool call non arriva come
                # StreamEvent, ma come `UserMessage` a se' (tipo dell'SDK
                # verificato sul venv con `inspect`, mai a memoria: la
                # trappola gia' pagata il 2026-08-26 leggendo la forma degli
                # eventi). Piano 06-17: da qui esce l'esito per singola tool
                # call, mai da un tipo dell'SDK — solo dict serializzabili.
                content = message.content if isinstance(message.content, list) else []
                for block in content:
                    if not isinstance(block, ToolResultBlock):
                        continue
                    tool_use_id = block.tool_use_id
                    name = pending_calls.pop(tool_use_id, None) if tool_use_id else None
                    correlation_id = tool_use_id
                    if name is None and pending_calls:
                        # Nessun identificatore utile per correlare: si
                        # prende il tool piu' RECENTE non ancora appaiato
                        # (popitem() senza argomenti = LIFO, l'ultimo
                        # inserito) — mai un nome inventato, mai un esito
                        # attribuito a caso (<action> del piano).
                        correlation_id, name = pending_calls.popitem()
                    if name is None:
                        log.warning(
                            "blocco di risultato tool senza correlazione possibile "
                            "(nessuna tool call in attesa), ignorato")
                        continue
                    bare = self._bare_tool_name(name)
                    denial = (self._tool_denials.pop(correlation_id, None)
                             if correlation_id else None)
                    if denial is not None:
                        _, reason = denial
                        tool_attempts.append({"tool": bare, "ok": False,
                                              "denied": True, "reason": reason})
                    else:
                        is_error = bool(block.is_error)
                        reason = self._tool_result_text(block.content) if is_error else ""
                        tool_attempts.append({"tool": bare, "ok": not is_error,
                                              "denied": False, "reason": reason or None})
            elif isinstance(message, ResultMessage):
                # `usage` e' un dict (vedi ResultMessage dell'SDK): un getattr
                # restituiva sempre None e lo store ha contato 0 token per
                # trenta chiamate vere (trappola trovata misurando, 2026-08-26).
                usage = getattr(message, "usage", None) or {}
                # `input_tokens` conta solo i token NON in cache: senza i campi
                # cache_* il consumo vero resta invisibile. In meta, non in
                # colonne nuove: e' diagnostica, non contabilita'.
                log.info("usage provider: %s", {k: usage.get(k) for k in (
                    "input_tokens", "output_tokens",
                    "cache_creation_input_tokens", "cache_read_input_tokens")})
                yield Result(
                    text="".join(chunks),
                    session=getattr(message, "session_id", None),
                    cost_usd=getattr(message, "total_cost_usd", None),
                    tokens_in=usage.get("input_tokens"),
                    tokens_out=usage.get("output_tokens"),
                    turns=getattr(message, "num_turns", None),
                    model=self.config.get("claude.model"),
                    meta={"usage": usage, "tool_attempts": list(tool_attempts)},
                )


# ------------------------------------------- implementazione: eco, per i test

class EchoProvider:
    """Seconda implementazione registrata, richiesta dal criterio di uscita
    PROV-01 della Fase 0: dimostra che cambiare provider e' una riga di config,
    e permette di provare il giro senza spendere token."""

    def __init__(self, config=None, **_: Any) -> None:
        self.config = config

    async def run(self, prompt: str, session: str | None = None
                  ) -> AsyncIterator[Delta | ToolCall | Result]:
        _, marker, command_block = prompt.rpartition("<comando>")
        command = command_block.partition("</comando>")[0] if marker else prompt
        text = f"eco: {command.strip()[:200]}"
        yield Delta(text)
        yield Result(text=text, session=session, cost_usd=0.0,
                     tokens_in=0, tokens_out=0, turns=0, model="echo")


class QwenProvider:
    """Provider locale tramite llama-server (API compatibile OpenAI).

    E' intenzionalmente un provider di conversazione: il router e la policy
    continuano a gestire i comandi deterministici. Il tool-use agentico resta
    una capacita' del provider Claude finche' non avremo un loop locale con la
    stessa policy fail-closed.

    Non lancia piu' un proprio processo (D-04/D-28): riceve `server`
    (`LocalModelServer`) dal composition root, stesso proprietario unico del
    router T2 e di `LocalServerProvider` — mai un secondo lanciatore sulla
    stessa porta (Pitfall 1).
    """
    def __init__(self, config, registry=None, policy=None, system_prompt: str = "",
                 workspace=None, server=None) -> None:
        self.config = config
        self.server = server
        self.system_prompt = system_prompt + (
            "\n\nLIMITAZIONE DEL PROVIDER QWEN: in questo percorso non hai tool e non puoi "
            "eseguire azioni sul computer. Non scrivere mai 'eseguito:', chiamate di "
            "funzione o frasi che dichiarano un'azione come già compiuta."
        )

    @staticmethod
    def _guard_execution_claim(text: str) -> str:
        """Nega dichiarazioni operative da un provider privo di tool."""
        normalized = text.casefold().replace("\\_", "_")
        fake_tool = "eseguito:" in normalized or bool(re.search(
            r"\b[a-z][a-z0-9_]{2,}\s*\(\s*\{", normalized))
        action_claim = bool(re.match(
            r"^\s*(?:apro|avvio|chiudo|sposto|elimino|cestino|imposto|attivo|"
            r"disattivo|blocco|spengo|riavvio)\b", normalized))
        if fake_tool or action_claim:
            log.warning("Qwen ha dichiarato un'azione senza tool; risposta negata: %r",
                        text[:300])
            return ("Non ho eseguito l'azione: il comando non è stato associato "
                    "con sicurezza a una skill. Prova a ripeterlo.")
        return text

    def _endpoint(self) -> str:
        if self.server is not None:
            self.server.ensure_running()
            return self.server.endpoint()
        return str(self.config.get("local.endpoint", "http://127.0.0.1:18082")).rstrip("/")

    def _complete(self, prompt: str) -> dict[str, Any]:
        endpoint = self._endpoint()
        payload = json.dumps({
            "model": self.config.get("local.model_name", "qwen-local"),
            "messages": [{"role": "system", "content": self.system_prompt},
                         {"role": "user", "content": prompt}],
            "temperature": self.config.get("local.temperature", 0.2),
            "max_tokens": self.config.get("local.max_tokens", 512),
        }).encode("utf-8")
        request = urllib.request.Request(
            f"{endpoint}/v1/chat/completions", data=payload,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.load(response)

    async def run(self, prompt: str, session: str | None = None
                  ) -> AsyncIterator[Delta | ToolCall | Result]:
        response = await asyncio.to_thread(self._complete, prompt)
        text = self._guard_execution_claim(
            response["choices"][0]["message"]["content"].strip())
        usage = response.get("usage") or {}
        if text:
            yield Delta(text)
        yield Result(text=text, session=session, cost_usd=0.0,
                     tokens_in=usage.get("prompt_tokens"),
                     tokens_out=usage.get("completion_tokens"), turns=1,
                     model="qwen-local", meta={"usage": usage})


class _StaticServer:
    """Adapter minimo usato quando nessun `LocalModelServer` reale e'
    iniettato (costruzione diretta di `LocalServerProvider`, es. nei test
    che non passano dal composition root): realizza la stessa interfaccia
    (`ensure_running`/`endpoint`) leggendo l'URL fissato da `[local]
    endpoint` di config, senza gestire alcun processo. Comportamento
    invariato rispetto al vecchio `_resolve_endpoint` prima dell'estrazione
    in `brain/qwen_client.py` (D-24)."""

    def __init__(self, endpoint: str) -> None:
        self._endpoint = endpoint

    def ensure_running(self) -> None:
        return None

    def endpoint(self) -> str:
        return self._endpoint


class LocalServerProvider:
    """Provider locale agentico tramite l'API OpenAI-compatible di llama-server.

    Riceve `server` (`LocalModelServer`, D-04/D-28) dal composition root:
    stesso proprietario unico del router T2 e di `QwenProvider`. Senza
    `server` (costruzione diretta, es. nei test) ricade su `_StaticServer`,
    che legge `local.endpoint` da config — comportamento invariato per chi
    non lo passa.

    Dal piano 06-04 (D-24) e' un involucro sottile: costruisce `QwenClient`
    (solo HTTP, `brain/qwen_client.py`) e `AgentRunner` (il loop di
    tool-use, `brain/agent_runner.py`) con `allowed_tools=None` —
    comportamento IDENTICO a prima dell'estrazione, tutte le skill del
    registry, nessuna allowlist di ruolo. `run()` delega per intero: il
    parsing dei `tool_calls`, la barriera di taint e l'esecuzione via
    policy+registry vivono ORA in `AgentRunner`, non qui."""

    def __init__(self, config, registry=None, policy=None, system_prompt: str = "",
                 workspace=None, server=None) -> None:
        from brain.agent_runner import AgentRunner, RunnerLimits
        from brain.qwen_client import create_qwen_client

        self.config = config
        self.registry = registry
        self.policy = policy
        self.system_prompt = system_prompt
        self.workspace = workspace
        self.server = server
        self.endpoint = config.get("local.endpoint", "http://127.0.0.1:18082").rstrip("/")
        self.model_name = config.get("local.model_name", "qwen-local")
        self.max_tokens = int(config.get("local.max_tokens", 256))
        self.timeout = float(config.get("local.timeout_s", 120))
        self.max_turns = int(config.get("local.max_turns", 6))

        # Stessa factory del coordinatore multi-agent (06-12, Task 2): un
        # solo modo di costruire un QwenClient da config+server, mai due
        # costruzioni indipendenti che potrebbero divergere.
        effective_server = server if server is not None else _StaticServer(self.endpoint)
        self.client = create_qwen_client(config, effective_server)
        self.runner = AgentRunner(
            self.client, registry, policy, system_prompt=system_prompt,
            allowed_tools=None,
            limits=RunnerLimits(max_turns=self.max_turns, max_tokens=self.max_tokens,
                                timeout_s=self.timeout),
        )

    async def run(self, prompt: str, session: str | None = None
                  ) -> AsyncIterator[Delta | ToolCall | Result]:
        async for item in self.runner.run(prompt, session=session):
            yield item


def warn_on_credential_shadowing() -> str | None:
    """La trappola numero uno, e costa soldi veri.

    L'ordine di precedenza e':
      cloud > ANTHROPIC_AUTH_TOKEN > ANTHROPIC_API_KEY > apiKeyHelper > CLAUDE_CODE_OAUTH_TOKEN

    Una chiave API lasciata nell'ambiente da un altro progetto VINCE in
    silenzio sul token dell'abbonamento: continui a lavorare e paghi a consumo
    senza accorgertene. Sta qui perche' e' il provider a decidere quale
    credenziale usare — dichiararlo altrove sarebbe conoscenza sparsa.

    Ritorna il messaggio da mostrare, o None se non c'e' conflitto.
    """
    import os
    if os.environ.get("ANTHROPIC_API_KEY") and os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return ("Sono impostate sia la chiave API sia il token dell'abbonamento: "
                "vince la prima e stai pagando a consumo. "
                "Verifica con: python tools/check_auth.py")
    return None


PROVIDERS = {
    "claude": ClaudeProvider,
    "qwen": QwenProvider,
    "qwen_text": QwenProvider,
    "local": LocalServerProvider,
    "echo": EchoProvider,
}


def create_provider(config, registry=None, policy=None, system_prompt: str = "",
                    workspace=None, provider_name: str | None = None,
                    server=None) -> Provider:
    """Il punto in cui `config.toml` decide quale cervello gira.

    `server` (`LocalModelServer`, D-04/D-28) arriva SOLO a chi parla col
    modello locale (`qwen`/`qwen_text`/`local`): claude ed echo non lo
    vedono, nulla cambia per loro."""
    name = provider_name or config.get("provider.name", "claude")
    if name not in PROVIDERS:
        raise ValueError(f"provider sconosciuto: {name!r}. Disponibili: {sorted(PROVIDERS)}")
    cls = PROVIDERS[name]
    if cls is EchoProvider:
        return cls(config)
    if cls in (QwenProvider, LocalServerProvider):
        return cls(config, registry, policy, system_prompt, workspace=workspace, server=server)
    return cls(config, registry, policy, system_prompt, workspace=workspace)
