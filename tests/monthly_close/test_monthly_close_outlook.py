from __future__ import annotations

from contextlib import contextmanager
from datetime import date

import pytest
from playwright.sync_api import sync_playwright

from monthly_close import main as app
from monthly_close.domain import CATEGORIES, Preferences
from monthly_close.outlook import Candidate, OutlookError, guard_page
from monthly_close.store import Store

URL = "https://outlook.live.com/mail/0/"
HTML = """
<!doctype html><html><body>
<input id="search" placeholder="Buscar">
<button id="all-folders">Todas las carpetas</button>
<div id="active" aria-selected="true">Todas las carpetas</div>
<div id="results" role="listbox" style="height:100px;overflow:auto">
  <div role="option" data-itemid="message-1" aria-selected="false"
    onclick="this.setAttribute('aria-selected','true');document.querySelector('#pane').setAttribute('data-itemid','message-1')">
    <span email="avisos@example.test">Banco</span><span class="subject">Compra Tienda</span>
  </div>
</div>
<section id="pane" data-itemid="old-message">
  <div class="thread">
    <span email="avisos@example.test">Banco</span><h2 class="subject">Compra Tienda</h2>
    <time datetime="2026-03-01T02:15:00Z">Fecha local</time>
    <div role="document">Compraste en Tienda $ 1.234,56.</div>
  </div>
</section>
</body></html>
"""

@pytest.fixture
def page():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        context.route("**/*", lambda route: route.fulfill(body=HTML, content_type="text/html"))
        page = context.new_page()
        page.set_default_timeout(300)
        page.goto(URL)
        try:
            yield page
        finally:
            browser.close()


def test_authentication_redirect_stops(page):
    page.goto("https://login.live.com/")
    with pytest.raises(OutlookError, match="Autenticacion"):
        guard_page(page, "outlook.live.com")


def test_mfa_inside_outlook_stops(page):
    page.locator("body").evaluate(
        "el => { const input=document.createElement('input'); input.name='otc'; el.append(input); }"
    )
    with pytest.raises(OutlookError, match="MFA"):
        guard_page(page, "outlook.live.com")


def test_scan_rejects_sender_before_opening_body(page, tmp_path, monkeypatch):
    preferences = Preferences(
        "Prueba", "America/Bogota", "COP", ["Tarjeta A"], [], "Familiar",
        True, True, ["avisos@example.test"], CATEGORIES, True,
    )
    @contextmanager
    def fake_browser(data_dir, settings):
        yield page

    class NoModel:
        def __init__(self, *args):
            pass

        def complete_json(self, system, user):
            pytest.fail("No debe procesarse contenido de un remitente no autorizado.")

    monkeypatch.setattr(app, "browser", fake_browser)
    monkeypatch.setattr(app, "OllamaJsonModel", NoModel)
    monkeypatch.setattr(app, "require_model", lambda settings: None)
    monkeypatch.setattr(
        app.OutlookSourceReader, "walk_folders",
        lambda reader, limit, visit: visit(Candidate("message-1", "other@example.test", "Compra")),
    )
    monkeypatch.setattr(
        app.OutlookSourceReader, "read",
        lambda *args: pytest.fail("No debe abrirse el cuerpo no autorizado."),
    )
    settings = {
        "folder_scan_verified": True, "outlook_url": URL,
        "ollama_url": "http://127.0.0.1:11436", "ollama_model": "fake", "max_messages": 10,
    }
    store = Store(tmp_path / "test.sqlite3")
    with pytest.raises(OutlookError, match="no autorizado"):
        app.scan(
            store, tmp_path, settings, preferences, date(2026, 2, 1), date(2026, 3, 1),
        )
    assert page.locator("#results [role=option]").get_attribute("aria-selected") == "false"
    assert store.state()["transactions"] == []
    assert store.state()["coverage"][0]["status"] == "partial"
