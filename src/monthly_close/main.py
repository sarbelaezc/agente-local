from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import logging
import sys
import traceback
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterator
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from shared.lan_access import LanAccess, start_in_background
from shared.local_model import LocalModelError, validate_local_url
from shared.storage import data_root
from monthly_close.model import OllamaJsonModel
from monthly_close.rule_suggestions import propose_statement_categories
from monthly_close import credit_ledger
from monthly_close import statements as statements_module
from monthly_close.domain import EMAIL, OWNERS, Preferences, received_local
from monthly_close.extraction import EvidenceError, card_named_in, extract, income_category
from monthly_close.graph_client import (
    AUTHORITIES, CLIENT_ID, GraphClient, interactive_login, silent_token,
)
from monthly_close.destinations import assign_destinations, is_short_key
from monthly_close.graph_reader import GraphSourceReader
from monthly_close import gmail_client
from monthly_close.gmail_client import GmailClient
from monthly_close.gmail_reader import GmailSourceReader
from monthly_close.mailboxes import Mailbox, find, load_mailboxes, primary_graph
from shared.run_log import run_log
from monthly_close.household_import import PARITY_FILE, import_member, parity_report
from monthly_close.outlook import Candidate, OutlookError
from monthly_close.server import create_server
from monthly_close.store import Store


def default_data_dir() -> Path:
    return data_root() / "MonthlyCloseAgent"


def runtime(data_dir: Path) -> dict:
    path = data_dir / "runtime.json"
    if not path.is_file():
        raise ValueError(
            "Copia config\\monthly-close.example.json a " + str(path)
            + " y configura graph_client_id."
        )
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Configuracion de ejecucion invalida.")
    url = validate_local_url(raw.get("ollama_url", ""))
    endpoint = urlparse(url)
    if (
        endpoint.port != 11434 or endpoint.username or endpoint.password
        or endpoint.path or endpoint.query or endpoint.fragment
    ):
        raise ValueError("Cierre mensual debe compartir Ollama de facturas en el puerto 11434.")
    if raw.get("source", "graph") != "graph":
        raise ValueError("source debe ser graph: el lector por navegador se retiro.")
    if not isinstance(raw.get("graph_client_id"), str) or not CLIENT_ID.fullmatch(raw["graph_client_id"]):
        raise ValueError("Configura graph_client_id con el Application (client) ID de tu app.")
    if raw.setdefault("graph_authority", "consumers") not in AUTHORITIES:
        raise ValueError("graph_authority debe ser consumers o common.")
    if type(raw.get("max_messages")) is not int or not 1 <= raw["max_messages"] <= 10000:
        raise ValueError("max_messages debe estar entre 1 y 10000.")
    if not isinstance(raw.get("ollama_model"), str) or not raw["ollama_model"].strip():
        raise ValueError("Configura ollama_model.")
    primary_graph(load_mailboxes(raw))  # validates the mailbox list; Santiago's Outlook is required
    return raw


@contextmanager
def open_source(
    data_dir: Path, settings: dict, approved_senders: list[str], mailbox: Mailbox | None = None,
) -> Iterator:
    """Read-only reader of one mailbox (Santiago's Outlook by default), using its saved session."""
    box = mailbox or primary_graph(load_mailboxes(settings))
    if box.provider == "gmail":
        client = GmailClient(gmail_client.silent_token(data_dir / box.token_cache))
        reader = GmailSourceReader(client, approved_senders, box.origin)
    else:
        client = GraphClient(silent_token(
            settings["graph_client_id"], settings.get("graph_authority", "consumers"), data_dir / box.token_cache,
        ))
        # The origin keeps message identifiers stable, so imported notices are not repeated.
        reader = GraphSourceReader(client, approved_senders, box.origin)
    try:
        yield reader
    finally:
        client.close()


def login(data_dir: Path, settings: dict, name: str | None) -> str:
    """Interactive sign-in for one mailbox; the person types the password in the browser."""
    box = find(load_mailboxes(settings), name)
    if box.provider == "gmail":
        assert box.client_secrets is not None
        account = gmail_client.interactive_login(
            data_dir / box.client_secrets, data_dir / box.token_cache, expected=box.address,
        )
        return f"Sesion de Google guardada para {account} (buzon {box.name}). Permiso: solo lectura de Gmail."
    account = interactive_login(
        settings["graph_client_id"], settings.get("graph_authority", "consumers"), data_dir / box.token_cache,
    )
    return f"Sesion de Microsoft guardada para {account}. Permiso: solo lectura de correo."


def months(start: date, end: date) -> list[str]:
    result = []
    current = start.replace(day=1)
    while current < end:
        result.append(current.strftime("%Y-%m"))
        current = (current.replace(day=28) + timedelta(days=4)).replace(day=1)
    return result


CLOSING_HOUR = 22
# The scheduled close opens Google's sign-in when Manuela's Gmail session expired and
# waits this long for her before stopping.
RELOGIN_MINUTES = 15


def due_close_month(now: datetime, timezone: str, last_closed: str | None) -> str | None:
    """Most recent month whose close is due (last local day from 22:00) and not yet done.

    A run delayed because the computer was off, for example the next morning, closes
    the month that just ended; a month already closed is never closed again.
    """
    if now.tzinfo is None:
        raise ValueError("El reloj del cierre necesita zona horaria explicita.")
    local = now.astimezone(ZoneInfo(timezone))
    if local.day == calendar.monthrange(local.year, local.month)[1] and local.hour >= CLOSING_HOUR:
        month = local.strftime("%Y-%m")
    else:
        month = (local.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    if last_closed is not None and month <= last_closed:
        return None
    return month


class Progress:
    """One-line terminal bar: how far back each folder has been read, by notice date.

    Folders are read newest first, so the bar fills from the end of the range towards
    its start. It only draws on an interactive terminal and never prints mail content.
    """

    WIDTH = 24

    def __init__(self, start: date, end: date, stream=None) -> None:
        self.start, self.end = start, end
        self.stream = sys.stderr if stream is None else stream
        self.enabled = bool(getattr(self.stream, "isatty", lambda: False)())
        self.notices = 0
        self.length = 0

    def update(self, folder: tuple[str, ...] | None, day: date, counted: bool = True) -> None:
        if counted:
            self.notices += 1
        if not self.enabled:
            return
        span = max((self.end - self.start).days, 1)
        done = min(max((self.end - day).days / span, 0.0), 1.0)
        filled = round(done * self.WIDTH)
        label = " / ".join(folder or ("?",))[:34]
        line = (
            f"[{'#' * filled}{'.' * (self.WIDTH - filled)}] {done:4.0%} | {label} | "
            f"{max(min(day, self.end), self.start).isoformat()} | {self.notices} avisos"
        )
        # Pad over the previous bar instead of relying on ANSI support in the console.
        self.stream.write("\r" + line.ljust(self.length))
        self.stream.flush()
        self.length = len(line)

    def clear(self) -> None:
        """Erase the bar so another message can use the line; the next update redraws it."""
        if self.length:
            self.stream.write("\r" + " " * self.length + "\r")
            self.stream.flush()
            self.length = 0


class _ClearProgress(logging.Filter):
    """Let extraction warnings start on a clean line instead of after the bar."""

    def __init__(self, progress: Progress) -> None:
        super().__init__()
        self.progress = progress

    def filter(self, record: logging.LogRecord) -> bool:
        self.progress.clear()
        return True


def check_sessions(data_dir: Path, boxes: list[Mailbox], relogin_minutes: int | None = None) -> None:
    """Check every Gmail session before reading any mailbox, so a run stops before importing.

    With relogin_minutes (the scheduled close), an expired session opens Google's
    sign-in in the browser and waits that long for the mailbox owner; the new session
    must belong to the mailbox's configured address. Otherwise the run stops.
    """
    for box in boxes:
        if box.provider != "gmail":
            continue
        try:
            gmail_client.silent_token(data_dir / box.token_cache)()
        except gmail_client.GmailSessionError as exc:
            if relogin_minutes is None:
                raise
            if box.address is None or box.client_secrets is None:
                raise gmail_client.GmailSessionError(
                    f"La sesion de Gmail del buzon {box.name} vencio y no tiene address configurado "
                    "para pedir un nuevo inicio de sesion. Ejecuta login con ese buzon."
                ) from exc
            print(
                f"La sesion de Gmail del buzon {box.name} vencio. Se abre el inicio de sesion de Google "
                f"en el navegador; se espera {relogin_minutes} minutos a que {box.owner.capitalize()} inicie sesion.",
                flush=True,
            )
            account = gmail_client.interactive_login(
                data_dir / box.client_secrets, data_dir / box.token_cache,
                timeout_seconds=relogin_minutes * 60, expected=box.address,
            )
            print(f"Sesion de Google renovada para {account} (buzon {box.name}).", flush=True)


def scan(
    store: Store, data_dir: Path, settings: dict, preferences: Preferences,
    start: date, end: date, read_only: bool = False, reprocess_empty: bool = False,
    incremental: bool = False, relogin_minutes: int | None = None,
) -> None:
    """Read every configured mailbox; incremental reads only what changed since last time.

    An incremental run ignores end: everything new since the saved position is read,
    and start only bounds a mailbox's first synchronisation.
    """
    boxes = load_mailboxes(settings)
    if incremental and (read_only or reprocess_empty):
        raise ValueError("El recorrido incremental no admite solo lectura ni reprocesar avisos.")
    if not read_only and settings.get("folder_scan_verified") is not True:
        raise ValueError(
            "Recorrido de carpetas no verificado. Ejecuta scan --read-only y valida "
            "Entrada, Spam, Eliminados y Dinero con todas sus subcarpetas antes de habilitarlo."
        )
    unverified = [box.name for box in boxes if not box.verified]
    if not read_only and unverified:
        raise ValueError(
            "Buzones sin recorrido verificado: " + ", ".join(unverified)
            + ". Ejecuta scan --read-only, revisa el resultado y marca verified en runtime.json."
        )
    if not preferences.approved_senders:
        raise ValueError("Aprueba primero los remitentes exactos en el panel.")
    if start >= end:
        raise ValueError("El inicio debe ser anterior al fin exclusivo.")
    if not read_only:
        require_model(settings)
        for month in months(start, end):
            store.coverage(
                month, "partial",
                "Recorrido explicito de Bandeja de entrada, Correo no deseado y Elementos "
                "eliminados y Dinero con todas sus subcarpetas; sin conciliar. "
                "Otras carpetas y avisos borrados no estan cubiertos.",
            )
    check_sessions(data_dir, boxes, relogin_minutes)
    model = None if read_only else OllamaJsonModel(settings["ollama_url"], settings["ollama_model"])
    mode = ScanMode(read_only, reprocess_empty, incremental)
    for box in boxes:
        box_start = mailbox_start(store, box, start)
        if len(boxes) > 1:
            print(f"Buzon {box.name} ({box.provider}, titular {box.owner}) desde {box_start.isoformat()}.", flush=True)
        if box_start >= end:
            print("Sin dias por leer en este buzon para el rango pedido.", flush=True)
            continue
        _scan_mailbox(store, data_dir, settings, box, preferences, box_start, end, mode, model)


@dataclass(frozen=True)
class ScanMode:
    read_only: bool
    reprocess_empty: bool
    incremental: bool


def mailbox_start(store: Store, box: Mailbox, start: date) -> date:
    """A member whose movements came by file starts on the last imported day, included.

    Movements of that day that match an imported one stay pending as possible duplicates.
    """
    last = store.last_import_day(box.owner)
    return max(start, date.fromisoformat(last)) if last else start


def _scan_mailbox(
    store: Store, data_dir: Path, settings: dict, box: Mailbox, preferences: Preferences,
    start: date, end: date, mode: ScanMode, model,
) -> None:
    scope = hashlib.sha256(json.dumps(
        [start.isoformat(), end.isoformat(), settings, preferences.to_dict()]
        # A reprocessing run keeps its own resume points and never reuses a normal run's.
        + (["reprocess-empty"] if mode.reprocess_empty else [])
        + ([box.name] if box.name != "santiago" else []) + (["incremental"] if mode.incremental else []),
        sort_keys=True, ensure_ascii=False,
    ).encode()).hexdigest()
    failed_ids: set[str] = set()
    progress = Progress(start, end)
    clear_progress = _ClearProgress(progress)
    extraction_log = logging.getLogger("monthly_close.extraction")
    extraction_log.addFilter(clear_progress)
    try:
        reports = _walk(
            store, data_dir, settings, box, box.preferences(preferences), start, end,
            mode, model, scope, failed_ids, progress,
        )
    finally:
        progress.clear()
        extraction_log.removeFilter(clear_progress)
    _report(store, scope, reports, failed_ids, mode.read_only)


def _walk(
    store: Store, data_dir: Path, settings: dict, box: Mailbox, preferences: Preferences,
    start: date, end: date, mode: ScanMode, model,
    scope: str, failed_ids: set[str], progress: Progress,
) -> list[dict]:
    read_only, reprocess_empty = mode.read_only, mode.reprocess_empty
    received_ids: set[str] = set()
    with open_source(data_dir, settings, preferences.approved_senders, box) as reader:

        def visit(candidate: Candidate) -> bool | None:
            if candidate.sender not in preferences.approved_senders:
                raise OutlookError("La carpeta devolvio un remitente no autorizado. Detenido.")
            if read_only:
                return
            checkpoint = reader.checkpoint_key(candidate)
            status = store.scan_status(scope, checkpoint)
            if status == "complete":
                # Resumed runs skip finished notices; show their date when the reader knows it.
                stamp = getattr(reader, "_received", {}).get(candidate.item_id)
                if isinstance(stamp, str):
                    progress.update(
                        getattr(reader, "active_folder", None),
                        received_local(stamp, preferences.timezone).date(),
                    )
                return
            if status == "pending":
                progress.clear()
                print("Reintentando aviso pendiente desde el punto de reanudacion.", flush=True)
            store.scan_checkpoint(scope, checkpoint, "pending")
            mail = reader.read(candidate)
            local_day = received_local(mail.received, preferences.timezone).date()
            progress.update(
                getattr(reader, "active_folder", None), local_day, counted=start <= local_day < end,
            )
            if local_day < start:
                if mode.incremental:
                    store.scan_checkpoint(scope, checkpoint, "complete")
                    return None
                return False
            if local_day >= end and not mode.incremental:
                store.scan_checkpoint(scope, checkpoint, "complete")
                return
            if mail.id in failed_ids:
                return
            if mail.id in received_ids or (store.seen(mail.id) and not (
                # Notices read before that left no movement, such as income once discarded.
                reprocess_empty and store.processed_without_events(mail.id)
            )):
                store.scan_checkpoint(scope, checkpoint, "complete")
                return
            assert model is not None
            try:
                proposals = extract(
                    mail, preferences, model, store.own_account_suffixes(box.owner),
                    [
                        {"suffix": c.suffix, "name": c.name}
                        for c in credit_ledger.load_configs(data_dir) if c.owner == box.owner
                    ],
                    card_accounts=card_accounts(store, box.owner, preferences),
                )
            except EvidenceError as exc:
                folder = " / ".join(reader.active_folder or ())
                store.record_extraction_failure(
                    mail.id, mail.sender, mail.subject, mail.received,
                    local_day.isoformat(), folder, exc.field,
                )
                failed_ids.add(mail.id)
                progress.clear()
                print(
                    "Aviso pendiente de revision por evidencia no literal; "
                    "sin importar movimientos: " + json.dumps({
                        "carpeta": folder, "remitente": mail.sender,
                        "asunto": mail.subject, "fecha": mail.received, "campo": exc.field,
                    }, ensure_ascii=True),
                    flush=True,
                )
                return
            store.import_message(mail.id, proposals, reprocess=reprocess_empty, owner=box.owner)
            received_ids.add(mail.id)
            store.scan_checkpoint(scope, checkpoint, "complete")

        since = datetime.combine(start, datetime.min.time(), ZoneInfo(preferences.timezone))
        if not mode.incremental:
            # Outlook folders are cut by visit (newest first); Gmail also narrows its listing.
            extra = {"since": since} if box.provider == "gmail" else {}
            return reader.walk_folders(settings["max_messages"], visit, **extra)
        saved: set[str] = set()

        def save(folder: str, token: str) -> None:
            store.save_sync(box.name, folder, token)
            saved.add(folder)

        reports = reader.walk_changes(store.sync_state(box.name), since, settings["max_messages"], visit, save)
        store.prune_sync(box.name, saved)
        retried = _retry_queue(store, reader, visit, failed_ids)
        if retried:
            reports.append({"folder": "Cola de revision", "rows": retried, "authorized": retried, "retried": True})
        return reports


# Received header and list timestamps of the same notice differ by seconds; this
# window finds it again without reading a day's worth of alerts.
QUEUE_WINDOW = timedelta(minutes=15)


def _retry_queue(store: Store, reader, visit, failed_ids: set[str]) -> int:
    """Read again every queued notice of this mailbox: Graph and Gmail no longer report them.

    A notice of another mailbox never matches, because its identity hashes its origin.
    Each one goes through the same visit as a new notice and is imported, queued again
    or skipped exactly as in a full run.
    """
    retried = 0
    for failure in store.queued_failures():
        if failure["message_id"] in failed_ids or failure["sender"] not in reader.approved_senders:
            continue
        received = datetime.fromisoformat(failure["received"])
        for candidate in reader.locate(failure["sender"], received, QUEUE_WINDOW):
            reader.active_folder = tuple(failure["folder"].split(" / "))
            visit(candidate)
            retried += 1
    return retried


def _report(
    store: Store, scope: str, reports: list[dict], failed_ids: set[str], read_only: bool,
) -> None:
    if not read_only:
        store.clear_scan_progress(scope)
        if failed_ids:
            print(
                f"Recorrido finalizado con {len(failed_ids)} aviso(s) sin extraer. "
                "Revisalos en Decision; cobertura parcial. La siguiente ejecucion los reintenta.",
                flush=True,
            )
    for report in reports:
        cutoff = (
            "; corte al encontrar un aviso anterior al inicio"
            if report.get("stopped_before_start") else ""
        )
        if report.get("retried"):
            print(f"Cola de revision: {report['rows']} aviso(s) leidos de nuevo para reintentar su extraccion.")
            continue
        if report.get("incremental"):
            cutoff += "; primera sincronizacion" if report.get("initial") else "; solo cambios desde la anterior"
        print(
            f"{report['folder']}: {report['rows']} filas verificadas; "
            f"{report['authorized']} de remitentes autorizados{cutoff}."
        )
    if read_only:
        print("Solo metadatos: sin abrir mensajes, Ollama, importaciones ni cambios de decisiones.")


def require_model(settings: dict) -> None:
    """Fail before opening Outlook if the configured Ollama endpoint lacks the model."""
    try:
        with httpx.Client(timeout=10, trust_env=False) as client:
            response = client.get(settings["ollama_url"] + "/api/tags")
            response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise LocalModelError("Se requiere Ollama activo en el endpoint configurado.") from exc
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list) or not any(
        isinstance(model, dict) and model.get("name") == settings["ollama_model"]
        for model in models
    ):
        raise LocalModelError("El modelo configurado no esta instalado en el endpoint de Ollama.")


def card_accounts(store: Store, owner: str, preferences: Preferences) -> dict[str, str]:
    """Last four digits -> configured card account, from the member's own statements.

    Only accounts in the configuration count, and digits seen on two different cards
    are left out, so the notice reading never guesses between them.
    """
    labels: dict[str, set[str]] = {}
    allowed = set(preferences.accounts + preferences.excluded_accounts)
    for product, suffix in store.card_statement_suffixes(owner):
        label = statements_module.PRODUCTS.get(product)
        if label in allowed:
            labels.setdefault(suffix, set()).add(label)
    return {suffix: found.pop() for suffix, found in labels.items() if len(found) == 1}


def run_pilot(store: Store, data_dir: Path, settings: dict, read_only: bool) -> None:
    """Validate or import at most one visible authorized notice, without a broad scan."""
    preferences = store.preferences()
    if not read_only:
        require_model(settings)
    with open_source(data_dir, settings, preferences.approved_senders) as reader:
        mail = reader.read(reader.candidate())
    received_local(mail.received, preferences.timezone)
    if read_only:
        print(
            "Piloto: 1 aviso autorizado validado por origen MIME, Message-ID y fecha con zona. "
            "Sin Ollama ni importacion. El escaneo general sigue bloqueado."
        )
        return
    if store.seen(mail.id):
        print("Piloto: aviso ya importado; se conservan las decisiones existentes.")
        return
    model = OllamaJsonModel(settings["ollama_url"], settings["ollama_model"])
    proposals = extract(
        mail, preferences, model, store.own_account_suffixes("santiago"),
        card_accounts=card_accounts(store, "santiago", preferences),
    )
    store.import_message(mail.id, proposals)
    for month in {event["day"][:7] for event in proposals}:
        store.coverage(month, "partial", "Piloto de un solo aviso; no representa el gasto del mes.")
    print(
        f"Piloto: 1 aviso procesado; {len(proposals)} propuestas. "
        "Revisa las decisiones en el panel. El escaneo general sigue bloqueado."
    )


OUTGOING_KINDS = ("transfer", "own_transfer", "unknown", "pse")


class _AllFound(Exception):
    """Every notice the backfill needed was read; the walk can stop."""


def reread_notices(
    data_dir: Path, settings: dict, preferences: Preferences, rows: list, handle, box: Mailbox | None = None,
) -> int:
    """Read again (read-only, no Ollama) the notices behind rows; handle(mail, its rows) each one.

    box is the mailbox to read (Santiago's Outlook by default). Folders are walked newest
    first and stop at notices older than the oldest row, or once every notice was read.
    Returns how many notices were read.
    """
    if not rows:
        return 0
    by_notice: dict[str, list] = {}
    for row in rows:
        by_notice.setdefault(row["message_id"], []).append(row)
    oldest = min(date.fromisoformat(row["day"]) for row in rows) - timedelta(days=10)
    remaining = set(by_notice)
    with open_source(data_dir, settings, preferences.approved_senders, box) as reader:

        def visit(candidate: Candidate) -> bool | None:
            if candidate.sender not in preferences.approved_senders:
                raise OutlookError("La carpeta devolvio un remitente no autorizado. Detenido.")
            stamp = getattr(reader, "_received", {}).get(candidate.item_id)
            if isinstance(stamp, str) and received_local(stamp, preferences.timezone).date() < oldest:
                return False
            mail = reader.read(candidate)
            if mail.id not in remaining:
                return None
            remaining.discard(mail.id)
            handle(mail, by_notice[mail.id])
            if not remaining:
                raise _AllFound
            return None

        # Gmail lists every approved notice unless the listing is narrowed to the dates needed.
        extra = (
            {"since": datetime.combine(oldest, datetime.min.time(), ZoneInfo(preferences.timezone))}
            if box is not None and box.provider == "gmail" else {}
        )
        try:
            reader.walk_folders(settings["max_messages"], visit, **extra)
        except _AllFound:
            pass
    return len(by_notice) - len(remaining)


def backfill_destinations(
    store: Store, data_dir: Path, settings: dict, preferences: Preferences,
) -> dict[str, int]:
    """Read every mailbox's notices again (read-only, no Ollama) to store whole destinations.

    Only notices behind outgoing movements without a destination, or with an account,
    Bre-B or QR key stored by its last four digits, are read; folders are walked newest first and
    stop at notices older than the oldest movement needed.
    """
    result = {"notices": 0, "read": 0, "updated": 0, "without_destination": 0}
    for box in load_mailboxes(settings):
        with store.connect() as db:
            rows = [
                row for row in db.execute(
                    "SELECT id,message_id,day,amount_minor,group_key FROM transactions WHERE owner=? "
                    f"AND instr(message_id, ':')=0 AND kind IN ({','.join('?' * len(OUTGOING_KINDS))})",
                    (box.owner, *OUTGOING_KINDS),
                ).fetchall()
                if row["group_key"] is None or is_short_key(row["group_key"])
            ]
        found: dict[str, str] = {}

        def destinations(mail, notice_rows) -> None:
            events = [{"amount_minor": row["amount_minor"]} for row in notice_rows]
            assign_destinations(events, mail.subject + "\n" + mail.body)
            found.update({
                row["id"]: event["group_key"] for row, event in zip(notice_rows, events) if event.get("group_key")
            })

        result["notices"] += len({row["message_id"] for row in rows})
        result["read"] += reread_notices(data_dir, settings, box.preferences(preferences), rows, destinations, box)
        result["updated"] += store.set_destinations(found)
        result["without_destination"] += len(rows) - len(found)
    return result


def backfill_card_accounts(
    store: Store, data_dir: Path, settings: dict, preferences: Preferences, read_only: bool,
) -> dict:
    """Read Santiago's notices again (read-only, no Ollama) and correct the card by its digits.

    Applies the same rule as a new import: a notice read by the model that names the last
    four digits of exactly one known card gets that card. Read-only mode only counts what
    would change; otherwise the database is copied first and each change is audited.
    """
    known = card_accounts(store, "santiago", preferences)
    with store.connect() as db:
        rows = db.execute(
            "SELECT id,message_id,day,account FROM transactions WHERE owner='santiago' "
            "AND id NOT LIKE 'stmt:%' AND message_id NOT LIKE 'statement:%' AND kind<>'income'",
        ).fetchall()
    result = {"notices": len({row["message_id"] for row in rows}), "read": 0, "changes": {}, "updated": 0, "backup": None}
    if not known:
        return result
    changes: dict[str, str] = {}
    pairs: Counter = Counter()

    def correct(mail, notice_rows) -> None:
        card = card_named_in(mail, preferences, known)
        for row in notice_rows:
            if card and row["account"] != card:
                changes[row["id"]] = card
                pairs[f"{row['account']} -> {card}"] += 1

    result.update(read=reread_notices(data_dir, settings, preferences, rows, correct), changes=dict(pairs))
    if not read_only:
        result["updated"], backup = store.set_card_accounts(changes)
        result["backup"] = backup.name if backup else None
    return result


STATEMENTS_FILE = "statements.json"


def statement_settings(data_dir: Path) -> dict:
    """Local statement settings: each owner's PDF password and the exact approved senders.

    passwords maps each owner to the password of their statements; the older
    password key is Santiago's. The file lives in the local data folder, outside
    Git; its values are never printed.
    """
    path = data_dir / STATEMENTS_FILE
    if not path.is_file():
        raise ValueError(f"Falta {STATEMENTS_FILE} en la carpeta de datos local.")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{STATEMENTS_FILE} debe ser un objeto.")
    senders = raw.get("approved_senders")
    passwords = raw.get("passwords", {})
    if not isinstance(passwords, dict) or set(passwords) - set(OWNERS) or (
        "password" in raw and "santiago" in passwords
    ):
        raise ValueError(f"{STATEMENTS_FILE}: passwords debe tener una contrasena por titular ({', '.join(OWNERS)}).")
    passwords = {**({"santiago": raw["password"]} if "password" in raw else {}), **passwords}
    if (
        not passwords or any(not isinstance(item, str) or not item for item in passwords.values())
        or not isinstance(senders, list) or not senders
        or any(not isinstance(item, str) or not EMAIL.fullmatch(item) for item in senders)
    ):
        raise ValueError(f"{STATEMENTS_FILE} necesita passwords por titular y remitentes exactos aprobados.")
    return {"passwords": passwords, "approved_senders": [item.lower() for item in senders]}


def reconcile_statements(
    store: Store, data_dir: Path, settings: dict, preferences: Preferences,
    start: date, read_only: bool,
) -> dict:
    """Read the statements of every mailbox in memory, reconcile them and import what only they show.

    Each statement belongs to the owner of the mailbox it arrived in. Read-only mode
    reports without writing. Any statement that cannot be read exactly stops the run:
    nothing is imported from a partial reading.
    """
    config = statement_settings(data_dir)
    since = datetime.combine(start - timedelta(days=31), datetime.min.time(), ZoneInfo(preferences.timezone))
    found: dict[str, statements_module.Statement] = {}
    owners: dict[tuple, str] = {}  # whose mailbox each numbered product's statement came from
    for box in load_mailboxes(settings):
        with open_source(data_dir, settings, preferences.approved_senders, box) as reader:
            for sender in config["approved_senders"]:
                for name, content in reader.statement_files(
                    sender, since, statements_module.STATEMENT_NAME, statements_module.MAX_PDF_BYTES,
                ):
                    statement = _read_statement(name, content, config["passwords"], start, box)
                    if statement is None:
                        continue
                    key = (statement.product, statement.suffix, statement.period_end)
                    known = owners.setdefault(key, statement.owner) if statement.suffix else statement.owner
                    if known != statement.owner:
                        # A shared product would count twice or for the wrong person.
                        raise ValueError(
                            f"El extracto {statement.product} *{statement.suffix} con corte "
                            f"{statement.period_end.isoformat()} llega a los buzones de {known} y "
                            f"{statement.owner}; no se sabe de quien es. Detenido sin importar."
                        )
                    found.setdefault(statement.identity, statement)
    with_register = {(config.owner, config.product, config.suffix) for config in credit_ledger.load_configs(data_dir)}
    for statement in found.values():
        if (statement.owner, statement.product, statement.suffix) in with_register:
            statement.movements = []  # its payment register is the source; balances still count
    report = statements_module.reconcile(list(found.values()), store.state()["transactions"], start)
    events = [
        statements_module.missing_event(
            candidate, preferences.categories[-1], income_category(candidate.movement.description),
        )
        for candidate in report.missing
    ]
    proposals: dict[str, dict] = {}
    if not read_only and any(event["kind"] == "purchase" for event in events):
        model = OllamaJsonModel(settings["ollama_url"], settings["ollama_model"])
        proposals, model_failed = propose_statement_categories(store, model, events)
        if model_failed:
            print("Ollama no respondio: las categorias se sugirieron solo con tus reglas y decisiones anteriores.", flush=True)
    imported = 0 if read_only else store.record_statements(report.checks(), events)
    if proposals:
        # Kept so the panel can say where each proposed category came from.
        store.save_category_suggestions(proposals, replace=False)
    proposed = len(proposals)
    reclassified = 0 if read_only else store.reclassify_statement_movements(
        _statement_kinds(report, preferences),
    )
    return {
        "statements": len(found), "matched": len(report.matched), "missing": len(report.missing),
        "imported": imported, "balances": sum(s.balance_minor is not None for s in found.values()),
        "reclassified": reclassified,
        "categories_proposed": proposed,
    }


def _read_statement(
    name: str, content: bytes, passwords: dict[str, str], start: date, box: Mailbox,
) -> statements_module.Statement | None:
    """One statement attachment of a mailbox, or None when it is not a statement to reconcile.

    It opens only with the password of the mailbox's owner: a statement of another
    person in that mailbox stops the run instead of being counted as the owner's.
    """
    found_name = statements_module.identify(name)
    if found_name is None:
        return None
    product, period = found_name
    if product not in statements_module.PRODUCTS or period < start.strftime("%Y%m"):
        return None
    if box.owner not in passwords:
        raise ValueError(f"Falta la contrasena de los extractos de {box.owner} en {STATEMENTS_FILE} (buzon {box.name}).")
    try:
        statement = statements_module.parse(
            product, period,
            statements_module.decrypt_text(content, passwords[box.owner], layout=product == "CTA_AHORROS"),
        )
    except statements_module.StatementError as exc:
        raise ValueError(
            f"Extracto {product} {period} del buzon {box.name}: {exc} Se probo solo la de {box.owner}: "
            "si el extracto es de otra persona, no se cuenta como suyo."
        ) from exc
    statement.owner = box.owner
    return statement


def _statement_kinds(report, preferences: Preferences) -> dict[str, str]:
    """Kind each statement line would get today, for the movements it imported itself.

    A movement imported from a statement matches its own line on the next run, so it
    is never imported again; this lets an improved reader correct it while pending.
    """
    kinds = {}
    for candidate, row_id in report.matched:
        event = statements_module.missing_event(
            candidate, preferences.categories[-1], income_category(candidate.movement.description),
        )
        if event["id"] == row_id:
            kinds[row_id] = event["kind"]
    return kinds


def import_credit_ledgers(store: Store, data_dir: Path, preferences: Preferences) -> list[str]:
    """Import every configured payment register after checking it against the statements.

    A register whose capital differs from any statement period stops the run without
    importing anything from it.
    """
    lines = []
    configs = credit_ledger.load_configs(data_dir)
    if not configs:
        raise ValueError(f"No hay registros de credito en {credit_ledger.LEDGERS_FILE}.")
    for config in configs:
        column = "balance_minor" if config.contrast == "capital" else "paid_minor"
        with store.connect() as db:
            periods = [
                (row["period_end"], row[column]) for row in db.execute(
                    f"""SELECT period_end, {column} FROM statement_checks WHERE product=?
                    AND suffix=? AND {column} IS NOT NULL ORDER BY period_end""", (config.product, config.suffix),
                )
            ]
        if len(periods) < 2:
            raise ValueError(f"Faltan extractos del credito *{config.suffix} para contrastar el registro.")
        first = date.fromisoformat(periods[0][0])
        payments = credit_ledger.read_payments(config, first.replace(day=1))
        problems = (
            credit_ledger.contrast(payments, periods) if config.contrast == "capital"
            else credit_ledger.contrast_payments(payments, periods)
        )
        if problems:
            raise ValueError(f"El registro del credito *{config.suffix} no cuadra con el banco: " + "; ".join(problems))
        balances = periods
        category = config.category if config.category in preferences.categories else preferences.categories[-1]
        result = store.import_credit_ledger(
            config.owner, config.suffix, credit_ledger.events(config, payments, category), config.product,
        )
        lines.append(
            f"Credito *{config.suffix}: {len(payments)} pagos leidos; cuadra con {len(balances) - 1} periodos del banco; "
            f"{result['imported']} nuevos, {result['replaced']} cuotas del extracto reemplazadas, "
            f"{result['possible_duplicates']} posibles duplicados anotados."
        )
    return lines


def refresh_reconciled_coverage(store: Store, preferences: Preferences) -> None:
    """Mark the closed months the statements reconcile, and undo those that stopped qualifying."""
    today = datetime.now(ZoneInfo(preferences.timezone)).date()
    changed = store.update_reconciled_coverage(today)
    if changed["reconciled"]:
        print("Meses conciliados con los extractos: " + ", ".join(changed["reconciled"]) + ".", flush=True)
    if changed["revoked"]:
        print("Meses que dejaron de estar conciliados: " + ", ".join(changed["revoked"]) + ".", flush=True)


def run_complements(store: Store, data_dir: Path, settings: dict, preferences: Preferences, start: date) -> None:
    """After a mailbox scan: reconcile the bank statements, then import the credit registers.

    Each step runs only when its local configuration exists. Statements come first,
    because the registers are checked against their balances and payments. Any
    failure stops the run, like the scan itself.
    """
    if (data_dir / STATEMENTS_FILE).is_file():
        since = date(start.year, 1, 1)
        result = reconcile_statements(store, data_dir, settings, preferences, since, read_only=False)
        print(
            f"Extractos: {result['statements']} leidos desde {since.isoformat()}; {result['matched']} coinciden con "
            f"los avisos; {result['imported']} importados como pendientes.", flush=True,
        )
    if credit_ledger.load_configs(data_dir):
        print("\n".join(import_credit_ledgers(store, data_dir, preferences)), flush=True)
    refresh_reconciled_coverage(store, preferences)


def run_member_import(store: Store, folder: Path) -> None:
    """Import Manuela's exported files and compare her monthly totals with her dashboard."""
    result = import_member(store, folder)
    print(
        f"Importacion de Manuela: {result['imported']} nuevos, {result['skipped']} ya importados; "
        f"{result['approved']} aprobados en su panel, {result['by_destination']} decididos por destino, "
        f"{result['pending']} pendientes; {result['between_owners']} entre titulares."
    )
    parity = folder / PARITY_FILE
    if not parity.is_file():
        print("Sin archivo de paridad: no se compararon totales.")
        return
    matched, lines = parity_report(store, parity)
    print("\n".join(lines))
    if not matched:
        raise ValueError("Los totales importados no coinciden con el panel de Manuela; revisa antes de usarlos.")


def run(args: argparse.Namespace) -> None:
    data_dir = args.data_dir.expanduser().resolve()
    store = Store(data_dir / "close.sqlite3")
    if args.command == "serve":
        try:
            settings = runtime(data_dir)
            model = OllamaJsonModel(settings["ollama_url"], settings["ollama_model"])
        except (ValueError, OSError):
            model = None  # Without a valid runtime.json the panel works, minus rule suggestions.
        server = create_server(store, args.port, model)
        print(f"Panel local: http://127.0.0.1:{server.server_port}", flush=True)
        # The same handler, with its checks, on the private address for paired devices.
        network = LanAccess("cierre", announce=lambda text: print(text, flush=True)).serve(
            server.RequestHandlerClass,
        ) if args.lan else None
        if network is not None:
            start_in_background(network)
        try:
            server.serve_forever()
        finally:
            server.server_close()
            if network is not None:
                network.shutdown()
                network.server_close()
        return
    if args.command == "summary":
        month = args.month or datetime.now().strftime("%Y-%m")
        datetime.strptime(month, "%Y-%m")
        print(store.summary(month, args.owner or "santiago"))
        return
    if args.command == "import-manuela":
        run_member_import(store, args.source_dir.expanduser().resolve())
        refresh_reconciled_coverage(store, store.preferences())
        return
    if args.command == "merge-category":
        if not args.source_category or not args.target_category:
            raise ValueError("Usa --from y --to con dos categorias configuradas.")
        result = store.merge_category(args.source_category, args.target_category)
        print(
            f"Categoria «{args.source_category}» unida a «{args.target_category}»: "
            f"{result['movements']} movimientos y {result['rules']} reglas movidos."
        )
        return
    if args.command == "import-credit-ledger":
        print("\n".join(import_credit_ledgers(store, data_dir, store.preferences())))
        refresh_reconciled_coverage(store, store.preferences())
        return
    if args.command == "payroll-deduction":
        if not args.owner or not args.suffix:
            raise ValueError("Usa --owner y --suffix con los ultimos cuatro digitos del credito.")
        changes = store.set_payroll_deduction(args.owner, args.suffix)
        print(f"Credito *{args.suffix} descontado por nomina de {args.owner}. Ingresos de nomina agregados o ajustados: {changes}.")
        return
    if args.command == "owner-account":
        if not args.owner or not args.suffix:
            raise ValueError("Usa --owner y --suffix con los ultimos cuatro digitos.")
        flagged = store.set_owner_account(args.owner, args.suffix)
        print(f"Cuenta *{args.suffix} asignada a {args.owner}. Movimientos marcados entre titulares: {flagged}.")
        return
    preferences = store.preferences()
    now = datetime.now(ZoneInfo(preferences.timezone))
    closing_month = (
        due_close_month(now, preferences.timezone, store.last_close()) if args.command == "scheduled" else None
    )
    if args.command == "scheduled" and closing_month is None:
        print("Sin cierre pendiente: el ultimo mes vencido ya esta cerrado.")
        return
    settings = runtime(data_dir)
    if args.command == "pilot":
        run_pilot(store, data_dir, settings, args.read_only)
        return
    if args.command == "login":
        print(login(data_dir, settings, getattr(args, "mailbox", None)))
        return
    if args.command == "backfill-destinations":
        result = backfill_destinations(store, data_dir, settings, preferences)
        print(
            f"Destinos: {result['notices']} avisos por revisar, {result['read']} leidos; "
            f"{result['updated']} movimientos con destino guardado, "
            f"{result['without_destination']} sin destino literal en el aviso. "
            "Solo lectura de los buzones; sin Ollama ni cambios de decisiones."
        )
        return
    if args.command == "backfill-card-accounts":
        result = backfill_card_accounts(store, data_dir, settings, preferences, args.read_only)
        detail = "; ".join(f"{count} x {pair}" for pair, count in sorted(result["changes"].items())) or "ninguno"
        print(
            f"Tarjetas: {result['notices']} avisos por revisar, {result['read']} leidos. "
            f"Cambios {'que se harian' if args.read_only else 'aplicados'}: {detail}. "
            + ("Solo lectura: nada se guardo." if args.read_only else
               f"{result['updated']} movimientos corregidos; copia previa: {result['backup'] or 'no hizo falta'}.")
            + " Sin Ollama; importes, categorias y decisiones no cambian."
        )
        return
    if args.command == "reconcile-statements":
        start = date.fromisoformat(args.start) if args.start else date(now.year, 1, 1)
        result = reconcile_statements(store, data_dir, settings, preferences, start, args.read_only)
        print(
            f"Extractos: {result['statements']} leidos desde {start.isoformat()}; "
            f"{result['matched']} movimientos coinciden con los avisos, {result['missing']} faltaban; "
            f"{result['balances']} saldos de inversiones y creditos. "
            + ("Solo lectura: nada se importo." if args.read_only else
               f"{result['imported']} importados como pendientes; decidelos en el panel."
               + (f" {result['reclassified']} pendientes reclasificados." if result["reclassified"] else "")
               + (f" {result['categories_proposed']} compras con categoria sugerida (sin decidir)."
                  if result["categories_proposed"] else ""))
        )
        if not args.read_only:
            refresh_reconciled_coverage(store, preferences)
        return
    if args.command == "check-config":
        if not preferences.approved_senders or settings.get("folder_scan_verified") is not True:
            raise ValueError("Faltan remitentes aprobados o recorrido de carpetas verificado.")
        print("Paso 0, remitentes y configuracion presentes. El buzon aun requiere validacion real.")
        return
    if args.command == "scheduled":
        assert closing_month is not None
        start = date.fromisoformat(closing_month + "-01")
        end = now.date() + timedelta(days=1)
        print(f"Cierre de {closing_month}.", flush=True)
        # A completed scan is idempotent and human choices survive every run.
        scan(
            store, data_dir, settings, preferences, start, end,
            incremental=True, relogin_minutes=RELOGIN_MINUTES,
        )
        run_complements(store, data_dir, settings, preferences, start)
        summary = store.summary(closing_month)
        (data_dir / "last-close.txt").write_text(summary + "\n", encoding="utf-8")
        store.record_close(closing_month)
        print(summary)
        return
    # Without dates, a normal scan reads only what changed since the last one (delta).
    incremental = not (args.start or args.end or args.read_only or args.reprocess_empty)
    start = date.fromisoformat(args.start) if args.start else date(now.year, 1, 1)
    if args.end:
        end = date.fromisoformat(args.end)
    else:
        end = now.date() + timedelta(days=1) if incremental else now.date().replace(day=1)
    if start >= end:
        raise ValueError("Usa --start y --end (fin exclusivo); el rango debe contener dias.")
    scan(
        store, data_dir, settings, preferences, start, end,
        read_only=args.read_only, reprocess_empty=args.reprocess_empty, incremental=incremental,
    )
    if not args.read_only:
        run_complements(store, data_dir, settings, preferences, start)
        print("Importacion terminada. Revisa movimientos, meses parciales y decisiones en el panel.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Cierre mensual local: buzon de Outlook por Microsoft Graph + Ollama.")
    parser.add_argument(
        "command",
        choices=[
            "serve", "login", "pilot", "scan", "summary", "scheduled", "check-config",
            "import-manuela", "owner-account", "backfill-destinations", "backfill-card-accounts", "reconcile-statements", "payroll-deduction", "import-credit-ledger", "merge-category",
        ],
    )
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument(
        "--source-dir", type=Path, default=data_root() / "ImportManuela",
        help="Con import-manuela, carpeta local con los archivos exportados de Manuela.",
    )
    parser.add_argument(
        "--owner", choices=["santiago", "manuela", "hogar"],
        help="Con summary, titular u hogar (por defecto santiago); con owner-account, titular.",
    )
    parser.add_argument("--suffix", help="Con owner-account, ultimos cuatro digitos de la cuenta.")
    parser.add_argument("--mailbox", help="Con login, nombre del buzon de runtime.json (por defecto Outlook de Santiago).")
    parser.add_argument(
        "--log-file", type=Path,
        help="Registro de esta ejecucion dentro de la carpeta logs de los datos (el lanzador lo indica).",
    )
    parser.add_argument("--from", dest="source_category", help="Con merge-category, categoria que desaparece.")
    parser.add_argument("--to", dest="target_category", help="Con merge-category, categoria que la recibe.")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument(
        "--lan", action="store_true",
        help="Con serve, abre tambien el panel a dispositivos emparejados de la red de casa (HTTPS).",
    )
    parser.add_argument("--start", help="YYYY-MM-DD")
    parser.add_argument("--end", help="YYYY-MM-DD, exclusivo")
    parser.add_argument("--month", help="YYYY-MM")
    parser.add_argument(
        "--read-only", action="store_true",
        help="Con pilot valida un aviso; con scan recorre las carpetas autorizadas solo con metadatos.",
    )
    parser.add_argument(
        "--reprocess-empty", action="store_true",
        help="Con scan, vuelve a leer los avisos ya procesados que no dejaron movimientos.",
    )
    args = parser.parse_args()
    if args.reprocess_empty and (args.command != "scan" or args.read_only):
        parser.error("--reprocess-empty solo se permite con scan, sin --read-only.")
    if args.mailbox and args.command != "login":
        parser.error("--mailbox solo se permite con login.")
    if args.read_only and args.command not in {"pilot", "scan", "reconcile-statements", "backfill-card-accounts"}:
        parser.error("--read-only solo se permite con pilot, scan, reconcile-statements o backfill-card-accounts.")
    if args.lan and args.command != "serve":
        parser.error("--lan solo se permite con serve.")
    data_dir = args.data_dir.expanduser().resolve()
    try:
        with run_log(data_dir, args.command, args.log_file):
            _run_reporting(args)
    except ValueError as exc:  # an invalid --log-file, before anything ran
        print(f"Detenido: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


def _run_reporting(args: argparse.Namespace) -> None:
    """Run a command and report failures without message bodies; the run log keeps them."""
    try:
        run(args)
    except KeyboardInterrupt:
        print("Detenido por el usuario.", file=sys.stderr)
        raise SystemExit(130)
    except (
        ValueError, OSError, KeyError, ZoneInfoNotFoundError,
        OutlookError, LocalModelError,
    ) as exc:
        # No message bodies or model payloads in terminal/task diagnostics.
        print(f"Detenido: {exc}", file=sys.stderr)
        frames = [
            frame for frame in traceback.extract_tb(exc.__traceback__)
            if Path(frame.filename).parent.name == "monthly_close"
        ]
        for frame in frames[-2:]:
            print(f"Punto de parada: {Path(frame.filename).name}:{frame.lineno}.", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
