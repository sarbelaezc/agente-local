from datetime import date
from unittest.mock import MagicMock

from mail_invoices.inbox import MESSAGE_SELECTOR, _find_message, is_candidate, parse_label

TODAY = date(2026, 10, 31)
NARROW = "\u202f"

BANCO_TODAY = (
    "No leído alertasynotificaciones@ayn.notificacionesbancolombia.com "
    f"Alertas y Notificaciones 9:09{NARROW}AM ¡Listo! Compraste COP50.000,00 en "
    "CAC*DROGUERIA con tu T.Cred *8243. No hay elementos seleccionados"
)
CESANTIAS_FRIDAY = (
    "No leído Tiene datos adjuntos Correo Proteccion Extracto de Cesantias Protección. "
    f"Vie 11:29{NARROW}PM Hola, SANTIAGO Llegó el momento de contarte cómo avanzaron"
)
FACTURA_OLD = (
    "Tiene datos adjuntos Proveedor Factura 2026-0456 15/10/2026 "
    f"4:10{NARROW}PM Adjunto la factura electrónica"
)


def test_time_after_subject_word_stays_undated_and_has_no_attachment():
    message = parse_label(BANCO_TODAY, TODAY)
    assert message.unread is True
    assert message.has_attachment is False
    assert message.received is None


def test_weekday_label_resolves_to_last_matching_day():
    message = parse_label(CESANTIAS_FRIDAY, TODAY)
    assert message.has_attachment is True
    assert message.received == date(2026, 10, 30)


def test_full_date_label_is_parsed():
    message = parse_label(FACTURA_OLD, TODAY)
    assert message.received == date(2026, 10, 15)
    assert message.unread is False


def test_unknown_date_token_stays_undated_instead_of_today():
    message = parse_label(f"Tiene datos adjuntos Proveedor Factura oct 15 3:00{NARROW}PM hola", TODAY)
    assert message.received is None


def test_date_without_time_is_parsed_from_label():
    label = "No leído Tiene datos adjuntos FacturaCTSColombia@cen.biz 811007832;IPS;1349603246;01;IPS 24/10/2026 SANTIAGO Ha recibido una factura"
    message = parse_label(label, TODAY)
    assert message.received == date(2026, 10, 24)
    assert is_candidate(message, TODAY, 30) is True
    assert is_candidate(message, TODAY, 3) is False


def test_candidate_needs_attachment_and_invoice_term():
    assert is_candidate(parse_label(FACTURA_OLD, TODAY), TODAY, 30) is True
    assert is_candidate(parse_label(CESANTIAS_FRIDAY, TODAY), TODAY, 30) is False


def test_candidate_respects_days_window():
    assert is_candidate(parse_label(FACTURA_OLD, TODAY), TODAY, 7) is False
    assert is_candidate(parse_label(FACTURA_OLD, TODAY), TODAY, 30) is True


def test_undated_candidate_is_kept_for_review():
    undated = f"Tiene datos adjuntos Proveedor Factura nueva oct 15 3:00{NARROW}PM hola"
    assert is_candidate(parse_label(undated, TODAY), TODAY, 30) is True


def test_virtualized_message_is_bound_to_identity_not_row_index():
    page = MagicMock()
    options = MagicMock()
    stable = MagicMock()
    stable.count.return_value = 1
    row = page.get_by_role.return_value
    row.count.return_value = 1
    row.get_attribute.return_value = "synthetic-conversation"
    options.evaluate_all.return_value = 4
    page.locator.side_effect = lambda selector: options if selector == MESSAGE_SELECTOR else stable
    result = _find_message(page, "Synthetic invoice label")
    assert result is stable
    page.get_by_role.assert_called_once_with("option", name="Synthetic invoice label", exact=True)
    options.nth.assert_not_called()


def test_lookup_continues_with_constant_rendered_row_count():
    page = MagicMock()
    options = page.locator.return_value
    options.count.return_value = 5
    options.evaluate_all.side_effect = [-1, -1, 2]
    options.last.evaluate.return_value = True
    row = page.get_by_role.return_value
    row.count.return_value = 1
    row.get_attribute.return_value = None
    assert _find_message(page, "Target") is row
    assert options.last.evaluate.call_count == 2


def test_lookup_stops_when_scroll_cannot_progress():
    page = MagicMock()
    options = page.locator.return_value
    options.evaluate_all.return_value = -1
    options.last.evaluate.return_value = False
    assert _find_message(page, "Missing") is None
    assert options.last.evaluate.call_count == 1


def test_real_virtual_list_lookup_reaches_later_rows_without_growing_dom():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content("""
                <div id="list" style="height:100px; overflow:auto" onscroll="render()">
                  <div style="height:500px; position:relative">
                    <div id="row" role="option" aria-label="First" data-convid="first"></div>
                  </div>
                </div>
                <script>
                  function render() {
                    const index = Math.round(document.getElementById('list').scrollTop / 100);
                    const row = document.getElementById('row');
                    row.setAttribute('aria-label', index >= 2 ? 'Target' : 'First');
                    row.setAttribute('data-convid', index >= 2 ? 'target' : 'first');
                    row.style.position = 'absolute';
                    row.style.top = (index * 100 + 40) + 'px';
                    row.innerText = 'Row';
                  }
                  render();
                </script>
            """)
            found = _find_message(page, "Target")
            assert found is not None
            assert found.get_attribute("data-convid") == "target"
            assert page.locator(MESSAGE_SELECTOR).count() == 1
        finally:
            browser.close()
