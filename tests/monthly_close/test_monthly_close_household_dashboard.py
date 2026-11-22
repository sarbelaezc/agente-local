"""Household panel flow in Chromium, with synthetic data only."""
from __future__ import annotations

import re
from datetime import datetime

import pytest
from playwright.sync_api import expect

from builders import file_imported


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
def household(make_store, start_panel, new_page):
    store = make_store()
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
    file_imported(store, [
        member("m1", 20000, "purchase", "personal", category_uncertain=1),
        member("m22", 5000, "card_payment", "third_party_payment", category="Pagos de otras personas"),
        member("m333", 3000, "transfer", None, category="Otros gastos", group_key="tr:55550009999"),
        member("m4444", 7000, "transfer", None, category="Otros gastos", group_key="tr:55550009999"),
    ])
    url = start_panel(store)
    page = new_page(viewport={"width": 1280, "height": 1000})
    page.set_default_timeout(3000)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(f"{url}/?month={MONTH}&currency=COP")
    expect(page.locator("main")).to_have_attribute("aria-busy", "false")
    yield page, store, errors
    # Every panel test also proves the page raised no JavaScript error.
    assert not errors, f"Errores de JavaScript en el panel: {errors}"


def spend(page) -> str:
    return page.locator("#month-metrics .metric").filter(has_text="Gasto del mes").inner_text()


def choose(page, owner: str) -> None:
    page.locator("#owner-switch").get_by_role("button", name=owner, exact=True).click()


def test_owner_switch_separates_members_and_household_adds_them(household):
    page, store, _ = household
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


def test_between_members_mark_leaves_money_out_of_household_only(household):
    page, store, _ = household
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


def test_year_view_shows_income_for_each_member_and_household(household):
    page, store, _ = household
    store.import_message("s3", [{
        "id": "s3", "message_id": "s3", "day": f"{MONTH}-15", "merchant": "EMPRESA SINTETICA",
        "account": "Tarjeta A", "amount_minor": 300000, "currency": "COP", "category": "Nómina",
        "kind": "income", "decision": "income", "reason": "Revisar.",
    }])
    file_imported(store, [member("m55555", 50000, "income", "income", category="Nómina")])
    page.reload()
    expect(page.locator("main")).to_have_attribute("aria-busy", "false")
    page.locator("#tab-year").click()
    for owner, income in [("Hogar", r"3[.\s]?500,00"), ("Santiago", r"3[.\s]?000,00"), ("Manuela", r"\b500,00")]:
        choose(page, owner)
        metric = page.locator("#year-metrics .metric").filter(has_text="Ingresos del año").inner_text()
        assert re.search(income, metric), (owner, metric)
        assert page.locator("#year-income-chart rect").count() >= 1, owner
        assert re.search(income, page.locator("#year-table").inner_text()), owner


def test_year_summary_capitalizes_months_and_leaves_months_without_data_blank(household):
    page, store, _ = household
    page.clock.set_fixed_time(datetime(2026, 11, 16, 12))  # January elapsed, December still to come
    page.reload()
    expect(page.locator("main")).to_have_attribute("aria-busy", "false")
    page.locator("#tab-year").click()
    rows = page.locator("#year-table tbody tr")
    expect(rows).to_have_count(12)
    march = rows.nth(2).locator("td")
    assert march.first.inner_text() == "Marzo de 2026"
    # A month with data shows a dash for the kinds it did not have; the balance is always computed.
    assert march.nth(1).inner_text() != "—"  # spending
    assert march.nth(2).inner_text() != "—"  # personal spending
    assert [march.nth(index).inner_text() for index in (5, 7, 8)] == ["—", "—", "—"]  # income, investments, cash
    assert re.fullmatch(r"[+−]?[\d.\s]+,\d{2}", march.nth(6).inner_text())  # balance, currency in the caption
    assert "Importes en COP" in page.locator("#year-table caption").inner_text()
    # An elapsed month without movements shows dashes, never 0,00.
    january = rows.nth(0).inner_text()
    assert january.startswith("Enero de 2026")
    assert "—" in january and "0,00" not in january
    # A month still to come waits for the bank's notices.
    december = rows.nth(11)
    assert december.locator("td").nth(1).inner_text() == "Sin notificaciones del banco aún"
    assert "0,00" not in december.inner_text()


def test_month_view_is_interactive_like_manuelas_dashboard(household):
    page, store, _ = household
    for event_id, day, merchant, amount, kind, decision, category in [
        ("u1", "20", "UBER RIDES", 15000, "purchase", "personal", "Transporte"),
        ("u2", "21", "UBER *TRIP", 25000, "purchase", "personal", "Transporte"),
        ("s3", "15", "EMPRESA SINTETICA", 300000, "income", "income", "Nómina"),
    ]:
        store.import_message(event_id, [{
            "id": event_id, "message_id": event_id, "day": f"{MONTH}-{day}", "merchant": merchant,
            "account": "Tarjeta A", "amount_minor": amount, "currency": "COP", "category": category,
            "kind": kind, "decision": decision, "reason": "Sintetico.",
        }])
    page.reload()
    expect(page.locator("main")).to_have_attribute("aria-busy", "false")
    tip = page.locator("#tip")

    # Instant tooltip on a chart slice, and on keyboard focus of a calendar day.
    # Point at the top of the ring (viewBox 240 × 190, center 120,95, radius 54–84).
    box = page.locator("#category-chart-svg").bounding_box()
    page.mouse.move(box["x"] + box["width"] * 121 / 240, box["y"] + box["height"] * 25 / 190)
    expect(tip).to_be_visible()
    assert re.search(r"(Transporte|Compras): .*COP \(", tip.inner_text())
    day = page.locator('#calendar button.day-cell[data-tip*=", 2 "]')
    day.focus()
    expect(tip).to_contain_text(re.compile(r"1[.\s]?000,00"))

    # A day opens its movements and closes on a second click.
    day.click()
    detail = page.locator("#day-detail")
    expect(detail).to_be_visible()
    assert "TIENDA S" in detail.inner_text() and "Personal" in detail.inner_text()
    expect(page.locator("#calendar button.day-cell.selected")).to_have_count(1)
    page.locator("#calendar button.day-cell.selected").click()
    expect(detail).to_be_hidden()
    assert page.locator("#calendar div.day-cell:not(.empty)").count() > 0  # days without spending stay plain

    # Rankings have proportional bars and group the same merchant written two ways.
    merchants = page.locator("#merchant-ranking").inner_text()
    assert "Uber · 2" in merchants and "UBER RIDES" not in merchants
    assert page.locator("#weekday-chart .bar-row").count() == 7
    assert page.locator("#merchant-ranking .bar-fill").count() >= 2

    # The income card takes you to its list in «Ingresos por tipo», opened and in view.
    income = page.locator("#month-metrics .metric").filter(has_text="Ingresos").first
    assert income.locator("summary").count() == 0
    income.get_by_role("button", name="Ver el ingreso").click()
    listing = page.locator("#income-card .inline-detail")
    expect(listing).to_have_attribute("open", "")
    expect(listing.get_by_text("EMPRESA SINTETICA")).to_be_in_viewport()
    expect(listing.locator("summary")).to_be_focused()
    # Third-party payments open from their tile.
    spending = page.locator("#month-metrics .metric").filter(has_text="Gasto del mes")
    assert spending.locator("summary").count() == 0  # the spending card only shows the subtraction
    paid_back = page.locator("#month-metrics .metric.minor").filter(has_text="Abonos de terceros")
    paid_back.locator("summary", has_text="Ver el abono").click()
    assert "Pagos de otras personas" in paid_back.inner_text()


def test_months_live_under_the_tabs_and_only_in_the_month_view(household):
    page, store, _ = household
    chips = page.locator("#month-chips")
    expect(chips).to_be_visible()
    assert chips.bounding_box()["y"] > page.locator(".tabs").bounding_box()["y"]
    # Chronological, short, with the year where it changes.
    months = chips.locator("button").evaluate_all("items => items.map(item => item.dataset.month)")
    assert months[0] == MONTH
    assert months == sorted(months)
    expect(chips.locator("button").first).to_have_attribute("aria-label", "Marzo de 2026")
    assert chips.locator("button").first.inner_text().startswith("Mar")
    assert "2026" in chips.locator("button").first.inner_text()
    assert "2026" not in chips.locator("button").nth(1).inner_text()  # same year: no repeat
    expect(chips.locator('button[aria-pressed="true"]')).to_have_attribute("data-month", MONTH)

    # The Decision tab shows how many destinations are pending, from any tab.
    expect(page.locator("#decision-badge")).to_have_text("1")
    expect(page.locator("#tab-decisions")).to_have_attribute("aria-label", "Decisión, 1 destino por decidir")

    page.locator("#tab-year").click()
    expect(chips).to_be_hidden()
    expect(page.locator("#coverage-summary")).to_be_hidden()
    page.locator("#tab-decisions").click()
    expect(chips).to_be_hidden()

    # A month of the year view opens the month tab on it.
    page.locator(f'#month-chips button:not([data-month="{MONTH}"])').first.evaluate("button => button.click()")
    page.locator("#tab-year").click()
    page.locator("#year-table").get_by_role("button", name="Ver marzo de 2026 en la pestaña Mes").click()
    expect(page.locator("#tab-month")).to_have_attribute("aria-selected", "true")
    expect(chips.locator('button[aria-pressed="true"]')).to_have_attribute("data-month", MONTH)
    assert f"month={MONTH}" in page.url and "tab=month" in page.url

    # A category of the donut opens its movements in the detail.
    page.locator("#category-chart").get_by_role("button", name=re.compile("^Compras:")).click()
    expect(page.locator('#category-details details[data-category="Compras"]')).to_have_attribute("open", "")


def test_destination_card_decides_every_pending_payment_with_audit(household):
    page, store, _ = household
    page.locator("#tab-decisions").click()
    summary = page.locator("#decision-summary").inner_text()
    assert "1" in summary and "Destino por decidir" in summary
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



def tiles_page(make_store, start_panel, new_page, **options):
    """A month with two secondary tiles side by side: third-party payments and card payments."""
    store = make_store()
    file_imported(store, [
        member("m1", 20000, "purchase", "personal"),
        member("m22", 5000, "card_payment", "third_party_payment", category="Pagos de otras personas"),
        member("m333", 9000, "card_payment", "exclude", category="Pago de tarjeta"),
    ])
    page = new_page(viewport={"width": 1280, "height": 1000}, **options)
    page.goto(f"{start_panel(store)}/?month={MONTH}&currency=COP")
    expect(page.locator("main")).to_have_attribute("aria-busy", "false")
    return page


def open_paid_back(page) -> list[int]:
    """Opens the third-party tile and returns the height of its list over the next frames."""
    return page.evaluate("""async () => {
        const detail = Array.from(document.querySelectorAll('#month-metrics .metric.minor'))
            .find(tile => tile.textContent.includes('Abonos de terceros')).querySelector('details');
        detail.querySelector('summary').click();
        const heights = [];
        for (let frame = 0; frame < 4; frame++) {
            heights.push(Math.round(detail.getBoundingClientRect().height));
            await new Promise(done => setTimeout(done, 50));
        }
        return heights;
    }""")


def tile_animations(page) -> int:
    return page.locator("#month-metrics .metric-strip").evaluate(
        "strip => strip.getAnimations({ subtree: true }).length"
    )


def test_opening_a_tile_list_slides_the_tiles_and_unfolds_the_list(make_store, start_panel, new_page):
    page = tiles_page(make_store, start_panel, new_page)
    assert page.locator("#month-metrics .metric-strip > .metric").count() == 2
    heights = open_paid_back(page)
    # The tiles slide from where they were and the list unfolds from its summary.
    assert tile_animations(page) >= 2
    assert heights == sorted(heights) and heights[0] < heights[-1], heights
    # The state changed at once: the list is already there to read.
    paid_back = page.locator("#month-metrics .metric.minor").filter(has_text="Abonos de terceros")
    expect(paid_back.locator("details")).to_have_attribute("open", "")
    assert "Pagos de otras personas" in paid_back.inner_text()
    page.locator("#tab-year").click()
    assert page.locator("#panel-year").evaluate("panel => panel.getAnimations().length") == 1  # the tab fades in


def test_reduced_motion_changes_the_dashboard_without_animations(make_store, start_panel, new_page):
    page = tiles_page(make_store, start_panel, new_page, reduced_motion="reduce")
    heights = open_paid_back(page)
    assert tile_animations(page) == 0
    assert len(set(heights)) == 1, heights  # the list is at its full height from the first frame
    page.locator("#tab-year").click()
    assert page.locator("#panel-year").evaluate("panel => panel.getAnimations().length") == 0
