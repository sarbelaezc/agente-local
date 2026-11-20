"""The monthly close panel from another device of the home network, in a real browser."""
from __future__ import annotations

import threading

import pytest

from monthly_close.server import create_server
from shared.lan_access import LanAccess

# A second loopback address stands in for the computer's address on the home network.
NETWORK_ADDRESS = "127.0.0.2"


@pytest.fixture
def network_panel(make_store, tmp_path):
    shown: list[str] = []
    local = create_server(make_store(), 0)
    lan = LanAccess("cierre", announce=shown.append, folder=tmp_path / "red", address=NETWORK_ADDRESS, port=0)
    network = lan.serve(local.RequestHandlerClass)
    thread = threading.Thread(target=network.serve_forever, kwargs={"poll_interval": 0.05})
    thread.start()
    yield f"https://{NETWORK_ADDRESS}:{network.server_port}", shown
    network.shutdown()
    thread.join(timeout=5)
    network.server_close()
    local.server_close()


def test_a_device_pairs_with_the_terminal_code_and_then_uses_the_panel(network_panel, new_page):
    url, shown = network_panel
    # The test address is outside what the home authority vouches for, so the browser is told
    # to accept it; tests/shared/test_lan_access.py checks the certificate itself.
    page = new_page(ignore_https_errors=True)
    page.goto(url + "/")
    assert page.url == url + "/emparejar"
    page.get_by_label("Nombre de este dispositivo").fill("Celular")
    page.get_by_role("button", name="Pedir código").click()
    page.get_by_label("Código de la terminal").wait_for()
    assert "Celular" in shown[-1]
    page.get_by_label("Código de la terminal").fill(shown[-1].split("Código: ")[1][:9])
    page.get_by_role("button", name="Entrar").click()
    page.wait_for_url(url + "/")
    page.wait_for_selector("text=Cierre mensual")
    [cookie] = page.context.cookies()
    assert cookie["name"] == "__Host-sesion-cierre" and cookie["httpOnly"] and cookie["secure"]
    assert cookie["sameSite"] == "Strict"
    # The page's own calls carry the session and the run's token: state loads.
    state = page.evaluate("fetch('/api/state').then((r) => r.status)")
    assert state == 200
