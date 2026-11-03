from __future__ import annotations

import os
import re
import socket
import threading
from datetime import date
from pathlib import Path

import pytest
import httpx
from playwright.sync_api import sync_playwright

from monthly_close.domain import CATEGORIES
from monthly_close.server import create_server
from monthly_close.store import Store


def test_idle_browser_connection_does_not_block_dashboard(tmp_path):
    server = create_server(Store(tmp_path / "idle.sqlite3"), 0)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    idle = socket.create_connection(("127.0.0.1", server.server_port), timeout=2)
    try:
        with httpx.Client(timeout=2, trust_env=False) as client:
            response = client.get(f"http://127.0.0.1:{server.server_port}/")
        assert response.status_code == 200
        assert "<title>Cierre mensual" in response.text
    finally:
        idle.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def dashboard(tmp_path):
    store = Store(tmp_path / "dashboard.sqlite3")
    store.configure({
        "location": "Ciudad de prueba", "timezone": "America/Bogota", "currency": "COP",
        "accounts": ["Tarjeta A"], "excluded_accounts": [],
        "transfer_label": "Familiar", "ask_savings": True, "ask_people": True,
        "approved_senders": ["avisos@example.test"], "categories": CATEGORIES,
        "confidential_confirmed": True,
    })
    month = date.today().strftime("%Y-%m")
    for index, (decision, amount) in enumerate([(None, 50002), ("personal", 120056), ("family", 30001)]):
        store.import_message(str(index), [{
            "id": str(index), "message_id": str(index), "day": month + "-05",
            "merchant": "<img src=x onerror=alert(1)> Tienda", "account": "Tarjeta A",
            "amount_minor": amount, "currency": "COP", "category": "Compras",
            "kind": "purchase", "decision": decision, "reason": "Revisar movimiento.",
        }])
    store.coverage(month, "partial", "Datos sinteticos, sin correo real.")
    server = create_server(store, 0)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.set_default_timeout(3000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(f"http://127.0.0.1:{server.server_port}")
        page.wait_for_load_state("networkidle")
        try:
            yield page, store, month, errors
        finally:
            browser.close()
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()


def test_dashboard_renders_without_scripts_from_untrusted_text(dashboard):
    page, _, _, errors = dashboard
    assert not errors
    assert page.get_by_role("tab", name="Mes", exact=True).count() == 1
    assert page.get_by_role("tab", name=re.compile("A.o")).count() == 1
    assert page.locator("img").count() == 0
    assert page.locator("script[src], link[rel=stylesheet]").count() == 0
    text = page.locator("#month-metrics").inner_text()
    assert re.search(r"\b1[.\s]?200,56", text), text
    assert "300,01" in text
    assert "500,02" in text


def test_decision_persists_and_updates_totals(dashboard):
    page, store, _, errors = dashboard
    page.get_by_role("tab", name=re.compile("Decisi")).click()
    button = page.get_by_role("button", name="Gasto personal", exact=True).first
    assert button.count(), page.locator("#panel-decisions").inner_text()[:3000]
    with page.expect_response(
        lambda response: response.url.endswith("/api/decision") and response.request.method == "POST"
    ) as saved:
        button.click()
    assert saved.value.status == 200, saved.value.text()
    page.reload()
    page.wait_for_load_state("networkidle")
    assert store.state()["transactions"][0]["decision"] == "personal"
    assert re.search(r"\b1[.\s]?700,58", page.locator("#month-metrics").inner_text())
    assert not errors


def test_investment_choice_updates_separate_month_year_totals_and_history(dashboard):
    page, store, month, errors = dashboard
    page.get_by_role("tab", name=re.compile("Decisi")).click()
    with page.expect_response(
        lambda response: response.url.endswith("/api/decision") and response.request.method == "POST"
    ) as saved:
        page.locator("#pending-list").get_by_role("button", name="Inversión", exact=True).click()
    assert saved.value.status == 200
    page.reload()
    page.wait_for_load_state("networkidle")
    row = next(tx for tx in store.state()["transactions"] if tx["id"] == "0")
    assert row["decision"] == "investment"
    assert row["category"] == "Inversiones"
    metric = page.locator("#month-metrics .metric").filter(has_text="Inversiones")
    assert "500,02" in metric.inner_text()
    total = page.locator("#month-metrics .metric").filter(has_text="Total decidido").inner_text()
    assert re.search(r"\b1[.\s]?500,57", total)
    assert "inversiones 500.02" in store.summary(month)
    investment_detail = page.locator("#category-details summary").filter(has_text="Inversiones")
    assert "500,02" in investment_detail.inner_text()
    assert "en inversiones" in investment_detail.inner_text()
    page.locator("#tab-year").click()
    assert "500,02" in page.locator("#year-metrics").inner_text()
    assert page.get_by_role("columnheader", name="Inversiones", exact=True).count() == 1
    assert "Inversiones" not in page.locator("#year-category-legend").inner_text()
    page.get_by_role("tab", name=re.compile("Decisi")).click()
    assert page.locator("#pending-list .decision-card").count() == 0
    assert page.locator('#history-list button[aria-pressed="true"]').filter(
        has_text="Inversión",
    ).count() == 1
    assert not errors


def test_dashboard_mobile_and_desktop_screenshots(dashboard, tmp_path):
    page, _, _, errors = dashboard
    output = Path(os.environ.get("MONTHLY_CLOSE_SCREENSHOTS", str(tmp_path)))
    output.mkdir(parents=True, exist_ok=True)
    for width, label in [(1440, "desktop"), (390, "mobile")]:
        page.set_viewport_size({"width": width, "height": 1000})
        for index, tab in enumerate(["month", "year", "decisions"]):
            page.locator("#tab-" + tab).click()
            assert page.evaluate(
                "document.documentElement.scrollWidth <= window.innerWidth"
            ), f"Desbordamiento horizontal en {label}/{tab}"
            page.screenshot(path=str(output / f"{label}-{index}.png"), full_page=True)
    page.get_by_role("button", name=re.compile("tema", re.I)).click()
    page.locator("#tab-month").click()
    page.screenshot(path=str(output / "mobile-dark.png"), full_page=True)
    assert not errors


@pytest.mark.parametrize("decision,label,category", [
    ("investment_withdrawal", "Retiro de inversión", "Retiros de inversión"),
    ("personal", "Gasto personal", "Retiro en efectivo"),
])
def test_withdrawal_buttons_persist_and_apply_correct_expense_totals(
    dashboard, decision, label, category,
):
    page, store, _, errors = dashboard
    page.get_by_role("tab", name=re.compile("Decisi")).click()
    assert next(tx for tx in store.state()["transactions"] if tx["id"] == "0")["decision"] is None
    assert page.get_by_role("button", name="Retiro en efectivo", exact=True).count() == 0
    if category == "Retiro en efectivo":
        page.locator("#pending-list select[name=category]").select_option(category)
    with page.expect_response(
        lambda response: response.url.endswith("/api/decision") and response.request.method == "POST"
    ) as saved:
        page.locator("#pending-list").get_by_role("button", name=label, exact=True).click()
    assert saved.value.status == 200
    page.reload()
    page.wait_for_load_state("networkidle")
    row = next(tx for tx in store.state()["transactions"] if tx["id"] == "0")
    assert (row["decision"], row["category"]) == (decision, category)
    total = page.locator("#month-metrics .metric").filter(has_text="Total decidido").inner_text()
    expected = r"\b2[.\s]?000,59" if category == "Retiro en efectivo" else r"\b1[.\s]?500,57"
    assert re.search(expected, total)
    if decision == "investment_withdrawal":
        metric = page.locator("#month-metrics .metric").filter(has_text="neto, no saldo")
        assert re.search(r"[-−].*500,02", metric.inner_text())
    else:
        assert "500,02" in page.locator("#month-metrics .metric").filter(
            has_text="Retiro en efectivo",
        ).inner_text()
        assert "Retiro en efectivo" in page.locator("#category-details").inner_text()
        assert "Retiro en efectivo" in page.locator("#category-chart").inner_text()
        assert re.search(r"\b2[.\s]?000,59", page.locator("#calendar").inner_text())
    page.locator("#tab-year").click()
    assert "500,02" in page.locator("#year-metrics").inner_text()
    assert "500,02" in page.locator("#year-table").inner_text()
    if category == "Retiro en efectivo":
        assert "Retiro en efectivo" in page.locator("#year-category-legend").inner_text()
        assert re.search(expected, page.locator("#year-metrics").inner_text())
    page.get_by_role("tab", name=re.compile("Decisi")).click()
    assert page.locator("#pending-list .decision-card").count() == 0
    assert page.locator("#history-list .decision-card").filter(
        has=page.locator('select[name=category] option:checked').filter(has_text=category),
    ).locator('button[aria-pressed="true"]').filter(has_text=label).count() == 1
    page.locator("#decision-history summary").click()
    with page.expect_response(
        lambda response: response.url.endswith("/api/decision") and response.request.method == "POST"
    ) as excluded:
        page.locator("#history-list .decision-card").filter(
            has=page.locator('select[name=category] option:checked').filter(has_text=category),
        ).filter(
            has=page.locator('button[aria-pressed="true"]').filter(has_text=label),
        ).get_by_role(
            "button", name="No cuenta", exact=True,
        ).click()
    assert excluded.value.status == 200
    assert not errors


def test_configuration_form_saves_all_step_zero_answers(dashboard):
    page, store, _, errors = dashboard
    with store.connect() as db:
        db.execute("DELETE FROM settings")
    page.reload()
    page.wait_for_load_state("networkidle")
    host = page.locator("#setup-form-host")
    host.locator("[name=location]").fill("Ciudad de prueba")
    host.locator("[name=timezone]").fill("America/Bogota")
    host.locator("[name=currency]").select_option("COP")
    host.locator("[name=accounts]").fill("Tarjeta A")
    host.locator("[name=excluded_accounts]").fill("Cuenta B")
    host.locator("[name=transfer_label]").fill("Entre mis cuentas")
    host.locator("[name=approved_senders]").fill("avisos@example.test")
    host.locator("[name=ask_savings]").check()
    host.locator("[name=ask_people]").check()
    host.locator("[name=confidential_confirmed]").check()
    with page.expect_response(
        lambda response: response.url.endswith("/api/config") and response.request.method == "POST"
    ) as response:
        host.get_by_role("button", name="Guardar configuración", exact=True).click()
    assert response.value.status == 200, response.value.text()
    saved = store.preferences()
    assert saved.excluded_accounts == ["Cuenta B"]
    assert saved.transfer_label == "Entre mis cuentas"
    assert saved.ask_savings and saved.ask_people
    assert not errors


def test_configuration_edit_preserves_existing_decisions(dashboard):
    page, store, _, errors = dashboard
    page.locator("button").filter(has_text="Configuración").click()
    host = page.locator("#config-form-host")
    host.locator("[name=timezone]").fill("Europe/Madrid")
    host.locator("[name=confidential_confirmed]").check()
    invalid = host.locator("input:invalid, select:invalid, textarea:invalid").evaluate_all(
        "items => items.map(item => ({name:item.name,message:item.validationMessage}))"
    )
    assert not invalid, invalid
    with page.expect_response(
        lambda response: response.url.endswith("/api/config") and response.request.method == "POST"
    ) as response:
        host.get_by_role("button", name="Guardar cambios", exact=True).click()
    assert response.value.status == 200, response.value.text()
    assert store.preferences().timezone == "Europe/Madrid"
    assert len(store.state()["transactions"]) == 3
    assert next(row for row in store.state()["transactions"] if row["id"] == "1")["decision"] == "personal"
    assert not errors


def test_annual_chart_has_visible_numeric_axis_labels(dashboard):
    page, _, _, _ = dashboard
    page.locator("#tab-year").click()
    labels = page.locator("#panel-year svg text").evaluate_all(
        """nodes => nodes.map(node => {
          const box = node.getBBox();
          return {
            text: [...node.childNodes].filter(child => child.nodeType === 3)
              .map(child => child.textContent).join('').trim(),
            x: box.x, width: box.width, height: box.height,
            fill: getComputedStyle(node).fill
          };
        })"""
    )
    numeric = [label for label in labels if re.match(r"^\d", label["text"])]
    assert numeric, labels
    assert all(label["x"] >= 0 and label["width"] > 0 and label["height"] > 0 for label in numeric), numeric
