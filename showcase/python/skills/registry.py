"""Registro delle skill.

Additivo per costruzione: una skill = un file proprio + una riga di
registrazione. Se per aggiungerne una serve toccare il dispatcher, il disegno
e' sbagliato — ed e' esattamente cio' che il gate `registry-coherence` verifica.

Dal C4 (SKL-01) il registro tiene ISTANZE di skill-classe, non funzioni: la
porta e' `core.contracts.Skill`, il contratto e' lo `SkillSpec` esposto da
ogni istanza.

CONFINI. Questo modulo vede solo `core`. In particolare NON importa
`claude_agent_sdk`: la conversione delle skill in tool dell'SDK avviene in
`brain/provider.py`, che e' l'unico file autorizzato. Se il registry conoscesse
l'SDK, l'astrazione `Provider` sarebbe gia' morta.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any

from core.contracts import Skill, SkillResult, SkillSpec  # noqa: F401
from core.trust import Risk, validate_capabilities

log = logging.getLogger(__name__)


class Registry:
    """Adapter di `SkillRegistry`."""

    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}

    # --------------------------------------------------------------- scrittura

    def add(self, skill: Skill) -> None:
        spec = skill.spec
        if spec.name in self._skills:
            raise ValueError(f"skill duplicata: {spec.name}")
        validate_capabilities(spec.skill_id, spec.capabilities)
        if spec.level == "L0" and spec.destructive:
            # Stessa regola del lint: il router deterministico non ha il
            # contesto per giudicare, e saltare la policy e' il modo in cui un
            # errore di trascrizione diventa un danno.
            raise ValueError(
                f"skill '{spec.skill_id}': L0 e destructive non possono coesistere")
        self._skills[spec.name] = skill

    # --------------------------------------------------------------- lettura

    def names(self) -> set[str]:
        return set(self._skills)

    def get(self, name: str) -> SkillSpec | None:
        skill = self._skills.get(name)
        return skill.spec if skill else None

    def specs(self) -> tuple[SkillSpec, ...]:
        return tuple(s.spec for s in self._skills.values())

    def describe(self) -> dict[str, dict[str, str]]:
        """Per il prompt del modello locale. E' CONTESTO, non pesi: aggiungere
        una skill dev'essere una riga qui, non un riaddestramento.

        Le skill L2 non compaiono: sono strumenti del provider (richiedono il
        contesto di un ragionamento in corso), non azioni che il router
        deterministico possa proporre da una frase."""
        return {
            s.name: {"description": s.description,
                     "args": ", ".join(f"{k}: {v.__name__}" for k, v in s.args.items())}
            for s in self.specs() if s.level != "L2"
        }

    def risk_of(self, name: str) -> Risk:
        """Default prudente: cio' che non e' classificato e' irreversibile.
        Un controllo che fallisce non concede."""
        spec = self.get(name)
        return spec.risk if spec else Risk.IRREVERSIBLE

    def validate_args(self, name: str, args: dict[str, Any]) -> bool:
        """Validazione meccanica: e' IL segnale di escalation, non
        l'autovalutazione del modello.

        La tabella VALIDATORS sostituisce una catena di confronti sul tipo:
        aggiungere un tipo ammesso e' una riga li', non un ramo qui.
        """
        spec = self.get(name)
        if spec is None:
            return False
        if set(args) - set(spec.args):
            return False
        for arg_name, arg_type in spec.args.items():
            value = args.get(arg_name)
            if value is None:
                continue                       # gli opzionali restano opzionali
            check = VALIDATORS.get(arg_type, _accept_any)
            if not check(value):
                return False
        return True

    # ------------------------------------------------------------ esecuzione

    async def execute(self, name: str, args: dict[str, Any]) -> SkillResult:
        skill = self._skills[name]
        spec = skill.spec
        accepted = {k: v for k, v in args.items() if k in spec.args}
        if inspect.iscoroutinefunction(skill.execute):
            result = await skill.execute(**accepted)
        else:
            # Le skill sincrone toccano COM e Win32: fuori dal loop asyncio.
            result = await asyncio.to_thread(lambda: skill.execute(**accepted))
        if isinstance(result, SkillResult):
            return result
        # Rete di sicurezza per skill non ancora migrate: una stringa e' la
        # frase da pronunciare.
        return SkillResult(speech=str(result))


def _is_bool(v: Any) -> bool:
    return isinstance(v, bool)


def _is_int(v: Any) -> bool:
    # bool e' sottotipo di int in Python: un True passato dove serve un numero
    # e' quasi sempre un errore di parsing del modello, non un intero.
    return isinstance(v, int) and not isinstance(v, bool)


def _is_str(v: Any) -> bool:
    return isinstance(v, str)


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_dict(v: Any) -> bool:
    return isinstance(v, dict)


def _is_list(v: Any) -> bool:
    return isinstance(v, list)


def _accept_any(_: Any) -> bool:
    return True


VALIDATORS: dict[type, Any] = {
    bool: _is_bool,
    int: _is_int,
    str: _is_str,
    float: _is_number,
    dict: _is_dict,
    list: _is_list,
}


def load_all(bus=None, workspace=None, store=None, machines=None) -> Registry:
    """Costruisce il registro dalla lista di `skills/catalog.py`.

    Questo modulo registra, valida ed esegue; QUALI skill esistono lo dice il
    catalogo, l'unica fonte di verita'. Una skill nuova e' una riga la', non
    qui.

    `bus` (core.bus.Bus) serve alle skill che pubblicano eventi interni,
    come `show-panel`: e' il composition root a passarlo, le altre skill
    non lo vedono. `workspace` e' il callable che espone la cartella corrente
    alle sole skill di filesystem; `store` realizza la porta persistente per
    le sole skill di memoria e di collegamento; `machines` espone le macchine
    configurate alla sola `connect-machine`."""
    from skills.catalog import build_skills

    registry = Registry()
    for skill in build_skills(bus=bus, workspace=workspace, store=store,
                              machines=machines):
        registry.add(skill)
    return registry
