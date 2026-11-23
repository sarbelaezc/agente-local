"""Reading mailboxes and the scheduled close: resuming, queueing, reprocessing and stopping."""

from __future__ import annotations

import io
import json
from argparse import Namespace
from contextlib import nullcontext
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from builders import CARD_NOTICE, FakeExtractionModel
from monthly_close import main as app
from monthly_close.extraction import Mail, extract
from monthly_close.gmail_client import GmailSessionError
from monthly_close.main import due_close_month, months
from monthly_close.outlook import Candidate, OutlookError
from monthly_close.store import Store


def test_scan_resumes_failed_notice_without_rereading_completed_or_losing_decisions(
    tmp_path, preferences, monkeypatch,
):
    store = Store(tmp_path / "resume.sqlite3")
    store.configure(preferences.to_dict())
    reads = []
    fail = [True]

    class Reader:
        client = SimpleNamespace(on_wait=None)

        def __init__(self, *args):
            pass

        def checkpoint_key(self, candidate):
            return candidate.item_id

        def read(self, candidate):
            reads.append(candidate.item_id)
            if candidate.item_id == "second" and fail[0]:
                raise OutlookError("Fallo de prueba.")
            return Mail(candidate.item_id, preferences.approved_senders[0], "Tienda",
                        "2026-02-15T12:00:00Z", "Tienda $ 1.234,56")

        def walk_folders(self, limit, visit):
            for identity in ["first", "second"]:
                visit(Candidate(identity, preferences.approved_senders[0], "Tienda"))
            return []

    monkeypatch.setattr(app, "open_source", lambda *args: nullcontext(Reader()))
    monkeypatch.setattr(app, "OllamaJsonModel", lambda *args: FakeExtractionModel())
    monkeypatch.setattr(app, "require_model", lambda settings: None)
    settings = {"folder_scan_verified": True,
                "ollama_url": "http://127.0.0.1:11434", "ollama_model": "test", "max_messages": 10}
    args = (store, tmp_path, settings, preferences, date(2026, 2, 1), date(2026, 3, 1))
    with pytest.raises(OutlookError, match="Fallo de prueba"):
        app.scan(*args)
    row = store.state()["transactions"][0]
    store.decide(row["id"], "family")
    with store.connect() as db:
        assert [r["status"] for r in db.execute(
            "SELECT status FROM scan_progress ORDER BY item",
        )] == ["complete", "pending"]
    fail[0] = False
    app.scan(*args)
    assert reads == ["first", "second", "second"]
    assert next(t for t in store.state()["transactions"] if t["id"] == row["id"])["decision"] == "family"
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM scan_progress").fetchone()[0] == 0


def test_scan_queues_invalid_evidence_continues_and_retries_without_losing_decisions(
    tmp_path, preferences, monkeypatch, capsys,
):
    store = Store(tmp_path / "evidence-queue.sqlite3")
    store.configure(preferences.to_dict())
    reads = []
    invalid = [True]

    class Reader:
        active_folder = ("dinero", "banco")
        client = SimpleNamespace(on_wait=None)

        def __init__(self, *args):
            pass

        def checkpoint_key(self, candidate):
            return candidate.item_id

        def read(self, candidate):
            reads.append(candidate.item_id)
            return Mail(candidate.item_id, preferences.approved_senders[0],
                        "<img src=x> Aviso", "2026-02-15T12:00:00Z",
                        "Tienda $ 1.234,56 CUERPO_PRIVADO")

        def walk_folders(self, limit, visit):
            for identity in ["bad", "good"]:
                visit(Candidate(identity, preferences.approved_senders[0], "Aviso"))
            return []

    def extraction(mail, prefs, model, own_accounts=None, known_credits=None, card_accounts=None, other_cards=frozenset(), own_cards=frozenset()):
        chosen = FakeExtractionModel(merchant="Inventado") if mail.id == "bad" and invalid[0] else FakeExtractionModel()
        return extract(mail, prefs, chosen)

    monkeypatch.setattr(app, "open_source", lambda *args: nullcontext(Reader()))
    monkeypatch.setattr(app, "extract", extraction)
    monkeypatch.setattr(app, "OllamaJsonModel", lambda *args: FakeExtractionModel())
    monkeypatch.setattr(app, "require_model", lambda settings: None)
    settings = {"folder_scan_verified": True,
                "ollama_url": "http://127.0.0.1:11434", "ollama_model": "test", "max_messages": 10}
    args = (store, tmp_path, settings, preferences, date(2026, 2, 1), date(2026, 3, 1))
    app.scan(*args)
    state = store.state()
    assert len(state["transactions"]) == 1
    assert not store.seen("bad")
    failure = state["extraction_failures"][0]
    assert failure["folder"] == "dinero / banco"
    assert failure["field"] == "merchant"
    assert "CUERPO_PRIVADO" not in json.dumps(state)
    assert "Inventado" not in json.dumps(state)
    output = capsys.readouterr().out
    assert "Aviso" in output
    assert "1 aviso(s) sin extraer" in output
    assert "CUERPO_PRIVADO" not in output
    assert "Avisos sin extraer: 1" in store.summary("2026-02")
    with pytest.raises(ValueError, match="pendientes de extraccion"):
        store.coverage("2026-02", "reviewed", "Revisado")
    row = state["transactions"][0]
    store.decide(row["id"], "family")
    app.scan(*args)
    assert len(store.state()["extraction_failures"]) == 1
    invalid[0] = False
    app.scan(*args)
    assert store.state()["extraction_failures"] == []
    assert store.seen("bad")
    assert len(store.state()["transactions"]) == 2
    assert next(t for t in store.state()["transactions"] if t["id"] == row["id"])["decision"] == "family"
    store.coverage("2026-02", "reviewed", "Revisado")
    assert store.state()["coverage"][0]["status"] == "reviewed"


def test_extraction_failure_invalidates_review_and_empty_import_resolves_queue(
    tmp_path, preferences,
):
    store = Store(tmp_path / "failure.sqlite3")
    store.configure(preferences.to_dict())
    store.coverage("2026-02", "reviewed", "Revisado")
    store.record_extraction_failure(
        "message", preferences.approved_senders[0], "Aviso", "2026-02-15T12:00:00Z",
        "2026-02-15", "dinero / banco", "amount_text",
    )
    assert Store(store.path).state()["coverage"][0]["status"] == "partial"
    assert Store(store.path).state()["extraction_failures"][0]["message_id"] == "message"
    store.import_message("message", [])
    assert store.seen("message")
    assert store.state()["extraction_failures"] == []


@pytest.mark.parametrize("error", [
    ValueError("Cuenta no autorizada"),
    ValueError("Dos movimientos indistinguibles"),
    OutlookError("Seleccion ambigua"),
])
def test_scan_does_not_continue_after_other_failures(tmp_path, preferences, monkeypatch, error):
    store = Store(tmp_path / "fatal.sqlite3")
    store.configure(preferences.to_dict())
    reads = []

    class Reader:
        client = SimpleNamespace(on_wait=None)

        def __init__(self, *args):
            pass

        def checkpoint_key(self, candidate):
            return candidate.item_id

        def read(self, candidate):
            reads.append(candidate.item_id)
            return Mail(candidate.item_id, preferences.approved_senders[0], "Aviso",
                        "2026-02-15T12:00:00Z", "Tienda $ 1.234,56")

        def walk_folders(self, limit, visit):
            for identity in ["first", "second"]:
                visit(Candidate(identity, preferences.approved_senders[0], "Aviso"))

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(app, "open_source", lambda *args: nullcontext(Reader()))
    monkeypatch.setattr(app, "extract", fail)
    monkeypatch.setattr(app, "OllamaJsonModel", lambda *args: FakeExtractionModel())
    monkeypatch.setattr(app, "require_model", lambda settings: None)
    settings = {"folder_scan_verified": True,
                "ollama_url": "http://127.0.0.1:11434", "ollama_model": "test", "max_messages": 10}
    with pytest.raises(type(error), match=str(error)):
        app.scan(store, tmp_path, settings, preferences, date(2026, 2, 1), date(2026, 3, 1))
    assert reads == ["first"]
    assert store.state()["extraction_failures"] == []
    assert not store.seen("first")


def test_reprocess_replaces_card_payments_guessed_by_the_model(tmp_path, preferences, mail):
    store = Store(tmp_path / "legacy.sqlite3")
    store.configure(preferences.to_dict())
    guessed = [
        {**extract(mail, preferences, FakeExtractionModel(kind="card_payment"))[0], "id": name, "account": name}
        for name in ("Tarjeta A", "Cuenta B")
    ]
    store.import_message(mail.id, guessed)
    assert store.processed_without_events(mail.id)
    source = Mail(mail.id, mail.sender, "Alertas", mail.received, CARD_NOTICE)
    store.import_message(mail.id, extract(source, preferences, FakeExtractionModel()), reprocess=True)
    rows = store.state()["transactions"]
    assert [(row["merchant"], row["category"]) for row in rows] == [
        ("tarjeta de credito *8243", "Pago de tarjeta"),
    ]
    assert not store.processed_without_events(mail.id)


def test_reprocess_only_adds_to_notices_that_left_no_movement(tmp_path, preferences, mail):
    store = Store(tmp_path / "reprocess.sqlite3")
    store.configure(preferences.to_dict())
    events = extract(mail, preferences, FakeExtractionModel(kind="income"))
    store.import_message(mail.id, [])
    assert store.processed_without_events(mail.id)
    store.import_message(mail.id, events)
    assert store.state()["transactions"] == []
    store.import_message(mail.id, events, reprocess=True)
    assert len(store.state()["transactions"]) == 1
    assert not store.processed_without_events(mail.id)
    other = [{**events[0], "id": "other"}]
    store.import_message(mail.id, other, reprocess=True)
    assert len(store.state()["transactions"]) == 1


@pytest.mark.parametrize(("stamp", "zone", "last_closed", "expected"), [
    # On time: the last local day from 22:00, with leap years and daylight saving time.
    ("2026-02-28T22:00:00-05:00", "America/Bogota", "2026-01", "2026-02"),
    ("2028-02-29T23:59:00-05:00", "America/Bogota", "2028-01", "2028-02"),
    ("2026-04-30T22:00:00+02:00", "Europe/Madrid", "2026-03", "2026-04"),
    ("2026-03-31T22:00:00+02:00", "Europe/Madrid", "2026-02", "2026-03"),
    # Before the closing moment the previous month is the one due, and it is closed.
    ("2026-03-30T22:00:00+02:00", "Europe/Madrid", "2026-02", None),
    ("2026-02-28T21:59:00-05:00", "America/Bogota", "2026-01", None),
    # The computer was off at the closing hour: the next run closes the month that ended.
    ("2026-03-01T08:00:00-05:00", "America/Bogota", "2026-01", "2026-02"),
    ("2026-03-05T19:00:00-05:00", "America/Bogota", "2026-01", "2026-02"),
    # Never closed before: the most recent month that ended.
    ("2026-03-05T19:00:00-05:00", "America/Bogota", None, "2026-02"),
    # A month already closed is never closed again.
    ("2026-02-28T23:00:00-05:00", "America/Bogota", "2026-02", None),
    # UTC already in March, but still February 28 at 22:30 in Bogota.
    ("2026-03-01T03:30:00+00:00", "America/Bogota", "2026-01", "2026-02"),
])
def test_due_month_follows_the_local_close_and_catches_up_after_a_missed_run(stamp, zone, last_closed, expected):
    assert due_close_month(datetime.fromisoformat(stamp), zone, last_closed) == expected


def test_due_month_needs_an_aware_clock():
    with pytest.raises(ValueError, match="zona horaria"):
        due_close_month(datetime(2026, 2, 28, 22), "America/Bogota", None)


def test_historical_months_include_missing_months():
    assert months(date(2026, 1, 1), date(2026, 4, 1)) == ["2026-01", "2026-02", "2026-03"]


def test_scheduled_without_a_pending_close_does_not_read_runtime_or_the_mailbox(tmp_path, preferences, monkeypatch):
    store = Store(tmp_path / "close.sqlite3")
    store.configure(preferences.to_dict())
    store.record_close("2026-02")

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 3, 30, 22, tzinfo=tz)

    monkeypatch.setattr(app, "datetime", Clock)
    monkeypatch.setattr(app, "runtime", lambda path: pytest.fail("No debe leer configuracion tecnica."))
    app.run(Namespace(data_dir=tmp_path, command="scheduled"))
    assert not (tmp_path / "last-close.txt").exists()


def test_scheduled_close_persists_self_contained_summary(tmp_path, preferences, monkeypatch):
    store = Store(tmp_path / "close.sqlite3")
    store.configure(preferences.to_dict())
    scanned = []

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2028, 2, 29, 22, tzinfo=tz)

    monkeypatch.setattr(app, "datetime", Clock)
    monkeypatch.setattr(app, "runtime", lambda path: {})
    monkeypatch.setattr(app, "scan", lambda *args, **kwargs: scanned.append(args + (kwargs,)))
    monkeypatch.setattr(app, "run_complements", lambda *args: None)
    app.run(Namespace(data_dir=tmp_path, command="scheduled"))
    # The close reads only what changed since the previous run and may ask for a Gmail sign-in.
    assert scanned[0][-3:] == (
        date(2028, 2, 1), date(2028, 3, 1), {"incremental": True, "relogin_minutes": app.RELOGIN_MINUTES},
    )
    assert "Cierre 2028-02" in (tmp_path / "last-close.txt").read_text(encoding="utf-8")
    assert store.last_close() == "2028-02"
    # Running again the same night finds nothing pending.
    app.run(Namespace(data_dir=tmp_path, command="scheduled"))
    assert len(scanned) == 1


def test_a_failed_scheduled_close_is_not_recorded_and_runs_again(tmp_path, preferences, monkeypatch):
    store = Store(tmp_path / "close.sqlite3")
    store.configure(preferences.to_dict())

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2028, 3, 1, 9, tzinfo=tz)  # the morning after a missed close

    def expired(*args, **kwargs):
        raise GmailSessionError("Nadie inicio sesion en Google a tiempo; la sesion sigue vencida.")

    monkeypatch.setattr(app, "datetime", Clock)
    monkeypatch.setattr(app, "runtime", lambda path: {})
    monkeypatch.setattr(app, "scan", expired)
    with pytest.raises(GmailSessionError):
        app.run(Namespace(data_dir=tmp_path, command="scheduled"))
    assert store.last_close() is None and not (tmp_path / "last-close.txt").exists()


def test_progress_bar_follows_notice_dates_and_stays_silent_off_a_terminal():
    class Terminal(io.StringIO):
        def isatty(self):
            return True

    screen = Terminal()
    progress = app.Progress(date(2026, 1, 1), date(2026, 11, 1), stream=screen)
    progress.update(("dinero", "bancolombia"), date(2026, 11, 20), counted=False)
    assert "]   0% | dinero / bancolombia | 2026-11-01 | 0 avisos" in screen.getvalue()
    progress.update(("dinero", "bancolombia"), date(2026, 6, 2))
    assert "[############............]  50% | dinero / bancolombia | 2026-06-02 | 1 avisos" in screen.getvalue()
    progress.update(("dinero", "bancolombia"), date(2025, 12, 30), counted=False)
    assert "[########################] 100%" in screen.getvalue()
    progress.clear()
    assert screen.getvalue().endswith(" \r")

    piped = io.StringIO()
    quiet = app.Progress(date(2026, 1, 1), date(2026, 11, 1), stream=piped)
    quiet.update(("dinero",), date(2026, 6, 2))
    quiet.listing("Gmail", 1, 2)
    quiet.clear()
    assert piped.getvalue() == ""


def test_one_bar_covers_the_whole_run_and_never_goes_back():
    """Two mailboxes share the bar: Outlook by folders, Gmail half list and half notices."""
    class Terminal(io.StringIO):
        def isatty(self):
            return True

    screen = Terminal()
    progress = app.Progress(date(2026, 1, 1), date(2026, 11, 1), stream=screen, oldest_first=True, mailboxes=2)
    bar = lambda: screen.getvalue().rsplit(chr(13), 1)[-1]  # noqa: E731

    progress.mailbox(0, "santiago")
    progress.folder(("Dinero",), 2, 4)
    assert bar().startswith("[###.....................]  12% | santiago · Dinero | Carpeta 2/4")
    progress.update(("Dinero",), date(2026, 11, 1))  # the second folder read to the end
    assert bar().startswith("[######..................]  25% | santiago · Dinero | 2026-11-01 | 1 avisos")
    progress.folder(("Spam",), 3, 4)
    progress.update(("Spam",), date(2026, 1, 1))  # an older notice never moves the bar back
    assert "25%" in bar()

    progress.mailbox(1, "manuela")
    progress.listing("Gmail", 6, 12)
    assert bar().startswith("[###############.........]  62% | manuela · Gmail | Revisando lista: 6/12 mensajes")
    progress.listing("Gmail", 12, 12)
    progress.update(("Gmail",), date(2026, 11, 1))
    assert bar().startswith("[########################] 100% | manuela · Gmail | 2026-11-01")
