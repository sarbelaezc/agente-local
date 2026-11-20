"""The security both local panels share: one implementation, used unchanged by each."""
from __future__ import annotations

import json
import threading
from http.server import ThreadingHTTPServer

import httpx
import pytest

from mail_invoices.review_server import ReviewPanel
from monthly_close.server import create_server
from monthly_close.store import Store
from shared.local_panel import LocalPanelHandler

CHECKS = ("valid_host", "respond", "json_response", "allowed_get", "allowed_post", "read_json", "route")


def test_both_panels_use_the_shared_checks_without_overriding_them(tmp_path):
    monthly = create_server(Store(tmp_path / "close.sqlite3"), 0)
    invoices = ReviewPanel(lambda: {"messages": []}, {}, open_browser=lambda url: None)
    try:
        for handler in (monthly.RequestHandlerClass, invoices._server.RequestHandlerClass):
            assert issubclass(handler, LocalPanelHandler)
            assert not set(CHECKS) & set(vars(handler)), handler
            assert handler.csrf  # each panel run has its own token
    finally:
        monthly.server_close()
        invoices._server.server_close()


@pytest.fixture
def echo():
    """A minimal panel that answers its route and body, to test the shared checks alone."""
    class Handler(LocalPanelHandler):
        csrf = "token-de-prueba"
        max_body = 100

        def do_POST(self) -> None:
            if not self.allowed_post():
                return
            try:
                self.json_response(200, {"route": self.route, "body": self.read_json()})
            except ValueError as error:
                self.json_response(400, {"error": str(error)})

    def start(csrf: str = "token-de-prueba"):
        handler = type("Panel", (Handler,), {"csrf": csrf})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
        thread.start()
        started.append((server, thread))
        return f"http://127.0.0.1:{server.server_port}"

    started: list = []
    yield start
    for server, thread in started:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def post(url: str, body: str, token: str = "token-de-prueba", path: str = "/api/eco?x=1") -> httpx.Response:
    headers = {"Origin": url, "X-CSRF-Token": token, "Content-Type": "application/json"}
    return httpx.post(url + path, content=body, headers=headers, trust_env=False)


def test_routes_ignore_the_query_and_bodies_respect_the_panel_limit(echo):
    url = echo()
    answer = post(url, json.dumps({"a": 1}))
    assert answer.status_code == 200 and answer.json() == {"route": "/api/eco", "body": {"a": 1}}
    too_big = post(url, json.dumps({"a": "x" * 200}))
    assert too_big.status_code == 400 and too_big.json()["error"] == "Tamaño de solicitud inválido."
    assert post(url, "[1]").json()["error"] == "Solicitud JSON inválida."


def test_a_panel_without_its_token_refuses_every_post(echo):
    url = echo(csrf="")
    assert post(url, "{}", token="").status_code == 403
