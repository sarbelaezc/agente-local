from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta

MESSAGE_SELECTOR = '[role="option"]'
ATTACHMENT_MARKER = "Tiene datos adjuntos"
INVOICE_TERMS = ("factura", "invoice", "recibo", "cfdi", "comprobante")
WEEKDAYS = {"lun": 0, "mar": 1, "mie": 2, "jue": 3, "vie": 4, "sab": 5, "dom": 6}
TIME_PATTERN = re.compile(r"\d{1,2}:\d{2}\s*(?:AM|PM)")
DATE_PATTERN = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")


@dataclass(frozen=True)
class InboxMessage:
    unread: bool
    has_attachment: bool
    received: date | None
    label: str


def _ascii_lower(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()


def _received_date(label: str, today: date) -> date | None:
    match = TIME_PATTERN.search(label)
    if match is None:
        return None
    before = label[: match.start()].split()
    token = before[-1] if before else ""
    if DATE_PATTERN.fullmatch(token):
        day, month, year = (int(part) for part in token.split("/"))
        try:
            return date(year, month, day)
        except ValueError:
            return None
    weekday = WEEKDAYS.get(_ascii_lower(token)[:3]) if token else None
    if weekday is not None and len(token) <= 4:
        return today - timedelta(days=(today.weekday() - weekday) % 7)
    return None


def parse_label(label: str, today: date) -> InboxMessage:
    return InboxMessage(
        unread=label.startswith("No leído"),
        has_attachment=ATTACHMENT_MARKER in label,
        received=_received_date(label, today),
        label=label,
    )


def is_candidate(message: InboxMessage, today: date, days: int) -> bool:
    if not message.has_attachment:
        return False
    if message.received is not None and message.received < today - timedelta(days=days):
        return False
    text = _ascii_lower(message.label)
    return any(term in text for term in INVOICE_TERMS)


def scan_inbox(page, days: int, today: date, max_scrolls: int = 40) -> list[InboxMessage]:
    """Reads the message list only: never opens, downloads or moves a message."""
    seen: dict[str, InboxMessage] = {}
    for _ in range(max_scrolls):
        labels = page.locator(MESSAGE_SELECTOR).evaluate_all(
            "items => items.map(item => item.getAttribute('aria-label') || '')"
        )
        before = len(seen)
        for label in labels:
            if label and label not in seen:
                seen[label] = parse_label(label, today)
        dated = [m.received for m in seen.values() if m.received is not None]
        if dated and min(dated) < today - timedelta(days=days):
            break
        if len(seen) == before:
            break
        page.locator(MESSAGE_SELECTOR).last.evaluate(
            "item => { let box = item.parentElement; "
            "while (box && box.scrollHeight <= box.clientHeight) box = box.parentElement; "
            "if (box) box.scrollTop = box.scrollHeight; }"
        )
        page.wait_for_timeout(1200)
    return [m for m in seen.values() if is_candidate(m, today, days)]
