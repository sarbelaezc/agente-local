from __future__ import annotations

import re
import json
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

MESSAGE_SELECTOR = '[role="option"]'
ATTACHMENT_MARKER = "Tiene datos adjuntos"
INVOICE_TERMS = ("factura", "invoice", "recibo", "cfdi", "comprobante")
WEEKDAYS = {"lun": 0, "mar": 1, "mie": 2, "jue": 3, "vie": 4, "sab": 5, "dom": 6}
TIME_PATTERN = re.compile(r"\d{1,2}:\d{2}\s*(?:AM|PM)")
DATE_PATTERN = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")
ATTACHMENT_PATTERN = re.compile(r"^(.+?\.(?:pdf|xml|zip))\s+Abrir\s+[\d.,]+\s*(?:KB|MB)", re.IGNORECASE)


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
        date_match = DATE_PATTERN.search(label)
        if date_match is None:
            return None
        return _parse_date_token(date_match.group(0), today)
    before = label[: match.start()].split()
    token = before[-1] if before else ""
    if DATE_PATTERN.fullmatch(token):
        return _parse_date_token(token, today)
    weekday = WEEKDAYS.get(_ascii_lower(token)[:3]) if token else None
    if weekday is not None and len(token) <= 4:
        return today - timedelta(days=(today.weekday() - weekday) % 7)
    return None


def _parse_date_token(token: str, today: date) -> date | None:
    day, month, year = (int(part) for part in token.split("/"))
    try:
        return date(year, month, day)
    except ValueError:
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


def filter_inbox_messages(messages: list[InboxMessage], text: str) -> list[InboxMessage]:
    query = _ascii_lower(text.strip())
    if not query:
        raise ValueError("Candidate text filter cannot be empty")
    return [message for message in messages if query in _ascii_lower(message.label)]


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


def _find_message(page, label: str, max_pages: int = 60):
    """Scrolls the virtual list from the top until the message with this label is rendered."""
    page.locator(MESSAGE_SELECTOR).first.evaluate(
        "item => { let box = item.parentElement; "
        "while (box && box.scrollHeight <= box.clientHeight) box = box.parentElement; "
        "if (box) box.scrollTop = 0; }"
    )
    page.wait_for_timeout(800)
    for _ in range(max_pages):
        index = page.locator(MESSAGE_SELECTOR).evaluate_all(
            "(items, label) => items.findIndex(item => item.getAttribute('aria-label') === label)",
            label,
        )
        if index >= 0:
            # Scrolling can change row indexes in Outlook's virtualized list.
            item = page.get_by_role("option", name=label, exact=True)
            if item.count() != 1:
                raise LookupError("Candidate message label is ambiguous")
            item.evaluate("item => item.scrollIntoView({ block: 'center' })")
            page.wait_for_timeout(300)
            conversation = item.get_attribute("data-convid")
            if conversation:
                item = page.locator(
                    f'{MESSAGE_SELECTOR}[data-convid={json.dumps(conversation)}]'
                )
                if item.count() != 1:
                    raise LookupError("Candidate message identifier is ambiguous")
            return item
        changed = page.locator(MESSAGE_SELECTOR).last.evaluate(
            "item => { let box = item.parentElement; "
            "while (box && box.scrollHeight <= box.clientHeight) box = box.parentElement; "
            "if (!box) return false; "
            "const before = box.scrollTop; box.scrollTop += box.clientHeight; "
            "return box.scrollTop !== before; }"
        )
        page.wait_for_timeout(800)
        if not changed:
            break
    return None


def download_attachments(page, label: str, target_dir: Path) -> list[Path]:
    """Opens one candidate message and saves its PDF, XML and ZIP attachments to target_dir."""
    message = _find_message(page, label)
    if message is None:
        raise LookupError("The candidate message is no longer in the inbox list")
    message.evaluate("el => el.click()")
    page.wait_for_timeout(3000)
    if message.get_attribute("aria-selected") != "true":
        raise LookupError("The candidate message did not open in the reading pane")
    target_dir.mkdir(parents=True, exist_ok=True)
    chip_labels = page.locator(MESSAGE_SELECTOR).evaluate_all(
        "items => items.map(item => item.getAttribute('aria-label') || '')"
    )
    saved: list[Path] = []
    saved_names: set[str] = set()
    for index, chip_label in enumerate(chip_labels):
        match = ATTACHMENT_PATTERN.match(chip_label)
        if match is None:
            continue
        file_name = Path(match.group(1)).name
        chip = page.locator(MESSAGE_SELECTOR).nth(index)
        saved.append(_save_with_context_menu(page, chip, target_dir, file_name))
        saved_names.add(file_name)
    for file_name in _attachment_text_names(page):
        if file_name in saved_names:
            continue
        leaf = page.get_by_text(file_name, exact=True).first
        saved.append(_save_with_context_menu(page, leaf, target_dir, file_name))
        saved_names.add(file_name)
    return saved


def _attachment_text_names(page) -> list[str]:
    """Returns attachment file names shown as plain text in the reading pane."""
    return page.evaluate(
        r"""() => {
          const names = new Set();
          for (const el of document.querySelectorAll('body *')) {
            if (el.children.length > 0) continue;
            const text = (el.textContent || '').trim();
            if (!/^[^\\/:*?"<>|\r\n]+\.(pdf|xml|zip)$/i.test(text)) continue;
            const option = el.closest('[role="option"]');
            if (option && !(option.getAttribute('aria-label') || '').startsWith(text)) continue;
            names.add(text);
          }
          return [...names];
        }"""
    )


def _save_with_context_menu(page, target, target_dir: Path, file_name: str) -> Path:
    target.click(button="right")
    download_item = page.locator('[role="menu"]').locator(
        '[role="menuitem"], button'
    ).filter(has_text=re.compile(r"Descargar\s*$")).first
    with page.expect_download() as download_info:
        download_item.click()
    path = target_dir / file_name
    download_info.value.save_as(path)
    return path
