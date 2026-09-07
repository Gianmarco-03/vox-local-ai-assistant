"""Porte e dataclass di scambio delle skill (REQUISITI.md §13).

`SkillSpec` è spostata qui da `skills/registry.py` (C1); il campo `fn` è
sparito con il C4 (SKL-01): una skill è una classe che realizza la porta
`Skill`, e il registro tiene istanze, non funzioni.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from core.trust import Risk, any_sensitive


@dataclass(frozen=True)
class SkillSpec:
    """Il contratto di una skill, in forma neutra rispetto al provider."""

    skill_id: str                       # kebab-case, come il descriptor
    name: str                           # snake_case, il nome del tool
    description: str
    args: dict[str, type] = field(default_factory=dict)
    required_args: tuple[str, ...] = ()  # solo argomenti sempre necessari
    capabilities: tuple[str, ...] = ()
    risk: Risk = Risk.IRREVERSIBLE
    level: str = "L1"                   # L0 | L1 | L2

    @property
    def destructive(self) -> bool:
        return self.risk >= Risk.IRREVERSIBLE

    @property
    def requires_confirmation(self) -> bool:
        # Coerente con il lint del descriptor: destructive o capability
        # sensibili implicano conferma.
        return self.destructive or any_sensitive(self.capabilities)


@dataclass(frozen=True)
class SkillResult:
    """Dataclass di scambio dell'esito di una skill. Adottata dal C4."""

    speech: str                         # la frase da pronunciare
    ok: bool = True
    data: Any = None                    # payload strutturato opzionale
    synthesize: bool = False            # il provider locale deve riassumere i dati


class TransientToolError(RuntimeError):
    """Una skill la solleva per dichiarare il proprio fallimento transitorio
    (Fase 6, docs/MULTIAGENT-QWEN.md §5.5): il coordinatore multi-agent
    (`brain/agent_queue.py`) concede UN solo retry automatico SOLO per
    questa eccezione, mai per un'eccezione generica — l'assenza della
    dichiarazione esplicita significa nessun retry."""


@runtime_checkable
class Skill(Protocol):
    """Una capacità del sistema: una classe con il suo contratto e la sua
    esecuzione (SKL-01, adottata dal C4)."""

    @property
    def spec(self) -> SkillSpec: ...
    def execute(self, **args: Any) -> SkillResult: ...


@runtime_checkable
class SkillRegistry(Protocol):
    """Il registro additivo: una skill = un file + una riga di registrazione.

    Implementata in `skills/registry.py`; nominata da `brain` e `main`.
    """

    def add(self, skill: Skill) -> None: ...
    def names(self) -> set[str]: ...
    def get(self, name: str) -> SkillSpec | None: ...
    def specs(self) -> tuple[SkillSpec, ...]: ...
    def describe(self) -> dict[str, dict[str, str]]: ...
    def risk_of(self, name: str) -> Risk: ...
    def validate_args(self, name: str, args: dict[str, Any]) -> bool: ...
    async def execute(self, name: str, args: dict[str, Any]) -> Any: ...
