"""What counts: human decisions, investments, withdrawals, USD rates and coverage."""

from __future__ import annotations

import json
import sqlite3

import pytest

from monthly_close.domain import Preferences
from monthly_close.extraction import Mail, extract
from monthly_close.store import SCHEMA, Store
from builders import FakeExtractionModel


def test_step_zero_blocks_missing_or_unconfirmed_answers(preferences):
    raw = preferences.to_dict()
    raw.pop("ask_people")
    with pytest.raises(ValueError):
        Preferences.parse(raw)
    raw = preferences.to_dict()
    raw["confidential_confirmed"] = False
    with pytest.raises(ValueError):
        Preferences.parse(raw)


def test_proposals_never_count_without_human_decision(tmp_path, preferences, mail):
    store = Store(tmp_path / "test.sqlite3")
    with pytest.raises(ValueError, match="paso 0"):
        store.preferences()
    store.configure(preferences.to_dict())
    events = extract(mail, preferences, FakeExtractionModel())
    assert events[0]["day"] == "2026-02-28"
    assert events[0]["amount_minor"] == 123456
    assert events[0]["decision"] is None
    store.import_message(mail.id, events)
    assert "personal 0.00" in store.summary("2026-02")
    store.decide(events[0]["id"], "personal")
    assert "personal 1234.56" in store.summary("2026-02")
    assert store.state()["transactions"][0]["decision"] == "personal"
    assert not hasattr(store, "send")


def test_investment_decision_is_separate_persistent_and_reversible(tmp_path, preferences, mail):
    store = Store(tmp_path / "investment.sqlite3")
    store.configure(preferences.to_dict())
    events = extract(mail, preferences, FakeExtractionModel())
    store.import_message(mail.id, events)
    assert events[0]["decision"] is None
    store.decide(events[0]["id"], "investment", "Compras")
    row = store.state()["transactions"][0]
    assert row["category"] == "Inversiones"
    assert row["decision"] == "investment"
    summary = store.summary("2026-02")
    assert "personal 0.00; familiar 0.00; inversiones 1234.56" in summary
    assert "Dudas abiertas: 0" in summary
    store.import_message(mail.id, events)
    assert store.state()["transactions"][0]["decision"] == "investment"
    with store.connect() as db:
        assert db.execute(
            "SELECT value FROM audit WHERE action='decision-local-user'",
        ).fetchone()[0] == "investment"
    with pytest.raises(ValueError, match="categoria de gasto"):
        store.decide(row["id"], "personal")
    store.decide(row["id"], "personal", "Compras")
    assert "personal 1234.56; familiar 0.00; inversiones 0.00" in store.summary("2026-02")


@pytest.mark.parametrize("decision,category,total", [
    ("investment_withdrawal", "Retiros de inversión", "inversiones -1234.56"),
    ("cash_withdrawal", "Retiro en efectivo", "retiro en efectivo 1234.56"),
])
def test_withdrawals_require_manual_decision_and_apply_correct_expense_totals(
    tmp_path, preferences, mail, decision, category, total,
):
    store = Store(tmp_path / "withdrawal.sqlite3")
    store.configure(preferences.to_dict())
    events = extract(mail, preferences, FakeExtractionModel())
    store.import_message(mail.id, events)
    assert store.state()["transactions"][0]["decision"] is None
    store.decide(events[0]["id"], decision)
    row = store.state()["transactions"][0]
    assert row["category"] == category
    assert row["amount_minor"] == 123456
    summary = store.summary("2026-02")
    expected_personal = "1234.56" if decision == "cash_withdrawal" else "0.00"
    assert f"personal {expected_personal}; familiar 0.00" in summary
    assert total in summary
    store.import_message(mail.id, events)
    assert store.state()["transactions"][0]["decision"] == decision
    with store.connect() as db:
        assert db.execute(
            "SELECT value FROM audit WHERE action='decision-local-user'",
        ).fetchone()[0] == decision
    if decision == "investment_withdrawal":
        with pytest.raises(ValueError, match="categoria de gasto"):
            store.decide(row["id"], "personal")
    else:
        store.decide(row["id"], "personal")
        assert store.state()["transactions"][0]["category"] == "Retiro en efectivo"
    store.decide(row["id"], "personal", "Compras")
    assert store.state()["transactions"][0]["decision"] == "personal"


@pytest.mark.parametrize("decision", ["investment_withdrawal", "cash_withdrawal"])
@pytest.mark.parametrize("kind,account", [
    ("income", "Tarjeta A"), ("loan", "Tarjeta A"), ("card_payment", "Tarjeta A"),
    ("transfer", "Cuenta B"),
])
def test_withdrawals_cannot_override_non_expenses_or_excluded_accounts(
    tmp_path, preferences, mail, decision, kind, account,
):
    store = Store(tmp_path / "withdrawal-guards.sqlite3")
    store.configure(preferences.to_dict())
    events = extract(
        received(mail) if kind == "income" else mail, preferences,
        FakeExtractionModel(kind=kind, account=account),
    )
    store.import_message(mail.id, events)
    with pytest.raises(ValueError):
        store.decide(events[0]["id"], decision)


def test_cash_expense_category_works_without_configuration_change(tmp_path, preferences, mail):
    store = Store(tmp_path / "cash-category.sqlite3")
    store.configure(preferences.to_dict())
    events = extract(mail, preferences, FakeExtractionModel())
    store.import_message(mail.id, events)
    store.decide(events[0]["id"], "personal", "Retiro en efectivo")
    assert "personal 1234.56" in store.summary("2026-02")
    assert "retiro en efectivo 1234.56" in store.summary("2026-02")
    store.decide(events[0]["id"], "family", "Retiro en efectivo")
    assert "personal 0.00; familiar 1234.56" in store.summary("2026-02")


def test_cash_withdrawal_cannot_turn_own_transfer_into_personal_expense(
    tmp_path, preferences, mail,
):
    store = Store(tmp_path / "cash-own-transfer.sqlite3")
    store.configure(preferences.to_dict())
    events = extract(mail, preferences, FakeExtractionModel(kind="own_transfer"))
    store.import_message(mail.id, events)
    with pytest.raises(ValueError, match="Transferencias propias"):
        store.decide(events[0]["id"], "cash_withdrawal")


def test_detected_investment_requires_human_choice_and_cannot_be_consumption(
    tmp_path, preferences, mail,
):
    events = extract(mail, preferences, FakeExtractionModel(kind="investment"))
    assert events[0]["decision"] is None
    store = Store(tmp_path / "investment-kind.sqlite3")
    store.configure(preferences.to_dict())
    store.import_message(mail.id, events)
    for decision in ("personal", "family"):
        with pytest.raises(ValueError, match="inversion"):
            store.decide(events[0]["id"], decision)
    store.decide(events[0]["id"], "investment")
    assert store.state()["transactions"][0]["category"] == "Inversiones"
    store.decide(events[0]["id"], "exclude")
    assert "inversiones 0.00" in store.summary("2026-02")


@pytest.mark.parametrize("kind,account", [("income", "Tarjeta A"), ("transfer", "Cuenta B")])
def test_income_and_excluded_accounts_cannot_become_own_investments(
    tmp_path, preferences, mail, kind, account,
):
    store = Store(tmp_path / "investment-restrictions.sqlite3")
    store.configure(preferences.to_dict())
    events = extract(
        received(mail) if kind == "income" else mail, preferences,
        FakeExtractionModel(kind=kind, account=account),
    )
    store.import_message(mail.id, events)
    with pytest.raises(ValueError):
        store.decide(events[0]["id"], "investment")


def test_repeat_import_and_config_changes_preserve_human_choice(tmp_path, preferences, mail):
    store = Store(tmp_path / "test.sqlite3")
    store.configure(preferences.to_dict())
    events = extract(mail, preferences, FakeExtractionModel())
    store.import_message(mail.id, events)
    store.decide(events[0]["id"], "family")
    changed = preferences.to_dict()
    changed["ask_people"] = False
    store.configure(changed)
    store.import_message(mail.id, events)
    reopened = Store(store.path)
    assert len(reopened.state()["transactions"]) == 1
    assert reopened.state()["transactions"][0]["decision"] == "family"
    assert "familiar 1234.56" in reopened.summary("2026-02")
    with reopened.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM audit WHERE action='decision-local-user'").fetchone()[0] == 1
        assert db.execute("SELECT value FROM audit WHERE action='import'").fetchone()[0] == "1"


def received(mail: Mail) -> Mail:
    """The same notice, worded as money received."""
    return Mail(
        mail.id, mail.sender, mail.subject, mail.received,
        "Recibiste un pago de Tienda en Tarjeta A por $ 1.234,56.",
    )


def test_income_is_extracted_pending_and_counts_only_as_income(tmp_path, preferences, mail):
    store = Store(tmp_path / "income.sqlite3")
    store.configure(preferences.to_dict())
    events = extract(received(mail), preferences, FakeExtractionModel(kind="income", category="Compras"))
    store.import_message(mail.id, events)
    # The person decides; an expense category from the model is never kept for income.
    assert (events[0]["decision"], events[0]["category"]) == (None, "Otros ingresos")
    with pytest.raises(ValueError, match="no es un gasto"):
        store.decide(events[0]["id"], "personal")
    store.decide(events[0]["id"], "income", "Nómina")
    row = store.state()["transactions"][0]
    assert (row["decision"], row["category"]) == ("income", "Nómina")
    summary = store.summary("2026-02")
    assert "personal 0.00; familiar 0.00" in summary
    assert "ingresos 1234.56; balance 1234.56" in summary
    store.decide(events[0]["id"], "exclude", "Nómina")
    assert "ingresos 0.00" in store.summary("2026-02")


def test_a_purchase_cannot_be_counted_as_income(tmp_path, preferences, mail):
    store = Store(tmp_path / "purchase.sqlite3")
    store.configure(preferences.to_dict())
    events = extract(mail, preferences, FakeExtractionModel())
    store.import_message(mail.id, events)
    with pytest.raises(ValueError, match="no puede contarse como ingreso"):
        store.decide(events[0]["id"], "income")


@pytest.mark.parametrize("kind", ["card_payment", "rejected", "promotion", "verification"])
def test_non_expenses_cannot_be_counted(tmp_path, preferences, mail, kind):
    store = Store(tmp_path / "test.sqlite3")
    events = extract(mail, preferences, FakeExtractionModel(kind=kind))
    store.import_message(mail.id, events)
    assert events[0]["decision"] == "exclude"
    with pytest.raises(ValueError, match="no cuentan"):
        store.decide(events[0]["id"], "personal")
    assert "personal 0.00" in store.summary("2026-02")


def test_manual_usd_rate_preserves_source_and_corrects_legacy_currency(tmp_path, preferences, mail):
    store = Store(tmp_path / "rates.sqlite3")
    store.configure(preferences.to_dict())
    event = extract(mail, preferences, FakeExtractionModel())[0]
    event["amount_minor"] = 1234
    store.import_message(mail.id, [event])
    store.decide(event["id"], "personal", currency="USD", exchange_rate="4200,50")
    row = Store(store.path).state()["transactions"][0]
    assert (row["currency"], row["amount_minor"]) == ("USD", 1234)
    assert row["exchange_rate"] == "4200.50"
    assert row["cop_amount_minor"] == 5183417
    assert "COP: personal 51834.17" in store.summary("2026-02")
    assert "USD:" not in store.summary("2026-02")
    store.decide(event["id"], "family", exchange_rate="4201")
    assert "familiar 51840.34" in store.summary("2026-02")
    store.import_message(mail.id, [event])
    assert len(store.state()["transactions"]) == 1
    with store.connect() as db:
        audit = db.execute(
            "SELECT value FROM audit WHERE action='exchange-local-user' ORDER BY id",
        ).fetchall()
    assert json.loads(audit[0]["value"])["previous_currency"] == "COP"
    assert json.loads(audit[1]["value"])["previous_exchange_rate"] == "4200.50"
    store.decide(event["id"], "exclude")
    assert store.state()["transactions"][0]["decision"] == "exclude"
    store.decide(event["id"], "personal", currency="COP", exchange_rate="")
    assert store.state()["transactions"][0]["cop_amount_minor"] is None


@pytest.mark.parametrize("rate", ["", "0", "-1", "NaN", "Infinity", "1e3", "4.200,50", "1.1234567", "999999999"])
def test_invalid_or_missing_usd_rate_does_not_save_decision(tmp_path, preferences, mail, rate):
    store = Store(tmp_path / "invalid-rate.sqlite3")
    store.configure(preferences.to_dict())
    event = extract(mail, preferences, FakeExtractionModel())[0]
    store.import_message(mail.id, [event])
    before = store.state()
    with pytest.raises(ValueError):
        store.decide(event["id"], "personal", currency="USD", exchange_rate=rate)
    assert store.state() == before


def test_usd_rate_rounding_and_exclusion_without_conversion(tmp_path, preferences, mail):
    store = Store(tmp_path / "rounding.sqlite3")
    store.configure(preferences.to_dict())
    event = extract(mail, preferences, FakeExtractionModel())[0]
    event.update(currency="USD", amount_minor=1)
    store.import_message(mail.id, [event])
    store.decide(event["id"], "exclude", exchange_rate="")
    assert store.state()["transactions"][0]["cop_amount_minor"] is None
    store.decide(event["id"], "personal", exchange_rate="1.5")
    assert store.state()["transactions"][0]["cop_amount_minor"] == 2


def test_usd_conversion_rejects_amount_outside_safe_integer_range(tmp_path, preferences, mail):
    store = Store(tmp_path / "large-rate.sqlite3")
    store.configure(preferences.to_dict())
    event = extract(mail, preferences, FakeExtractionModel())[0]
    event.update(currency="USD", amount_minor=9007199254740991)
    store.import_message(mail.id, [event])
    with pytest.raises(ValueError, match="fuera del rango"):
        store.decide(event["id"], "personal", exchange_rate="2")
    assert store.state()["transactions"][0]["decision"] is None


def test_existing_database_migration_preserves_rows_and_decisions(tmp_path, preferences, mail):
    path = tmp_path / "legacy.sqlite3"
    event = extract(mail, preferences, FakeExtractionModel())[0]
    event["decision"] = "family"
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA)
        db.execute(
            """INSERT INTO transactions VALUES (
            :id,:message_id,:day,:merchant,:account,:amount_minor,:currency,
            :category,:kind,:decision,:reason)""", event,
        )
    store = Store(path)
    with Store(path).connect() as db:
        row = dict(db.execute("SELECT * FROM transactions").fetchone())
    assert all(row[key] == value for key, value in event.items())
    assert row["exchange_rate"] is None
    assert row["cop_amount_minor"] is None


def test_coverage_never_claims_completeness_automatically(tmp_path):
    store = Store(tmp_path / "test.sqlite3")
    store.coverage("2026-02", "partial", "Faltan avisos.")
    store.coverage("2026-02", "reviewed", "Comparado manualmente.")
    store.coverage("2026-02", "partial", "Nuevo escaneo.")
    assert store.state()["coverage"][0]["note"] == "Comparado manualmente."


def test_new_events_invalidate_reconciled_coverage(tmp_path, preferences, mail):
    store = Store(tmp_path / "test.sqlite3")
    store.coverage("2026-02", "reviewed", "Comparado manualmente.")
    store.import_message(mail.id, extract(mail, preferences, FakeExtractionModel()))
    assert store.state()["coverage"][0]["status"] == "partial"


def test_own_transfers_and_excluded_accounts_are_not_personal(tmp_path, preferences, mail):
    store = Store(tmp_path / "test.sqlite3")
    store.configure(preferences.to_dict())
    events = extract(mail, preferences, FakeExtractionModel(kind="own_transfer"))
    store.import_message(mail.id, events)
    with pytest.raises(ValueError, match="Transferencias propias"):
        store.decide(events[0]["id"], "personal")
    store.decide(events[0]["id"], "family", "Otros gastos")
    assert store.state()["transactions"][0]["category"] == "Otros gastos"
    with pytest.raises(ValueError, match="Categoria"):
        store.decide(events[0]["id"], "family", "Inventada")


def test_excluded_account_cannot_count_as_personal(tmp_path, preferences, mail):
    store = Store(tmp_path / "test.sqlite3")
    store.configure(preferences.to_dict())
    events = extract(mail, preferences, FakeExtractionModel(account="Cuenta B"))
    store.import_message(mail.id, events)
    with pytest.raises(ValueError, match="excluidas"):
        store.decide(events[0]["id"], "personal")
