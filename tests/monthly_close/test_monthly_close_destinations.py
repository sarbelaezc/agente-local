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
from builders import SENDER, FakeExtractionModel

RECEIVED = "2026-03-01T15:00:00Z"


@pytest.fixture
def preferences(make_preferences) -> Preferences:
    return make_preferences()  # no excluded accounts


@pytest.mark.parametrize(("text", "expected"), [
    ("Bancolombia: Transferiste $120,000\ndesde tu cuenta *0001 a la cuenta * 12345670007 el 04/08/2026",
     [(12000000, "tr:12345670007")]),
    ("Transferiste $38,700.00 por QR desde tu cuenta 0001 a la cuenta 7433, el 2026/08/04 10:00",
     [(3870000, "tr:7433")]),
    ("Bancolombia: Transferiste $45,000 a la llave @Tienda.Ficticia desde tu cuenta *0001 a PERSONA FICTICIA el 01/03/2026",
     [(4500000, "bb:@tienda.ficticia")]),
    ("Transferiste $45,000 a la llave 3000000042 desde tu cuenta *0001 a PERSONA FICTICIA el 01/03/2026",
     [(4500000, "bb:3000000042")]),
    ("Bancolombia: Pagaste $12,500 por codigo QR desde tu cuenta *0001 a la llave 3000000099 el 01/03/2026",
     [(1250000, "qr:3000000099")]),
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


class NoModel:
    def complete_json(self, system, user):
        raise AssertionError("Esta frase fija no se envia a Ollama.")


def notice(body: str) -> Mail:
    return Mail("m-fixed", "avisos@example.test", "Alertas", RECEIVED, body)


def test_backfill_turns_short_keys_into_whole_keys_and_moves_their_rule(preferences, tmp_path, monkeypatch):
    store = Store(tmp_path / "close.sqlite3")
    store.configure(preferences.to_dict())
    store.import_message("msg-q", [{**santiago_row("q", 1250000), "message_id": "msg-q", "group_key": "qr:0099"}])
    store.import_message("msg-o", [{**santiago_row("o", 4500000), "message_id": "msg-o", "group_key": "bb:1111"}])
    store.import_message("msg-t", [{**santiago_row("t", 12000000), "message_id": "msg-t", "group_key": "tr:0007"}])
    store.decide_group("santiago", "qr:0099", "personal", remember=True, category="Compras")
    bodies = {
        "msg-q": "Pagaste $12,500 por codigo QR desde tu cuenta *0001 a la llave 3000000099 el 01/03/2026",
        # The whole key must end in the stored digits: another key is never taken for it.
        "msg-o": "Transferiste $45,000 a la llave 3000002222 desde tu cuenta *0001 a PERSONA el 01/03/2026",
        "msg-t": "Transferiste $120,000 desde tu cuenta *0001 a la cuenta * 12345670007 el 01/03/2026",
    }

    class Reader:
        _received = {key: RECEIVED for key in bodies}

        def walk_folders(self, limit, visit):
            for key in bodies:
                visit(Candidate(key, "avisos@example.test", "Alertas"))
            return []

        def read(self, candidate):
            return Mail(candidate.item_id, candidate.sender, candidate.subject, RECEIVED, bodies[candidate.item_id])

    @contextmanager
    def fake_source(*args):
        yield Reader()

    monkeypatch.setattr(app, "open_source", fake_source)
    result = app.backfill_destinations(store, tmp_path, {"max_messages": 100}, preferences)
    assert (result["notices"], result["updated"]) == (3, 2)
    rows = {row["id"]: row for row in store.state()["transactions"]}
    assert (rows["q"]["group_key"], rows["q"]["decision"], rows["q"]["category"]) == ("qr:3000000099", "personal", "Compras")
    assert rows["o"]["group_key"] == "bb:1111"
    assert rows["t"]["group_key"] == "tr:12345670007"  # accounts are kept whole too
    with store.connect() as db:
        rules = [tuple(row) for row in db.execute("SELECT group_key, decision, category FROM group_rules")]
        audit = db.execute("SELECT value FROM audit WHERE action='destination-backfill' AND target='q'").fetchone()[0]
    assert rules == [("qr:3000000099", "personal", "Compras")]
    assert audit == "qr:0099 -> qr:3000000099"
    # A later payment to the whole key follows the moved rule.
    store.import_message("msg-n", [{**santiago_row("n", 700), "message_id": "msg-n", "group_key": "qr:3000000099"}])
    later = {row["id"]: row for row in store.state()["transactions"]}["n"]
    assert (later["decision"], later["category"]) == ("personal", "Compras")


def test_card_abono_by_someone_else_is_a_card_payment_never_income(preferences):
    events = extract(notice(
        "Bancolombia: PERSONA FICTICIA hizo un abono por $482,000 a tu tarjeta de credito "
        "terminada en **1234, el 11/04/2026 11:23."
    ), preferences, NoModel())
    assert [(e["kind"], e["merchant"], e["amount_minor"], e["day"], e["time"], e["decision"]) for e in events] == [
        ("card_payment", "Abono de PERSONA FICTICIA a la tarjeta *1234", 48200000, "2026-04-11", "11:23", None),
    ]


def test_transfer_to_a_key_keeps_the_recipient_and_is_never_income(preferences):
    events = extract(notice(
        "Bancolombia: USUARIO, transferiste $69,244.00 a la llave @ficticia547 desde tu cuenta *0001 "
        "a PERSONA DE PRUEBA el 06/05/26 a las 11:19."
    ), preferences, NoModel())
    event = events[0]
    assert (event["kind"], event["merchant"], event["day"], event["time"]) == (
        "transfer", "PERSONA DE PRUEBA", "2026-05-06", "11:19",
    )
    assert (event["group_key"], event["note"]) == ("bb:@ficticia547", "Bre-B a la llave @ficticia547")


def test_received_transfer_is_income_with_its_sender(preferences):
    events = extract(notice(
        "Bancolombia: USUARIO, recibiste una transferencia de PERSONA FICTICIA por $17,000.00 en tu "
        "cuenta *0001 conectada a la llave 3000000000 el 02/11/26 a las 12:26."
    ), preferences, NoModel())
    assert [(e["kind"], e["merchant"], e["category"], e["amount_minor"]) for e in events] == [
        ("income", "PERSONA FICTICIA", "Transferencia recibida", 1700000),
    ]


def test_fixed_sentences_wrapped_across_lines_are_still_read_without_the_model(preferences):
    """The real case: the mail wraps the sentence ("...hizo\\nun abono...") and Ollama got it wrong."""
    abono = extract(notice(
        "Todo salio bien con tus movimientos Bancolombia: PERSONA FICTICIA hizo\r\nun abono por $482,000 a tu "
        "tarjeta de credito\nterminada en **1234, el 11/04/2026"
    ), preferences, NoModel())
    assert [(e["merchant"], e["amount_minor"], e["day"]) for e in abono] == [
        ("Abono de PERSONA FICTICIA a la tarjeta *1234", 48200000, "2026-04-11"),
    ]
    key = extract(notice(
        "Bancolombia: USUARIO, transferiste $69,244.00 a la\nllave @ficticia547 desde tu\ncuenta *0001 "
        "a PERSONA DE PRUEBA el 06/05/26"
    ), preferences, NoModel())
    assert [(e["merchant"], e["day"]) for e in key] == [("PERSONA DE PRUEBA", "2026-05-06")]
    received = extract(notice(
        "Bancolombia: USUARIO, recibiste una\ntransferencia de PERSONA FICTICIA por $17,000.00 en tu\n"
        "cuenta *0001 el 02/11/26"
    ), preferences, NoModel())
    assert [(e["kind"], e["merchant"]) for e in received] == [("income", "PERSONA FICTICIA")]


def test_transfer_to_an_own_registered_account_is_an_own_transfer(preferences):
    mail = notice("Transferiste $800,000.00 desde tu cuenta 0001 a la cuenta *3000005586 el 09/07/2026 a las 16:50.")
    assert extract(mail, preferences, NoModel())[0]["kind"] == "transfer"
    event = extract(mail, preferences, NoModel(), ["5586"])[0]
    assert (event["kind"], event["group_key"]) == ("own_transfer", "tr:3000005586")


def test_qr_payment_to_a_key_that_is_not_own_is_never_an_own_transfer(preferences):
    """The model may read the payer's name and call a QR payment an own transfer."""
    mail = Mail(
        "m-qr", SENDER, "Alertas", RECEIVED,
        "USUARIO, pagaste $ 1.234,56 por codigo QR desde tu cuenta *0001 a la llave 3000004817",
    )
    model = FakeExtractionModel(merchant="USUARIO", kind="own_transfer", category="Otros gastos")
    event = extract(mail, preferences, model, ["0001"])[0]
    assert (event["kind"], event["group_key"]) == ("transfer", "qr:3000004817")
    own_key = extract(mail, preferences, model, ["4817"])[0]
    assert own_key["kind"] == "own_transfer"


def test_stored_qr_own_transfers_become_transfers_keeping_the_decision(preferences, tmp_path):
    path = tmp_path / "close.sqlite3"
    store = Store(path)
    store.configure(preferences.to_dict())
    store.set_owner_account("santiago", "0099")
    store.import_message("msg-q", [
        {**santiago_row("q", 100, kind="own_transfer"), "group_key": "qr:4817"},
        {**santiago_row("f", 200, kind="own_transfer"), "group_key": "qr:9966", "decision": "family"},
        {**santiago_row("o", 300, kind="own_transfer"), "group_key": "qr:3000000099"},
        {**santiago_row("t", 400, kind="own_transfer"), "group_key": "tr:5555"},
    ])
    rows = {row["id"]: row for row in Store(path).state()["transactions"]}
    assert {key: (row["kind"], row["decision"]) for key, row in rows.items()} == {
        "q": ("transfer", None), "f": ("transfer", "family"),
        "o": ("own_transfer", None), "t": ("own_transfer", None),
    }
    Store(path).decide("q", "personal", "Otros gastos")
    assert {row["id"]: row["decision"] for row in Store(path).state()["transactions"]}["q"] == "personal"


def test_a_lone_movement_gets_the_hour_its_notice_arrived(preferences):
    mail = Mail("m-hour", SENDER, "Alertas", RECEIVED, "Compraste $ 1.234,56 en Tienda con Tarjeta A.")
    event = extract(mail, preferences, FakeExtractionModel())[0]
    assert (event["day"], event["time"]) == ("2026-03-01", "10:00")  # 15:00 UTC in Bogota
    stated = extract(notice(
        "Bancolombia: USUARIO, transferiste $69,244.00 a la llave @ficticia547 desde tu cuenta *0001 "
        "a PERSONA DE PRUEBA el 01/03/26 a las 09:41."
    ), preferences, NoModel())[0]
    assert stated["time"] == "09:41"  # the hour the notice states wins
    earlier = extract(notice(
        "Bancolombia: USUARIO, transferiste $69,244.00 a la llave @ficticia547 desde tu cuenta *0001 "
        "a PERSONA DE PRUEBA el 27/02/26"
    ), preferences, NoModel())[0]
    assert earlier["time"] is None  # another day: the arrival hour says nothing about it


def test_merge_category_moves_movements_and_rules_and_removes_it(preferences, tmp_path):
    raw = preferences.to_dict()
    raw["categories"] = raw["categories"] + ["Predial", "Vivienda"]
    store = Store(tmp_path / "close.sqlite3")
    store.configure(raw)
    store.import_message("p1", [{**santiago_row("p1", 1000, kind="purchase"), "category": "Predial", "decision": "family"}])
    store.add_rule("IMPUESTO PREDIAL", "Predial", "family")
    assert store.merge_category("Predial", "Vivienda") == {"movements": 1, "rules": 1}
    row = store.state()["transactions"][0]
    assert (row["category"], row["decision"], row["amount_minor"]) == ("Vivienda", "family", 1000)
    assert "Predial" not in store.preferences().categories
    assert store.rules()[0]["category"] == "Vivienda"
    with pytest.raises(ValueError):
        store.merge_category("Predial", "Vivienda")



def test_backfill_card_accounts_previews_then_fixes_only_the_card_named_by_digits(make_store, tmp_path, monkeypatch):
    accounts = ["Cuenta de ahorros", "Tarjeta de crédito Master", "Tarjeta de crédito Visa"]
    store = make_store(accounts=accounts)
    preferences = store.preferences()
    store.record_statements([{
        "identity": "TARJETA_MASTERCARD|0002|2026-02-01", "owner": "santiago", "product": "TARJETA_MASTERCARD",
        "suffix": "0002", "period_start": "2026-02-01", "period_end": "2026-02-28", "movements": 1,
        "matched": 1, "missing": 0, "balance_minor": None,
    }], [])

    def row(event_id, account, kind="purchase"):
        return {**santiago_row(event_id, 123456, kind=kind), "account": account, "merchant": "Tienda"}

    store.import_message("msg-wrong", [row("wrong", "Tarjeta de crédito Visa")])
    store.import_message("msg-right", [row("right", "Tarjeta de crédito Master")])
    store.import_message("msg-fixed", [row("fixed", "Desconocida", kind="card_payment")])
    store.import_message("msg-income", [row("income", "Cuenta de ahorros", kind="income")])
    store.decide("wrong", "personal", "Compras")
    bodies = {
        "msg-wrong": "Compraste $1.234,56 en Tienda con tu tarjeta *0002.",
        "msg-right": "Compraste $1.234,56 en Tienda con tu tarjeta *0002.",
        # A fixed sentence names the card it pays, but keeps its account Desconocida.
        "msg-fixed": "Pagaste $218,699 en la tarjeta de credito *0002 desde la cuenta *0111, el 14/10/2026 11:47.",
        "msg-income": "Recibiste $1.234,56 en tu tarjeta *0002.",
    }

    class Reader:
        _received = {key: RECEIVED for key in bodies}

        def walk_folders(self, limit, visit):
            for key in bodies:
                visit(Candidate(key, "avisos@example.test", "Alertas"))
            return []

        def read(self, candidate):
            return Mail(candidate.item_id, candidate.sender, "Aviso", RECEIVED, bodies[candidate.item_id])

    @contextmanager
    def fake_source(*args):
        yield Reader()

    monkeypatch.setattr(app, "open_source", fake_source)
    settings = {"max_messages": 100}
    preview = app.backfill_card_accounts(store, tmp_path, settings, preferences, read_only=True)
    assert preview["changes"] == {"Tarjeta de crédito Visa -> Tarjeta de crédito Master": 1}
    assert preview["updated"] == 0 and preview["backup"] is None
    assert {r["id"]: r["account"] for r in store.state()["transactions"]}["wrong"] == "Tarjeta de crédito Visa"

    applied = app.backfill_card_accounts(store, tmp_path, settings, preferences, read_only=False)
    assert applied["updated"] == 1 and (tmp_path / applied["backup"]).exists()
    rows = {r["id"]: r for r in store.state()["transactions"]}
    assert (rows["wrong"]["account"], rows["wrong"]["decision"], rows["wrong"]["amount_minor"]) == (
        "Tarjeta de crédito Master", "personal", 123456,
    )
    assert rows["fixed"]["account"] == "Desconocida" and rows["income"]["account"] == "Cuenta de ahorros"
    with store.connect() as db:
        assert [tuple(r) for r in db.execute("SELECT target, value FROM audit WHERE action='card-account-backfill'")] == [
            ("wrong", "Tarjeta de crédito Visa -> Tarjeta de crédito Master"),
        ]
    assert app.backfill_card_accounts(store, tmp_path, settings, preferences, read_only=False)["updated"] == 0
