"""Opening a panel to the home network: certificate, pairing, sessions and the shared checks."""
from __future__ import annotations

import ipaddress
import sqlite3
import threading

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.x509.verification import PolicyBuilder, Store as TrustStore, VerificationError

from mail_invoices.review_server import ReviewPanel
from monthly_close.store import Store
from shared.lan_access import (
    MAX_FAILURES, SESSION_SECONDS, DeviceRegistry, LanAccess, LanError, Pairing,
    ensure_authority, issue_server_certificate, require_private,
)
from shared.local_panel import LocalPanelHandler, remote_device

# A second loopback address stands in for the computer's address on the home network.
NETWORK_ADDRESS = "127.0.0.2"
TOKEN = "token-de-prueba"


# ------------------------------------------------------------------ certificate


def _verify(authority: x509.Certificate, leaf: x509.Certificate, address: str) -> None:
    verifier = PolicyBuilder().store(TrustStore([authority])).build_server_verifier(
        x509.IPAddress(ipaddress.ip_address(address)),
    )
    verifier.verify(leaf, [])


def test_the_home_authority_vouches_only_for_private_addresses(tmp_path):
    authority = ensure_authority(tmp_path)
    assert ensure_authority(tmp_path).fingerprint(hashes.SHA256()) == authority.fingerprint(hashes.SHA256())
    for address in ("192.168.1.20", "10.0.0.5", "172.16.3.4"):
        cert_path, _ = issue_server_certificate(tmp_path, address)
        _verify(authority, x509.load_pem_x509_certificate(cert_path.read_bytes()), address)
    # Even signed by the same key, a public address is outside the authority's constraints.
    cert_path, _ = issue_server_certificate(tmp_path, "8.8.8.8")
    with pytest.raises(VerificationError, match="constraint"):
        _verify(authority, x509.load_pem_x509_certificate(cert_path.read_bytes()), "8.8.8.8")


@pytest.mark.parametrize("address", ["8.8.8.8", "100.64.0.1", "169.254.1.1", "127.0.0.1"])
def test_only_a_private_network_address_is_accepted(address):
    assert require_private("192.168.0.10") == "192.168.0.10"
    with pytest.raises(LanError):
        require_private(address)


# ------------------------------------------------------------------ pairing and devices


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def test_a_code_works_once_only_for_the_client_that_asked_and_before_it_expires():
    shown, clock = [], Clock()
    pairing = Pairing(shown.append, clock)
    pairing.request("Celular", "192.168.1.30", "cierre")
    code = shown[-1].split("Código: ")[1][:9]
    with pytest.raises(LanError):
        pairing.request("Intruso", "192.168.1.66", "cierre")  # another device while one is pending
    with pytest.raises(LanError):
        pairing.confirm(code, "192.168.1.66")
    assert pairing.confirm(code, "192.168.1.30") == "Celular"
    with pytest.raises(LanError):
        pairing.confirm(code, "192.168.1.30")  # used
    pairing.request("Tableta", "192.168.1.31", "cierre")
    clock.now += 301
    with pytest.raises(LanError):
        pairing.confirm(shown[-1].split("Código: ")[1][:9], "192.168.1.31")


def test_failed_codes_close_pairing_until_the_panel_restarts():
    shown = []
    pairing = Pairing(shown.append)
    pairing.request("Celular", "192.168.1.30", "cierre")
    code = shown[-1].split("Código: ")[1][:9]
    for _ in range(MAX_FAILURES):
        with pytest.raises(LanError):
            pairing.confirm("00000000" if code.replace(" ", "") != "00000000" else "11111111", "192.168.1.30")
    with pytest.raises(LanError, match="bloqueado"):
        pairing.confirm(code, "192.168.1.30")
    with pytest.raises(LanError, match="bloqueado"):
        pairing.request("Celular", "192.168.1.30", "cierre")


def test_sessions_keep_only_a_hash_expire_and_can_be_revoked(tmp_path):
    clock = Clock()
    registry = DeviceRegistry(tmp_path / "dispositivos.json", clock)
    token = registry.pair("Celular")
    other = registry.pair("Tableta")
    assert token not in (tmp_path / "dispositivos.json").read_text(encoding="utf-8")
    assert registry.device_for(token) == "Celular" and registry.device_for("") is None
    assert registry.revoke("Celular") == 1 and registry.device_for(token) is None
    clock.now += SESSION_SECONDS + 1
    assert registry.device_for(other) is None


# ------------------------------------------------------------------ server


@pytest.fixture
def network_panel(tmp_path):
    """An echo panel served on 127.0.0.1 and, like -Red, on the network address."""
    class Handler(LocalPanelHandler):
        csrf = TOKEN

        def do_GET(self) -> None:
            if self.allowed_get():
                self.json_response(200, {"device": remote_device.get()})

        def do_POST(self) -> None:
            if self.allowed_post():
                self.json_response(200, {"device": remote_device.get(), "body": self.read_json()})

    shown: list[str] = []
    lan = LanAccess("cierre", announce=shown.append, folder=tmp_path, address=NETWORK_ADDRESS, port=0)
    server = lan.serve(Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.start()
    yield f"https://{NETWORK_ADDRESS}:{server.server_port}", shown
    server.shutdown()
    thread.join(timeout=5)
    server.server_close()


def _client(url: str) -> httpx.Client:
    # The authority only vouches for private addresses, so the test address is not verified here;
    # test_the_home_authority_vouches_only_for_private_addresses covers the certificate.
    return httpx.Client(base_url=url, verify=False, trust_env=False, headers={"Origin": url})


def _pair(client: httpx.Client, shown: list[str], name: str = "Celular") -> httpx.Response:
    assert client.post("/api/pairing/request", json={"name": name}).status_code == 200
    return client.post("/api/pairing/confirm", json={"code": shown[-1].split("Código: ")[1][:9]})


def test_an_unpaired_device_only_reaches_the_pairing_page(network_panel):
    url, _ = network_panel
    with _client(url) as client:
        home = client.get("/", follow_redirects=False)
        assert home.status_code == 303 and home.headers["location"] == "/emparejar"
        assert client.get("/emparejar").text.startswith("<!doctype html>")
        assert ":" in client.get("/api/pairing/info").json()["fingerprint"]
        assert b"BEGIN CERTIFICATE" in client.get("/certificado-red-de-casa.crt").content
        assert client.get("/api/state").status_code == 401
        # The CSRF token alone is not enough without a paired session.
        assert client.post("/api/decision", json={}, headers={"X-CSRF-Token": TOKEN}).status_code == 403


def test_a_paired_device_gets_a_strict_session_and_its_name_is_audited(network_panel):
    url, shown = network_panel
    with _client(url) as client:
        paired = _pair(client, shown)
        assert paired.status_code == 200
        cookie = paired.headers["set-cookie"]
        for attribute in ("__Host-sesion-cierre=", "HttpOnly", "Secure", "SameSite=Strict", "Path=/"):
            assert attribute in cookie
        assert client.get("/api/state").json() == {"device": "Celular"}
        answer = client.post("/api/decision", json={"a": 1}, headers={"X-CSRF-Token": TOKEN})
        assert answer.json() == {"device": "Celular", "body": {"a": 1}}
        # The session does not replace the other checks.
        assert client.post("/api/decision", json={}).status_code == 403
        assert client.post("/api/decision", json={}, headers={"X-CSRF-Token": TOKEN, "Origin": url.replace("https", "http")}).status_code == 403
        assert client.get("/api/state", headers={"Host": f"192.168.1.20:{url.rsplit(':', 1)[1]}"}).status_code == 403


def test_a_wrong_code_gives_no_session(network_panel):
    url, shown = network_panel
    with _client(url) as client:
        client.post("/api/pairing/request", json={"name": "Celular"})
        code = shown[-1].split("Código: ")[1][:9].replace(" ", "")
        wrong = "0" * 8 if code != "0" * 8 else "1" * 8
        answer = client.post("/api/pairing/confirm", json={"code": wrong})
        assert answer.status_code == 400 and "set-cookie" not in answer.headers
        assert client.get("/api/state").status_code == 401


def test_pairing_calls_still_need_the_exact_origin(network_panel):
    url, _ = network_panel
    with _client(url) as client:
        answer = client.post("/api/pairing/request", json={"name": "Celular"}, headers={"Origin": "https://ejemplo.com"})
        assert answer.status_code == 403


def test_the_invoice_panel_serves_the_network_only_while_it_runs(tmp_path):
    lan = LanAccess("facturas", announce=lambda text: None, folder=tmp_path, address=NETWORK_ADDRESS, port=0)
    panel = ReviewPanel(lambda: {"messages": []}, {}, open_browser=lambda url: None, lan=lan)
    panel.start()
    url = f"https://{NETWORK_ADDRESS}:{panel._network.server_port}"
    with _client(url) as client:
        assert client.get("/api/view").status_code == 401
    panel.stop()
    assert panel._network.socket.fileno() == -1


def test_the_monthly_audit_records_the_device_behind_a_change(tmp_path):
    store = Store(tmp_path / "close.sqlite3")
    token = remote_device.set("Celular")
    try:
        with store.connect() as db:
            Store._audit(db, "decision", "movimiento-1", "family")
    finally:
        remote_device.reset(token)
    with store.connect() as db:
        Store._audit(db, "decision", "movimiento-2", "personal")
    with sqlite3.connect(store.path) as db:
        rows = db.execute("SELECT target, device FROM audit ORDER BY id").fetchall()
    assert rows == [("movimiento-1", "Celular"), ("movimiento-2", None)]
