"""Household panel flow in Chromium, with synthetic data only."""
from __future__ import annotations

import re
import threading

import pytest
from playwright.sync_api import expect, sync_playwright

from monthly_close.server import create_server
from monthly_close.store import Store

MONTH = "2026-03"


def member(event_id: str, amount: int, kind: str, decision: str | None, **extra) -> dict:
    return {
        "id": f"manuela:{event_id}", "message_id": f"manuela-import:{event_id}", "owner": "manuela",
        "day": f"{MONTH}-1{len(event_id) % 9}", "time": "10:15", "merchant": extra.pop("merchant", "MERCADO SINTETICO"),
        "account": "TC *0001", "account_label": None, "amount_minor": amount, "currency": "COP",
        "category": extra.pop("category", "Compras"), "kind": kind, "decision": decision,
        "reason": "Sintetico.", "group_key": extra.pop("group_key", None), "note": None,
        "category_uncertain": extra.pop("category_uncertain", 0),
    }


@pytest.fixture
def household(tmp_path, categories):
    store = Store(tmp_path / "close.sqlite3")
    store.configure({
        "location": "Ciudad de prueba", "timezone": "America/Bogota", "currency": "COP",
        "accounts": ["Tarjeta A"], "excluded_accounts": [],
        "transfer_label": "Familiar", "ask_savings": True, "ask_people": True,
        "approved_senders": ["avisos@example.test"], "categories": categories,
        "confidential_confirmed": True,
    })
    store.import_message("s1", [{
        "id": "s1", "message_id": "s1", "day": f"{MONTH}-02", "merchant": "TIENDA S",
        "account": "Tarjeta A", "amount_minor": 100000, "currency": "COP", "category": "Compras",
        "kind": "purchase", "decision": "personal", "reason": "Revisar.",
    }])
    store.import_message("s2", [{
        "id": "s2", "message_id": "s2", "day": f"{MONTH}-03", "merchant": "Transferencia a cuenta *0002",
        "account": "Tarjeta A", "amount_minor": 40000, "currency": "COP", "category": "Otros gastos",
        "kind": "transfer", "decision": None, "reason": "Revisar.",
    }])
    store.decide("s2", "family")
    store.import_external("manuela", "manuela", [
        member("m1", 20000, "purchase", "personal", category_uncertain=1),
        member("m22", 5000, "card_payment", "third_party_payment", category="Pagos de otras personas"),
        member("m333", 3000, "transfer", None, category="Otros gastos", group_key="tr:55550009999"),
        member("m4444", 7000, "transfer", None, category="Otros gastos", group_key="tr:55550009999"),
    ], {})
    server = create_server(store, 0)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 1000})
        page.set_default_timeout(3000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(f"http://127.0.0.1:{server.server_port}/?month={MONTH}&currency=COP")
        page.wait_for_load_state("networkidle")
        try:
            yield page, store, errors
        finally:
            browser.close()
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()


def spend(page) -> str:
    return page.locator("#month-metrics .metric").filter(has_text="Gasto del mes").inner_text()


def choose(page, owner: str) -> None:
    page.locator("#owner-switch").get_by_role("button", name=owner, exact=True).click()


def test_owner_switch_separates_members_and_household_adds_them(household):
    page, store, errors = household
    assert page.locator('#owner-switch button[aria-pressed="true"]').inner_text() == "Hogar"
    # Hogar: 1000 + 400 (Santiago) + 200 − 50 (Manuela) = 1550.
    assert re.search(r"\b1[.\s]?550,00", spend(page))
    legend = spend(page)
    assert "Personal Santiago" in legend and "Personal Manuela" in legend and "Familiar" in legend
    choose(page, "Santiago")
    assert re.search(r"\b1[.\s]?400,00", spend(page))
    assert "Manuela" not in page.locator("#category-details").inner_text()
    choose(page, "Manuela")
    assert re.search(r"\b150,00", spend(page))
    assert "abonos de terceros" in spend(page)
    assert "owner=manuela" in page.url
    choose(page, "Hogar")
    details = page.locator("#category-details")
    details.locator("summary").first.click()
    assert "Categoría por confirmar" in details.inner_text()
    assert not errors


def test_between_members_mark_leaves_money_out_of_household_only(household):
    page, store, errors = household
    choose(page, "Santiago")
    page.locator("#tab-decisions").click()
    page.locator("#decision-history > summary").click()
    card = page.locator("#history-list .decision-card").filter(has_text="*0002")
    with page.expect_response(lambda response: response.url.endswith("/api/between-owners")) as saved:
        card.get_by_role("button", name="Entre Santiago y Manuela").click()
    assert saved.value.status == 200
    expect(card.get_by_role("button", name="Entre Santiago y Manuela")).to_have_attribute("aria-pressed", "true")
    page.locator("#tab-month").click()
    assert re.search(r"\b1[.\s]?400,00", spend(page))  # unchanged in Santiago's own view
    choose(page, "Hogar")
    assert re.search(r"\b1[.\s]?150,00", spend(page))
    assert "no cuentan en Hogar" in page.locator("#month-metrics").inner_text()
    assert next(tx for tx in store.state()["transactions"] if tx["id"] == "s2")["between_owners"] == 1
    assert not errors


def test_destination_card_decides_every_pending_payment_with_audit(household):
    page, store, errors = household
    page.locator("#tab-decisions").click()
    summary = page.locator("#decision-summary").inner_text()
    assert "1" in summary and "destino por decidir" in summary
    card = page.locator("#pending-list .group-card")
    assert card.count() == 1
    assert "Transferencia a la cuenta *55550009999" in card.inner_text()
    assert "2 movimientos" in card.inner_text()
    card.get_by_label("Aplicar también a pagos futuros a este destino").check()
    with page.expect_response(lambda response: response.url.endswith("/api/decision-group")) as saved:
        card.get_by_role("button", name=re.compile("^Familiar: 2 movimientos")).click()
    assert saved.value.status == 200
    expect(page.locator("#pending-list .group-card")).to_have_count(0)
    decided ={tx["id"]: tx["decision"] for tx in store.state()["transactions"]}
    assert decided["manuela:m333"] == decided["manuela:m4444"] == "family"
    with store.connect() as db:
        audited = db.execute(
            "SELECT target FROM audit WHERE action='decision-group-local-user' ORDER BY target",
        ).fetchall()
    assert [row["target"] for row in audited] == ["manuela:m333", "manuela:m4444"]
    assert store.state()["group_rules"][0]["decision"] == "family"
    page.locator("#tab-month").click()
    assert re.search(r"\b1[.\s]?650,00", spend(page))  # + 100 in family spending
    assert not errors
