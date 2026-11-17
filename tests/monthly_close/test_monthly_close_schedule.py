"""Scheduled close: run logs, Gmail sign-in renewal and closes, with fakes only."""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from monthly_close import gmail_client
from monthly_close import main as app
from monthly_close import run_log
from monthly_close.domain import Preferences
from monthly_close.mailboxes import load_mailboxes
from monthly_close.store import Store

CLIENT_ID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def preferences(categories) -> Preferences:
    return Preferences.parse({
        "location": "Ciudad de prueba", "timezone": "America/Bogota", "currency": "COP",
        "accounts": ["Tarjeta A"], "excluded_accounts": [],
        "transfer_label": "Familiar", "ask_savings": True, "ask_people": True,
        "approved_senders": ["avisos@example.test"], "categories": categories,
        "confidential_confirmed": True,
    })


# ---------------------------------------------------------------- run logs


def test_run_log_copies_the_output_but_not_progress_redraws(tmp_path, monkeypatch):
    screen = io.StringIO()
    monkeypatch.setattr(sys, "stdout", screen)
    with run_log.run_log(tmp_path, "scan") as path:
        print("Bandeja de entrada: 3 filas verificadas")
        sys.stdout.write("\r[####....] 50% | dinero")
        print("Importacion terminada.")
    assert sys.stdout is screen  # the console stream is restored
    text = path.read_text(encoding="utf-8")
    assert path.parent == tmp_path / "logs" and run_log.LOG_NAME.fullmatch(path.name)
    assert "cierre mensual: scan" in text and "Bandeja de entrada: 3 filas verificadas" in text
    assert "[####" not in text and "[####" in screen.getvalue()
    assert text.rstrip().endswith("fin: terminado")


def test_run_log_records_a_stop_and_lets_it_propagate(tmp_path):
    with pytest.raises(SystemExit):
        with run_log.run_log(tmp_path, "scheduled") as path:
            print("Detenido: Sesion de Google vencida.", file=sys.stderr)
            raise SystemExit(1)
    text = path.read_text(encoding="utf-8")
    assert "Detenido: Sesion de Google vencida." in text and "fin: detenido (codigo 1)" in text


def test_only_the_last_ten_run_logs_are_kept(tmp_path):
    folder = tmp_path / "logs"
    folder.mkdir()
    names = [f"202611{day:02d}-220000-scheduled.log" for day in range(1, 13)]
    for name in names:
        (folder / name).write_text("x", encoding="utf-8")
    (folder / "notas.txt").write_text("no es un registro", encoding="utf-8")
    removed = run_log.prune(folder)
    assert [path.name for path in removed] == names[:2]
    assert sorted(path.name for path in folder.iterdir()) == sorted(names[2:] + ["notas.txt"])


def test_a_log_still_open_is_skipped_by_the_cleanup(tmp_path, monkeypatch):
    folder = tmp_path / "logs"
    folder.mkdir()
    for day in range(1, 13):
        (folder / f"202611{day:02d}-220000-serve.log").write_text("x", encoding="utf-8")
    busy = folder / "20261101-220000-serve.log"
    original = Path.unlink

    def unlink(self, *args, **kwargs):
        if self == busy:
            raise PermissionError("en uso")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    assert [path.name for path in run_log.prune(folder)] == ["20261102-220000-serve.log"]
    assert busy.exists()


@pytest.mark.parametrize("name", ["../fuera.log", "logs/notas.txt", "logs/sin-fecha.log"])
def test_a_log_path_outside_the_logs_folder_is_refused(tmp_path, name):
    with pytest.raises(ValueError, match="carpeta logs"):
        run_log.checked_log_path(tmp_path, tmp_path / name)


def test_every_command_run_from_the_cli_leaves_its_log(tmp_path, preferences, monkeypatch, capsys):
    Store(tmp_path / "close.sqlite3").configure(preferences.to_dict())
    monkeypatch.setattr(sys, "argv", ["monthly-close", "summary", "--month", "2026-11", "--data-dir", str(tmp_path)])
    app.main()
    [path] = (tmp_path / "logs").glob("*-summary.log")
    assert "Cierre 2026-11" in capsys.readouterr().out
    assert "Cierre 2026-11" in path.read_text(encoding="utf-8")


def test_a_failed_command_leaves_its_reason_in_the_log(tmp_path, monkeypatch):
    log = tmp_path / "logs" / "20261130-220000-scheduled.log"
    monkeypatch.setattr(sys, "argv", [
        "monthly-close", "scan", "--data-dir", str(tmp_path), "--log-file", str(log),
    ])
    with pytest.raises(SystemExit):
        app.main()  # no step-zero answers yet
    text = log.read_text(encoding="utf-8")
    assert "Detenido: Completa primero" in text and "fin: detenido (codigo 1)" in text


# ---------------------------------------------------------------- Gmail sessions


GMAIL = {"name": "manuela", "provider": "gmail", "owner": "manuela", "verified": True}
OUTLOOK = {"name": "santiago", "provider": "graph", "owner": "santiago"}


def boxes(**gmail):
    return load_mailboxes({"graph_client_id": CLIENT_ID, "mailboxes": [OUTLOOK, {**GMAIL, **gmail}]})


def test_mailbox_address_is_optional_lowercased_and_only_for_gmail():
    assert boxes()[1].address is None
    assert boxes(address="Persona@Example.test")[1].address == "persona@example.test"
    for bad in ({"address": "no-es-correo"}, {"address": 7}):
        with pytest.raises(ValueError, match="address"):
            boxes(**bad)
    with pytest.raises(ValueError, match="address"):
        load_mailboxes({"mailboxes": [{**OUTLOOK, "address": "a@b.test"}]})


def expired_session(monkeypatch):
    def token(path):
        def fail():
            raise gmail_client.GmailSessionError("Sesion de Google vencida.")
        return fail

    monkeypatch.setattr(gmail_client, "silent_token", token)


def test_an_expired_gmail_session_stops_a_normal_run_before_reading(tmp_path, monkeypatch):
    expired_session(monkeypatch)
    monkeypatch.setattr(gmail_client, "interactive_login", lambda *a, **k: pytest.fail("No debe abrir el navegador."))
    with pytest.raises(gmail_client.GmailSessionError, match="vencida"):
        app.check_sessions(tmp_path, boxes(address="persona@example.test"))


def test_the_scheduled_close_opens_the_google_sign_in_and_waits(tmp_path, monkeypatch, capsys):
    expired_session(monkeypatch)
    calls = []
    monkeypatch.setattr(
        gmail_client, "interactive_login",
        lambda *args, **kwargs: calls.append((args, kwargs)) or "persona@example.test",
    )
    app.check_sessions(tmp_path, boxes(address="persona@example.test"), relogin_minutes=15)
    assert calls == [(
        (tmp_path / "gmail_client_secret.json", tmp_path / "gmail_token_manuela.json"),
        {"timeout_seconds": 900, "expected": "persona@example.test"},
    )]
    output = capsys.readouterr().out
    assert "se espera 15 minutos a que Manuela inicie sesion" in output and "Sesion de Google renovada" in output


def test_the_scheduled_close_needs_the_mailbox_address_to_ask_for_a_sign_in(tmp_path, monkeypatch):
    expired_session(monkeypatch)
    monkeypatch.setattr(gmail_client, "interactive_login", lambda *a, **k: pytest.fail("Sin address no se abre."))
    with pytest.raises(gmail_client.GmailSessionError, match="address"):
        app.check_sessions(tmp_path, boxes(), relogin_minutes=15)


def test_a_valid_gmail_session_and_outlook_only_need_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(gmail_client, "silent_token", lambda path: (lambda: "token"))
    monkeypatch.setattr(gmail_client, "interactive_login", lambda *a, **k: pytest.fail("No debe abrir el navegador."))
    app.check_sessions(tmp_path, boxes(address="persona@example.test"), relogin_minutes=15)
    app.check_sessions(tmp_path, load_mailboxes({"graph_client_id": CLIENT_ID}), relogin_minutes=15)


def test_a_missing_gmail_session_is_a_session_error(tmp_path):
    with pytest.raises(gmail_client.GmailSessionError, match="ausente"):
        gmail_client.silent_token(tmp_path / "gmail_token_manuela.json")


class FakeCredentials:
    token = "nuevo-token"
    scopes = list(gmail_client.SCOPES)
    granted_scopes = list(gmail_client.SCOPES)

    def to_json(self) -> str:
        return json.dumps({"token": "nuevo-token"})


def fake_google(monkeypatch, outcome, address="persona@example.test"):
    class Timeout(Exception):
        pass

    class Flow:
        @classmethod
        def from_client_secrets_file(cls, path, scopes):
            return cls()

        def run_local_server(self, **kwargs):
            Flow.kwargs = kwargs
            if outcome == "timeout":
                raise Timeout("sin respuesta")
            return FakeCredentials()

    class Client:
        def __init__(self, token):
            pass

        def get_json(self, url):
            return {"emailAddress": address}

        def close(self):
            pass

    monkeypatch.setattr(gmail_client, "_google", lambda: (None, None, (Exception, Exception), (Flow, Timeout)))
    monkeypatch.setattr(gmail_client, "GmailClient", Client)
    return Flow


def test_sign_in_waits_only_the_given_time(tmp_path, monkeypatch):
    (tmp_path / "secret.json").write_text("{}", encoding="utf-8")
    flow = fake_google(monkeypatch, "timeout")
    with pytest.raises(gmail_client.GmailSessionError, match="a tiempo"):
        gmail_client.interactive_login(tmp_path / "secret.json", tmp_path / "token.json", timeout_seconds=900)
    assert flow.kwargs["timeout_seconds"] == 900 and not (tmp_path / "token.json").exists()


def test_a_sign_in_with_another_google_account_is_never_saved(tmp_path, monkeypatch):
    (tmp_path / "secret.json").write_text("{}", encoding="utf-8")
    fake_google(monkeypatch, "ok", address="otra@example.test")
    with pytest.raises(gmail_client.GmailSessionError, match="otra cuenta"):
        gmail_client.interactive_login(tmp_path / "secret.json", tmp_path / "token.json", expected="persona@example.test")
    assert not (tmp_path / "token.json").exists()
    fake_google(monkeypatch, "ok", address="Persona@Example.test")
    account = gmail_client.interactive_login(
        tmp_path / "secret.json", tmp_path / "token.json", expected="persona@example.test",
    )
    assert account == "Persona@Example.test" and (tmp_path / "token.json").is_file()


# ---------------------------------------------------------------- closes


def test_closes_are_recorded_audited_and_validated(tmp_path):
    store = Store(tmp_path / "close.sqlite3")
    assert store.last_close() is None
    store.record_close("2026-10")
    store.record_close("2026-11")
    store.record_close("2026-10")  # repeating an older close never moves the last one back
    assert store.last_close() == "2026-11"
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM audit WHERE action='scheduled-close'").fetchone()[0] == 3
    with pytest.raises(ValueError, match="Mes"):
        store.record_close("2026-13")


# ---------------------------------------------------------------- launcher


def launcher(tmp_path, preflight_exit: int):
    powershell = shutil.which("powershell.exe")
    if not powershell:
        pytest.skip("Requires Windows PowerShell.")
    root = tmp_path / "project"
    (root / "scripts").mkdir(parents=True)
    original = Path(__file__).parents[2] / "scripts" / "agente-local.ps1"
    (root / "scripts" / original.name).write_text(
        original.read_text().replace(".venv\\Scripts\\python.exe", ".venv\\Scripts\\python.ps1"),
        encoding="utf-8",
    )
    fake = root / ".venv" / "Scripts" / "python.ps1"
    fake.parent.mkdir(parents=True)
    fake.write_text(
        f"if ($args[0] -eq '-c') {{ exit {preflight_exit} }}\n"
        "$args | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $env:LOCALAPPDATA 'args.json')\n"
        "exit 0\n", encoding="utf-8",
    )
    return powershell, root, original.name


def test_launcher_logs_a_scheduled_run_with_nothing_due(tmp_path):
    powershell, root, name = launcher(tmp_path, preflight_exit=3)
    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "RemoteSigned", "-File", str(root / "scripts" / name), "scheduled"],
        env=os.environ | {"LOCALAPPDATA": str(root)}, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    [log] = (root / ".local" / "MonthlyCloseAgent" / "logs").glob("*-scheduled.log")
    assert "[lanzador] Sin cierre pendiente" in log.read_text(encoding="utf-8-sig")
    assert not (root / "args.json").exists()  # nothing due: Python's close never ran


def test_launcher_passes_this_runs_log_to_python(tmp_path):
    powershell, root, name = launcher(tmp_path, preflight_exit=0)
    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "RemoteSigned", "-File", str(root / "scripts" / name), "serve"],
        env=os.environ | {"LOCALAPPDATA": str(root)}, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    args = json.loads((root / "args.json").read_text(encoding="utf-8-sig"))
    log = Path(args[args.index("--log-file") + 1])
    assert log.parent == root / ".local" / "MonthlyCloseAgent" / "logs"
    assert run_log.LOG_NAME.fullmatch(log.name) and log.name.endswith("-serve.log")


# ---------------------------------------------------------------- coverage of the remaining new lines


def test_the_log_tee_behaves_like_the_console_stream(tmp_path, monkeypatch):
    class Console(io.StringIO):
        encoding = "cp1252"

        def isatty(self):
            return True

    screen = Console()
    monkeypatch.setattr(sys, "stdout", screen)
    with run_log.run_log(tmp_path, "scan"):
        assert sys.stdout.isatty() and sys.stdout.encoding == "cp1252"
        sys.stdout.flush()


def test_a_run_stopped_with_ctrl_c_is_logged(tmp_path):
    with pytest.raises(KeyboardInterrupt):
        with run_log.run_log(tmp_path, "import") as path:
            raise KeyboardInterrupt
    assert "fin: detenido por el usuario" in path.read_text(encoding="utf-8")


def test_an_invalid_log_file_stops_before_running(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", [
        "monthly-close", "summary", "--data-dir", str(tmp_path), "--log-file", str(tmp_path / "fuera.log"),
    ])
    with pytest.raises(SystemExit):
        app.main()
    assert "Detenido: El registro debe ser" in capsys.readouterr().err


@pytest.mark.parametrize("box,message", [
    ({"name": "Manuela"}, "name"),
    ({"origin": "otra origen"}, "origin"),
])
def test_mailbox_names_and_origins_are_validated(box, message):
    with pytest.raises(ValueError, match=message):
        boxes(**box)


class StoredCredentials:
    """Saved Google session as google-auth loads it, with a scripted refresh."""

    outcome = "ok"

    def __init__(self, valid=False, refresh_token="r"):
        self.valid, self.refresh_token, self.token = valid, refresh_token, "viejo"

    def refresh(self, request):
        if self.outcome == "revoked":
            raise RevokedError("invalid_grant")
        if self.outcome == "offline":
            raise OfflineError("sin red")
        self.token = "renovado"

    def to_json(self):
        return json.dumps({"token": self.token})


class RevokedError(Exception):
    pass


class OfflineError(Exception):
    pass


def saved_session(tmp_path, monkeypatch, credentials=None, unreadable=False):
    class Loader:
        @staticmethod
        def from_authorized_user_file(path, scopes):
            if unreadable:
                raise ValueError("json invalido")
            return credentials

    path = tmp_path / "gmail_token_manuela.json"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        gmail_client, "_google", lambda: (Loader, lambda: "request", (RevokedError, OfflineError), (None, None)),
    )
    return path


def test_a_valid_saved_session_is_used_without_refreshing(tmp_path, monkeypatch):
    path = saved_session(tmp_path, monkeypatch, StoredCredentials(valid=True))
    assert gmail_client.silent_token(path)() == "viejo"


def test_an_expired_access_token_is_renewed_and_saved(tmp_path, monkeypatch):
    path = saved_session(tmp_path, monkeypatch, StoredCredentials())
    assert gmail_client.silent_token(path)() == "renovado"
    assert json.loads(path.read_text(encoding="utf-8")) == {"token": "renovado"}


@pytest.mark.parametrize("outcome,refresh_token,error,message", [
    ("revoked", "r", gmail_client.GmailSessionError, "vencida o revocada"),
    ("ok", None, gmail_client.GmailSessionError, "vencida"),
    ("offline", "r", gmail_client.GmailError, "contactar con Google"),
])
def test_session_problems_ask_for_a_sign_in_but_network_problems_do_not(
    tmp_path, monkeypatch, outcome, refresh_token, error, message,
):
    credentials = StoredCredentials(refresh_token=refresh_token)
    credentials.outcome = outcome
    path = saved_session(tmp_path, monkeypatch, credentials)
    with pytest.raises(error, match=message) as raised:
        gmail_client.silent_token(path)()
    # Only a session problem may open Google's sign-in from the scheduled close.
    assert isinstance(raised.value, gmail_client.GmailSessionError) is (outcome != "offline")


def test_an_unreadable_saved_session_needs_a_new_sign_in(tmp_path, monkeypatch):
    path = saved_session(tmp_path, monkeypatch, unreadable=True)
    with pytest.raises(gmail_client.GmailSessionError, match="ilegible"):
        gmail_client.silent_token(path)


def test_sign_in_needs_the_client_file_and_an_identified_account(tmp_path, monkeypatch):
    fake_google(monkeypatch, "ok")
    with pytest.raises(gmail_client.GmailError, match="cliente OAuth"):
        gmail_client.interactive_login(tmp_path / "falta.json", tmp_path / "token.json")
    (tmp_path / "secret.json").write_text("{}", encoding="utf-8")
    fake_google(monkeypatch, "ok", address=None)
    with pytest.raises(gmail_client.GmailError, match="no devolvio la cuenta"):
        gmail_client.interactive_login(tmp_path / "secret.json", tmp_path / "token.json")
    assert not (tmp_path / "token.json").exists()
