from __future__ import annotations

import json
import os
import re
import socket
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import httpx
from playwright.sync_api import expect

from builders import movement
from monthly_close.store import Store


def test_idle_browser_connection_does_not_block_dashboard(tmp_path, start_panel):
    url = start_panel(Store(tmp_path / "idle.sqlite3"))
    idle = socket.create_connection(("127.0.0.1", urlsplit(url).port), timeout=2)
    try:
        with httpx.Client(timeout=2, trust_env=False) as client:
            response = client.get(url + "/")
        assert response.status_code == 200
        assert "<title>Cierre mensual" in response.text
    finally:
        idle.close()


@pytest.fixture
def dashboard(make_store, start_panel, new_page):
    store = make_store("dashboard.sqlite3")
    month = date.today().strftime("%Y-%m")
    for index, (decision, amount) in enumerate([(None, 50002), ("personal", 120056), ("family", 30001)]):
        store.import_message(str(index), [{
            "id": str(index), "message_id": str(index), "day": month + "-05",
            "merchant": "<img src=x onerror=alert(1)> Tienda", "account": "Tarjeta A",
            "amount_minor": amount, "currency": "COP", "category": "Compras",
            "kind": "purchase", "decision": decision, "reason": "Revisar movimiento.",
        }])
    store.coverage(month, "partial", "Datos sinteticos, sin correo real.")
    url = start_panel(store)
    page = new_page(viewport={"width": 1440, "height": 1000})
    page.set_default_timeout(3000)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(url)
    expect(page.locator("main")).to_have_attribute("aria-busy", "false")
    yield page, store, month, errors
    # Every panel test also proves the page raised no JavaScript error.
    assert not errors, f"Errores de JavaScript en el panel: {errors}"


def test_dashboard_renders_without_scripts_from_untrusted_text(dashboard):
    page, _, _, _ = dashboard
    assert page.get_by_role("tab", name="Mes", exact=True).count() == 1
    assert page.get_by_role("tab", name=re.compile("A.o")).count() == 1
    assert page.locator("img").count() == 0
    assert page.locator("script[src], link[rel=stylesheet]").count() == 0
    text = page.locator("#month-metrics").inner_text()
    assert re.search(r"\b1[.\s]?200,56", text), text
    assert "300,01" in text
    assert "500,02" in text


def test_extraction_review_is_visible_across_currencies_and_renders_only_text(dashboard):
    page, store, month, _ = dashboard
    store.record_extraction_failure(
        "blocked", "avisos@example.test", "<img src=x onerror=alert(1)> Aviso bloqueado",
        month + "-06T12:00:00-05:00", month + "-06", "dinero / banco", "merchant",
    )
    page.reload()
    expect(page.locator("main")).to_have_attribute("aria-busy", "false")
    assert "1 aviso(s) sin extraer" in page.locator("#extraction-warning").inner_text()
    page.locator("#tab-decisions").click()
    review = page.locator("#extraction-review")
    assert review.is_visible()
    assert "Aviso bloqueado" in review.inner_text()
    assert "avisos@example.test" in review.inner_text()
    assert "dinero / banco" in review.inner_text()
    # The notice text never becomes markup, and the only action is to discard it.
    assert review.locator("img").count() == 0
    assert review.locator("button").all_inner_texts() == ["Descartar aviso"]
    store.import_message("blocked", [])
    page.reload()
    expect(page.locator("main")).to_have_attribute("aria-busy", "false")
    assert not page.locator("#extraction-warning").is_visible()


def test_queued_notice_can_be_discarded_from_the_panel(dashboard):
    page, store, month, _ = dashboard
    store.record_extraction_failure(
        "informative", "avisos@example.test", "Aviso informativo",
        month + "-06T12:00:00-05:00", month + "-06", "dinero / banco", "amount_text",
    )
    page.reload()
    expect(page.locator("main")).to_have_attribute("aria-busy", "false")
    page.locator("#tab-decisions").click()
    review = page.locator("#extraction-review")
    assert "el monto que propuso Ollama no aparece en el aviso" in review.inner_text()
    assert "12:00" in review.inner_text()  # the time, to find it among the same day's alerts
    # The queue and the movements to decide never overlap: the queue ends before the list starts.
    expect(page.locator("#pending-heading")).to_be_visible()
    queue_box = review.bounding_box()
    list_box = page.locator("#pending-list").bounding_box()
    assert queue_box["y"] + queue_box["height"] < list_box["y"]
    # Cancelling the confirmation keeps the notice in the queue.
    asked = []
    page.once("dialog", lambda dialog: (asked.append(dialog.message), dialog.dismiss()))
    review.get_by_role("button", name=re.compile("Descartar")).click()
    assert "Aviso informativo" in asked[0]
    assert len(store.state()["extraction_failures"]) == 1
    page.once("dialog", lambda dialog: dialog.accept())
    with page.expect_response(lambda response: response.url.endswith("/api/failure/dismiss")) as saved:
        review.get_by_role("button", name=re.compile("Descartar")).click()
    assert saved.value.status == 200
    review.wait_for(state="hidden")
    assert store.state()["extraction_failures"] == []
    assert store.seen("informative")


def test_decision_persists_and_updates_totals(dashboard):
    page, store, _, _ = dashboard
    page.get_by_role("tab", name=re.compile("Decisi")).click()
    button = page.get_by_role("button", name="Gasto personal", exact=True).first
    assert button.count(), page.locator("#panel-decisions").inner_text()[:3000]
    with page.expect_response(
        lambda response: response.url.endswith("/api/decision") and response.request.method == "POST"
    ) as saved:
        button.click()
    assert saved.value.status == 200, saved.value.text()
    page.reload()
    expect(page.locator("main")).to_have_attribute("aria-busy", "false")
    assert store.state()["transactions"][0]["decision"] == "personal"
    assert re.search(r"\b1[.\s]?700,58", page.locator("#month-metrics").inner_text())


def test_usd_decision_requires_manual_rate_and_updates_cop_reports(dashboard):
    page, store, month, _ = dashboard
    store.import_message("usd", [{
        "id": "usd", "message_id": "usd", "day": month + "-06",
        "merchant": "AMAZON MKTPLACE", "account": "Tarjeta A",
        "amount_minor": 1234, "currency": "USD", "category": "Compras",
        "kind": "purchase", "decision": None, "reason": "Aviso USD.",
    }])
    page.reload()
    expect(page.locator("main")).to_have_attribute("aria-busy", "false")
    page.locator("#tab-decisions").click()
    card = page.locator("#pending-list .decision-card").filter(has_text="AMAZON MKTPLACE")
    assert "USD" in card.inner_text()
    with page.expect_response(lambda response: response.url.endswith("/api/decision")) as saved:
        card.get_by_role("button", name="Gasto personal", exact=True).click()
    assert saved.value.status == 400
    assert next(t for t in store.state()["transactions"] if t["id"] == "usd")["decision"] is None
    card.get_by_label(re.compile("Tasa estimada COP por USD")).fill("4200,50")
    with page.expect_response(lambda response: response.url.endswith("/api/decision")) as saved:
        card.get_by_role("button", name="Gasto personal", exact=True).click()
    assert saved.value.status == 200
    page.reload()
    expect(page.locator("main")).to_have_attribute("aria-busy", "false")
    # Existing COP expenses (1500.57) plus the USD estimate (51834.17).
    assert re.search(r"\b53[.\s]?334,74", page.locator("#month-metrics").inner_text())
    page.locator("#tab-year").click()
    assert re.search(r"\b53[.\s]?334,74", page.locator("#year-metrics").inner_text())
    page.locator("#tab-decisions").click()
    page.locator("#decision-history > summary").click()
    history = page.locator("#history-list .decision-card").filter(has_text="AMAZON MKTPLACE")
    assert "12,34" in history.inner_text()
    assert re.search(r"51[.\s]?834,17", history.inner_text())
    assert history.get_by_label(re.compile("Tasa estimada COP por USD")).input_value() == "4200.50"


def test_grouped_usd_purchases_show_dollars_not_zero_cop(dashboard):
    page, store, month, _ = dashboard
    for number, amount in (("1", 1000), ("2", 2500)):
        store.import_message(f"usd-group-{number}", [{
            "id": f"usd-group-{number}", "message_id": f"usd-group-{number}", "day": f"{month}-0{number}",
            "merchant": "OPENAI CHATGPT", "account": "Tarjeta A",
            "amount_minor": amount, "currency": "USD", "category": "Compras",
            "kind": "purchase", "decision": None, "reason": "Aviso USD.",
        }])
    page.reload()
    expect(page.locator("main")).to_have_attribute("aria-busy", "false")
    page.locator("#tab-decisions").click()
    card = page.locator("#pending-list .group-card").filter(has_text="OPENAI CHATGPT")
    amount = card.locator(".group-amount").inner_text()
    assert "35,00" in amount and "US$" in amount
    assert "0,00" not in amount.replace("35,00", "")


def test_legacy_cop_record_can_be_corrected_to_usd_only_on_decision(dashboard):
    page, store, _, _ = dashboard
    page.locator("#tab-decisions").click()
    card = page.locator("#pending-list .decision-card").first
    card.get_by_text("Corregir moneda").click()  # folded away: rarely needed
    card.get_by_label("Moneda original del aviso", exact=True).select_option("USD")
    assert store.state()["transactions"][0]["currency"] == "COP"
    card.get_by_label(re.compile("Tasa estimada COP por USD")).fill("4000")
    with page.expect_response(lambda response: response.url.endswith("/api/decision")) as saved:
        card.get_by_role("button", name="Familiar", exact=True).click()
    assert saved.value.status == 200
    row = next(tx for tx in store.state()["transactions"] if tx["id"] == "0")
    assert row["currency"] == "USD"
    assert row["amount_minor"] == 50002
    assert row["cop_amount_minor"] == 200008000


def test_stale_backend_cannot_silently_ignore_exchange_fields(dashboard):
    page, _, _, _ = dashboard

    def stale_state(route):
        response = route.fetch()
        data = response.json()
        for tx in data["transactions"]:
            tx.pop("exchange_rate")
            tx.pop("cop_amount_minor")
        route.fulfill(response=response, json=data)

    page.route("**/api/state", stale_state)
    page.reload()
    expect(page.locator("main")).to_have_attribute("aria-busy", "false")
    assert "Reinicia el servidor" in page.locator("body").inner_text()
    assert not page.locator("#dashboard-area").is_visible()


def test_investment_choice_updates_separate_month_year_totals_and_history(dashboard):
    page, store, month, _ = dashboard
    page.get_by_role("tab", name=re.compile("Decisi")).click()
    with page.expect_response(
        lambda response: response.url.endswith("/api/decision") and response.request.method == "POST"
    ) as saved:
        page.locator("#pending-list").get_by_role("button", name="Inversión", exact=True).click()
    assert saved.value.status == 200
    page.reload()
    expect(page.locator("main")).to_have_attribute("aria-busy", "false")
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


def test_dashboard_mobile_and_desktop_screenshots(dashboard, tmp_path):
    page, _, _, _ = dashboard
    output = Path(os.environ.get("MONTHLY_CLOSE_SCREENSHOTS", str(tmp_path)))
    output.mkdir(parents=True, exist_ok=True)
    for width, label in [(1440, "desktop"), (390, "mobile"), (320, "narrow")]:
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


def test_dashboard_links_reopen_selected_view_and_keyboard_can_skip_to_content(dashboard):
    page, _, month, _ = dashboard
    page.goto(f"{page.url.split('?')[0]}?tab=year&month={month}&year={month[:4]}&currency=COP")
    expect(page.locator("main")).to_have_attribute("aria-busy", "false")
    page.keyboard.press("Tab")
    skip_link = page.locator(".skip-link")
    assert skip_link.get_attribute("href") == "#main-content"
    assert skip_link.evaluate("element => getComputedStyle(element).transform === 'matrix(1, 0, 0, 1, 0, 0)'")

    assert page.locator("#tab-year").get_attribute("aria-selected") == "true"
    assert page.locator("#year-select").input_value() == month[:4]

    params = page.evaluate("Object.fromEntries(new URLSearchParams(location.search))")
    assert params == {"tab": "year", "month": month, "year": month[:4], "currency": "COP", "owner": "hogar"}
    page.locator("#tab-decisions").click()
    assert page.evaluate("new URLSearchParams(location.search).get('tab')") == "decisions"


def test_decision_lists_only_render_fifty_cards_at_a_time(dashboard):
    page, store, _, _ = dashboard
    original = next(tx for tx in store.state()["transactions"] if tx["id"] == "1")
    for index in range(51):
        event_id = f"pending-{index}"
        store.import_message(event_id, [{
            **original, "id": event_id, "message_id": event_id, "decision": None,
            "merchant": f"Tienda {index}",  # one destination per movement
        }])

    page.reload()
    expect(page.locator("main")).to_have_attribute("aria-busy", "false")
    page.locator("#tab-decisions").click()
    pending = page.locator("#pending-list .decision-card")
    assert pending.count() == 50
    page.get_by_role("button", name="Mostrar 2 destinos pendientes más").click()
    assert pending.count() == 52


def test_legacy_cash_withdrawal_updates_cumulative_personal_total(dashboard):
    page, store, _, _ = dashboard
    store.decide("0", "cash_withdrawal")
    page.reload()
    expect(page.locator("main")).to_have_attribute("aria-busy", "false")
    text = page.locator("#cumulative-chart .legend-row").inner_text()
    assert re.search(r"Personal\s+1[.\s]?700,58", text), text
    assert "Familiar 300,01" in text


@pytest.mark.parametrize("configured", [True, False])
def test_failed_state_load_can_be_retried(dashboard, configured):
    page, store, _, _ = dashboard
    if not configured:
        with store.connect() as db:
            db.execute("DELETE FROM settings")
    page.route("**/api/state", lambda route: route.abort())
    page.reload()
    page.locator("#load-error").wait_for(state="visible")
    retry = page.get_by_role("button", name="Actualizar", exact=True)
    assert retry.is_visible()
    page.unroute("**/api/state")
    retry.click()
    page.locator("#dashboard-area" if configured else "#setup-area").wait_for(state="visible")
    assert page.locator("#load-error").is_hidden()


def test_saving_blocks_duplicate_decisions_and_recovers_after_error(dashboard):
    page, store, _, _ = dashboard
    requests = []
    page.route("**/api/decision", lambda route: requests.append(route))
    page.locator("#tab-decisions").click()
    button = page.locator("#pending-list").get_by_role("button", name="Gasto personal", exact=True)
    button.evaluate("""button => {
        button.click();
        button.dispatchEvent(new MouseEvent("click", {bubbles: true}));
    }""")
    page.wait_for_function("() => document.querySelector('#refresh-button').disabled")
    assert len(requests) == 1
    assert page.locator(
        "button:not(#theme-toggle):enabled, input:enabled, select:enabled, textarea:enabled"
    ).count() == 0
    requests[0].fulfill(status=500, json={"error": "Fallo simulado."})
    page.wait_for_function("() => !document.querySelector('#refresh-button').disabled")
    assert "Fallo simulado" in page.locator("#global-message").inner_text()
    assert next(tx for tx in store.state()["transactions"] if tx["id"] == "0")["decision"] is None
    assert button.is_enabled()


def test_category_chart_uses_unique_consistent_colors_for_every_category(dashboard, categories):
    page, store, month, _ = dashboard
    original = next(tx for tx in store.state()["transactions"] if tx["id"] == "1")
    extended_categories = categories + ["Categoria adicional A", "Categoria adicional B"]
    store.configure({**store.preferences().to_dict(), "categories": extended_categories})
    for index, category in enumerate(extended_categories):
        event_id = f"legend-{index}"
        store.import_message(event_id, [{
            **original, "id": event_id, "message_id": event_id,
            "day": month + "-06", "category": category, "amount_minor": 100,
        }])
    page.reload()
    expect(page.locator("main")).to_have_attribute("aria-busy", "false")
    labels = page.locator("#category-chart .legend-label").all_text_contents()
    assert set(labels) == set(extended_categories)
    month_colors = page.locator("#category-chart .legend-row").evaluate_all("""rows =>
        Object.fromEntries(rows.map(row => {
            const label = row.querySelector(".legend-label");
            return [label.lastElementChild.textContent, getComputedStyle(label.firstElementChild).backgroundColor];
        }))
    """)
    assert len(set(month_colors.values())) == len(extended_categories)
    page.locator("#tab-year").click()
    year_colors = page.locator("#year-category-legend .legend-label").evaluate_all("""labels =>
        Object.fromEntries(labels.map(label => [
            label.lastElementChild.textContent,
            getComputedStyle(label.firstElementChild).backgroundColor
        ]))
    """)
    assert month_colors == year_colors


def test_primary_buttons_have_readable_contrast_in_both_themes(dashboard):
    page, _, _, _ = dashboard
    for theme in ["light", "dark"]:
        if theme == "dark":
            page.locator("#theme-toggle").click()
        contrast = page.locator("#coverage-form button").evaluate("""button => {
            const luminance = color => {
                const channels = color.match(/[\\d.]+/g).slice(0, 3).map(Number)
                    .map(value => value / 255)
                    .map(value => value <= .04045 ? value / 12.92 : ((value + .055) / 1.055) ** 2.4);
                return channels[0] * .2126 + channels[1] * .7152 + channels[2] * .0722;
            };
            const style = getComputedStyle(button);
            const values = [luminance(style.color), luminance(style.backgroundColor)];
            return (Math.max(...values) + .05) / (Math.min(...values) + .05);
        }""")
        assert contrast >= 4.5, (theme, contrast)


def test_closing_configuration_returns_keyboard_focus(dashboard):
    page, _, _, _ = dashboard
    page.locator("#edit-config-button").click()
    page.locator("#close-config-button").click()
    assert page.locator("#edit-config-button").evaluate("button => button === document.activeElement")


@pytest.mark.parametrize("decision,label,category", [
    ("investment_withdrawal", "Retiro de inversión", "Retiros de inversión"),
    ("personal", "Gasto personal", "Retiro en efectivo"),
])
def test_withdrawal_buttons_persist_and_apply_correct_expense_totals(
    dashboard, decision, label, category,
):
    page, store, _, _ = dashboard
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
    expect(page.locator("main")).to_have_attribute("aria-busy", "false")
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
        calendar_labels = page.locator("#calendar .day-cell[aria-label]").evaluate_all(
            "items => items.map(item => item.getAttribute('aria-label'))"
        )
        assert any(re.search(r"\b2[.\s]?000,59", label) for label in calendar_labels)
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
    page.locator("#decision-history > summary").click()
    # The three movements share a merchant, so they form one destination card.
    page.locator("#history-list .group-card > details > summary").click()
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


def test_configuration_form_saves_all_step_zero_answers(dashboard):
    page, store, _, _ = dashboard
    with store.connect() as db:
        db.execute("DELETE FROM settings")
    page.reload()
    expect(page.locator("main")).to_have_attribute("aria-busy", "false")
    host = page.locator("#setup-form-host")
    host.locator("[name=location]").fill("Ciudad de prueba")
    host.locator("[name=timezone]").fill("America/Bogota")
    host.locator("[name=currency]").select_option("COP")
    host.locator("[name=accounts]").fill("Tarjeta A")
    host.locator("[name=excluded_accounts]").fill("Cuenta B")
    host.locator("[name=transfer_label]").fill("Entre mis cuentas")
    host.locator("[name=approved_senders]").fill("avisos@example.test")
    category_field = host.locator("[name=categories]")
    assert category_field.input_value() == ""
    category_field.fill("Compras\nVacaciones")
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
    assert saved.categories == ["Compras", "Vacaciones"]
    assert saved.ask_savings and saved.ask_people


def test_configuration_edit_preserves_existing_decisions(dashboard):
    page, store, _, _ = dashboard
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


def test_year_category_legend_sits_under_the_chart(dashboard):
    page, _, _, _ = dashboard
    page.locator("#tab-year").click()
    chart = page.locator("#year-chart-svg").bounding_box()
    legend = page.locator("#year-category-legend").bounding_box()
    assert legend["y"] >= chart["y"] + chart["height"] - 1
    assert legend["width"] > chart["width"] * .8  # it spans the card instead of a side column


def test_pending_card_hides_when_everything_is_decided_and_the_rest_share_one_row(dashboard):
    page, store, _, _ = dashboard
    for panel in ("#month-metrics", "#year-metrics"):
        if panel == "#year-metrics":
            page.locator("#tab-year").click()
        expect(page.locator(f"{panel} .metric.hero.action")).to_have_count(1)
    page.get_by_role("tab", name=re.compile("Decisi")).click()
    with page.expect_response(
        lambda response: response.url.endswith("/api/decision") and response.request.method == "POST"
    ) as saved:
        page.locator("#pending-list").get_by_role("button", name="Familiar", exact=True).click()
    assert saved.value.status == 200
    page.reload()
    expect(page.locator("main")).to_have_attribute("aria-busy", "false")
    for tab, panel in (("#tab-month", "#month-metrics"), ("#tab-year", "#year-metrics")):
        page.locator(tab).click()
        heroes = page.locator(f"{panel} .metric.hero")
        expect(heroes).to_have_count(3)
        assert "Por decidir" not in page.locator(panel).inner_text()
        tops = {round(heroes.nth(index).bounding_box()["y"]) for index in range(3)}
        assert len(tops) == 1, (panel, tops)


def test_a_new_card_number_is_asked_in_decisions_with_the_option_of_a_new_card(dashboard):
    page, store, _, _ = dashboard

    def card(product: str, suffix: str, start: str) -> dict:
        return {
            "identity": f"{product}|{suffix}|{start}", "owner": "santiago", "product": product, "suffix": suffix,
            "period_start": start, "period_end": start[:8] + "28", "movements": 0, "matched": 0, "missing": 0,
            "balance_minor": None,
        }

    store.record_statements([
        card("TARJETA_VISA", "0002", "2026-01-01"), card("TARJETA_VISA", "0002", "2026-02-01"),
        card("TARJETA_AMEX", "0005", "2026-01-01"), card("TARJETA_VISA", "0009", "2026-03-01"),
    ], [])
    store.update_reconciled_coverage(date(2026, 4, 15))
    page.reload()
    expect(page.locator("main")).to_have_attribute("aria-busy", "false")
    page.locator("#tab-decisions").click()
    review = page.locator("#card-review")
    expect(review).to_be_visible()
    assert "Visa *0009" in review.inner_text()
    assert "2026-03" in review.inner_text()
    assert review.locator("option").all_inner_texts() == [
        "Es la Visa *0002 con número nuevo, último extracto 2026-02",
        "Es la Amex *0005 con número nuevo, último extracto 2026-01",
        "Es una tarjeta de crédito nueva",
    ]
    # Cancelling the confirmation saves nothing.
    page.once("dialog", lambda dialog: dialog.dismiss())
    review.get_by_role("button", name=re.compile("Guardar")).click()
    assert len(store.state()["card_questions"]) == 1
    review.locator("select").select_option("TARJETA_VISA:0002")
    page.once("dialog", lambda dialog: dialog.accept())
    with page.expect_response(lambda response: response.url.endswith("/api/card-series")) as saved:
        review.get_by_role("button", name=re.compile("Guardar")).click()
    assert saved.value.status == 200
    review.wait_for(state="hidden")
    with store.connect() as db:
        assert db.execute("SELECT replaces FROM card_series WHERE suffix='0009'").fetchone()[0] == "TARJETA_VISA:0002"


def test_rule_panel_buttons_and_fields_are_readable_in_both_themes(dashboard):
    page, _, _, _ = dashboard
    page.route("**/api/rules/suggest", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body='{"suggestions":[{"pattern":"FLYPASS PEAJE","category":"Compras","count":2}]}',
    ))
    page.get_by_role("tab", name=re.compile("Decisi")).click()
    page.locator("#rules-detail > summary").click()
    page.get_by_role("button", name="Sugerir reglas").click()
    expect(page.get_by_role("button", name="Aprobar regla")).to_be_visible()
    expect(page.get_by_label("El comercio contiene").last).to_have_value("FLYPASS PEAJE")
    for theme in ["light", "dark"]:
        if theme == "dark":
            page.locator("#theme-toggle").click()
        ratios = page.locator(
            "#rules-detail button:visible, #rules-detail input:visible, #rules-detail select:visible",
        ).evaluate_all(r"""controls => controls.map(control => {
            const luminance = color => {
                const channels = color.match(/[\d.]+/g).slice(0, 3).map(Number)
                    .map(value => value / 255)
                    .map(value => value <= .04045 ? value / 12.92 : ((value + .055) / 1.055) ** 2.4);
                return channels[0] * .2126 + channels[1] * .7152 + channels[2] * .0722;
            };
            let host = control, background = "rgba(0, 0, 0, 0)";
            while (host && /rgba\(.*, 0\)$/.test(background)) {
                background = getComputedStyle(host).backgroundColor;
                host = host.parentElement;
            }
            const values = [luminance(getComputedStyle(control).color), luminance(background)];
            return [control.textContent.trim() || control.value, (Math.max(...values) + .05) / (Math.min(...values) + .05)];
        })""")
        assert len(ratios) >= 7
        assert all(ratio >= 4.5 for _, ratio in ratios), (theme, ratios)


def test_ollama_category_suggestion_is_preselected_and_saved_only_on_decision(dashboard):
    page, store, month, _ = dashboard
    store.import_message("stmt", [
        {
            "id": f"stmt:{index}", "message_id": "stmt", "day": f"{month}-0{index}",
            "merchant": "FLYPASS PEAJE", "account": "Tarjeta A", "amount_minor": 1000000,
            "currency": "COP", "category": "Compras", "kind": "purchase", "decision": None,
            "reason": "Extracto.",
        } for index in (6, 7)
    ])
    suggestions = {key: {"category": "Transporte", "source": "history"} for key in ("stmt:6", "stmt:7")}

    def answer(route):
        # Stands in for the server with Ollama: it saves the suggestions, then answers.
        store.save_category_suggestions(suggestions, replace=True)
        route.fulfill(status=200, content_type="application/json", body=json.dumps({"by_id": suggestions}))

    page.route("**/api/category-suggestions", answer)
    page.reload()
    expect(page.locator("main")).to_have_attribute("aria-busy", "false")
    page.get_by_role("tab", name=re.compile("Decisi")).click()
    page.get_by_role("button", name="Volver a sugerir categorías").click()
    group_select = page.locator("#pending-list .group-card").filter(has_text="FLYPASS PEAJE").get_by_label(
        re.compile("Categoría para los 2 movimientos"),
    )
    expect(group_select).to_have_value("Transporte")
    expect(page.get_by_text("Sugerida por tus decisiones anteriores").first).to_be_visible()
    assert all(row["decision"] is None for row in store.state()["transactions"] if row["id"].startswith("stmt:"))
    page.get_by_role("button", name=re.compile("^Familiar: 2 movimientos")).click()
    expect(page.get_by_role("status").filter(has_text="2 movimientos")).to_be_visible()
    saved = {row["id"]: (row["decision"], row["category"]) for row in store.state()["transactions"]}
    assert saved["stmt:6"] == saved["stmt:7"] == ("family", "Transporte")


def test_group_category_can_be_set_without_suggestions_or_kept_per_movement(dashboard):
    page, store, month, _ = dashboard
    def purchase(event_id, merchant, category, day):
        return movement(event_id, merchant, message_id="g", day=f"{month}-0{day}", amount_minor=1000000, category=category)
    store.import_message("g", [
        purchase("same:1", "PEAJE NORTE", "Compras", 6), purchase("same:2", "PEAJE NORTE", "Compras", 7),
        purchase("mixed:1", "TIENDA SOL", "Compras", 6), purchase("mixed:2", "TIENDA SOL", "Transporte", 7),
    ])
    page.reload()
    expect(page.locator("main")).to_have_attribute("aria-busy", "false")
    page.get_by_role("tab", name=re.compile("Decisi")).click()
    same = page.locator(".group-card").filter(has_text="PEAJE NORTE")
    mixed = page.locator(".group-card").filter(has_text="TIENDA SOL")
    expect(same.get_by_label(re.compile("Categoría para los 2 movimientos"))).to_have_value("Compras")
    expect(mixed.get_by_label(re.compile("Categoría para los 2 movimientos"))).to_have_value("")

    same.get_by_label(re.compile("Categoría para los 2 movimientos")).select_option("Transporte")
    same.get_by_role("button", name=re.compile("^Gasto personal: 2 movimientos")).click()
    expect(page.get_by_role("status").filter(has_text="2 movimientos")).to_be_visible()
    mixed = page.locator(".group-card").filter(has_text="TIENDA SOL")
    mixed.get_by_role("button", name=re.compile("^Familiar: 2 movimientos")).click()
    expect(page.locator(".group-card").filter(has_text="TIENDA SOL")).to_have_count(1)

    saved = {row["id"]: (row["decision"], row["category"]) for row in store.state()["transactions"]}
    assert saved["same:1"] == saved["same:2"] == ("personal", "Transporte")
    # "Keep each one's" sends no category, so each movement keeps its own.
    assert saved["mixed:1"] == ("family", "Compras") and saved["mixed:2"] == ("family", "Transporte")


def test_group_category_shows_for_transfers_but_not_for_investments(dashboard):
    page, store, month, _ = dashboard
    def of_kind(event_id, merchant, kind, day):
        return movement(event_id, merchant, message_id="k", day=f"{month}-0{day}", amount_minor=1000000, kind=kind)
    store.import_message("k", [
        of_kind("tr:1", "TRASLADO A NEQUI", "transfer", 6), of_kind("tr:2", "TRASLADO A NEQUI", "transfer", 7),
        of_kind("inv:1", "TRASLADO A FONDO", "investment", 6), of_kind("inv:2", "TRASLADO A FONDO", "investment", 7),
    ])
    page.reload()
    expect(page.locator("main")).to_have_attribute("aria-busy", "false")
    page.get_by_role("tab", name=re.compile("Decisi")).click()
    label = re.compile("Categoría para los 2 movimientos")
    transfer = page.locator("#pending-list .group-card").filter(has_text="TRASLADO A NEQUI")
    investment = page.locator("#pending-list .group-card").filter(has_text="TRASLADO A FONDO")
    expect(investment.get_by_label(label)).to_have_count(0)
    transfer.get_by_label(label).select_option("Retiro en efectivo")
    transfer.get_by_role("button", name=re.compile("^Familiar: 2 movimientos")).click()
    expect(page.locator("#pending-list .group-card").filter(has_text="TRASLADO A NEQUI")).to_have_count(0)

    saved = {row["id"]: (row["decision"], row["category"]) for row in store.state()["transactions"]}
    assert saved["tr:1"] == saved["tr:2"] == ("family", "Retiro en efectivo")


def test_saved_suggestions_survive_a_reload_and_owner_counts_filter_the_view(dashboard):
    page, store, month, _ = dashboard
    def purchase(event_id, merchant, day):
        return movement(event_id, merchant, day=f"{month}-0{day}", amount_minor=1000000)
    store.import_message("p-s", [purchase("s1", "PEAJE NORTE", 6)])
    store.import_message("p-m", [purchase("m1", "TIENDA SOL", 7)], owner="manuela")
    store.save_category_suggestions({"s1": {"category": "Transporte", "source": "history"}}, replace=True)
    page.reload()
    expect(page.locator("main")).to_have_attribute("aria-busy", "false")
    page.get_by_role("tab", name=re.compile("Decisi")).click()
    card = page.locator("#pending-list .decision-card").filter(has_text="PEAJE NORTE")
    expect(card.get_by_label(re.compile("Categoría para PEAJE NORTE"))).to_have_value("Transporte")
    expect(card.get_by_text("Sugerida por tus decisiones anteriores")).to_be_visible()

    counts = page.get_by_role("group", name="Pendientes por titular")
    expect(counts.get_by_role("button", name=re.compile("^Manuela: 1 movimiento"))).to_be_visible()
    counts.get_by_role("button", name=re.compile("^Manuela:")).click()
    expect(page.locator("#owner-switch button[data-owner='manuela']")).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#pending-list").get_by_text("PEAJE NORTE")).to_have_count(0)
    expect(page.locator("#pending-list").get_by_text("TIENDA SOL")).to_be_visible()


def test_focus_moves_to_the_next_pending_card_after_a_decision(dashboard):
    page, store, month, _ = dashboard
    store.import_message("f", [{
        "id": f"f{index}", "message_id": "f", "day": f"{month}-0{index}", "merchant": f"COMERCIO {name}",
        "account": "Tarjeta A", "amount_minor": amount, "currency": "COP", "category": "Compras",
        "kind": "purchase", "decision": None, "reason": "Aviso.",
    } for index, name, amount in ((2, "UNO", 9000000), (3, "DOS", 8000000))])
    page.reload()
    expect(page.locator("main")).to_have_attribute("aria-busy", "false")
    page.get_by_role("tab", name=re.compile("Decisi")).click()
    first = page.locator("#pending-list > article").first
    expect(first).to_contain_text("COMERCIO UNO")
    first.get_by_role("button", name="Gasto personal").first.click()
    expect(page.locator("#pending-list > article").first).to_contain_text("COMERCIO DOS")
    focused = page.evaluate("document.activeElement && document.activeElement.textContent")
    assert focused == "COMERCIO DOS"


def test_owner_count_buttons_are_readable_and_large_enough_in_both_themes(dashboard):
    page, _, _, _ = dashboard
    page.get_by_role("tab", name=re.compile("Decisi")).click()
    page.locator("#owner-switch button[data-owner='santiago']").click()
    for theme in ["light", "dark"]:
        if theme == "dark":
            page.locator("#theme-toggle").click()
        found = page.locator(".owner-progress .chip").evaluate_all(r"""chips => chips.map(chip => {
            const luminance = color => color.match(/[\d.]+/g).slice(0, 3).map(Number).map(v => v / 255)
                .map(v => v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4)
                .reduce((sum, v, i) => sum + v * [.2126, .7152, .0722][i], 0);
            let host = chip, background = "rgba(0, 0, 0, 0)";
            while (host && /rgba\(.*, 0\)$/.test(background)) {
                background = getComputedStyle(host).backgroundColor;
                host = host.parentElement;
            }
            const values = [luminance(getComputedStyle(chip).color), luminance(background)];
            return [(Math.max(...values) + .05) / (Math.min(...values) + .05), chip.getBoundingClientRect().height];
        })""")
        assert len(found) == 2
        assert all(ratio >= 4.5 and height >= 38 for ratio, height in found), (theme, found)


def test_pending_destinations_are_ordered_oldest_first(dashboard):
    page, store, month, _ = dashboard
    store.import_message("o", [{
        "id": f"o{day}", "message_id": "o", "day": f"{month}-0{day}", "merchant": name,
        "account": "Tarjeta A", "amount_minor": amount, "currency": "COP", "category": "Compras",
        "kind": "purchase", "decision": None, "reason": "Aviso.",
    } for day, name, amount in ((8, "RECIENTE GRANDE", 90000000), (2, "ANTIGUO PEQUENO", 100), (4, "MEDIO", 500))])
    page.reload()
    expect(page.locator("main")).to_have_attribute("aria-busy", "false")
    page.get_by_role("tab", name=re.compile("Decisi")).click()
    titles = page.locator("#pending-list > article h3").all_inner_texts()
    order = [title for title in titles if title in {"RECIENTE GRANDE", "ANTIGUO PEQUENO", "MEDIO"}]
    assert order == ["ANTIGUO PEQUENO", "MEDIO", "RECIENTE GRANDE"]
