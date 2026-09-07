"""Conferme esplicite della policy attraverso un componente VOX//OUT.

Il broker non decide quali azioni siano rischiose: riceve dalla policy la
descrizione completa da rileggere. Se il cockpit non e' collegato, scade o
risponde con un valore inatteso, la risposta e' sempre negativa.
"""
from __future__ import annotations

import asyncio
import collections
import logging
import uuid

from core.bus import Bus, Event, Message

log = logging.getLogger(__name__)

# Quanti id ricordare per distinguere «non è mia» da «è mia ed è scaduta».
_ISSUED_MAX = 64


class ConfirmationBroker:
    def __init__(self, bus: Bus, timeout_s: float = 30.0) -> None:
        self._bus = bus
        self._timeout_s = max(0.1, float(timeout_s))
        self._pending: dict[str, asyncio.Future[bool]] = {}
        # Gli id che QUESTO broker ha emesso. Col server collegato sul bus
        # passano anche le conferme sue (§7.4): sono componenti nati la', e la
        # risposta torna la'. Senza questo insieme il broker le scambierebbe
        # per proprie conferme arrivate tardi e riempirebbe il log di avvisi
        # falsi — «non è mia» e «è mia ed è scaduta» sono cose diverse.
        self._issued: collections.OrderedDict[str, None] = collections.OrderedDict()
        bus.subscribe(Event.CONFIRMATION_RESPONSE, self._on_response)

    async def confirm(self, readback: str) -> bool:
        """Mostra la rilettura e attende il click conferma/annulla."""
        if not self._bus.has_subscribers(Event.VOX_OUT):
            log.warning("cockpit non disponibile, conferma negata: %s", readback)
            return False

        component_id = f"confirmation-{uuid.uuid4().hex}"
        future = asyncio.get_running_loop().create_future()
        self._pending[component_id] = future
        self._issued[component_id] = None
        while len(self._issued) > _ISSUED_MAX:
            self._issued.popitem(last=False)
        timeout_seconds = max(1, int(round(self._timeout_s)))
        component = {
            "id": component_id,
            "title": "Conferma richiesta",
            "meta": "azione sensibile",
            "spec": {
                "type": "confirm",
                "title": "Autorizzare questa azione?",
                "text": str(readback),
                "danger": True,
                "timeout": timeout_seconds,
                "confirmLabel": "Conferma",
                "cancelLabel": "Annulla",
            },
        }

        await self._bus.publish(
            Event.CONFIRMATION_REQUESTED,
            {"id": component_id, "readback": str(readback)},
        )
        await self._bus.publish(Event.VOX_OUT, component)
        try:
            answered = await asyncio.wait_for(asyncio.shield(future), self._timeout_s)
        except asyncio.TimeoutError:
            self._pending.pop(component_id, None)
            await self._bus.publish(
                Event.CONFIRMATION_RESPONSE,
                {"id": component_id, "action": "timeout"},
            )
            await self._report(component_id, "scaduta", readback)
            return False
        else:
            if not answered:
                await self._report(component_id, "annullata", readback)
            else:
                await self._bus.publish(
                    Event.VOX_OUT, {"id": component_id, "remove": True})
            return answered
        finally:
            self._pending.pop(component_id, None)

    async def _report(self, component_id: str, esito: str, readback: str) -> None:
        """Una conferma negata deve LASCIARE UN SEGNO, nel log e sullo schermo.

        Prima non ne lasciava nessuno: il broker taceva, lo store non registrava
        niente, e chi usava l'app vedeva solo una risposta evasiva del modello —
        senza modo di distinguere «hai sbagliato tu», «è caduta la rete» e «l'ha
        negata la policy». L'unico modo di ricostruirlo era contare i secondi
        fra due righe di token nel log.

        Il componente si SOSTITUISCE invece di essere rimosso: stesso `id`,
        quindi il pannello aggiorna l'involucro al posto di svuotarsi. Una
        conferma che sparisce senza dire com'è finita è esattamente ciò che ha
        reso invisibile questo caso.
        """
        log.warning("conferma %s, azione negata: %s", esito, readback)
        await self._bus.publish(Event.VOX_OUT, {
            "id": component_id,
            "title": "Azione non eseguita",
            "meta": f"conferma {esito}",
            "spec": {
                "type": "alert",
                "level": "warn",
                "title": f"Conferma {esito}: non ho eseguito",
                "text": str(readback),
            },
        })

    def pending(self) -> int:
        """Numero di conferme in attesa. Sola lettura, usato SOLO dai test
        per rendere osservabile il caso concorrente senza sonde sul
        dizionario privato (06-08); nessun altro cambiamento al broker."""
        return len(self._pending)

    def _on_response(self, msg: Message) -> None:
        payload = msg.payload
        if not isinstance(payload, dict):
            return
        component_id = str(payload.get("id", ""))
        future = self._pending.get(component_id)
        if future is None or future.done():
            # Una conferma che arriva quando nessuno la aspetta piu'. Il caso
            # normale e' l'eco del nostro stesso `timeout`; quello che conta e'
            # l'altro: l'utente ha cliccato, ma la finestra era gia' scaduta —
            # magari di un secondo — e il click cade nel vuoto. Senza questa
            # riga i due casi sono indistinguibili, e il secondo e' il piu'
            # frustrante che ci sia: hai autorizzato, e non e' successo niente.
            if (component_id in self._issued
                    and payload.get("action") in {"confirm", "cancel"}):
                log.warning(
                    "conferma arrivata troppo tardi (finestra gia' chiusa): %s %s — "
                    "l'azione NON e' stata eseguita, va richiesta di nuovo",
                    component_id, payload.get("action"))
            return
        action = payload.get("action")
        if action in {"confirm", "cancel"}:
            future.set_result(action == "confirm")
