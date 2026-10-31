from __future__ import annotations

from urllib.parse import urlparse

from playwright.sync_api import BrowserContext, Locator, TimeoutError as PlaywrightTimeoutError

from automation.selectors import (
    CHAT_HEADER_SELECTOR,
    COMPOSE_SELECTOR,
    MESSAGE_LIST_SELECTOR,
    SEND_BUTTON_SELECTOR,
)
from config.settings import AllowedChat


class TeamsSafetyError(RuntimeError):
    pass


TEAMS_HOSTS = frozenset(
    {
        "teams.microsoft.com",
        "teams.cloud.microsoft",
        "teams.cloud.microsoft.mcas.ms",
    }
)


def require_teams_origin(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in TEAMS_HOSTS:
        host = parsed.hostname or "unknown"
        raise TeamsSafetyError(f"Unrecognized Teams origin ({host}); automation stopped")


class TeamsWebAutomation:
    def __init__(self, context: BrowserContext, teams_url: str, allowed_chats: tuple[AllowedChat, ...]) -> None:
        self._context = context
        self._teams_url = teams_url
        self._allowed_chats = {chat.id: chat for chat in allowed_chats}

    def open(self) -> None:
        page = self.page
        page.goto(self._teams_url, wait_until="domcontentloaded")
        require_teams_origin(page.url)

    @property
    def page(self):
        if not self._context.pages:
            return self._context.new_page()
        page = self._context.pages[-1]
        return page

    def _resolve_chat(self, chat_id: str) -> AllowedChat:
        chat = self._allowed_chats.get(chat_id)
        if chat is None:
            raise TeamsSafetyError(f"Chat is not authorized: {chat_id}")
        return chat

    def open_chat(self, chat_id: str) -> AllowedChat:
        chat = self._resolve_chat(chat_id)
        page = self.page
        require_teams_origin(page.url)
        locator = page.get_by_text(chat.name, exact=True)
        matches = locator.count()
        if matches != 1:
            raise TeamsSafetyError(
                f"Expected exactly one match for authorized chat {chat.name!r}; found {matches}"
            )
        locator.first.click()
        page.wait_for_load_state("domcontentloaded")
        self.require_active_chat(chat)
        return chat

    def require_active_chat(self, chat: AllowedChat) -> None:
        require_teams_origin(self.page.url)
        try:
            header = self.page.locator(CHAT_HEADER_SELECTOR)
            header.wait_for(state="visible", timeout=10_000)
            if header.count() != 1 or header.inner_text(timeout=10_000).strip() != chat.name:
                raise TeamsSafetyError(
                    f"Active chat does not match authorized destination {chat.name!r}"
                )
        except PlaywrightTimeoutError as exc:
            raise TeamsSafetyError("Chat header could not be verified safely") from exc

    def capture_messages(self, chat_id: str, limit: int = 20) -> list[str]:
        chat = self._resolve_chat(chat_id)
        self.require_active_chat(chat)
        messages = self.page.locator(MESSAGE_LIST_SELECTOR)
        messages.wait_for(state="visible", timeout=10_000)
        texts = messages.locator("[data-tid='chat-message']").all_inner_texts()
        return texts[-limit:]

    def insert_draft(self, chat_id: str, text: str) -> None:
        chat = self._resolve_chat(chat_id)
        self.require_active_chat(chat)
        editor = self._single_visible(COMPOSE_SELECTOR, "message editor")
        editor.fill(text)
        if editor.inner_text().strip() != text.strip():
            raise TeamsSafetyError("Approved text was not inserted exactly as reviewed")

    def send(self, chat_id: str, approved_text: str, approver: str) -> None:
        if not approver.strip():
            raise TeamsSafetyError("An approver identity is required")
        chat = self._resolve_chat(chat_id)
        self.require_active_chat(chat)
        editor = self._single_visible(COMPOSE_SELECTOR, "message editor")
        current = editor.inner_text().strip()
        if current != approved_text.strip():
            raise TeamsSafetyError("Editor content changed after approval; send blocked")
        button = self._single_visible(SEND_BUTTON_SELECTOR, "send button")
        button.click()

    def _single_visible(self, selector: str, description: str) -> Locator:
        locator = self.page.locator(selector)
        if locator.count() != 1:
            raise TeamsSafetyError(
                f"Expected exactly one visible {description}; found {locator.count()}"
            )
        if not locator.first.is_visible():
            raise TeamsSafetyError(f"{description.capitalize()} is not visible")
        return locator.first
