from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import logging
import os
import sys
import traceback
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterator
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from mail_invoices.local_model import LocalModelError, validate_local_url
from mail_invoices.storage import data_root
from monthly_close.model import OllamaJsonModel
from monthly_close.domain import Preferences, received_local
from monthly_close.extraction import EvidenceError, extract
from monthly_close.graph_client import (
    AUTHORITIES, CACHE_NAME, CLIENT_ID, GraphClient, interactive_login, silent_token,
)
from monthly_close.graph_reader import DEFAULT_ORIGIN, GraphSourceReader
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
    return raw


@contextmanager
def open_source(data_dir: Path, settings: dict, approved_senders: list[str]) -> Iterator:
    """Read-only mailbox reader over Microsoft Graph, using the saved session."""
    client = GraphClient(silent_token(
        settings["graph_client_id"], settings.get("graph_authority", "consumers"), data_dir / CACHE_NAME,
    ))
    try:
        # DEFAULT_ORIGIN keeps message identifiers stable, so imported notices are not repeated.
        yield GraphSourceReader(client, approved_senders, DEFAULT_ORIGIN)
    finally:
        client.close()


def months(start: date, end: date) -> list[str]:
    result = []
    current = start.replace(day=1)
    while current < end:
        result.append(current.strftime("%Y-%m"))
        current = (current.replace(day=28) + timedelta(days=4)).replace(day=1)
    return result


def scheduled_month(now: datetime, timezone: str) -> str | None:
    """Only close on the last local calendar day, during the 22:00 hour."""
    if now.tzinfo is None:
        raise ValueError("El reloj del cierre necesita zona horaria explicita.")
    local = now.astimezone(ZoneInfo(timezone))
    if local.day == calendar.monthrange(local.year, local.month)[1] and local.hour == 22:
        return local.strftime("%Y-%m")
    return None


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


def scan(
    store: Store, data_dir: Path, settings: dict, preferences: Preferences,
    start: date, end: date, read_only: bool = False, reprocess_empty: bool = False,
) -> None:
    if not read_only and settings.get("folder_scan_verified") is not True:
        raise ValueError(
            "Recorrido de carpetas no verificado. Ejecuta scan --read-only y valida "
            "Entrada, Spam, Eliminados y Dinero con todas sus subcarpetas antes de habilitarlo."
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
    model = None if read_only else OllamaJsonModel(settings["ollama_url"], settings["ollama_model"])
    scope = hashlib.sha256(json.dumps(
        [start.isoformat(), end.isoformat(), settings, preferences.to_dict()]
        # A reprocessing run keeps its own resume points and never reuses a normal run's.
        + (["reprocess-empty"] if reprocess_empty else []),
        sort_keys=True, ensure_ascii=False,
    ).encode()).hexdigest()
    received_ids: set[str] = set()
    failed_ids: set[str] = set()
    progress = Progress(start, end)
    clear_progress = _ClearProgress(progress)
    extraction_log = logging.getLogger("monthly_close.extraction")
    extraction_log.addFilter(clear_progress)
    try:
        reports = _walk(
            store, data_dir, settings, preferences, start, end, read_only, reprocess_empty,
            model, scope, received_ids, failed_ids, progress,
        )
    finally:
        progress.clear()
        extraction_log.removeFilter(clear_progress)
    _report(store, scope, reports, failed_ids, read_only)


def _walk(
    store: Store, data_dir: Path, settings: dict, preferences: Preferences,
    start: date, end: date, read_only: bool, reprocess_empty: bool, model,
    scope: str, received_ids: set[str], failed_ids: set[str], progress: Progress,
) -> list[dict]:
    with open_source(data_dir, settings, preferences.approved_senders) as reader:

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
                return False
            if local_day >= end:
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
                proposals = extract(mail, preferences, model)
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
            store.import_message(mail.id, proposals, reprocess=reprocess_empty)
            received_ids.add(mail.id)
            store.scan_checkpoint(scope, checkpoint, "complete")

        return reader.walk_folders(settings["max_messages"], visit)


def _report(
    store: Store, scope: str, reports: list[dict], failed_ids: set[str], read_only: bool,
) -> None:
    if not read_only:
        store.clear_scan_progress(scope)
        if failed_ids:
            print(
                f"Recorrido finalizado con {len(failed_ids)} aviso(s) sin extraer. "
                "Revisalos en Decision; cobertura parcial. Repite el rango para reintentarlos.",
                flush=True,
            )
    for report in reports:
        cutoff = (
            "; corte al encontrar un aviso anterior al inicio"
            if report.get("stopped_before_start") else ""
        )
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
    proposals = extract(mail, preferences, model)
    store.import_message(mail.id, proposals)
    for month in {event["day"][:7] for event in proposals}:
        store.coverage(month, "partial", "Piloto de un solo aviso; no representa el gasto del mes.")
    print(
        f"Piloto: 1 aviso procesado; {len(proposals)} propuestas. "
        "Revisa las decisiones en el panel. El escaneo general sigue bloqueado."
    )


def run(args: argparse.Namespace) -> None:
    data_dir = args.data_dir.expanduser().resolve()
    store = Store(data_dir / "close.sqlite3")
    if args.command == "serve":
        server = create_server(store, args.port)
        print(f"Panel local: http://127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        finally:
            server.server_close()
        return
    if args.command == "summary":
        month = args.month or datetime.now().strftime("%Y-%m")
        datetime.strptime(month, "%Y-%m")
        print(store.summary(month))
        return
    preferences = store.preferences()
    now = datetime.now(ZoneInfo(preferences.timezone))
    closing_month = scheduled_month(now, preferences.timezone) if args.command == "scheduled" else None
    if args.command == "scheduled" and closing_month is None:
        return
    settings = runtime(data_dir)
    if args.command == "pilot":
        run_pilot(store, data_dir, settings, args.read_only)
        return
    if args.command == "login":
        account = interactive_login(
            settings["graph_client_id"], settings.get("graph_authority", "consumers"), data_dir / CACHE_NAME,
        )
        print(f"Sesion de Microsoft guardada para {account}. Permiso: solo lectura de correo.")
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
        # A completed scan is idempotent and human choices survive every run.
        scan(store, data_dir, settings, preferences, start, end)
        summary = store.summary(closing_month)
        (data_dir / "last-close.txt").write_text(summary + "\n", encoding="utf-8")
        print(summary)
        return
    start = date.fromisoformat(args.start) if args.start else date(now.year, 1, 1)
    end = date.fromisoformat(args.end) if args.end else now.date().replace(day=1)
    if start >= end:
        raise ValueError("Usa --start y --end (fin exclusivo); el rango debe contener dias.")
    scan(
        store, data_dir, settings, preferences, start, end,
        read_only=args.read_only, reprocess_empty=args.reprocess_empty,
    )
    if not args.read_only:
        print("Importacion terminada. Revisa movimientos, meses parciales y decisiones en el panel.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Cierre mensual local: buzon de Outlook por Microsoft Graph + Ollama.")
    parser.add_argument(
        "command",
        choices=["serve", "login", "pilot", "scan", "summary", "scheduled", "check-config"],
    )
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--port", type=int, default=8766)
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
    if args.read_only and args.command not in {"pilot", "scan"}:
        parser.error("--read-only solo se permite con pilot o scan.")
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
