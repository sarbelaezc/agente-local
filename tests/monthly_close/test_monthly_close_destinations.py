"""Destinations of Santiago's payments, read from fictitious bank sentences."""
from __future__ import annotations

import pytest

from monthly_close.destinations import assign_destinations, notice_destinations
from monthly_close.domain import Preferences
from monthly_close.extraction import Mail, extract
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






class NoModel:
    def complete_json(self, system, user):
        raise AssertionError("Esta frase fija no se envia a Ollama.")


def notice(body: str) -> Mail:
    return Mail("m-fixed", "avisos@example.test", "Alertas", RECEIVED, body)




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


def test_qr_payment_to_a_key_is_read_without_the_model(preferences):
    """Ollama read "pagaste ... desde tu cuenta" as a card payment and returned nothing."""
    events = extract(notice(
        "Bancolombia: USUARIO, pagaste $12,500.00 por codigo QR desde tu cuenta *0001 a la llave\n"
        "3000000099 el 06/04/2026 a las 16:51. Pagar con codigo QR es facil y de una."
    ), preferences, NoModel())
    assert [
        (e["kind"], e["merchant"], e["amount_minor"], e["day"], e["time"], e["group_key"], e["decision"])
        for e in events
    ] == [("transfer", "Pago QR a llave 3000000099", 1250000, "2026-04-06", "16:51", "qr:3000000099", None)]


def test_payment_to_a_business_from_a_product_is_read_without_the_model(preferences):
    events = extract(notice(
        "Todo salio bien con tus movimientos Bancolombia: Pagaste $350,000.00 a CONJUNTO RESIDENCIAL\n"
        "FICTICIO desde tu producto *0001 el 05/05/2026 10:20:30. ¿Dudas? Llamanos."
    ), preferences, NoModel())
    assert [(e["kind"], e["merchant"], e["amount_minor"], e["day"], e["time"], e["decision"]) for e in events] == [
        ("purchase", "CONJUNTO RESIDENCIAL FICTICIO", 35000000, "2026-05-05", "10:20", None),
    ]


def test_scheduled_invesbot_contribution_is_an_investment_never_income(preferences):
    """Ollama called it income; the notice has no date, so it takes the day it arrived."""
    events = extract(notice(
        "Bancolombia: Realizamos la inversión que programaste por COP $ 150.000,00 a tu plan\n"
        "Plan Ficticio en Invesbot. Recibe tus rendimientos cada mes."
    ), preferences, NoModel())
    assert [(e["kind"], e["category"], e["merchant"], e["amount_minor"], e["day"], e["decision"]) for e in events] == [
        ("investment", "Inversiones", "Invesbot Plan Ficticio", 15000000, "2026-03-01", None),
    ]


@pytest.mark.parametrize("body", [
    "Transferiste COP $ 150.000,00 a tu inversión Plan Ficticio. Gracias por invertir.",
    "Transferiste $ COP $ 150.000,00 a tu inversión Plan Ficticio.",
    "Adicionaste COP $ 150.000,00 a tu plan Plan Ficticio en Invesbot.",
])
def test_every_wording_of_an_invesbot_contribution_is_the_same_plan(preferences, body):
    events = extract(notice(body), preferences, NoModel())
    assert [(e["kind"], e["merchant"], e["amount_minor"]) for e in events] == [
        ("investment", "Invesbot Plan Ficticio", 15000000),
    ]


@pytest.mark.parametrize("body", [
    "Ya ingresamos los COP $ 150.000,00 que transferiste a tu inversión Plan Ficticio.",
    "Retiramos COP $ 150.000,00 y los agregamos a tu inversión Plan Ficticio.",
    "El 14 de octubre tomaremos de tu cuenta de ahorros los COP $ 150.000 que elegiste sumar cada mes a tu inversión.",
    "Asegúrate de tener $150.000,00 COP en tu producto origen cuenta bancaria.",
])
def test_invesbot_notices_without_money_of_their_own_leave_no_movement(preferences, body):
    """The statement shows one debit per contribution: these repeat, confirm or announce it."""
    assert extract(notice(body), preferences, NoModel()) == []


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
