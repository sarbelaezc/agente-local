from __future__ import annotations

import argparse
import calendar
import json
import os
import sys
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterator
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, sync_playwright

from mail_invoices.local_model import LocalModelError, OllamaJsonModel, validate_local_url
from monthly_close.domain import Preferences, received_local
from monthly_close.extraction import extract
from monthly_close.outlook import Candidate, OutlookError, OutlookReader, sender_query, validate_url
from monthly_close.server import create_server
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
    if type(raw.get("max_messages")) is not int or not 1 <= raw["max_messages"] <= 1000:
        raise ValueError("max_messages debe estar entre 1 y 1000.")
    if not isinstance(raw.get("ollama_model"), str) or not raw["ollama_model"].strip():
        raise ValueError("Configura ollama_model.")
    if not isinstance(raw.get("selectors"), dict):
        raise ValueError("Configura los selectores.")
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
    start: date, end: date, discover: bool = False,
) -> None:
    if settings.get("selectors_verified") is not True:
        raise ValueError(
            "Selectores no verificados. Calibra con Outlook y marca selectors_verified "
            "solo despues de validar Todas las carpetas, Spam, Eliminados y mensajes sin agrupar."
        )
    if not discover and not preferences.approved_senders:
        raise ValueError("Aprueba primero los remitentes exactos en el panel.")
    for month in months(start, end):
        store.coverage(
            month, "partial",
            "Sin conciliar. Outlook puede omitir Spam, Eliminados, avisos borrados o hilos. "
            "Compara con extractos antes de marcar revisado.",
        )
    model = OllamaJsonModel(settings["ollama_url"], settings["ollama_model"])
    with browser(data_dir, settings) as page:
        reader = OutlookReader(page, settings["outlook_url"], settings["selectors"])
        if discover:
            # Discovery is explicitly requested and reads result metadata, never bodies.
            query = f"received:>={start.isoformat()} received:<{end.isoformat()}"
            reader.walk(query, settings["max_messages"], lambda row: store.discover(row.sender, row.subject))
            return
        for sender in preferences.approved_senders:
            def visit(candidate: Candidate) -> None:
                if candidate.sender != sender:
                    raise OutlookError("La busqueda devolvio un remitente no autorizado. Detenido.")
                mail = reader.read(candidate)
                if store.seen(mail.id):
                    return
                local_day = received_local(mail.received, preferences.timezone).date()
                if not start <= local_day < end:
                    return
                proposals = extract(mail, preferences, model)
                store.import_message(mail.id, proposals)

            # Widen query boundaries for Outlook's UI timezone; filter exact local dates above.
            reader.walk(
                sender_query(sender, start - timedelta(days=1), end + timedelta(days=1)),
                settings["max_messages"], visit,
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
    if args.command == "login":
        with browser(data_dir, settings):
            input(
                "Inicia sesion manualmente. El agente no actua sobre MFA ni captcha. "
                "Cuando veas tu buzon, pulsa Enter aqui para cerrar el perfil: "
            )
        return
    if args.command == "check-config":
        if not preferences.approved_senders or settings.get("selectors_verified") is not True:
            raise ValueError("Faltan remitentes aprobados o selectores verificados.")
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
    scan(store, data_dir, settings, preferences, start, end, discover=args.command == "discover")
    if args.command == "discover":
        print("Remitentes y asuntos visibles en el panel. Aprueba solo avisos financieros.")
    else:
        print("Importacion terminada. Revisa movimientos, meses parciales y decisiones en el panel.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Cierre mensual local: Outlook Web + Ollama.")
    parser.add_argument(
        "command", choices=["serve", "login", "discover", "scan", "summary", "scheduled", "check-config"],
    )
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--start", help="YYYY-MM-DD")
    parser.add_argument("--end", help="YYYY-MM-DD, exclusivo")
    parser.add_argument("--month", help="YYYY-MM")
    args = parser.parse_args()
    try:
        run(args)
    except KeyboardInterrupt:
        print("Detenido por el usuario.", file=sys.stderr)
        raise SystemExit(130)
    except (
        ValueError, OSError, KeyError, ZoneInfoNotFoundError, PlaywrightError,
        OutlookError, LocalModelError,
    ) as exc:
        # No message bodies or model payloads in terminal/task diagnostics.
        print(f"Detenido: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
