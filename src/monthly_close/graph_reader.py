from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterator
from urllib.parse import quote

from monthly_close.domain import EMAIL, clean_text
from monthly_close.extraction import Mail
from monthly_close.graph_client import GRAPH_ROOT, GraphClient, GraphError
from monthly_close.outlook import Candidate, OutlookError
from monthly_close.source_reader import checkpoint_key, parse_source

# Message identifiers hash this origin with the Message-ID. Keep the Outlook Web value so
# notices imported before Graph was used are recognised, not imported twice.
DEFAULT_ORIGIN = "outlook.live.com"
WELL_KNOWN = (
    (("bandeja de entrada",), "Bandeja de entrada", "inbox"),
    (("correo no deseado",), "Correo no deseado", "junkemail"),
    (("elementos eliminados",), "Elementos eliminados", "deleteditems"),
)
FOLDER_FIELDS = "id,displayName,childFolderCount"
MESSAGE_FIELDS = "id,subject,from,receivedDateTime"
PAGE_SIZE = 100
PILOT_WINDOW = 50
SNAPSHOT_ATTEMPTS = 3
# A saved delta link Graph no longer accepts; the folder is synchronised again from scratch.
SYNC_EXPIRED_CODES = {"syncStateNotFound", "syncStateInvalid", "resyncRequired"}


@dataclass(frozen=True)
class Folder:
    path: tuple[str, ...]
    label: str
    id: str


def _timestamp(value: object) -> datetime:
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise OutlookError("Fecha de recepcion no reconocida en la lista de Graph.") from exc
    if stamp.tzinfo is None:
        raise OutlookError("La fecha de recepcion de Graph no trae zona horaria.")
    return stamp


def _merge_delta(rows: dict[str, dict], values: object) -> None:
    """Apply one delta page: removed messages leave, replayed ones keep their last version."""
    if not isinstance(values, list) or not all(isinstance(item, dict) for item in values):
        raise GraphError("Respuesta delta no reconocida.")
    for item in values:
        identifier = item.get("id")
        if not isinstance(identifier, str) or not identifier:
            raise OutlookError("Falta identificador de mensaje en la respuesta delta.")
        if "@removed" in item:
            rows.pop(identifier, None)
            continue
        _timestamp(item.get("receivedDateTime"))
        rows[identifier] = item


class GraphSourceReader:
    """Read authorized messages through Microsoft Graph (Mail.Read, read-only).

    Reads the mailbox through Microsoft Graph: folders are identified by id,
    messages arrive newest first, and the raw MIME goes through the same parse_source checks.
    """

    def __init__(
        self, client: GraphClient, approved_senders: list[str], origin: str = DEFAULT_ORIGIN,
    ) -> None:
        if not approved_senders or any(not EMAIL.fullmatch(sender) for sender in approved_senders):
            raise ValueError("El piloto requiere remitentes exactos aprobados.")
        self.client = client
        self.origin = origin
        self.approved_senders = [sender.lower() for sender in approved_senders]
        self.active_folder: tuple[str, ...] | None = None
        self._received: dict[str, str] = {}
        # Told (position, total) as each folder starts, before its messages are known.
        self.on_folder: Callable[[int, int], None] | None = None

    def _enter(self, folder: Folder, position: int, total: int) -> None:
        self.active_folder = folder.path
        if self.on_folder is not None:
            self.on_folder(position, total)

    @staticmethod
    def _folder_url(folder_id: str, tail: str) -> str:
        return f"{GRAPH_ROOT}/me/mailFolders/{quote(folder_id, safe='')}/{tail}"

    def _pages(self, url: str, params: dict | None) -> Iterator[dict]:
        while url:
            data = self.client.get_json(url, params)
            values = data.get("value")
            link = data.get("@odata.nextLink")
            if not isinstance(values, list) or not all(isinstance(item, dict) for item in values):
                raise GraphError("Respuesta de lista no reconocida.")
            if link is not None and not isinstance(link, str):
                raise GraphError("Enlace de pagina no reconocido.")
            yield from values
            url, params = link, None

    @staticmethod
    def _folder_fields(raw: dict) -> tuple[str, str, int]:
        identifier, name, children = raw.get("id"), raw.get("displayName"), raw.get("childFolderCount")
        if (
            not isinstance(identifier, str) or not identifier or not isinstance(name, str)
            or type(children) is not int or children < 0
        ):
            raise GraphError("Respuesta de carpeta no reconocida.")
        return identifier, name, children

    def folders(self) -> list[Folder]:
        """Inbox, junk, deleted and Dinero with every descendant, by unambiguous full path."""
        result: list[Folder] = []
        for path, label, well_known in WELL_KNOWN:
            raw = self.client.get_json(
                f"{GRAPH_ROOT}/me/mailFolders/{well_known}", {"$select": FOLDER_FIELDS},
            )
            identifier, _, _ = self._folder_fields(raw)
            result.append(Folder(path, label, identifier))
        roots = [
            raw for raw in self._pages(
                f"{GRAPH_ROOT}/me/mailFolders", {"$select": FOLDER_FIELDS, "$top": PAGE_SIZE},
            ) if str(raw.get("displayName", "")).casefold() == "dinero"
        ]
        if len(roots) != 1:
            raise GraphError("Carpeta Dinero ausente o ambigua.")
        pending = [(("dinero",), "Dinero", roots[0])]
        while pending:
            path, label, raw = pending.pop()
            identifier, _, children = self._folder_fields(raw)
            result.append(Folder(path, label, identifier))
            if not children:
                continue
            entries, keys = [], set()
            for child in self._pages(
                self._folder_url(identifier, "childFolders"),
                {"$select": FOLDER_FIELDS, "$top": PAGE_SIZE},
            ):
                _, name, _ = self._folder_fields(child)
                key = name.casefold()
                if key in keys:
                    raise GraphError("Identidad o jerarquia de subcarpetas ambigua.")
                keys.add(key)
                entries.append((path + (key,), label + " / " + name, child))
            if len(entries) != children:
                raise GraphError("La cantidad de subcarpetas no coincide con la anunciada.")
            pending.extend(reversed(entries))
        return result

    def _authorized(self, message: dict) -> Candidate | None:
        """Candidate for an exact approved sender; unrelated messages are never opened."""
        sender = ((message.get("from") or {}).get("emailAddress") or {}).get("address")
        address = sender.strip().lower() if isinstance(sender, str) else ""
        if not EMAIL.fullmatch(address) or address not in self.approved_senders:
            return None
        try:
            subject = clean_text(message.get("subject"), 300)
        except ValueError as exc:
            raise OutlookError("Asunto ausente o no valido en el resultado.") from exc
        self._received[message["id"]] = message["receivedDateTime"]
        return Candidate(message["id"], address, subject)

    def _messages(self, folder: Folder) -> Iterator[dict]:
        """Newest first, verifying identity and order of every row Graph returns."""
        ids: set[str] = set()
        previous: datetime | None = None
        for message in self._pages(
            self._folder_url(folder.id, "messages"),
            {"$select": MESSAGE_FIELDS, "$orderby": "receivedDateTime desc", "$top": PAGE_SIZE},
        ):
            identifier = message.get("id")
            if not isinstance(identifier, str) or not identifier:
                raise OutlookError("Falta identificador de mensaje en la lista.")
            stamp = _timestamp(message.get("receivedDateTime"))
            if identifier in ids:
                raise OutlookError("Identificador de mensaje repetido; la carpeta cambio durante el recorrido.")
            if previous is not None and stamp > previous:
                raise OutlookError("No se confirma el orden de fecha descendente.")
            ids.add(identifier)
            previous = stamp
            yield message

    def _total(self, folder: Folder) -> int:
        raw = self.client.get_json(
            f"{GRAPH_ROOT}/me/mailFolders/{quote(folder.id, safe='')}",
            {"$select": "totalItemCount"},
        )
        total = raw.get("totalItemCount")
        if type(total) is not int or total < 0:
            raise GraphError("Cantidad de carpeta no verificable.")
        return total

    def _snapshot(self, folder: Folder) -> list[dict]:
        """Whole folder, newest first, taken in seconds and checked against its total.

        Reading everything before the slow extraction starts means mail arriving later
        cannot shift pages mid-scan; the next run picks it up.
        """
        for _ in range(SNAPSHOT_ATTEMPTS):
            before = self._total(folder)
            rows = list(self._messages(folder))
            after = self._total(folder)
            if len(rows) == before == after:
                return rows
        raise OutlookError(
            f"Cobertura no demostrada en {folder.label}: Graph listo {len(rows)} mensajes "
            f"y la carpeta anuncia {before} antes y {after} despues. Repite el recorrido."
        )

    def walk_folders(
        self, limit: int, visit: Callable[[Candidate], bool | None],
    ) -> list[dict]:
        """Visit folders; False ends only the current verified newest-first folder."""
        if type(limit) is not int or limit < 1:
            raise ValueError("El limite de filas debe ser positivo.")
        folders = self.folders()
        reports: list[dict] = []
        examined = 0
        for position, folder in enumerate(folders, 1):
            self._enter(folder, position, len(folders))
            rows = authorized = 0
            stopped_before_start = False
            for message in self._snapshot(folder):
                if examined >= limit:
                    raise OutlookError(
                        "Limite total de filas alcanzado; recorrido parcial, no se omiten carpetas."
                    )
                examined += 1
                rows += 1
                candidate = self._authorized(message)
                if candidate is None:
                    continue
                keep_reading = visit(candidate)
                authorized += 1
                if keep_reading is False:
                    stopped_before_start = True
                    break
            report = {"folder": folder.label, "rows": rows, "authorized": authorized}
            if stopped_before_start:
                report["stopped_before_start"] = True
            reports.append(report)
        if self.folders() != folders:
            raise OutlookError("El arbol de subcarpetas cambio durante el recorrido.")
        return reports

    def _delta_rows(self, folder: Folder, link: str | None) -> tuple[list[dict], str]:
        """One complete delta round for a folder: changed messages and the next delta link.

        Without a saved link this is the initial round, which lists the whole folder.
        No date filter is sent: Graph caps a filtered delta at 5,000 messages, so the
        date cut happens locally on list metadata. Removed items are ignored and a
        replayed message keeps its last version.
        """
        url = link or self._folder_url(folder.id, "messages/delta")
        params: dict | None = None if link else {"$select": MESSAGE_FIELDS}
        rows: dict[str, dict] = {}
        while True:
            data = self.client.get_json(url, params)
            params = None
            values, next_link, delta = data.get("value"), data.get("@odata.nextLink"), data.get("@odata.deltaLink")
            _merge_delta(rows, values)
            if isinstance(next_link, str) and delta is None:
                url = next_link
            elif isinstance(delta, str) and next_link is None:
                return list(rows.values()), delta
            else:
                raise GraphError("Respuesta delta sin un enlace de continuacion reconocible.")

    def _folder_changes(self, folder: Folder, link: str | None) -> tuple[list[dict], str, bool]:
        """Changes since the saved link; an expired link restarts that folder from scratch."""
        if link is None:
            return (*self._delta_rows(folder, None), True)
        try:
            return (*self._delta_rows(folder, link), False)
        except GraphError as exc:
            if not (exc.status == 410 or exc.code in SYNC_EXPIRED_CODES):
                raise
        return (*self._delta_rows(folder, None), True)

    def walk_changes(
        self, state: dict[str, str], since: datetime, limit: int,
        visit: Callable[[Candidate], bool | None], save: Callable[[str, str], None],
    ) -> list[dict]:
        """Visit authorized messages that arrived or changed since the saved delta links.

        Each folder's new delta link is saved only after all its notices were visited,
        so a failure repeats that folder's changes next time instead of losing them.
        Messages received before since are not visited.
        """
        if since.tzinfo is None:
            raise ValueError("La fecha inicial necesita zona horaria.")
        if type(limit) is not int or limit < 1:
            raise ValueError("El limite de filas debe ser positivo.")
        folders = self.folders()
        reports: list[dict] = []
        examined = 0
        for position, folder in enumerate(folders, 1):
            self._enter(folder, position, len(folders))
            link = state.get(folder.id)
            rows, delta, initial = self._folder_changes(folder, link)
            recent = sorted(
                (row for row in rows if _timestamp(row["receivedDateTime"]) >= since),
                key=lambda row: _timestamp(row["receivedDateTime"]),
            )
            examined += len(recent)
            if examined > limit:
                raise OutlookError(
                    "Limite total de filas alcanzado; recorrido parcial, no se omiten carpetas."
                )
            authorized = 0
            for message in recent:
                candidate = self._authorized(message)
                if candidate is None:
                    continue
                authorized += 1
                visit(candidate)
            save(folder.id, delta)
            report = {"folder": folder.label, "rows": len(recent), "authorized": authorized, "incremental": True}
            if initial:
                report["initial"] = True
            reports.append(report)
        if self.folders() != folders:
            raise OutlookError("El arbol de subcarpetas cambio durante el recorrido.")
        return reports

    def locate(self, sender: str, received: datetime, window: timedelta) -> list[Candidate]:
        """Authorized messages of one exact sender received around a moment, metadata only.

        Used to retry a queued notice in incremental runs, where Graph no longer reports
        it as a change. The caller compares the read identity with the queued one.
        """
        if not EMAIL.fullmatch(sender) or "'" in sender or sender not in self.approved_senders:
            raise ValueError("Remitente invalido para buscar un aviso en cola.")
        if received.tzinfo is None:
            raise ValueError("La fecha del aviso en cola necesita zona horaria.")

        def stamp(value: datetime) -> str:
            return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        found = []
        for message in self._pages(f"{GRAPH_ROOT}/me/messages", {
            "$filter": (
                f"receivedDateTime ge {stamp(received - window)} and receivedDateTime le {stamp(received + window)}"
                f" and from/emailAddress/address eq '{sender}'"
            ),
            "$select": MESSAGE_FIELDS, "$top": PAGE_SIZE,
        }):
            if not isinstance(message.get("id"), str) or not message["id"]:
                raise OutlookError("Falta identificador de mensaje en la busqueda.")
            _timestamp(message.get("receivedDateTime"))
            candidate = self._authorized(message)
            if candidate is None or candidate.sender != sender:
                raise OutlookError("Graph devolvio un correo de otro remitente al buscar un aviso en cola.")
            found.append(candidate)
        return found

    def candidate(self) -> Candidate:
        """Newest authorized notice among the latest messages of each folder."""
        for folder in self.folders():
            data = self.client.get_json(
                self._folder_url(folder.id, "messages"),
                {"$select": MESSAGE_FIELDS, "$orderby": "receivedDateTime desc", "$top": PILOT_WINDOW},
            )
            values = data.get("value")
            if not isinstance(values, list):
                raise GraphError("Respuesta de lista no reconocida.")
            for message in values:
                if not isinstance(message, dict) or not isinstance(message.get("id"), str):
                    raise OutlookError("Falta identificador de mensaje en la lista.")
                _timestamp(message.get("receivedDateTime"))
                candidate = self._authorized(message)
                if candidate is not None:
                    self.active_folder = folder.path
                    return candidate
        raise OutlookError(
            f"No hay avisos de remitentes autorizados entre los {PILOT_WINDOW} mas recientes "
            "de cada carpeta. No se buscan ni abren mensajes fuera de esa lista."
        )

    def checkpoint_key(self, candidate: Candidate) -> str:
        return checkpoint_key(self.active_folder, candidate, self._received.get(candidate.item_id))

    def statement_files(
        self, sender: str, since: datetime, name_pattern, max_bytes: int,
    ) -> Iterator[tuple[str, bytes]]:
        """PDF statements attached by one exact approved sender, read into memory only.

        Messages are listed by that sender only; any other sender in the answer stops
        the run. Only attachments whose name matches the bank's statement pattern and
        size limit are downloaded.
        """
        if not EMAIL.fullmatch(sender) or "'" in sender:
            raise ValueError("Remitente de extractos invalido.")
        if since.tzinfo is None:
            raise ValueError("La fecha inicial de extractos necesita zona horaria.")
        stamp = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for message in self._pages(f"{GRAPH_ROOT}/me/messages", {
            "$filter": f"receivedDateTime ge {stamp} and from/emailAddress/address eq '{sender}'",
            "$select": "id,from,receivedDateTime", "$orderby": "receivedDateTime desc", "$top": PAGE_SIZE,
        }):
            address = ((message.get("from") or {}).get("emailAddress") or {}).get("address")
            identifier = message.get("id")
            if not isinstance(address, str) or address.lower() != sender or not isinstance(identifier, str):
                raise OutlookError("Graph devolvio un correo de otro remitente al listar extractos.")
            base = f"{GRAPH_ROOT}/me/messages/{quote(identifier, safe='')}/attachments"
            for attachment in self._pages(base, {"$select": "id,name,size"}):
                name, size, attachment_id = attachment.get("name"), attachment.get("size"), attachment.get("id")
                if not isinstance(name, str) or not name_pattern.fullmatch(name):
                    continue
                if type(size) is not int or not 0 < size <= max_bytes or not isinstance(attachment_id, str):
                    raise OutlookError("Adjunto de extracto con tamano no verificable.")
                raw = self.client.get_json(f"{base}/{quote(attachment_id, safe='')}")
                content = raw.get("contentBytes")
                if raw.get("@odata.type") != "#microsoft.graph.fileAttachment" or not isinstance(content, str):
                    raise OutlookError("Adjunto de extracto no reconocido.")
                try:
                    yield name, base64.b64decode(content, validate=True)
                except binascii.Error as exc:
                    raise OutlookError("Contenido de extracto no valido.") from exc

    def read(self, candidate: Candidate) -> Mail:
        raw = self.client.get_bytes(
            f"{GRAPH_ROOT}/me/messages/{quote(candidate.item_id, safe='')}/$value"
        )
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise OutlookError("Origen no UTF-8 estricto; requiere revision manual.") from exc
        return parse_source(text, candidate, self.origin)
