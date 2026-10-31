from hashlib import sha256

import pytest
from playwright.sync_api import sync_playwright

from mail_invoices import movement
from mail_invoices.outlook_folders import MailFolder


@pytest.mark.parametrize("changed_body", [False, True])
def test_real_locators_persist_before_click_and_verify_destination(monkeypatch, changed_body):
    html = """
    <div role="treeitem" id="destination" aria-level="2" title="Salud" data-folder-name="salud"
        onclick="this.innerText='Salud seleccionado'; document.querySelector('#message').hidden=false;">Salud</div>
    <div role="option" id="message" data-convid="TEST" aria-selected="false"
         onclick="this.setAttribute('aria-selected','true')">
        <span title="sender@example.invalid">Sender</span><span class="TtcXM">Factura TEST</span>
    </div>
    <div role="document" aria-label="Cuerpo del mensaje">Consulta</div>
    <button aria-label="Mover a" onclick="document.querySelector('#menu').hidden=false">Mover a</button>
    <div role="menu" id="menu" hidden>
        <button role="menuitem" onclick="document.querySelector('#dialog').hidden=false">Seleccionar una carpeta diferente</button>
    </div>
    <div role="dialog" id="dialog" hidden>
        <div role="treeitem" aria-level="1">
            <div role="group">
                <div role="treeitem" aria-level="2" tabindex="-1" data-fui-tree-item-value="SALUD"
                    onclick="this.setAttribute('tabindex','0')"><div title="Salud">Salud</div>
                </div>
            </div>
        </div>
        <button onclick="window.moveClicks++; document.querySelector('#dialog').hidden=true;
             document.querySelector('#message').hidden=true">Mover</button>
    </div>
    <script>window.moveClicks=0;</script>
    """
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content(html)
            monkeypatch.setattr(movement, "discover_folders", lambda page: [MailFolder("Salud", "salud", 0)])
            monkeypatch.setattr(movement, "reopen_outlook", lambda *args: None)
            message = {
                "conversation": "TEST", "sender": "sender@example.invalid",
                "subject": "Factura TEST", "eligible": True,
                "body_digest": sha256(("Changed" if changed_body else "Consulta").encode()).hexdigest(),
            }
            events = []

            def before_move():
                assert page.evaluate("window.moveClicks") == 0
                events.append("persisted")

            if changed_body:
                with pytest.raises(LookupError, match="body changed"):
                    movement.move_registered_message(
                        page, message, "Salud", approved=True, before_move=before_move,
                    )
                assert not events
                assert page.evaluate("window.moveClicks") == 0
            else:
                movement.move_registered_message(
                    page, message, "Salud", approved=True, before_move=before_move,
                )
                assert events == ["persisted"]
                assert page.evaluate("window.moveClicks") == 1
                assert message["movement_status"] == "verified"
        finally:
            browser.close()
