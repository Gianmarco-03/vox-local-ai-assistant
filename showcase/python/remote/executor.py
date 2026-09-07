"""§5 del protocollo: il server propone, il client dispone.

Questo è il cuore del client. Il server manda `{skill, args}`; qui, sul
dispositivo che agisce, si decide se quell'azione si fa — allowlist annunciata,
contratto degli argomenti, policy, conferma **riletta**, esecuzione — e si
risponde con esattamente un `result` per `call_id`.

L'ordine dei cinque passi non è uno stile: è il contratto
(`docs/PROTOCOLLO-CLIENT-SERVER.md` §5). Invertirne due significherebbe
eseguire prima di aver giudicato, o giudicare una skill che non era stata
annunciata. Un server compromesso, o un modello che sbaglia, non deve poter
saltare nessuno di questi passi — è la ragione per cui la policy sta qui e non
là (§0.1).
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from brain.policy import Outcome, Policy
from core.contracts import SkillResult, TransientToolError
from core.desktop import DesktopError
from remote.protocol import ExecuteRequest, ExecuteResult

log = logging.getLogger(__name__)

ConfirmFn = Callable[[str], Awaitable[bool]]

# Frasi sicure per l'utente: nessuna nomina un percorso, un token o un dettaglio
# interno. Sono le stesse che il server vedrà come `speech` di un
# `SkillResult(ok=False)` e che il coordinatore leggerà come `blocked`.
SPEECH_DENIED = "Non posso eseguire questa azione su questo dispositivo"
SPEECH_CANCELLED = "Ho annullato"
SPEECH_NO_CONFIRMATION = "Nessuna conferma ricevuta, non ho eseguito"
SPEECH_ERROR = "L'azione non è riuscita"
ERROR_GENERIC = "errore durante l'esecuzione della skill"


class SkillExecutor:
    """Realizza §5 contro il registro VERO, quello locale.

    `accepted` sono le `accepted_skills` del `welcome` (§2): l'intersezione che
    il server ha confermato. Il controllo è doppio — registro **e** annuncio —
    perché sono due domande diverse: «esiste su questa macchina?» e «era fra
    quelle che ho dichiarato in questa sessione?». Una sola delle due lascerebbe
    passare una skill che il modello non avrebbe mai dovuto vedere.
    """

    def __init__(self, registry, policy: Policy, accepted: frozenset[str] | set[str],
                 confirm: ConfirmFn | None = None, bindings: dict[str, str] | None = None,
                 clock: Callable[[], float] = time.perf_counter) -> None:
        self.registry = registry
        self.policy = policy
        self.accepted = frozenset(accepted)
        self.bindings = dict(bindings or {})
        self.confirm = confirm
        self._clock = clock

    async def run(self, request: ExecuteRequest) -> ExecuteResult:
        started = self._clock()

        def elapsed() -> int:
            return round((self._clock() - started) * 1000)

        # 1. annunciata e registrata? Allowlist, mai blocklist.
        local_name = self.bindings.get(request.name, request.name)
        spec = self.registry.get(local_name)
        if spec is None or request.name not in self.accepted:
            reason = ("skill non registrata su questo dispositivo" if spec is None
                      else "skill fuori dalle accepted_skills della sessione")
            log.warning("execute %s rifiutata: %s", request.name, reason)
            return self._denied(request, reason, elapsed())

        # 2. il contratto degli argomenti. Validazione meccanica, mai fiducia.
        if not self.registry.validate_args(local_name, request.args):
            return self._denied(request, "argomenti non conformi al contratto", elapsed())

        # 3. la policy, e la conferma RILETTA, qui sul dispositivo.
        decision = self.policy.judge(local_name, request.args)
        if decision.outcome is Outcome.DENY:
            return self._denied(request, decision.reason, elapsed())
        if decision.outcome is Outcome.NEEDS_CONFIRMATION:
            answer = await self._ask(request, decision.readback)
            if answer == "unavailable":
                # Non «l'utente ha detto no», ma «non c'era modo di chiedere»:
                # e' un rifiuto del client, come in `Policy.authorize`.
                return self._denied(request, "canale di conferma non disponibile", elapsed())
            if answer == "timeout":
                return self._cancelled(request, SPEECH_NO_CONFIRMATION, elapsed())
            if answer == "no":
                return self._cancelled(request, SPEECH_CANCELLED, elapsed())

        # 4. esecuzione, con l'adapter di QUESTA piattaforma.
        try:
            result = await self.registry.execute(local_name, request.args)
        except DesktopError as exc:
            # `core.desktop.DesktopError` è per contratto già ripulito: è il solo
            # testo di eccezione che ha il diritto di arrivare all'utente.
            return self._error(request, str(exc), elapsed())
        except TransientToolError as exc:
            # §11.5: il client riferisce che il guasto è transitorio; sarà il
            # server a permettere il retry soltanto per una skill read-only.
            log.warning("fallimento transitorio su %s: %s", request.name, exc)
            return self._error(request, ERROR_GENERIC, elapsed(), retryable=True)
        except Exception:
            # Il testo dell'eccezione non esce: §8 vieta stack trace e percorsi.
            log.exception("esecuzione di %s fallita", request.name)
            return self._error(request, ERROR_GENERIC, elapsed())

        # 5. il contenuto letto resta dato, anche attraverso la rete.
        tainted = self.policy.taints_context(local_name)
        if not isinstance(result, SkillResult):
            result = SkillResult(speech=str(result))
        return ExecuteResult(
            call_id=request.call_id,
            ok=bool(result.ok),
            outcome="done" if result.ok else "error",
            speech=result.speech,
            data=result.data,
            synthesize=bool(result.synthesize),
            tainted=tainted,
            error=None if result.ok else result.speech,
            latency_ms=elapsed(),
        )

    # ------------------------------------------------------------------ conferma

    async def _ask(self, request: ExecuteRequest, readback: str) -> str:
        """`yes` | `no` | `timeout` | `unavailable`.

        `no` e `timeout` finiscono entrambi in `cancelled` (§5.3), ma non sono
        la stessa cosa da dire ad alta voce: uno e' una risposta, l'altro un
        silenzio. `unavailable` non e' nessuno dei due — e' il client che si
        rifiuta di chiedere, e vale `denied`.

        La frase che l'utente legge è quella **ricalcolata in locale** (§5.3): se
        il server ne ha mandata una diversa, la sua si scarta e si annota. Non è
        pedanteria — è l'unico punto in cui un server bugiardo potrebbe far
        autorizzare all'utente un'azione diversa da quella che legge.
        """
        if request.readback and request.readback != readback:
            log.warning("readback del server diverso da quello locale su %s: uso il locale",
                        request.name)
        if self.confirm is None:
            # Un controllo che non può essere eseguito non è un controllo
            # superato: senza canale di conferma si nega.
            log.warning("nessun canale di conferma: nego %s", request.name)
            return "unavailable"
        try:
            ok = await asyncio.wait_for(self.confirm(readback), request.timeout_s)
        except asyncio.TimeoutError:
            log.warning("conferma scaduta dopo %.1f s su %s", request.timeout_s, request.name)
            return "timeout"
        except Exception:
            log.exception("canale di conferma fallito su %s", request.name)
            return "unavailable"
        return "yes" if ok else "no"

    # -------------------------------------------------------------------- esiti

    def _denied(self, request: ExecuteRequest, reason: str, latency_ms: int) -> ExecuteResult:
        return ExecuteResult(call_id=request.call_id, ok=False, outcome="denied",
                             speech=SPEECH_DENIED, error=reason, latency_ms=latency_ms)

    def _cancelled(self, request: ExecuteRequest, speech: str,
                   latency_ms: int) -> ExecuteResult:
        return ExecuteResult(call_id=request.call_id, ok=False, outcome="cancelled",
                             speech=speech, latency_ms=latency_ms)

    def _error(self, request: ExecuteRequest, error: str, latency_ms: int,
               retryable: bool = False) -> ExecuteResult:
        return ExecuteResult(call_id=request.call_id, ok=False, outcome="error",
                             speech=SPEECH_ERROR, error=error, latency_ms=latency_ms,
                             retryable=retryable)


def create_executor(registry, policy: Policy, accepted: frozenset[str] | set[str],
                    confirm: ConfirmFn | None = None,
                    bindings: dict[str, str] | None = None) -> SkillExecutor:
    return SkillExecutor(registry, policy, accepted, confirm=confirm, bindings=bindings)


def announced_specs(registry, allowlist: tuple[str, ...] | list[str] | None = None
                    ) -> tuple[Any, ...]:
    """Le skill che questo dispositivo annuncia (§2).

    Con `allowlist` a `None` si annuncia tutto il registro. Con una lista, si
    annuncia l'intersezione: è così che un dispositivo «ospite» espone solo
    skill in sola lettura, senza che il server debba saperne niente. Un nome
    nell'allowlist che il registro non ha non aggiunge niente — restringere è
    l'unica direzione possibile.
    """
    specs = registry.specs()
    if allowlist is None:
        return specs
    wanted = set(allowlist)
    return tuple(s for s in specs if s.name in wanted)
