"""Policy: chi puo' eseguire cosa, e quando serve una conferma.

CONFINI. Questo modulo NON importa `claude_agent_sdk`: espone decisioni pure,
e' `brain/provider.py` a tradurle nel formato che l'SDK si aspetta. Cosi' la
policy resta la stessa qualunque sia il core cognitivo — che e' il punto.

Principi (CLAUDE.md):
  - allowlist, mai blocklist;
  - conferma esplicita per ogni azione irreversibile, e la conferma vale solo
    se all'utente e' stata RILETTA l'azione, non se ha detto si' a una domanda
    generica;
  - il contenuto letto e' input non fidato: dati, mai istruzioni;
  - un controllo che fallisce non concede.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, Awaitable, Callable

from core.trust import Risk, reads_untrusted

log = logging.getLogger(__name__)


class Outcome(Enum):
    ALLOW = auto()
    NEEDS_CONFIRMATION = auto()
    DENY = auto()


@dataclass(frozen=True)
class Decision:
    outcome: Outcome
    reason: str = ""
    readback: str = ""     # la frase da rileggere all'utente prima di eseguire


ConfirmFn = Callable[[str], Awaitable[bool]]


class Policy:
    def __init__(self, registry, threshold: Risk = Risk.IRREVERSIBLE,
                 confirm: ConfirmFn | None = None) -> None:
        self.registry = registry
        self.threshold = threshold
        self._confirm = confirm

    # ------------------------------------------------------------- giudizio

    def judge(self, tool: str, args: dict[str, Any]) -> Decision:
        """CR-03 (06-REVIEW.md), regola della conferma non specifica (piano
        06-13): la conferma vale solo se all'utente e' stata RILETTA
        l'azione, non se ha detto si' a una domanda generica (CLAUDE.md). Se
        la decisione e' NEEDS_CONFIRMATION e gli argomenti sono vuoti mentre
        lo spec della skill ne dichiara almeno uno (`spec.args`), la
        rilettura (`readback`) non potrebbe mai essere specifica: si nega,
        invece di mostrare una frase generica che l'utente potrebbe
        scambiare per specifica. Se lo spec non dichiara nessun argomento la
        descrizione del tool E' l'azione, e la conferma generica resta
        legittima (comportamento invariato)."""
        spec = self.registry.get(tool)
        if spec is None:
            # Allowlist: cio' che non e' dichiarato non e' permesso.
            return Decision(Outcome.DENY, f"skill '{tool}' non registrata")
        if not self.registry.validate_args(tool, args):
            return Decision(Outcome.DENY, f"argomenti non conformi al contratto di '{tool}'")
        if spec.requires_confirmation or spec.risk >= self.threshold:
            if not args and spec.args:
                return Decision(
                    Outcome.DENY,
                    "conferma non specifica rifiutata: lo spec dichiara argomenti "
                    "ma questa chiamata non ne porta nessuno, la rilettura (readback) "
                    "non potrebbe mai nominare l'azione concreta")
            return Decision(Outcome.NEEDS_CONFIRMATION,
                            f"{spec.risk.name.lower()}", readback=self.readback(spec, args))
        return Decision(Outcome.ALLOW)

    @staticmethod
    def readback(spec, args: dict[str, Any]) -> str:
        """L'azione riletta all'utente. Una conferma su 'vuoi procedere?' non
        e' una conferma: l'utente deve sentire COSA sta autorizzando."""
        if args:
            details = ", ".join(f"{k} {v}" for k, v in args.items() if v is not None)
            return f"{spec.description}. {details}. Confermi?"
        return f"{spec.description}. Confermi?"

    # ----------------------------------------------------------- esecuzione

    async def authorize(self, tool: str, args: dict[str, Any]) -> Decision:
        d = self.judge(tool, args)
        if d.outcome is not Outcome.NEEDS_CONFIRMATION:
            return d
        if self._confirm is None:
            # Nessun canale di conferma cablato: si nega. Un controllo che non
            # puo' essere eseguito non e' un controllo superato.
            log.warning("conferma non disponibile: nego %s(%s)", tool, args)
            return Decision(Outcome.DENY, "canale di conferma non disponibile", d.readback)
        ok = await self._confirm(d.readback)
        # «annullato dall'utente» era una BUGIA quando la conferma scadeva: il
        # canale ritorna un solo booleano, e un `False` da timeout e un `False`
        # da click su Annulla finivano nella stessa frase — che il modello poi
        # riferisce all'utente come se avesse rifiutato lui. Sono due fatti
        # diversi e non vanno riportati uguali (2026-08-30). Qui si dice cio'
        # che e' vero in entrambi i casi; QUALE dei due sia lo dice il log del
        # canale, che e' l'unico posto dove l'informazione esiste davvero.
        return Decision(Outcome.ALLOW if ok else Outcome.DENY,
                        "confermato" if ok else "conferma non ottenuta", d.readback)

    # ------------------------------------------------------ input non fidato

    def taints_context(self, tool: str) -> bool:
        """True se l'esito di questa skill e' testo scritto da qualcun altro.

        Il chiamante deve consegnarlo al modello dentro un tag di contenuto,
        e non deve concedere capability nuove nello stesso turno.
        """
        spec = self.registry.get(tool)
        return bool(spec and reads_untrusted(spec.capabilities))


UNTRUSTED_OPEN = "<contenuto_non_fidato origine=\"{source}\">"
UNTRUSTED_CLOSE = "</contenuto_non_fidato>"


def wrap_untrusted(source: str, text: str) -> str:
    """Marca il contenuto letto come dato. Il modello ha istruzione di non
    eseguirlo; il marcatore rende la regola verificabile invece che sperata."""
    return f"{UNTRUSTED_OPEN.format(source=source)}\n{text}\n{UNTRUSTED_CLOSE}"


def create_policy(config, registry, confirm: ConfirmFn | None = None) -> Policy:
    from core.trust import risk_from_name
    return Policy(
        registry,
        threshold=risk_from_name(config.get("policy.confirmation_threshold", "irreversible")),
        confirm=confirm,
    )
