"""Hotmail access for the invoice organizer through Microsoft Graph.

The invoice organizer uses the monthly close's Azure app with the delegated
Mail.ReadWrite permission added, because filing a registered invoice moves its
message. That permission would also allow deleting, sending or flagging, so this
client enforces a narrower allow-list in code: GET requests under /v1.0/me/ and a single write, POST
/v1.0/me/messages/{id}/move. Message ids are immutable, so a message keeps its id
after the move and can be verified in its destination.
"""
from __future__ import annotations

import os
import re
import time
import unicodedata
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time as day_time, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx

from mail_invoices.documents import MAX_FILE_BYTES
from mail_invoices.folders import FiledMessage
from shared.storage import data_root

GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
SCOPES = ["Mail.ReadWrite"]
AUTHORITIES = {"consumers", "common"}
CLIENT_ID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
# Immutable ids survive a move; text bodies read the same on every request.
PREFER = 'IdType="ImmutableId", outlook.body-content-type="text"'
RETRYABLE = {429, 500, 502, 503, 504}
MOVE_PATH = re.compile(r"/v1\.0/me/messages/[^/]+/move")
BODY_LIMIT = 6000
PAGE_SIZE = 100
CANDIDATE_SCAN_LIMIT = 500
INVOICE_TERMS = ("factura", "invoice", "recibo", "cfdi", "comprobante")
SUPPORTED_EXTENSIONS = {".pdf", ".xml", ".zip"}
# Mailbox folders that never receive an invoice; well-known ids are checked too.
WELL_KNOWN = ("inbox", "junkemail", "drafts", "sentitems", "deleteditems", "outbox", "conversationhistory")
SYSTEM_FOLDERS = {
    "bandeja de entrada", "correo no deseado", "borradores", "elementos enviados",
    "elementos eliminados", "notas", "conversation history", "bandeja de salida",
}


class MailboxError(RuntimeError):
    """Microsoft Graph did not answer as expected; the batch stops instead of guessing."""


def token_cache_path() -> Path:
    return data_root() / "MailInvoiceAgent" / "graph_token_cache.bin"


def validate_request(method: str, url: str) -> str:
    """Only reads under /v1.0/me/ and the move action may leave this process."""
    parsed = urlparse(url)
    if (
        parsed.scheme != "https" or parsed.hostname != "graph.microsoft.com"
        or parsed.port not in (None, 443) or parsed.username or parsed.password
        or not parsed.path.startswith("/v1.0/me/")
    ):
        raise MailboxError("Solo se permite Microsoft Graph v1.0 por HTTPS; no se envia el token a otros destinos.")
    if method == "GET":
        return url
    if method == "POST" and MOVE_PATH.fullmatch(parsed.path) and not parsed.query:
        return url
    raise MailboxError("Operacion de Graph no permitida: el agente solo lee y mueve correos.")


def _error_fields(response) -> tuple[str, str]:
    try:
        error = response.json().get("error", {})
        return str(error.get("code", "")), str(error.get("message", ""))
    except (ValueError, AttributeError):
        return "", ""


class InvoiceGraphClient:
    """Graph client with bounded retries and the read-and-move allow-list."""

    def __init__(
        self, token: Callable[[], str], http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep, retries: int = 3,
    ) -> None:
        self._token = token
        self._http = http or httpx.Client(timeout=30, follow_redirects=False)
        self._sleep = sleep
        self._retries = retries

    def close(self) -> None:
        self._http.close()

    def _send(self, method: str, url: str, params: dict | None = None, payload: dict | None = None):
        validate_request(method, url)
        for attempt in range(self._retries + 1):
            headers = {"Authorization": "Bearer " + self._token(), "Prefer": PREFER}
            try:
                if method == "GET":
                    response = self._http.get(url, params=params, headers=headers)
                else:
                    response = self._http.post(url, json=payload, headers=headers)
            except httpx.HTTPError as exc:
                # A read is safe to repeat; a move whose answer was lost is verified instead.
                if method == "GET" and attempt < self._retries:
                    self._sleep(5.0 * (attempt + 1))
                    continue
                raise MailboxError("No se pudo contactar con Microsoft Graph.") from exc
            # Throttling (429) rejects the request before acting, so even a move may wait.
            retry = response.status_code in RETRYABLE if method == "GET" else response.status_code == 429
            if retry and attempt < self._retries:
                self._sleep(_retry_after(response))
                continue
            break
        if response.status_code not in (200, 201):
            code, text = _error_fields(response)
            raise MailboxError(
                f"Graph respondio {response.status_code}" + (f" ({code}: {text[:200]})" if code or text else ".")
            )
        return response

    def get_json(self, url: str, params: dict | None = None) -> dict:
        return self._json(self._send("GET", url, params))

    def get_bytes(self, url: str) -> bytes:
        return self._send("GET", url).content

    def post_json(self, url: str, payload: dict) -> dict:
        return self._json(self._send("POST", url, payload=payload))

    @staticmethod
    def _json(response) -> dict:
        try:
            data = response.json()
        except ValueError as exc:
            raise MailboxError("Graph devolvio una respuesta que no es JSON.") from exc
        if not isinstance(data, dict):
            raise MailboxError("Graph devolvio una respuesta JSON inesperada.")
        return data


def _retry_after(response) -> float:
    try:
        return max(0.0, min(float(response.headers.get("Retry-After", "5")), 60.0))
    except (TypeError, ValueError):
        return 5.0


# ------------------------------------------------------------------ session


def _msal_app(client_id: str, authority: str, cache_path: Path):
    try:
        import msal
    except ImportError as exc:
        raise MailboxError("Falta el paquete msal. Ejecuta: pip install -e \".[dev]\"") from exc
    if not CLIENT_ID.fullmatch(client_id) or authority not in AUTHORITIES:
        raise MailboxError("invoices.graph_client_id o graph_authority invalidos.")
    cache = msal.SerializableTokenCache()
    if cache_path.is_file():
        cache.deserialize(cache_path.read_text(encoding="utf-8"))
    app = msal.PublicClientApplication(
        client_id, authority=f"https://login.microsoftonline.com/{authority}", token_cache=cache,
    )
    return app, cache


def _save(cache, cache_path: Path) -> None:
    if cache.has_state_changed:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = cache_path.with_suffix(".tmp")
        temporary.write_text(cache.serialize(), encoding="utf-8")
        os.replace(temporary, cache_path)


def silent_token(client_id: str, authority: str, cache_path: Path) -> Callable[[], str]:
    """Tokens from the saved session only; a batch never opens a sign-in window."""
    app, cache = _msal_app(client_id, authority, cache_path)

    def token() -> str:
        accounts = app.get_accounts()
        if len(accounts) != 1:
            raise MailboxError("Sesion de Microsoft para facturas ausente o ambigua. Ejecuta --login.")
        result = app.acquire_token_silent(SCOPES, account=accounts[0])
        _save(cache, cache_path)
        if not result or "access_token" not in result:
            raise MailboxError("Sesion de Microsoft para facturas vencida. Ejecuta --login.")
        return result["access_token"]

    return token


def interactive_login(client_id: str, authority: str, cache_path: Path) -> str:
    """Sign in through the system browser; the agent never handles the password."""
    app, cache = _msal_app(client_id, authority, cache_path)
    result = app.acquire_token_interactive(SCOPES)
    _save(cache, cache_path)
    if "access_token" not in result:
        raise MailboxError(f"No se completo el inicio de sesion: {result.get('error', 'desconocido')}.")
    accounts = app.get_accounts()
    return accounts[0]["username"] if len(accounts) == 1 else "cuenta sin identificar"


# ------------------------------------------------------------------ mailbox model


@dataclass(frozen=True)
class MailFolder:
    path: str
    name: str
    id: str


@dataclass(frozen=True)
class InboxCandidate:
    id: str
    sender: str
    subject: str
    received: datetime
    preview: str
    unread: bool

    @property
    def text(self) -> str:
        """What a person sees in the list: sender, subject and preview."""
        return f"{self.sender}\n{self.subject}\n{self.preview}"


@dataclass(frozen=True)
class MessageDetail:
    id: str
    sender: str
    subject: str
    body: str
    folder_id: str

    @property
    def body_digest(self) -> str:
        return sha256(self.body.encode("utf-8")).hexdigest()


def _plain(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()


def is_invoice_candidate(candidate: InboxCandidate) -> bool:
    text = _plain(candidate.text)
    return any(term in text for term in INVOICE_TERMS)


def filter_candidates(candidates: list[InboxCandidate], text: str) -> list[InboxCandidate]:
    query = _plain(text.strip())
    if not query:
        raise ValueError("Candidate text filter cannot be empty")
    return [candidate for candidate in candidates if query in _plain(candidate.text)]


def safe_file_name(name: str, taken: set[str]) -> str:
    """Attachment name without folders or reserved characters, unique in its directory."""
    base = Path(name.replace("\\", "/")).name
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", base).strip(" .") or "adjunto"
    stem, suffix = Path(base).stem[:120] or "adjunto", Path(base).suffix.lower()
    candidate, counter = f"{stem}{suffix}", 2
    while candidate.casefold() in taken:
        candidate = f"{stem} ({counter}){suffix}"
        counter += 1
    taken.add(candidate.casefold())
    return candidate


def _sender(message: dict) -> str:
    address = ((message.get("from") or {}).get("emailAddress") or {}).get("address")
    if not isinstance(address, str) or not re.fullmatch(r"[^@\s]+@[^@\s]+", address):
        raise LookupError("Message sender is missing or ambiguous")
    return address


def _subject(message: dict) -> str:
    subject = message.get("subject")
    if not isinstance(subject, str) or not subject.strip():
        raise LookupError("Message subject is empty")
    return subject.strip()


def _received(value: object) -> datetime:
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise MailboxError("Fecha de recepcion no reconocida en Graph.") from exc
    if stamp.tzinfo is None:
        raise MailboxError("La fecha de recepcion de Graph no trae zona horaria.")
    return stamp


class GraphMailbox:
    """Read invoice candidates, download their attachments and move approved messages."""

    def __init__(self, client: InvoiceGraphClient) -> None:
        self.client = client
        self._folders: list[MailFolder] | None = None

    def _pages(self, url: str, params: dict | None) -> Iterator[dict]:
        while url:
            data = self.client.get_json(url, params)
            values, link = data.get("value"), data.get("@odata.nextLink")
            if not isinstance(values, list) or not all(isinstance(item, dict) for item in values):
                raise MailboxError("Respuesta de lista no reconocida.")
            if link is not None and not isinstance(link, str):
                raise MailboxError("Enlace de pagina no reconocido.")
            yield from values
            url, params = link, None

    def _system_ids(self) -> set[str]:
        ids = set()
        for name in WELL_KNOWN:
            try:
                folder = self.client.get_json(f"{GRAPH_ROOT}/me/mailFolders/{name}", {"$select": "id"})
            except MailboxError:
                continue  # a mailbox without that well-known folder (e.g. outbox) is fine
            if isinstance(folder.get("id"), str):
                ids.add(folder["id"])
        return ids

    def folders(self) -> list[MailFolder]:
        """Every user folder by its full display path; never creates or changes folders."""
        if self._folders is not None:
            return self._folders
        system = self._system_ids()
        found: list[MailFolder] = []
        pending = [("", f"{GRAPH_ROOT}/me/mailFolders")]
        while pending:
            parent, url = pending.pop()
            for raw in self._pages(url, {"$select": "id,displayName,childFolderCount", "$top": PAGE_SIZE}):
                identifier, name = raw.get("id"), raw.get("displayName")
                children = raw.get("childFolderCount")
                if not isinstance(identifier, str) or not isinstance(name, str) or not name.strip():
                    raise LookupError("Outlook folder has no recognizable name")
                if type(children) is not int or children < 0:
                    raise MailboxError("Cantidad de subcarpetas no verificable.")
                if identifier in system or name.casefold() in SYSTEM_FOLDERS and not parent:
                    continue
                path = f"{parent}/{name.strip()}" if parent else name.strip()
                found.append(MailFolder(path, name.strip(), identifier))
                if children:
                    pending.append((path, f"{GRAPH_ROOT}/me/mailFolders/{quote(identifier, safe='')}/childFolders"))
        paths = [folder.path for folder in found]
        if len(paths) != len(set(paths)):
            raise LookupError("Primary mailbox folder tree is ambiguous")
        self._folders = sorted(found, key=lambda folder: folder.path.casefold())
        return self._folders

    def folder(self, path: str) -> MailFolder:
        matches = [folder for folder in self.folders() if folder.path == path]
        if len(matches) != 1:
            raise LookupError(f"Destination path is missing or ambiguous: {path}")
        return matches[0]

    def candidates(self, days: int, today: date) -> list[InboxCandidate]:
        """Inbox messages with attachments that look like invoices; opens none of them."""
        since = datetime.combine(today - timedelta(days=days), day_time.min, timezone.utc)
        found = []
        for raw in self._pages(f"{GRAPH_ROOT}/me/mailFolders/inbox/messages", {
            "$filter": f"receivedDateTime ge {since.strftime('%Y-%m-%dT%H:%M:%SZ')} and hasAttachments eq true",
            "$select": "id,subject,from,receivedDateTime,bodyPreview,isRead",
            "$orderby": "receivedDateTime desc", "$top": PAGE_SIZE,
        }):
            if len(found) >= CANDIDATE_SCAN_LIMIT:
                raise MailboxError("Demasiados correos con adjuntos en el rango; reduce --days.")
            identifier = raw.get("id")
            if not isinstance(identifier, str) or not identifier:
                raise MailboxError("Falta identificador de mensaje en la lista.")
            try:
                sender, subject = _sender(raw), _subject(raw)
            except LookupError:
                continue  # without sender or subject it cannot be an invoice to file
            candidate = InboxCandidate(
                identifier, sender, subject, _received(raw.get("receivedDateTime")),
                str(raw.get("bodyPreview") or "")[:300], raw.get("isRead") is False,
            )
            if is_invoice_candidate(candidate):
                found.append(candidate)
        return found

    def message(self, identifier: str) -> MessageDetail:
        raw = self.client.get_json(
            f"{GRAPH_ROOT}/me/messages/{quote(identifier, safe='')}",
            {"$select": "id,subject,from,body,parentFolderId"},
        )
        if raw.get("id") != identifier:
            raise LookupError("Graph returned another message")
        body = (raw.get("body") or {}).get("content")
        folder = raw.get("parentFolderId")
        if not isinstance(body, str) or not isinstance(folder, str):
            raise MailboxError("Mensaje sin cuerpo o carpeta reconocibles.")
        return MessageDetail(identifier, _sender(raw), _subject(raw), body[:BODY_LIMIT], folder)

    def download_attachments(self, identifier: str, target_dir: Path) -> tuple[list[Path], list[str]]:
        """Save the message's PDF, XML and ZIP files; links and attached e-mails are skipped."""
        target_dir.mkdir(parents=True, exist_ok=True)
        base = f"{GRAPH_ROOT}/me/messages/{quote(identifier, safe='')}/attachments"
        saved, errors, taken = [], [], set()
        for raw in self._pages(base, {"$select": "id,name,size,isInline"}):
            name, size, attachment = raw.get("name"), raw.get("size"), raw.get("id")
            if raw.get("@odata.type") != "#microsoft.graph.fileAttachment" or not isinstance(name, str):
                continue
            if Path(name).suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            if not isinstance(attachment, str) or type(size) is not int:
                raise MailboxError("Adjunto sin identificador o tamano reconocibles.")
            if size > 2 * MAX_FILE_BYTES:
                errors.append(f"{Path(name).name}: larger than 10 MB")
                continue
            data = self.client.get_bytes(f"{base}/{quote(attachment, safe='')}/$value")
            if len(data) > MAX_FILE_BYTES:
                errors.append(f"{Path(name).name}: larger than 10 MB")
                continue
            path = target_dir / safe_file_name(name, taken)
            path.write_bytes(data)
            saved.append(path)
        return saved, errors

    def history(self, authorized: list[str], limit: int = 5) -> list[FiledMessage]:
        """Sender and subject of the latest messages in explicitly authorized folders."""
        if not 1 <= limit <= 5:
            raise ValueError("History pilot limit must be between 1 and 5")
        history = []
        for path in authorized:
            folder = self.folder(path)
            data = self.client.get_json(
                f"{GRAPH_ROOT}/me/mailFolders/{quote(folder.id, safe='')}/messages",
                {"$select": "from,subject", "$orderby": "receivedDateTime desc", "$top": limit},
            )
            for raw in (data.get("value") or [])[:limit]:
                history.append(FiledMessage(_sender(raw), _subject(raw), path))
        return history

    def move(self, identifier: str, folder: MailFolder) -> str:
        moved = self.client.post_json(
            f"{GRAPH_ROOT}/me/messages/{quote(identifier, safe='')}/move", {"destinationId": folder.id},
        )
        if not isinstance(moved.get("id"), str):
            raise MailboxError("Graph no confirmo el mensaje movido.")
        return moved["id"]


@contextmanager
def open_mailbox(settings) -> Generator[GraphMailbox]:
    """Mailbox over the saved invoice session; the HTTP client is always closed."""
    client = InvoiceGraphClient(silent_token(settings.graph_client_id, settings.graph_authority, token_cache_path()))
    try:
        yield GraphMailbox(client)
    finally:
        client.close()


# ------------------------------------------------------------------ batch operations


def discover_folders(mailbox: GraphMailbox) -> list[MailFolder]:
    return mailbox.folders()


def scan_candidates(mailbox: GraphMailbox, days: int, today: date) -> list[InboxCandidate]:
    return mailbox.candidates(days, today)


def read_history(mailbox: GraphMailbox | None, authorized: list[str], limit: int = 5) -> list[FiledMessage]:
    if not 1 <= limit <= 5:
        raise ValueError("History pilot limit must be between 1 and 5")
    if not authorized:
        return []
    assert mailbox is not None
    return mailbox.history(authorized, limit)


def _same_identity(detail: MessageDetail, message: dict) -> bool:
    return (detail.sender, detail.subject) == (message["sender"], message["subject"])


def verify_destination(mailbox: GraphMailbox, message: dict, folder: str) -> None:
    """The approved message is in the approved folder, with the reviewed sender and subject."""
    destination = mailbox.folder(folder)
    detail = mailbox.message(message["conversation"])
    if detail.folder_id != destination.id:
        raise LookupError("Destination verification failed; review Outlook manually")
    if not _same_identity(detail, message):
        raise LookupError("Destination message metadata differs; review Outlook manually")


def move_registered_message(
    mailbox: GraphMailbox, message: dict, folder: str, *, approved: bool,
    before_move: Callable[[], None] | None = None,
) -> None:
    """Move one approved, registered message after checking it is still the reviewed one."""
    if not approved or not message["eligible"]:
        raise ValueError("Movement requires verified registration and explicit approval")
    destination = mailbox.folder(folder)
    detail = mailbox.message(message["conversation"])
    if not _same_identity(detail, message):
        raise LookupError("Conversation metadata changed; movement blocked")
    if message.get("body_digest") and detail.body_digest != message["body_digest"]:
        raise LookupError("Message body changed after review; movement blocked")
    # No retries after this side effect: a verification failure requires manual review.
    if before_move is not None:
        before_move()
    message["movement_attempted"] = True
    message["movement_status"] = "unverified"
    moved_id = mailbox.move(detail.id, destination)
    if moved_id != detail.id:
        raise LookupError("Graph changed the message identity while moving; review Outlook manually")
    verify_destination(mailbox, message, folder)
    message["movement_status"] = "verified"
