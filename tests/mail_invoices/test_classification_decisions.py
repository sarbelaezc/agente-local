import json
import sqlite3

import pytest

from mail_invoices.classification_decisions import ClassificationDecisionStore, decisions_path
from mail_invoices.folders import ConfirmedExample, IncomingMessage, suggest_folder


def example(folder="Salud"):
    return ConfirmedExample(
        IncomingMessage(
            "intermediario@example.invalid", "Factura hospitalaria", "Hospital",
            "Consulta general",
            ({"supplier": "Hospital", "descriptions": "Consulta", "evidence": ""},),
        ),
        folder,
    )


def test_missing_store_is_read_only(tmp_path):
    store = ClassificationDecisionStore(tmp_path / "local" / "decisions.sqlite3")
    assert store.load() == []
    assert not store.path.parent.exists()


def test_confirmed_decisions_survive_reopening_and_corrections_replace_destination(tmp_path):
    path = tmp_path / "decisions.sqlite3"
    store = ClassificationDecisionStore(path)
    assert store.save([example()], " Santi ", ["Salud", "Personal"]) == 1
    assert ClassificationDecisionStore(path).load() == [example()]
    store.save([example("Personal")], "Santi", ["Salud", "Personal"])
    assert ClassificationDecisionStore(path).load() == [example("Personal")]
    with sqlite3.connect(path) as connection:
        rows = connection.execute("SELECT approver, confirmed_at FROM decisions").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "Santi"
    assert rows[0][1].endswith("+00:00")


@pytest.mark.parametrize(
    ("examples", "approver"),
    [([example()], ""), ([], "Santi"), ([example("Nueva")], "Santi"),
     ([example(), example("")], "Santi")],
)
def test_invalid_batch_is_not_partially_saved(tmp_path, examples, approver):
    store = ClassificationDecisionStore(tmp_path / "decisions.sqlite3")
    with pytest.raises(ValueError):
        store.save(examples, approver, ["Salud"])
    assert not store.path.exists()


def test_content_retention_is_bounded(tmp_path):
    store = ClassificationDecisionStore(tmp_path / "decisions.sqlite3")
    incoming = IncomingMessage(
        "x" * 200, "s" * 500, "p" * 500, "b" * 7000,
        tuple({"supplier": "p" * 500, "descriptions": "d" * 2000, "evidence": "e" * 2000}
              for _ in range(12)),
    )
    store.save([ConfirmedExample(incoming, "Salud")], "Santi", ["Salud"])
    retained = store.load()[0].message
    assert len(retained.sender) == 120
    assert len(retained.subject) == 200
    assert len(retained.body) == 1000
    assert len(retained.invoices) == 10
    assert len(retained.invoices[0]["descriptions"]) == 500
    assert len(retained.invoices[0]["evidence"]) == 500


def test_saved_content_reused_by_model_without_sender_rule(tmp_path):
    store = ClassificationDecisionStore(tmp_path / "decisions.sqlite3")
    store.save([example()], "Santi", ["Salud"])
    incoming = IncomingMessage("otro@example.invalid", "Factura", body="Consulta hospitalaria")

    class Model:
        def complete_json(self, system, user):
            payload = json.loads(user)
            assert payload["decisiones_confirmadas"][0]["carpeta"] == "Salud"
            assert payload["decisiones_confirmadas"][0]["facturas"][0]["descriptions"] == "Consulta"
            return {"folder": "Salud", "reason": "Contenido confirmado similar"}

    confirmed = ClassificationDecisionStore(store.path).load()
    assert suggest_folder(incoming, ["Salud"], model=Model(), confirmed_examples=confirmed).folder == "Salud"
    assert suggest_folder(incoming, ["Salud"], confirmed_examples=confirmed).folder is None


def test_examples_prioritize_content_and_exclude_missing_folders():
    incoming = IncomingMessage("igual@example.invalid", "Factura", body="Consulta hospitalaria")
    examples = [
        ConfirmedExample(IncomingMessage("igual@example.invalid", "Pago energia", body="Electricidad"), "Energia"),
        example(),
        example("Eliminada"),
    ]

    class Model:
        def complete_json(self, system, user):
            payload = json.loads(user)
            assert [e["carpeta"] for e in payload["decisiones_confirmadas"]] == ["Salud"]
            return {"folder": "Inventada"}

    assert suggest_folder(
        incoming, ["Salud", "Energia"], model=Model(), confirmed_examples=examples,
    ).folder is None


def test_corrupt_store_is_reported_not_ignored(tmp_path):
    store = ClassificationDecisionStore(tmp_path / "decisions.sqlite3")
    store.path.write_text("not a database")
    with pytest.raises(sqlite3.DatabaseError):
        store.load()


def test_default_path_is_outside_workspace(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert decisions_path() == tmp_path / "MailInvoiceAgent" / "classification-decisions.sqlite3"
    monkeypatch.delenv("LOCALAPPDATA")
    with pytest.raises(ValueError, match="LOCALAPPDATA"):
        decisions_path()


def test_example_context_has_a_total_budget():
    examples = [
        ConfirmedExample(IncomingMessage(
            "sender@example.invalid", f"Consulta {index}", "Hospital", "Consulta " * 100,
            tuple({"supplier": "Hospital" * 20, "descriptions": "Consulta " * 60,
                   "evidence": "Laboratorio " * 60} for _ in range(10)),
        ), "Salud") for index in range(8)
    ]

    class Model:
        def complete_json(self, system, user):
            payload = json.loads(user)
            selected = payload["decisiones_confirmadas"]
            assert 1 <= len(selected) <= 6
            assert sum(len(json.dumps(e, ensure_ascii=False)) for e in selected) <= 6000
            return {"folder": "Salud"}

    suggest_folder(
        IncomingMessage("new@example.invalid", "Consulta", body="Hospital"),
        ["Salud"], model=Model(), confirmed_examples=examples,
    )


def test_simulation_automatically_loads_local_decisions(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from mail_invoices import classification
    from mail_invoices.settings import MailSettings

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    store = ClassificationDecisionStore(decisions_path())
    store.save([example()], "Santi", ["Salud"])
    executable = tmp_path / "synthetic.exe"
    executable.touch()
    page = SimpleNamespace(wait_for_selector=lambda *a, **kw: None)
    closed = []
    context = SimpleNamespace(close=lambda: closed.append(True))
    playwright = SimpleNamespace(
        chromium=SimpleNamespace(launch_persistent_context=lambda *a, **kw: context),
    )

    class Session:
        def __enter__(self):
            return playwright

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(classification, "sync_playwright", Session)
    monkeypatch.setattr(classification, "outlook_page", lambda context: page)
    monkeypatch.setattr(classification, "discover_folders", lambda page: [SimpleNamespace(path="Salud")])
    monkeypatch.setattr(classification, "read_history", lambda *a: [])
    monkeypatch.setattr(classification, "reopen_outlook", lambda *a: None)
    monkeypatch.setattr(classification, "scan_inbox", lambda *a: [])
    settings = MailSettings(
        executable=executable, workbook=tmp_path / "unused.xlsx", backup_dir=tmp_path,
        sheet="Hoja1", profile_dir=tmp_path, batch_limit=1,
        tesseract_executable=tmp_path / "missing.exe",
    )
    report = classification.simulate_classification(settings, 30, [], False)
    assert report["confirmed_examples"] == 1
    assert report["moved"] == 0
    assert closed == [True]
