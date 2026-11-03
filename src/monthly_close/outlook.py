from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date
from typing import Callable
from urllib.parse import urlparse

from playwright.sync_api import Locator, Page

from monthly_close.domain import EMAIL, clean_text
from monthly_close.extraction import Mail

ALLOWED_HOSTS = {"outlook.live.com", "outlook.office.com", "outlook.office365.com"}
SELECTOR_NAMES = {
    "search", "all_folders", "all_folders_active", "results_ready", "rows",
    "row_sender", "row_subject", "scroll_container", "selected_row", "message",
    "sender", "subject", "received", "body", "thread_messages",
}


class OutlookError(RuntimeError):
    pass


@dataclass(frozen=True)
class Candidate:
    item_id: str
    sender: str
    subject: str


def validate_url(url: str) -> str:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS
        or parsed.port not in (None, 443) or parsed.username or parsed.password
        or not parsed.path.startswith("/mail/")
    ):
        raise ValueError("Solo se permite Outlook Web HTTPS, sin credenciales en la URL.")
    return url


class OutlookReader:
    """Read only visible Outlook DOM; never send, move or download messages."""

    def __init__(self, page: Page, url: str, selectors: dict[str, str]) -> None:
        self.page = page
        self.origin = urlparse(validate_url(url)).netloc
        if set(selectors) != SELECTOR_NAMES or any(
            not isinstance(value, str) or not value.strip() for value in selectors.values()
        ):
            raise ValueError("Completa todos los selectores de Outlook.")
        self.selectors = selectors

    def guard(self) -> None:
        parsed = urlparse(self.page.url)
        if parsed.scheme != "https" or parsed.netloc != self.origin or not parsed.path.startswith("/mail/"):
            raise OutlookError("Autenticacion o pantalla inesperada. Detenido; inicia sesion manualmente.")
        if self.page.locator(
            "input[type='password'], iframe[src*='captcha'], input[name='otc'], #idDiv_SAOTCS_Proofs"
        ).count():
            raise OutlookError("Autenticacion, MFA o captcha detectado. Detenido.")

    def unique(self, name: str, parent: Locator | None = None) -> Locator:
        self.guard()
        locator = (parent or self.page).locator(self.selectors[name])
        if locator.count() != 1 or not locator.is_visible():
            raise OutlookError(f"Selector ausente o ambiguo: {name}. Detenido.")
        return locator

    def search(self, query: str) -> None:
        search = self.unique("search")
        search.fill(query)
        search.press("Enter")
        self.page.locator(self.selectors["all_folders"]).wait_for(state="visible")
        self.unique("all_folders").click()
        self.page.locator(self.selectors["all_folders_active"]).wait_for(state="visible")
        self.unique("all_folders_active")
        self.page.locator(self.selectors["results_ready"]).wait_for(state="visible")
        self.unique("results_ready")

    def candidates(self) -> list[Candidate]:
        self.guard()
        self.unique("all_folders_active")
        candidates = []
        rows = self.page.locator(self.selectors["rows"])
        for index in range(rows.count()):
            row = rows.nth(index)
            item_id = row.get_attribute("data-itemid")
            if not item_id:
                raise OutlookError("Outlook no expone un identificador de mensaje estable.")
            sender = self.unique("row_sender", row).get_attribute("email")
            if sender is None or not EMAIL.fullmatch(sender):
                raise OutlookError("No se pudo verificar el remitente del resultado.")
            subject = clean_text(self.unique("row_subject", row).inner_text(), 300)
            candidates.append(Candidate(item_id, sender.lower(), subject))
        if len({item.item_id for item in candidates}) != len(candidates):
            raise OutlookError("Resultados duplicados o hilo no expandido.")
        return candidates

    def read(self, candidate: Candidate) -> Mail:
        self.guard()
        rows = self.page.locator(self.selectors["rows"]).filter(
            has=self.page.locator(self.selectors["row_sender"])
        )
        matching = [
            rows.nth(index) for index in range(rows.count())
            if rows.nth(index).get_attribute("data-itemid") == candidate.item_id
        ]
        if len(matching) != 1:
            raise OutlookError("El mensaje cambio o su identificador es ambiguo.")
        matching[0].click()
        self.page.wait_for_function(
            """({selector,id}) => {
              const panes = document.querySelectorAll(selector);
              return panes.length === 1 && panes[0].getAttribute('data-itemid') === id;
            }""",
            arg={"selector": self.selectors["message"], "id": candidate.item_id},
        )
        selected = self.unique("selected_row")
        pane = self.unique("message")
        if (
            selected.get_attribute("data-itemid") != candidate.item_id
            or pane.get_attribute("data-itemid") != candidate.item_id
        ):
            raise OutlookError("El panel no corresponde al mensaje seleccionado.")
        if pane.locator(self.selectors["thread_messages"]).count() != 1:
            raise OutlookError("Hilo agrupado o incompleto. Desactiva conversaciones antes de continuar.")
        sender = self.unique("sender", pane).get_attribute("email")
        if sender is None or sender.lower() != candidate.sender:
            raise OutlookError("El remitente abierto no corresponde al resultado.")
        subject = clean_text(self.unique("subject", pane).inner_text(), 300)
        if subject != candidate.subject:
            raise OutlookError("El asunto abierto no corresponde al resultado.")
        received = self.unique("received", pane).get_attribute("datetime")
        if not received:
            raise OutlookError("Fecha sin zona horaria. No se supone que Outlook use UTC.")
        body = clean_text(self.unique("body", pane).inner_text(), 40000)
        self.guard()
        identifier = hashlib.sha256(
            (self.origin + "\n" + candidate.item_id).encode(),
        ).hexdigest()
        return Mail(identifier, candidate.sender, subject, received, body)

    def next_page(self, current: list[Candidate]) -> bool:
        container = self.unique("scroll_container")
        before = [candidate.item_id for candidate in current]
        at_end = container.evaluate(
            "el => el.scrollTop + el.clientHeight >= el.scrollHeight - 2"
        )
        if at_end:
            return False
        container.evaluate("el => { el.scrollTop += el.clientHeight; }")
        self.page.wait_for_function(
            """({selector,before}) => {
              const now = [...document.querySelectorAll(selector)].map(e=>e.getAttribute('data-itemid'));
              return JSON.stringify(now) !== JSON.stringify(before);
            }""", arg={"selector": self.selectors["rows"], "before": before},
        )
        return True

    def walk(
        self, query: str, limit: int, visit: Callable[[Candidate], None],
    ) -> int:
        self.search(query)
        seen: set[str] = set()
        while True:
            current = self.candidates()
            for candidate in current:
                if candidate.item_id in seen:
                    continue
                if len(seen) >= limit:
                    raise OutlookError("Limite de mensajes alcanzado; cobertura parcial. Reduce el rango.")
                visit(candidate)
                seen.add(candidate.item_id)
            if not self.next_page(current):
                return len(seen)


def sender_query(sender: str, start: date, end: date) -> str:
    if not EMAIL.fullmatch(sender) or re.search(r'[()"\\]', sender):
        raise ValueError("Remitente no valido para busqueda.")
    return f"from:({sender}) received:>={start.isoformat()} received:<{end.isoformat()}"
