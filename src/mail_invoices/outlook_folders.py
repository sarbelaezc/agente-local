from __future__ import annotations

import re
from dataclasses import dataclass

from playwright.sync_api import Page

from mail_invoices.folders import FiledMessage

TREE_SELECTOR = '[role="treeitem"]'
SUBJECT_SELECTOR = "span.TtcXM"
SYSTEM_FOLDERS = {
    "bandeja de entrada", "correo no deseado", "borradores", "elementos enviados",
    "elementos eliminados", "notas", "conversation history",
}


@dataclass(frozen=True)
class MailFolder:
    path: str
    name: str
    index: int


def folder_paths(rows: list[dict[str, str]]) -> list[MailFolder]:
    """Ignore Favorites duplicates and retain the primary mailbox hierarchy."""
    parents: dict[int, str] = {}
    folders = []
    in_mailbox = False
    mailbox_found = False
    for index, row in enumerate(rows):
        level = int(row["level"] or "1")
        if level == 1:
            in_mailbox = row["id"].startswith("primaryMailboxRoot_")
            mailbox_found = mailbox_found or in_mailbox
            parents.clear()
            continue
        if not in_mailbox:
            continue
        name = re.split(r" : Elementos| - \d+ elemento", row["title"])[0].strip()
        if not name or not row["name"]:
            raise LookupError("Outlook folder has no recognizable name")
        parent = parents.get(level - 1, "")
        if level > 2 and not parent:
            raise LookupError("Outlook folder hierarchy is incomplete")
        path = f"{parent}/{name}" if parent else name
        parents[level] = path
        if row["name"].casefold() not in SYSTEM_FOLDERS:
            folders.append(MailFolder(path, row["name"], index))
    paths = [folder.path for folder in folders]
    if not mailbox_found or len(paths) != len(set(paths)):
        raise LookupError("Primary mailbox folder tree is missing or ambiguous")
    return folders


def discover_folders(page: Page) -> list[MailFolder]:
    """Expand navigation only; never creates folders or changes messages."""
    for _ in range(100):
        collapsed = page.locator(f'{TREE_SELECTOR}[aria-expanded="false"]')
        if not collapsed.count():
            break
        collapsed.first.locator("button").first.evaluate("el => el.click()")
        page.wait_for_timeout(200)
    else:
        raise LookupError("Folder expansion exceeded the pilot limit")
    rows = page.locator(TREE_SELECTOR).evaluate_all(
        """els => els.map(el => ({
        id: el.id, level: el.getAttribute('aria-level') || '',
        title: el.getAttribute('title') || '',
        name: el.getAttribute('data-folder-name') || ''}))"""
    )
    return folder_paths(rows)


def message_metadata(option) -> tuple[str, str]:
    """Read sender and subject from the list row, never from the body."""
    addresses = option.locator('span[title*="@"]').evaluate_all(
        "els => [...new Set(els.map(el => el.getAttribute('title')))]"
    )
    if len(addresses) != 1 or not re.fullmatch(r"[^@\s]+@[^@\s]+", addresses[0]):
        raise LookupError("Message sender is missing or ambiguous")
    subjects = option.locator(SUBJECT_SELECTOR)
    if subjects.count() != 1:
        raise LookupError("Message subject selector is missing or ambiguous")
    subject = subjects.inner_text().strip()
    if not subject:
        raise LookupError("Message subject is empty")
    return addresses[0], subject


def read_message_body(page: Page) -> str:
    """Read only the opened message body, not navigation or other list previews."""
    body = page.get_by_role("document", name="Cuerpo del mensaje", exact=True)
    if body.count() != 1:
        raise LookupError("Message body is missing or ambiguous")
    return body.inner_text()[:6000]


def read_history(page: Page, authorized: list[str], limit: int = 5) -> list[FiledMessage]:
    """Read only visible list metadata in explicitly authorized folders."""
    if not 1 <= limit <= 5:
        raise ValueError("History pilot limit must be between 1 and 5")
    history = []
    for path in authorized:
        matches = [folder for folder in discover_folders(page) if folder.path == path]
        if len(matches) != 1:
            raise LookupError(f"Authorized history folder is unavailable: {path}")
        folder = matches[0]
        item = page.locator(TREE_SELECTOR).nth(folder.index)
        previous = page.locator('[role="option"][data-convid]').evaluate_all(
            "els => els.map(el => el.getAttribute('data-convid')).join('|')"
        )
        item.click()
        page.wait_for_function(
            "el => (el.innerText || '').includes('seleccionado')",
            arg=item.element_handle(),
        )
        page.wait_for_function(
            """previous => [...document.querySelectorAll('[role="option"][data-convid]')]
            .map(el => el.getAttribute('data-convid')).join('|') !== previous""",
            arg=previous,
        )
        page.wait_for_timeout(1200)
        options = page.locator('[role="option"][data-convid]')
        for index in range(min(options.count(), limit)):
            sender, subject = message_metadata(options.nth(index))
            history.append(FiledMessage(sender, subject, path))
    return history
