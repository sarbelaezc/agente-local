from __future__ import annotations

import argparse
import calendar
import hashlib
import json
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
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, sync_playwright

from mail_invoices.local_model import LocalModelError, validate_local_url
from monthly_close.model import OllamaJsonModel
from monthly_close.domain import Preferences, received_local
from monthly_close.extraction import extract
from monthly_close.outlook import Candidate, OutlookError, validate_url
from monthly_close.server import create_server
from monthly_close.source_reader import OutlookSourceReader
from monthly_close.store import Store


def default_data_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", ".local")) / "MonthlyCloseAgent"


def runtime(data_dir: Path) -> dict:
    path = data_dir / "runtime.json"
    if not path.is_file():
        raise ValueError(
            "Copia config\\monthly-close.example.json a " + str(path)
            + " y configura el navegador y los selectores."
        )
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Configuracion de ejecucion invalida.")
    url = validate_local_url(raw.get("ollama_url", ""))
    endpoint = urlparse(url)
    if (
        endpoint.port in (None, 11434, 11435) or endpoint.username or endpoint.password
        or endpoint.path or endpoint.query or endpoint.fragment
    ):
        raise ValueError("Ollama requiere un puerto exclusivo (11436), sin ruta ni credenciales.")
    validate_url(raw.get("outlook_url", ""))
    executable = raw.get("browser_executable")
    if not isinstance(executable, str):
        raise ValueError("Configura browser_executable.")
    executable = os.path.expandvars(executable)
    if "%" in executable or not Path(executable).is_file():
        raise ValueError("No se encuentra el ejecutable del navegador configurado.")
    raw["browser_executable"] = executable
    if type(raw.get("max_messages")) is not int or not 1 <= raw["max_messages"] <= 10000:
        raise ValueError("max_messages debe estar entre 1 y 10000.")
    if not isinstance(raw.get("ollama_model"), str) or not raw["ollama_model"].strip():
        raise ValueError("Configura ollama_model.")
    return raw


@contextmanager
def browser(data_dir: Path, settings: dict) -> Iterator[Page]:
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(data_dir / "BrowserProfile"),
            executable_path=settings["browser_executable"],
            headless=False,
            viewport={"width": 1440, "height": 1000},
            accept_downloads=False,
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(15000)
            page.goto(settings["outlook_url"], wait_until="domcontentloaded")
            yield page
        finally:
            context.close()


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


def scan(
    store: Store, data_dir: Path, settings: dict, preferences: Preferences,
    start: date, end: date, read_only: bool = False,
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
        [start.isoformat(), end.isoformat(), settings, preferences.to_dict()],
        sort_keys=True, ensure_ascii=False,
    ).encode()).hexdigest()
    received_ids: set[str] = set()
    with browser(data_dir, settings) as page:
        reader = OutlookSourceReader(page, settings["outlook_url"], preferences.approved_senders)

        def visit(candidate: Candidate) -> bool | None:
            if candidate.sender not in preferences.approved_senders:
                raise OutlookError("La carpeta devolvio un remitente no autorizado. Detenido.")
            if read_only:
                return
            checkpoint = reader.checkpoint_key(candidate)
            status = store.scan_status(scope, checkpoint)
            if status == "complete":
                return
            if status == "pending":
                print("Reintentando aviso pendiente desde el punto de reanudacion.", flush=True)
            store.scan_checkpoint(scope, checkpoint, "pending")
            mail = reader.read(candidate)
            local_day = received_local(mail.received, preferences.timezone).date()
            if local_day < start:
                return False
            if local_day >= end:
                store.scan_checkpoint(scope, checkpoint, "complete")
                return
            if mail.id in received_ids or store.seen(mail.id):
                store.scan_checkpoint(scope, checkpoint, "complete")
                return
            assert model is not None
            proposals = extract(mail, preferences, model)
            store.import_message(mail.id, proposals)
            received_ids.add(mail.id)
            store.scan_checkpoint(scope, checkpoint, "complete")

        reports = reader.walk_folders(settings["max_messages"], visit)
    if not read_only:
        store.clear_scan_progress(scope)
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
    """Fail before opening Outlook if the exclusive Ollama instance lacks the model."""
    try:
        with httpx.Client(timeout=10, trust_env=False) as client:
            response = client.get(settings["ollama_url"] + "/api/tags")
            response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise LocalModelError("Se requiere la instancia exclusiva de Ollama activa.") from exc
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list) or not any(
        isinstance(model, dict) and model.get("name") == settings["ollama_model"]
        for model in models
    ):
        raise LocalModelError("El modelo configurado no esta instalado en Ollama del cierre mensual.")


def run_pilot(store: Store, data_dir: Path, settings: dict, read_only: bool) -> None:
    """Validate or import at most one visible authorized notice, without a broad scan."""
    preferences = store.preferences()
    if not read_only:
        require_model(settings)
    with browser(data_dir, settings) as page:
        reader = OutlookSourceReader(page, settings["outlook_url"], preferences.approved_senders)
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
        with browser(data_dir, settings):
            input(
                "Inicia sesion manualmente. El agente no actua sobre MFA ni captcha. "
                "Cuando veas tu buzon, pulsa Enter aqui para cerrar el perfil: "
            )
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
    scan(store, data_dir, settings, preferences, start, end, read_only=args.read_only)
    if not args.read_only:
        print("Importacion terminada. Revisa movimientos, meses parciales y decisiones en el panel.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Cierre mensual local: Outlook Web + Ollama.")
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
    args = parser.parse_args()
    if args.read_only and args.command not in {"pilot", "scan"}:
        parser.error("--read-only solo se permite con pilot o scan.")
    try:
        run(args)
    except KeyboardInterrupt:
        print("Detenido por el usuario.", file=sys.stderr)
        raise SystemExit(130)
    except PlaywrightError as exc:
        # Playwright call logs can embed financial text from Outlook's aria-labels.
        print(
            "Detenido: Outlook no confirmo la interfaz esperada. "
            "No se continua ni se imprime contenido de los mensajes.",
            file=sys.stderr,
        )
        frames = [
            frame for frame in traceback.extract_tb(exc.__traceback__)
            if Path(frame.filename).parent.name == "monthly_close"
        ]
        if frames:
            frame = frames[-1]
            print(f"Punto de parada: {Path(frame.filename).name}:{frame.lineno}.", file=sys.stderr)
        raise SystemExit(1) from None
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
