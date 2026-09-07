"""Il protocollo client ↔ server v1, lato CLIENT.

Contratto: `docs/protocol/vox-ws-protocol.md` per ciò che il server ha già
costruito, compreso l'annuncio del catalogo del Mac, e
`docs/PROTOCOLLO-CLIENT-SERVER.md` §0.1/§3/§5 per l'esecuzione di skill. Questo modulo NON
ridefinisce una porta del sistema (README.md, regola del ramo `client`): traduce
fra il JSON del filo e le dataclass che il client già ha
(`core.contracts.SkillSpec`, `SkillResult`).

Principio 1 del contratto, «fallire chiuso», qui è letterale: ogni `decode_*`
solleva `ProtocolError` invece di indovinare. Un messaggio che non si capisce
non è un messaggio da eseguire.

**Allineamento del 2026-08-30.** L'handshake applicativo non esiste più: niente
`hello`, niente `welcome`, niente sessione a scadenza. Le credenziali viaggiano
nella query string della connessione (`connection_url`), il controllo avviene
prima dell'upgrade WebSocket e il segnale «sono dentro» è l'arrivo dello
`snapshot`. Restano vive, perché il Mac esegue skill, la serializzazione del
catalogo (§3) e la coppia `execute`/`result` (§5).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from core.contracts import SkillSpec
from core.trust import Risk

# La versione 1 è IMPLICITA: la connessione non porta un parametro di versione.
# Non è una dimenticanza del server, è una scelta dichiarata — il parametro entra
# col primo cambiamento incompatibile, insieme al test che lo esercita.
PROTOCOL_VERSION = "1"

# §9.1. Limiti per messaggio. Sono del protocollo, non di questa implementazione:
# non si allargano qui, si allargano nel documento e poi qui.
MAX_JSON_BYTES = 64 * 1024
MAX_BINARY_BYTES = 64 * 1024       # oltre, il server scarta il frame
MAX_TEXT_CHARS = 2000              # `{"text"}`: il server TRONCA, non rifiuta
MAX_ACTION_ID_CHARS = 128          # `vox_out_action.id`: idem
MAX_TEXT_BYTES = 4 * 1024          # `speech` di un `result` (§5)
MAX_DATA_BYTES = 32 * 1024
DATA_TOO_LARGE = "risultato troppo grande"

# §6.1 client → server: PCM puro, senza marcatore, solo fra `ptt down` e `ptt up`.
MIC_SAMPLE_RATE = 16000
MIC_FRAME_SAMPLES = 512
MIC_FRAME_BYTES = MIC_FRAME_SAMPLES * 2

# §6.2 server → client: primo byte `0x01`, poi SOLO PCM. L'ambiguità del vecchio
# documento («0x01 + id») è stata chiusa dal server il 2026-08-29: nel frame non
# c'è nessun identificatore, nessuna lunghezza, nessun numero di sequenza —
# l'`id` viaggia già nei due messaggi testuali che aprono e chiudono la frase.
SPEAK_SAMPLE_RATE = 22050
SPEAK_FRAME_MARKER = 0x01

# §5. Vocabolario chiuso degli esiti. `done` è l'unico che dice «è successo».
OUTCOMES: frozenset[str] = frozenset({"done", "denied", "cancelled", "timeout", "error"})

# Codici di diagnosi LOCALE. Non sono più messaggi: il contratto v1 non ha errori
# applicativi in nessuno dei due versi, e un `{"error"}` mandato al server
# verrebbe ignorato in silenzio. Restano perché distinguono, nel log e
# nell'interfaccia di questo lato, «non ho capito» da «non sei autorizzato».
ERROR_CODES: frozenset[str] = frozenset({"malformed", "unauthorized", "protocol_unsupported"})

# §2. Capability del client: cosa questo dispositivo sa fare, non cosa può fare
# una skill (quelle sono in `loop/capabilities.json`, vocabolario diverso).
# Sono annunciate nel `catalog` insieme alle skill disponibili (§11.1).
CLIENT_CAPABILITIES: frozenset[str] = frozenset({"capture", "playback", "confirm", "cockpit"})

# §4.5. Le sole azioni ammesse su un componente. Allowlist: qualunque altro
# valore non produce nulla lato server, quindi mandarlo è solo rumore.
VOX_OUT_ACTIONS: frozenset[str] = frozenset({"change", "submit", "confirm", "cancel"})

# §5.2. I nove stati del demone, nell'ordine di indice del contratto.
STATES: tuple[str, ...] = (
    "IDLE", "LISTENING", "TRANSCRIBING", "THINKING", "EXECUTING",
    "ELABORATING", "SPEAK", "LONG_TALK", "AWAITING_CONFIRMATION",
)

# §5. I messaggi che il server può mandare: allowlist, mai blocklist.
#
# `line` resta perché è il messaggio del cockpit LOCALE (`ui/bridge.py`), che su
# questa macchina parla lo stesso codec: da un server remoto non arriva mai, e
# riceverlo da lì sarebbe un difetto suo. `execute` resta perché il Mac esegue
# skill su richiesta del server.
SERVER_MESSAGES: frozenset[str] = frozenset({
    "snapshot", "state", "rms", "mic", "commands", "queue",
    "speak", "speak_done", "abort",
    "vox_out", "clear", "agent_update", "line",
    "execute",
})

# §3. I tipi degli argomenti sul filo. È la stessa tabella di
# `skills/registry.py:VALIDATORS`, vista dal lato del nome: un tipo ammesso in
# più è una riga qui e una là, mai un ramo `if` da qualche parte.
TYPE_NAMES: dict[str, type] = {
    "int": int, "str": str, "float": float, "bool": bool, "dict": dict, "list": list,
}
NAME_BY_TYPE: dict[type, str] = {t: n for n, t in TYPE_NAMES.items()}

RISK_NAMES: dict[Risk, str] = {
    Risk.READ_ONLY: "readonly", Risk.REVERSIBLE: "reversible", Risk.IRREVERSIBLE: "irreversible",
}
RISK_BY_NAME: dict[str, Risk] = {n: r for r, n in RISK_NAMES.items()}


class ProtocolError(RuntimeError):
    """Messaggio che non si può eseguire. Porta il codice di diagnosi locale."""

    def __init__(self, text: str, code: str = "malformed", ref: str | None = None) -> None:
        super().__init__(text)
        self.code = code if code in ERROR_CODES else "malformed"
        self.ref = ref


# --------------------------------------------------------------------- §3 catalogo

def spec_to_wire(spec: SkillSpec) -> dict[str, Any]:
    """`SkillSpec` → JSON base; il catalogo inoltrato vive nel JSON statico."""
    unknown = [t for t in spec.args.values() if t not in NAME_BY_TYPE]
    if unknown:
        raise ProtocolError(
            f"skill '{spec.skill_id}': tipo di argomento non serializzabile {unknown}")
    return {
        "skill_id": spec.skill_id,
        "name": spec.name,
        "description": spec.description,
        "args": {k: NAME_BY_TYPE[t] for k, t in spec.args.items()},
        "required_args": list(spec.required_args),
        "capabilities": list(spec.capabilities),
        "risk": RISK_NAMES[spec.risk],
        "level": spec.level,
    }


def catalog_message(payload: Any) -> dict[str, Any]:
    """Busta client → server che annuncia le skill disponibili su questo Mac."""
    if not isinstance(payload, dict):
        raise ProtocolError("catalog: atteso un oggetto JSON")
    required = {"platform", "app_version", "audio", "capabilities", "skills"}
    missing = required - payload.keys()
    if missing:
        raise ProtocolError(f"catalog: campi mancanti {sorted(missing)}")
    if not isinstance(payload["platform"], str) or not payload["platform"]:
        raise ProtocolError("catalog.platform: attesa una stringa non vuota")
    if not isinstance(payload["app_version"], str) or not payload["app_version"]:
        raise ProtocolError("catalog.app_version: attesa una stringa non vuota")
    if not isinstance(payload["audio"], dict):
        raise ProtocolError("catalog.audio: atteso un oggetto JSON")
    if not (isinstance(payload["capabilities"], list)
            and all(isinstance(item, str) for item in payload["capabilities"])):
        raise ProtocolError("catalog.capabilities: attesa una lista di stringhe")
    if not (isinstance(payload["skills"], list)
            and all(isinstance(item, dict) for item in payload["skills"])):
        raise ProtocolError("catalog.skills: attesa una lista di oggetti")
    for index, skill in enumerate(payload["skills"]):
        if skill.get("side") not in {"client", "server"}:
            raise ProtocolError(
                f"catalog.skills[{index}].side: atteso 'client' o 'server'")
    return {"catalog": payload}


def spec_from_wire(payload: Any) -> SkillSpec:
    """JSON di §3 → `SkillSpec`.

    Sul filo vero il client solo serializza (è lui a possedere il registro): il
    verso opposto lo fa il `RemoteSkillRegistry` del server. Vive qui perché è
    ciò che rende dimostrabile, con un test di andata e ritorno, che §3 non
    perde niente per strada.
    """
    if not isinstance(payload, dict):
        raise ProtocolError("SkillSpec: atteso un oggetto JSON")
    try:
        args_raw = payload["args"]
        if not isinstance(args_raw, dict):
            raise ProtocolError("SkillSpec.args: atteso un oggetto JSON")
        unknown = [v for v in args_raw.values() if v not in TYPE_NAMES]
        if unknown:
            raise ProtocolError(f"SkillSpec.args: tipi non nel vocabolario {unknown}")
        risk = payload.get("risk", "irreversible")
        if risk not in RISK_BY_NAME:
            raise ProtocolError(f"SkillSpec.risk sconosciuto: {risk!r}")
        return SkillSpec(
            skill_id=str(payload["skill_id"]),
            name=str(payload["name"]),
            description=str(payload["description"]),
            args={k: TYPE_NAMES[v] for k, v in args_raw.items()},
            required_args=tuple(payload.get("required_args", ())),
            capabilities=tuple(payload.get("capabilities", ())),
            risk=RISK_BY_NAME[risk],
            level=str(payload.get("level", "L1")),
        )
    except KeyError as exc:
        raise ProtocolError(f"SkillSpec: campo obbligatorio assente {exc}") from exc


# ------------------------------------------------------------ §3 connessione

def connection_url(*, host: str, port: int, device: str, token: str,
                   secure: bool = False) -> str:
    """L'URL della connessione: è TUTTO l'handshake che il contratto v1 prevede.

    Le credenziali viaggiano nella query string, non in un header e non in un
    primo messaggio: il client apre un WebSocket nudo e il cancello del server
    decide **prima** dell'upgrade. Se non passa, non nasce nessun WebSocket e
    non arriva nessun messaggio applicativo — solo un 401 con una riga sola,
    uguale per ogni motivo (§3.3).

    `secure` esiste per una pagina servita in https, dove il browser rifiuta un
    socket in chiaro. Non è TLS nostro: la riservatezza resta della rete mesh.
    """
    if not host.strip():
        raise ProtocolError("host assente: non c'è un server a cui collegarsi")
    if not isinstance(port, int) or isinstance(port, bool) or not (1024 <= port <= 65535):
        raise ProtocolError(f"porta fuori intervallo: {port!r}")
    if not device:
        raise ProtocolError("device assente: l'identificativo lo emette `/pair` sul PC",
                            code="unauthorized")
    if not token:
        raise ProtocolError("token assente: il codice lo emette `/pair` sul PC",
                            code="unauthorized")
    scheme = "wss" if secure else "ws"
    query = urlencode({"device": device, "token": token})
    return f"{scheme}://{host.strip()}:{port}/?{query}"


@dataclass(frozen=True)
class Snapshot:
    """Il primo messaggio dopo l'apertura (§5.1), e il segnale «sono dentro».

    `device_id` è l'identità che il cancello ha assegnato a QUESTA connessione:
    si confronta con quella configurata, perché se differisce stiamo parlando
    con un pairing diverso da quello che credevamo.

    `deferred` sono le frasi maturate mentre questo dispositivo non c'era, e il
    server le **consuma all'invio**: se non le mostriamo adesso sono perse per
    sempre. Non esiste un riscontro di lettura in questa versione (§8).
    """

    device_id: str = ""
    state: str | None = None
    workspace: str | None = None
    deferred: tuple[str, ...] = ()


def decode_snapshot(payload: Any) -> Snapshot:
    """§5.1. Il caso vuoto ha le stesse quattro chiavi del caso pieno: un client
    non deve avere due percorsi di lettura, e infatti qui non ce ne sono."""
    if not isinstance(payload, dict):
        raise ProtocolError("snapshot: atteso un oggetto JSON")
    missing = {"device_id", "state", "workspace", "deferred"} - payload.keys()
    if missing:
        raise ProtocolError(f"snapshot: campi obbligatori assenti {sorted(missing)}")
    device_id = payload["device_id"]
    if not isinstance(device_id, str):
        raise ProtocolError("snapshot.device_id: atteso testo")
    state = payload.get("state")
    if state is not None and state not in STATES:
        raise ProtocolError(f"snapshot.state fuori dal vocabolario: {state!r}")
    workspace = payload.get("workspace")
    if workspace is not None and not isinstance(workspace, str):
        raise ProtocolError("snapshot.workspace: atteso testo oppure null")
    deferred = payload.get("deferred", [])
    if not isinstance(deferred, list) or not all(isinstance(d, str) for d in deferred):
        raise ProtocolError("snapshot.deferred: attesa una lista di frasi")
    return Snapshot(device_id=device_id, state=state, workspace=workspace,
                    deferred=tuple(deferred))


def decode_state(payload: Any) -> str:
    if payload not in STATES:
        raise ProtocolError(f"state fuori dal vocabolario: {payload!r}")
    return str(payload)


def decode_speak_frame(frame: bytes) -> bytes | None:
    """Un frame binario di voce → il PCM che contiene, o `None` se non è voce.

    §6.2: primo byte `0x01`, poi **solo** PCM `s16le` mono a 22050 Hz. Nessun
    identificatore nel frame — l'`id` è quello del `speak` ancora aperto. Un
    frame il cui primo byte non sia `0x01` non è voce: oggi non ne esistono
    altri, e un client corretto lo ignora invece di interpretarlo.

    Un numero dispari di byte di PCM è un frame troncato: si scarta, non si
    suona mezzo campione.
    """
    if len(frame) < 1 or frame[0] != SPEAK_FRAME_MARKER:
        return None
    pcm = frame[1:]
    if len(pcm) % 2 != 0:
        return None
    return pcm


# --------------------------------------------------------------------- §5 esecuzione

@dataclass(frozen=True)
class ExecuteRequest:
    """La proposta del server. È una PROPOSTA: policy e conferma stanno qui, sul
    dispositivo che agisce (§0.1). `readback` arriva ma non fa testo: il client
    la ricalcola in locale e vince la sua (§5.3)."""

    device: str
    call_id: str
    name: str
    args: dict[str, Any]
    session: str = ""
    readback: str = ""
    timeout_s: float = 30.0


def decode_execute(payload: Any) -> ExecuteRequest:
    if not isinstance(payload, dict):
        raise ProtocolError("execute: atteso un oggetto JSON")
    call_id = payload.get("call_id")
    device = payload.get("device")
    name = payload.get("name")
    if not isinstance(call_id, str) or not call_id:
        raise ProtocolError("execute.call_id assente")
    if not isinstance(device, str) or not device:
        raise ProtocolError("execute.device assente", ref=call_id)
    if not isinstance(name, str) or not name:
        raise ProtocolError("execute.name assente", ref=call_id)
    args = payload.get("args", {})
    if not isinstance(args, dict):
        raise ProtocolError("execute.args: atteso un oggetto JSON", ref=call_id)
    timeout = payload.get("timeout_s", 30.0)
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise ProtocolError("execute.timeout_s: atteso un numero positivo", ref=call_id)
    readback = payload.get("readback", "")
    if readback is not None and not isinstance(readback, str):
        raise ProtocolError("execute.readback: atteso testo", ref=call_id)
    return ExecuteRequest(
        device=device,
        call_id=call_id,
        name=name,
        args=dict(args),
        session=str(payload.get("session", "")),
        readback=readback or "",
        timeout_s=float(timeout),
    )


@dataclass(frozen=True)
class ExecuteResult:
    """Esattamente una risposta per `call_id` (§5)."""

    call_id: str
    ok: bool
    outcome: str
    speech: str = ""
    data: Any = None
    synthesize: bool = False
    tainted: bool = False
    error: str | None = None
    latency_ms: int = 0
    retryable: bool = False

    def __post_init__(self) -> None:
        if self.outcome not in OUTCOMES:
            raise ProtocolError(f"outcome fuori dal vocabolario: {self.outcome!r}")

    def to_wire(self) -> dict[str, Any]:
        data, error = self.data, self.error
        if data is not None and _json_size(data) > MAX_DATA_BYTES:
            # §8: si tronca lato client. Mandare 300 KB e sperare che il server
            # li rifiuti bene è far decidere all'altro un limite che è nostro.
            data, error = None, DATA_TOO_LARGE
        return {"result": {
            "call_id": self.call_id,
            "ok": self.ok,
            "speech": _clip(self.speech, MAX_TEXT_BYTES),
            "data": data,
            "synthesize": self.synthesize,
            "tainted": self.tainted,
            "outcome": self.outcome,
            "error": _clip(error, MAX_TEXT_BYTES) if error is not None else None,
            "latency_ms": self.latency_ms,
            "retryable": self.retryable,
        }}


# --------------------------------------------------------------------- ingresso

def decode(raw: str | bytes) -> tuple[str, Any]:
    """Un messaggio del server → `(kind, payload)`, con `kind` nell'allowlist.

    Le buste con più di una chiave, con una chiave sconosciuta o oltre i 64 KiB
    di §8 non sono messaggi ambigui da interpretare al meglio: sono `malformed`.
    """
    data = raw.encode("utf-8") if isinstance(raw, str) else raw
    if len(data) > MAX_JSON_BYTES:
        raise ProtocolError(f"messaggio oltre {MAX_JSON_BYTES} byte")
    try:
        msg = json.loads(data)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ProtocolError("JSON non valido") from exc
    if not isinstance(msg, dict) or len(msg) != 1:
        raise ProtocolError("atteso un oggetto JSON con esattamente una chiave")
    kind = next(iter(msg))
    if kind not in SERVER_MESSAGES:
        raise ProtocolError(f"messaggio sconosciuto: {kind!r}")
    return kind, msg[kind]


def encode(message: dict[str, Any]) -> str:
    """Busta → testo da spedire. `ensure_ascii=False`: il testo per l'utente è
    italiano, e un accento non è un carattere da nascondere in `\\u00e0`."""
    text = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
    if len(text.encode("utf-8")) > MAX_JSON_BYTES:
        raise ProtocolError(f"messaggio oltre {MAX_JSON_BYTES} byte")
    return text


# ------------------------------------------------------------------ §4 controllo

def ptt(down: bool) -> dict[str, Any]:
    """§4.2. Apre e chiude la finestra di ascolto. I frame del microfono sono
    accettati **solo** fra i due, e fuori vengono scartati in silenzio.

    Il server non risponde: a dire se l'attivazione è valsa è il passaggio a
    `LISTENING`. Se il turno è di un altro dispositivo l'attivazione viene
    ignorata e non accodata (§7.3), quindi mostrare «sto registrando» subito
    dopo il `down` mentirebbe all'utente.
    """
    return {"ptt": "down" if down else "up"}


def listen() -> dict[str, Any]:
    """§4.3. Pulsante del microfono a una pressione: quale dei due versi sia lo
    sa il server. Non fa nulla se il PC ha mandato `{"mic": false}`."""
    return {"listen": "toggle"}


def stop() -> dict[str, Any]:
    """§4.4. Barge-in. Vale solo se il turno in corso è di questo dispositivo:
    altrimenti il server lo ignora e nessuno se ne accorge (§7.2)."""
    return {"stop": True}


def text_input(text: str) -> dict[str, Any]:
    """§4.1. Un turno scritto, che fa lo stesso giro della voce trascritta."""
    return {"text": _clip_chars(text, MAX_TEXT_CHARS)}


def vox_out_action(component_id: str, action: str, value: Any = None) -> dict[str, Any]:
    """§4.5. Risposta a un componente del pannello.

    Un `id` che inizia per `confirmation-` con `confirm`/`cancel` è la risposta
    a una **conferma riletta**: va alla policy del server come autorizzazione,
    portando con sé il dispositivo della connessione, e non passa mai dal
    modello. Un «sì» da un dispositivo che non è il proprietario del turno non
    autorizza niente (§7.4).
    """
    if action not in VOX_OUT_ACTIONS:
        raise ProtocolError(f"azione fuori dal vocabolario: {action!r}")
    payload: dict[str, Any] = {
        "id": _clip_chars(component_id, MAX_ACTION_ID_CHARS),
        "action": action,
    }
    if value is not None:
        payload["value"] = value
    return {"vox_out_action": payload}


# --------------------------------------------------------------------- utilità

def _json_size(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        # Non serializzabile: per il limite conta come «oltre», così il
        # troncamento di §8 scatta invece di far esplodere l'invio.
        return MAX_DATA_BYTES + 1


def _clip_chars(text: str | None, limit: int) -> str:
    """I limiti di §9.1 su `text` e `vox_out_action.id` sono in CARATTERI, non in
    byte: il server tronca lì, e troncare qui alla stessa misura è ciò che rende
    prevedibile cosa arriva. Tagliare in byte spezzerebbe un accento."""
    if not text:
        return ""
    return text[:limit]


def _clip(text: str | None, limit: int) -> str:
    if not text:
        return ""
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", errors="ignore")
