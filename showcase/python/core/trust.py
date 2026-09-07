"""Rischio e capability: il vocabolario di fiducia del sistema.

Sta in `core` e non in `brain` per una ragione di confini, non di comodo:
le skill devono poter dichiarare cosa toccano senza sapere che esiste un
agente. `boundaries.json` prevede esplicitamente "trust" fra le fondamenta.

Le capability sono un vocabolario CHIUSO, definito in `loop/capabilities.json`.
Aggiungerne una e' una decisione, non un dettaglio: ogni voce e' una cosa in
piu' che l'assistente puo' fare al PC.
"""
from __future__ import annotations

import json
from enum import IntEnum
from pathlib import Path
from typing import TypeVar

ROOT = Path(__file__).resolve().parents[1]
CAPABILITIES_PATH = ROOT / "loop" / "capabilities.json"
T = TypeVar("T")


class Risk(IntEnum):
    """Quanto costa sbagliarsi."""

    READ_ONLY = 0      # legge stato: che ore sono, cosa suona, batteria
    REVERSIBLE = 1     # volume, finestre, apri app
    IRREVERSIBLE = 2   # sposta, elimina, invia, chiudi senza salvare, spegni


RISK_NAMES = {"readonly": Risk.READ_ONLY, "reversible": Risk.REVERSIBLE,
              "irreversible": Risk.IRREVERSIBLE}


def risk_from_name(name: str) -> Risk:
    return RISK_NAMES[name]


class CapabilityError(RuntimeError):
    pass


def load_vocabulary(path: Path | None = None) -> dict[str, dict]:
    """Il vocabolario chiuso. Se il file manca si FALLISCE, non si deduce.

    Un controllo che si inventa la regola che verifica non verifica niente.
    """
    p = path or CAPABILITIES_PATH
    if not p.exists():
        raise CapabilityError(
            f"{p} assente: il vocabolario delle capability e' chiuso e dichiarato, "
            "non deducibile dal codice")
    data = json.loads(p.read_text(encoding="utf-8"))
    caps = data.get("capabilities")
    if not caps:
        raise CapabilityError(f"{p} non dichiara alcuna capability")
    return caps


_VOCABULARY: dict[str, dict] | None = None


def vocabulary() -> dict[str, dict]:
    global _VOCABULARY
    if _VOCABULARY is None:
        _VOCABULARY = load_vocabulary()
    return _VOCABULARY


def validate_capabilities(skill_id: str, declared: tuple[str, ...]) -> None:
    """Fallisce all'import se una skill dichiara una capability inesistente.

    Meglio non partire che partire con un permesso che nessuno ha concesso.
    """
    vocab = vocabulary()
    unknown = [c for c in declared if c not in vocab]
    if unknown:
        raise CapabilityError(
            f"skill '{skill_id}' dichiara capability non nel vocabolario: {unknown}. "
            f"Aggiungerle a {CAPABILITIES_PATH.name} e' una decisione esplicita.")


def is_sensitive(capability: str) -> bool:
    return bool(vocabulary().get(capability, {}).get("sensibile"))


def any_sensitive(capabilities: tuple[str, ...]) -> bool:
    return any(is_sensitive(c) for c in capabilities)


# Le fonti da cui arriva testo scritto da qualcun altro. Il loro contenuto e'
# DATO, mai istruzione: non puo' attivare capability nuove ne' generare da solo
# una tool call.
UNTRUSTED_SOURCES = ("fs.read", "clipboard.read", "screen.ocr", "net", "net.search",
                     "net.fetch", "messaging.read")


def reads_untrusted(capabilities: tuple[str, ...]) -> bool:
    return any(c in UNTRUSTED_SOURCES for c in capabilities)


def untrusted(value: T, *, fonte: str) -> T:
    """Etichetta il punto d'ingresso di dati esterni per i gate statici.

    Il valore resta strutturato e invariato; la policy/provider applica il
    delimitatore testuale quando il risultato entra nel contesto del modello.
    """
    if not fonte:
        raise ValueError("la fonte del contenuto non fidato è obbligatoria")
    return value
