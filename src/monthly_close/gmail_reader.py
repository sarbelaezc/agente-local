from __future__ import annotations

import base64
import binascii
import hashlib
import json
from datetime import datetime, timedelta, timezone
from email.utils import getaddresses
from typing import Callable, Iterator
from urllib.parse import quote

from monthly_close.domain import EMAIL, clean_text
from monthly_close.extraction import Mail
from monthly_close.gmail_client import GMAIL_ROOT, GmailClient, GmailError
from monthly_close.outlook import Candidate, OutlookError
from monthly_close.source_reader import parse_source

DEFAULT_ORIGIN = "mail.google.com"
FOLDER = ("Gmail",)
HISTORY_KEY = "history"
PAGE_SIZE = 500
# Messages the person wrote are never notices from a bank.
OWN_LABELS = {"SENT", "DRAFT"}
# Gmail search dates are coarse; list one extra day and cut exactly on the received time.
QUERY_MARGIN = timedelta(days=1)


def _received(message: dict) -> datetime:
    try:
        millis = int(message.get("internalDate"))
    except (TypeError, ValueError) as exc:
        raise OutlookError("Fecha de recepcion no reconocida en Gmail.") from exc
    return datetime.fromtimestamp(millis / 1000, timezone.utc)


def _header(message: dict, name: str) -> str | None:
    headers = (message.get("payload") or {}).get("headers")
    if not isinstance(headers, list):
        raise GmailError("Encabezados de Gmail no reconocidos.")
    values = [
        item.get("value") for item in headers
        if isinstance(item, dict) and str(item.get("name", "")).lower() == name.lower()
    ]
    if len(values) != 1 or not isinstance(values[0], str):
        return None
    return values[0]


class GmailSourceReader:
    """Read authorized notices from Gmail with the read-only gmail.readonly scope.

    Messages are listed by exact approved senders (spam and trash included), checked
    against their headers, and their raw MIME goes through the same parse_source checks
    as Outlook. Only the message text is decoded; attachments are never read.
    """

    def __init__(self, client: GmailClient, approved_senders: list[str], origin: str = DEFAULT_ORIGIN) -> None:
        if not approved_senders or any(
            not EMAIL.fullmatch(sender) or any(char in sender for char in "\"(){}") for sender in approved_senders
        ):
            raise ValueError("El lector de Gmail requiere remitentes exactos aprobados.")
        self.client = client
        self.origin = origin
        self.approved_senders = [sender.lower() for sender in approved_senders]
        self.active_folder: tuple[str, ...] | None = FOLDER
        self._received: dict[str, str] = {}

    def _query(self, since: datetime | None) -> str:
        senders = " OR ".join(f'"{sender}"' for sender in self.approved_senders)
        query = f"from:({senders})"
        if since is not None:
            query += f" after:{int((since - QUERY_MARGIN).timestamp())}"
        return query

    def _ids(self, query: str) -> Iterator[str]:
        token: str | None = None
        while True:
            params: dict = {"q": query, "includeSpamTrash": "true", "maxResults": PAGE_SIZE}
            if token:
                params["pageToken"] = token
            data = self.client.get_json(f"{GMAIL_ROOT}/messages", params)
            messages = data.get("messages", [])
            if not isinstance(messages, list):
                raise GmailError("Respuesta de lista de Gmail no reconocida.")
            for item in messages:
                identifier = item.get("id") if isinstance(item, dict) else None
                if not isinstance(identifier, str) or not identifier:
                    raise OutlookError("Falta identificador de mensaje en la lista de Gmail.")
                yield identifier
            token = data.get("nextPageToken")
            if token is None:
                return
            if not isinstance(token, str):
                raise GmailError("Enlace de pagina de Gmail no reconocido.")

    def _metadata(self, identifier: str) -> dict | None:
        """List metadata of one message; None when it was deleted after being listed."""
        try:
            message = self.client.get_json(
                f"{GMAIL_ROOT}/messages/{quote(identifier, safe='')}",
                {"format": "metadata", "metadataHeaders": ["From", "Subject"]},
            )
        except GmailError as exc:
            if exc.status == 404:
                return None
            raise
        if message.get("id") != identifier:
            raise GmailError("Gmail devolvio otro mensaje al pedir sus metadatos.")
        return message

    def _authorized(self, message: dict) -> Candidate | None:
        """Candidate for an exact approved sender; unrelated messages are never opened."""
        labels = message.get("labelIds", [])
        if not isinstance(labels, list) or OWN_LABELS & set(labels):
            return None
        sender = _header(message, "From")
        addresses = getaddresses([sender]) if sender else []
        address = addresses[0][1].strip().lower() if len(addresses) == 1 else ""
        if not EMAIL.fullmatch(address) or address not in self.approved_senders:
            return None
        try:
            subject = clean_text(_header(message, "Subject"), 300)
        except ValueError as exc:
            raise OutlookError("Asunto ausente o no valido en el resultado.") from exc
        self._received[message["id"]] = _received(message).isoformat()
        return Candidate(message["id"], address, subject)

    def _candidates(self, identifiers: list[str], since: datetime | None) -> list[tuple[datetime, dict]]:
        found: list[tuple[datetime, dict]] = []
        for identifier in dict.fromkeys(identifiers):
            message = self._metadata(identifier)
            if message is None:
                continue
            stamp = _received(message)
            if since is None or stamp >= since:
                found.append((stamp, message))
        return found

    def walk_folders(
        self, limit: int, visit: Callable[[Candidate], bool | None], since: datetime | None = None,
    ) -> list[dict]:
        """Visit notices newest first; False stops the walk (older notices are not needed)."""
        if type(limit) is not int or limit < 1:
            raise ValueError("El limite de filas debe ser positivo.")
        identifiers = list(self._ids(self._query(since)))
        if len(identifiers) > limit:
            raise OutlookError("Limite total de filas alcanzado; recorrido parcial, no se omiten mensajes.")
        rows = sorted(self._candidates(identifiers, None), key=lambda item: item[0], reverse=True)
        authorized = 0
        stopped = False
        for _stamp, message in rows:
            candidate = self._authorized(message)
            if candidate is None:
                continue
            authorized += 1
            if visit(candidate) is False:
                stopped = True
                break
        report = {"folder": "Gmail", "rows": len(rows), "authorized": authorized}
        if stopped:
            report["stopped_before_start"] = True
        return [report]

    def _history(self, start: str) -> tuple[list[str], str] | None:
        """Messages added since a history id, and the newest id; None when it expired."""
        identifiers: list[str] = []
        token: str | None = None
        while True:
            params: dict = {"startHistoryId": start, "historyTypes": "messageAdded", "maxResults": PAGE_SIZE}
            if token:
                params["pageToken"] = token
            try:
                data = self.client.get_json(f"{GMAIL_ROOT}/history", params)
            except GmailError as exc:
                if exc.status == 404:
                    return None
                raise
            records = data.get("history", [])
            if not isinstance(records, list):
                raise GmailError("Historial de Gmail no reconocido.")
            for record in records:
                for added in (record.get("messagesAdded", []) if isinstance(record, dict) else None) or []:
                    identifier = ((added or {}).get("message") or {}).get("id")
                    if not isinstance(identifier, str) or not identifier:
                        raise OutlookError("Falta identificador de mensaje en el historial de Gmail.")
                    identifiers.append(identifier)
            token = data.get("nextPageToken")
            if token is None:
                latest = data.get("historyId")
                if not isinstance(latest, str) or not latest.isdigit():
                    raise GmailError("Gmail no devolvio el identificador de historial.")
                return identifiers, latest
            if not isinstance(token, str):
                raise GmailError("Enlace de pagina de Gmail no reconocido.")

    def _current_history(self) -> str:
        latest = self.client.get_json(f"{GMAIL_ROOT}/profile").get("historyId")
        if not isinstance(latest, str) or not latest.isdigit():
            raise GmailError("Gmail no devolvio el identificador de historial.")
        return latest

    def walk_changes(
        self, state: dict[str, str], since: datetime, limit: int,
        visit: Callable[[Candidate], bool | None], save: Callable[[str, str], None],
    ) -> list[dict]:
        """Visit notices added since the saved history id; a full listing the first time.

        The history id is read before a full listing, so a message arriving meanwhile
        is seen again next time instead of being lost; repeats are skipped by the store.
        The new id is saved only after every notice was visited.
        """
        if since.tzinfo is None:
            raise ValueError("La fecha inicial necesita zona horaria.")
        if type(limit) is not int or limit < 1:
            raise ValueError("El limite de filas debe ser positivo.")
        changes = self._history(state[HISTORY_KEY]) if HISTORY_KEY in state else None
        initial = changes is None
        if changes is None:
            latest = self._current_history()
            identifiers = list(self._ids(self._query(since)))
        else:
            identifiers, latest = changes
        if len(set(identifiers)) > limit:
            raise OutlookError("Limite total de filas alcanzado; recorrido parcial, no se omiten mensajes.")
        rows = sorted(self._candidates(identifiers, since), key=lambda item: item[0])
        authorized = 0
        for _stamp, message in rows:
            candidate = self._authorized(message)
            if candidate is None:
                continue
            authorized += 1
            visit(candidate)
        save(HISTORY_KEY, latest)
        report = {"folder": "Gmail", "rows": len(rows), "authorized": authorized, "incremental": True}
        if initial:
            report["initial"] = True
        return [report]

    def locate(self, sender: str, received: datetime, window: timedelta) -> list[Candidate]:
        """Authorized messages of one exact sender received around a moment, metadata only."""
        if sender not in self.approved_senders:
            raise ValueError("Remitente invalido para buscar un aviso en cola.")
        if received.tzinfo is None:
            raise ValueError("La fecha del aviso en cola necesita zona horaria.")
        start, end = received - window, received + window
        query = (
            f'from:("{sender}") after:{int((start - QUERY_MARGIN).timestamp())}'
            f" before:{int((end + QUERY_MARGIN).timestamp())}"
        )
        found = []
        for stamp, message in self._candidates(list(self._ids(query)), start):
            candidate = self._authorized(message) if stamp <= end else None
            if candidate is not None and candidate.sender == sender:
                found.append(candidate)
        return found

    def checkpoint_key(self, candidate: Candidate) -> str:
        """Hash the list metadata of a notice; never persist message text."""
        received = self._received.get(candidate.item_id)
        if received is None:
            raise OutlookError("Aviso desconocido para este recorrido; no hay punto de reanudacion.")
        return hashlib.sha256(json.dumps(
            [self.active_folder, candidate.item_id, candidate.sender, candidate.subject, received],
            sort_keys=True, ensure_ascii=False,
        ).encode()).hexdigest()

    def read(self, candidate: Candidate) -> Mail:
        data = self.client.get_json(
            f"{GMAIL_ROOT}/messages/{quote(candidate.item_id, safe='')}", {"format": "raw"},
        )
        encoded = data.get("raw")
        if data.get("id") != candidate.item_id or not isinstance(encoded, str):
            raise OutlookError("Gmail no devolvio el origen del aviso solicitado.")
        try:
            raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            text = raw.decode("utf-8")
        except (binascii.Error, ValueError) as exc:
            raise OutlookError("Origen no UTF-8 estricto; requiere revision manual.") from exc
        return parse_source(text, candidate, self.origin)
