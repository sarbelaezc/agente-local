"""Destinations of Santiago's payments, read from fictitious bank sentences."""
from __future__ import annotations

from contextlib import contextmanager

import pytest

from monthly_close import main as app
from monthly_close.destinations import assign_destinations, notice_destinations
from monthly_close.domain import Preferences
from monthly_close.extraction import Mail, extract
from monthly_close.outlook import Candidate
from monthly_close.store import Store

RECEIVED = "2026-03-01T15:00:00Z"


@pytest.fixture
def preferences(categories) -> Preferences:
    return Preferences.parse({
        "location": "Ciudad de prueba", "timezone": "America/Bogota", "currency": "COP",
        "accounts": ["Tarjeta A"], "excluded_accounts": [],
        "transfer_label": "Familiar", "ask_savings": True, "ask_people": True,
        "approved_senders": ["avisos@example.test"], "categories": categories,
        "confidential_confirmed": True,
    })


@pytest.mark.parametrize(("text", "expected"), [
    ("Bancolombia: Transferiste $120,000\ndesde tu cuenta *0001 a la cuenta * 12345670007 el 04/08/2026",
     [(12000000, "tr:0007")]),
    ("Transferiste $38,700.00 por QR desde tu cuenta 0001 a la cuenta 7433, el 2026/08/04 10:00",
     [(3870000, "tr:7433")]),
    ("Bancolombia: Transferiste $45,000 a la llave @Tienda.Ficticia desde tu cuenta *0001 a PERSONA FICTICIA el 01/03/2026",
     [(4500000, "bb:@tienda.ficticia")]),
    ("Transferiste $45,000 a la llave 3000000042 desde tu cuenta *0001 a PERSONA FICTICIA el 01/03/2026",
     [(4500000, "bb:0042")]),
    ("Bancolombia: Pagaste $12,500 por codigo QR desde tu cuenta *0001 a la llave 3000000099 el 01/03/2026",
     [(1250000, "qr:0099")]),
    ("Retiraste $200,000 de tu cuenta de inversion *ab12 fondo de inversion Fiducuenta el 01/03/2026",
     [(20000000, "fd:ab12")]),
    ("Compraste $ 1.234,56 en Tienda con Tarjeta A.", []),
    ("Recibiste una transferencia de PERSONA por $50,000 en tu cuenta *0001", []),
])
def test_destination_is_read_only_from_fixed_sentences(text, expected):
    assert notice_destinations(text) == expected


def test_ambiguous_destination_is_left_empty():
    source = (
        "Transferiste $10,000 desde tu cuenta *0001 a la cuenta *0007 el 01/03/2026. "
        "Transferiste $10,000 desde tu cuenta *0001 a la cuenta *0008 el 01/03/2026."
    )
    events = assign_destinations([{"amount_minor": 1000000}, {"amount_minor": 5}], source)
    assert [event["group_key"] for event in events] == [None, None]


def test_new_notices_store_their_destination(preferences, tmp_path):
    class NoModel:
        def complete_json(self, system, user):
            raise AssertionError("Esta frase fija no se envia a Ollama.")

    mail = Mail("m1", "avisos@example.test", "Alertas", RECEIVED,
                "Transferiste $120,000 desde tu cuenta *0001 a la cuenta *0007 el 04/08/2026.")
    events = extract(mail, preferences, NoModel())
    assert events[0]["group_key"] == "tr:0007"
    store = Store(tmp_path / "close.sqlite3")
    store.configure(preferences.to_dict())
    store.set_owner_account("manuela", "0007")
    store.import_message("m1", events)
    row = store.state()["transactions"][0]
    assert (row["group"], row["between_owners"]) == ("tr:0007", 1)


def santiago_row(event_id: str, amount: int, kind: str = "transfer") -> dict:
    return {
        "id": event_id, "message_id": f"msg-{event_id}", "day": "2026-03-01", "merchant": "Bancolombia",
        "account": "Desconocida", "amount_minor": amount, "currency": "COP", "category": "Otros gastos",
        "kind": kind, "decision": None, "reason": "Revisar.",
    }


def test_set_destinations_only_fills_missing_keys_and_audits(preferences, tmp_path):
    store = Store(tmp_path / "close.sqlite3")
    store.configure(preferences.to_dict())
    store.import_message("msg-a", [santiago_row("a", 100)])
    store.import_message("msg-b", [{**santiago_row("b", 200), "group_key": "tr:0001"}])
    store.decide("a", "family")
    assert store.set_destinations({"a": "tr:0008", "b": "tr:0009"}) == 1
    rows = {row["id"]: row for row in store.state()["transactions"]}
    assert rows["a"]["group_key"] == "tr:0008" and rows["a"]["decision"] == "family"
    assert rows["b"]["group_key"] == "tr:0001"
    with store.connect() as db:
        assert db.execute("SELECT target FROM audit WHERE action='destination-backfill'").fetchall()[0][0] == "a"
    with pytest.raises(ValueError):
        store.set_destinations({"a": "x:<script>"})


def test_backfill_reads_notices_again_without_changing_decisions(preferences, tmp_path, monkeypatch):
    store = Store(tmp_path / "close.sqlite3")
    store.configure(preferences.to_dict())
    store.import_message("msg-a", [santiago_row("a", 4500000)])
    store.import_message("msg-b", [santiago_row("b", 999, kind="unknown")])
    store.import_message("msg-c", [{**santiago_row("c", 100, kind="purchase")}])
    store.decide("a", "personal")
    bodies = {
        "msg-a": "Transferiste $45,000 a la llave @tienda desde tu cuenta *0001 a PERSONA el 01/03/2026",
        "msg-b": "Aviso sin destino literal por $9,99.",
        "other": "Compraste $ 1.234,56 en Tienda.",
    }
    read = []

    class Reader:
        _received = {key: RECEIVED for key in bodies}

        def walk_folders(self, limit, visit):
            for key in bodies:
                visit(Candidate(key, "avisos@example.test", "Alertas"))
            return []

        def read(self, candidate):
            read.append(candidate.item_id)
            return Mail(candidate.item_id, candidate.sender, candidate.subject, RECEIVED, bodies[candidate.item_id])

    @contextmanager
    def fake_source(*args):
        yield Reader()

    monkeypatch.setattr(app, "open_source", fake_source)
    result = app.backfill_destinations(store, tmp_path, {"max_messages": 100}, preferences)
    assert result == {"notices": 2, "read": 2, "updated": 1, "without_destination": 1}
    assert "other" not in read  # stops once every needed notice was read
    rows = {row["id"]: row for row in store.state()["transactions"]}
    assert (rows["a"]["group_key"], rows["a"]["decision"], rows["a"]["amount_minor"]) == (
        "bb:@tienda", "personal", 4500000,
    )
    assert rows["b"]["group_key"] is None and rows["c"]["group_key"] is None
    again = app.backfill_destinations(store, tmp_path, {"max_messages": 100}, preferences)
    assert again["updated"] == 0
