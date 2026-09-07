"""Costruttori delle implementazioni Python registrate su questo processo.

Registrazione esplicita, mai auto-discovery: la lista dev'essere leggibile, e
una skill nuova e' un file suo piu' una riga qui. `skills/registry.py` non
sa quali skill esistono: riceve questa lista e la registra, valida, esegue.

Il catalogo annunciato sul filo ha invece una fonte statica e revisionabile:
`skills/catalog.json`. All'avvio il client verifica che i nomi dei due elenchi
coincidano; il campo `side` del JSON decide quale processo esegue ogni skill.

Le dipendenze (bus, cartella di lavoro, store) le passa il composition root
(`main.py`) attraverso `load_all`; ogni skill vede solo quella che le serve.
"""
from __future__ import annotations

from core.contracts import Skill


def build_skills(bus=None, workspace=None, store=None, machines=None) -> list[Skill]:
    from skills.calculate import CalculateSkill
    from skills.clipboard_read import ClipboardReadSkill
    from skills.clipboard_write import ClipboardWriteSkill
    from skills.close_window import CloseWindowSkill
    from skills.connect_machine import ConnectMachineSkill
    from skills.current_time import CurrentTimeSkill
    from skills.file_search import FileSearchSkill
    from skills.list_directory import ListDirectorySkill
    from skills.media_control import MediaControlSkill
    from skills.memory_read import MemoryReadSkill
    from skills.memory_write import MemoryWriteSkill
    from skills.move_file import MoveFileSkill
    from skills.open_app import OpenAppSkill
    from skills.open_resource import OpenResourceSkill
    from skills.power_control import PowerControlSkill
    from skills.reasoning_mode import ReasoningModeSkill
    from skills.reminder import ReminderSkill
    from skills.screen import ScreenSkill
    from skills.set_volume import SetVolumeSkill
    from skills.show_panel import ShowPanelSkill
    from skills.system_info import SystemInfoSkill
    from skills.timer import TimerSkill
    from skills.trash_file import TrashFileSkill
    from skills.web_search import WebSearchSkill
    from skills.window_control import WindowControlSkill
    from skills.window_list import WindowListSkill

    panel = ShowPanelSkill(bus)
    return [
        OpenAppSkill(),
        SetVolumeSkill(),
        panel,
        CalculateSkill(),
        CurrentTimeSkill(),
        SystemInfoSkill(),
        ListDirectorySkill(workspace, panel.execute),
        MediaControlSkill(),
        WindowListSkill(),
        WindowControlSkill(),
        CloseWindowSkill(),
        ClipboardReadSkill(),
        ClipboardWriteSkill(),
        FileSearchSkill(workspace),
        OpenResourceSkill(workspace),
        MoveFileSkill(workspace),
        TrashFileSkill(workspace),
        ScreenSkill(),
        PowerControlSkill(),
        TimerSkill(),
        ReminderSkill(),
        MemoryReadSkill(store),
        MemoryWriteSkill(store),
        WebSearchSkill(),
        ConnectMachineSkill(store, machines),
        ReasoningModeSkill(store),
    ]
