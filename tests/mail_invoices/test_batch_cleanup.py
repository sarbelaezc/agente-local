from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone

import pytest

from mail_invoices import main
from mail_invoices.batch_cleanup import cleanup_batches, plan_cleanup
from mail_invoices.batch_state import batch_lock, batch_scope, batch_state_path
from mail_invoices.settings import MailSettings

NOW = datetime(2026, 11, 18, 15, 0, tzinfo=timezone.utc)
EXCLUDED = "recibos@extranjero.example"


@pytest.fixture
def settings(tmp_path):
    return MailSettings(
        "11111111-2222-3333-4444-555555555555", tmp_path / "book.xlsx", tmp_path, "Hoja1", 20,
        excluded_senders=(EXCLUDED,), approver="Persona Sintetica",
    )


def store_rows(rows):
    """rows: (scope, id, state, sender, days ago); the payload holds only what cleanup reads."""
    path = batch_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS messages (scope TEXT NOT NULL, conversation TEXT NOT NULL, "
            "payload TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(scope, conversation))"
        )
        connection.executemany("INSERT INTO messages VALUES (?, ?, ?, ?)", [
            (scope, key, json.dumps({"state": state, "sender": sender, "subject": "Factura SYNTH"}),
             (NOW - timedelta(days=age)).isoformat())
            for scope, key, state, sender, age in rows
        ])
    return path


def remaining(path):
    with closing(sqlite3.connect(path)) as connection:
        return sorted(row[0] for row in connection.execute("SELECT conversation FROM messages"))


@pytest.fixture
def stored(settings):
    current = batch_scope(settings)
    return store_rows([
        (current, "moved-old", "moved", "a@proveedor.example", 40),
        (current, "moved-recent", "moved", "a@proveedor.example", 5),
        (current, "approved-old", "approved", "a@proveedor.example", 400),
        (current, "registered-old", "registered", "a@proveedor.example", 400),
        (current, "moving-old", "moving", "a@proveedor.example", 400),
        (current, "blocked-old", "blocked", "a@proveedor.example", 400),
        (current, "pending", "pending", "a@proveedor.example", 400),
        (current, "excluded-pending", "pending", EXCLUDED, 1),
        (current, "excluded-approved", "approved", EXCLUDED, 400),
        ("opera-scope", "opera-moved", "moved", "a@proveedor.example", 1),
        ("opera-scope", "opera-registered", "registered", "a@proveedor.example", 1),
        ("opera-scope", "opera-pending", "pending", "a@proveedor.example", 1),
    ])


KEPT = sorted([
    "moved-recent", "approved-old", "registered-old", "moving-old", "blocked-old", "pending", "excluded-approved",
])


def test_the_plan_never_touches_work_in_progress_of_the_current_configuration(settings, stored):
    plan = plan_cleanup(stored, batch_scope(settings), 30, settings.excluded_senders, NOW)
    assert [key for _, key in plan.archived] == ["moved-old"]
    assert [key for _, key in plan.excluded_pending] == ["excluded-pending"]
    assert sorted(key for _, key in plan.other_configuration) == ["opera-moved", "opera-pending", "opera-registered"]
    assert dict(plan.other_states) == {"moved": 1, "registered": 1, "pending": 1}
    assert plan.kept == len(KEPT)


def test_a_preview_changes_nothing(settings, stored, capsys):
    before = stored.read_bytes()
    assert cleanup_batches(settings, 30, apply=False, now=NOW) == 0
    out = capsys.readouterr().out
    assert "vista previa" in out and "repite con -Apply" in out
    assert "Archivados hace mas de 30 dias: 1" in out
    assert "(moved 1, pending 1, registered 1)" in out
    assert stored.read_bytes() == before
    assert not list(stored.parent.glob("*.backup-*"))


def test_apply_backs_up_first_and_deletes_exactly_the_plan(settings, stored, capsys):
    assert cleanup_batches(settings, 30, apply=True, now=NOW) == 0
    assert remaining(stored) == KEPT
    [backup] = stored.parent.glob("invoice-batches.sqlite3.backup-*")
    assert len(remaining(backup)) == 12  # the copy holds everything that was deleted
    out = capsys.readouterr().out
    assert "Borrados 5 registros; aprobado por Persona Sintetica" in out
    # The output has counts only: no sender or subject of any message.
    assert "proveedor.example" not in out and "SYNTH" not in out and EXCLUDED not in out


def test_zero_days_forgets_every_archived_message(settings, stored):
    cleanup_batches(settings, 0, apply=True, now=NOW)
    assert "moved-recent" not in remaining(stored)
    assert "approved-old" in remaining(stored)


@pytest.mark.parametrize("days", [-1, 3651])
def test_retention_outside_the_range_is_refused(settings, stored, days):
    with pytest.raises(ValueError, match="retencion"):
        cleanup_batches(settings, days, apply=True, now=NOW)
    assert len(remaining(stored)) == 12


def test_cleanup_waits_for_no_batch_running(settings, stored):
    with batch_lock(stored.with_suffix(".lock")):
        with pytest.raises(ValueError, match="Ya hay un lote"):
            cleanup_batches(settings, 30, apply=True, now=NOW)
    assert len(remaining(stored)) == 12


def test_without_a_database_there_is_nothing_to_delete(settings, capsys):
    assert cleanup_batches(settings, 30, apply=True, now=NOW) == 0
    assert "Nada que borrar." in capsys.readouterr().out
    assert not batch_state_path().exists()


def test_cli_routes_cleanup_and_logs_its_counts(settings, monkeypatch):
    monkeypatch.setattr(main, "load_settings", lambda path: settings)
    calls = []
    import mail_invoices.batch_cleanup as cleanup

    monkeypatch.setattr(cleanup, "cleanup_batches", lambda *args: calls.append(args) or print("- Se conservan: 3") or 0)
    monkeypatch.setattr("sys.argv", ["mail-invoices", "--cleanup-batches", "--older-than-days", "60", "--apply"])
    assert main.main() == 0
    assert calls == [(settings, 60, True)]
    [log] = (main.invoice_data_dir() / "logs").glob("*-cleanup.log")
    assert "- Se conservan: 3" in log.read_text(encoding="utf-8")


def test_cli_defaults_to_a_thirty_day_preview(settings, monkeypatch):
    monkeypatch.setattr(main, "load_settings", lambda path: settings)
    calls = []
    import mail_invoices.batch_cleanup as cleanup

    monkeypatch.setattr(cleanup, "cleanup_batches", lambda *args: calls.append(args) or 0)
    monkeypatch.setattr("sys.argv", ["mail-invoices", "--cleanup-batches"])
    assert main.main() == 0
    assert calls == [(settings, 30, False)]


@pytest.mark.parametrize("arguments", [
    ["--process-invoice-batch", "--apply"],
    ["--scan-inbox", "--older-than-days", "10"],
    ["--cleanup-batches", "--older-than-days", "-1"],
])
def test_cli_rejects_cleanup_options_out_of_place(monkeypatch, arguments):
    monkeypatch.setattr("sys.argv", ["mail-invoices", *arguments])
    with pytest.raises(SystemExit) as error:
        main.main()
    assert error.value.code == 2
