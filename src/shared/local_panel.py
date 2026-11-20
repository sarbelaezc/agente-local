"""Security every local web panel shares, so both features enforce exactly the same checks.

A panel listens on 127.0.0.1. Every request must name that exact Host; every POST
must also come from that exact Origin, carry the run's CSRF token, send a JSON body
within the panel's size limit and use no chunked encoding. Responses forbid caching,
type sniffing, referrers, framing and any external resource.

With `-Red`, the same handler also serves an HTTPS server on the private network
address (shared.lan_access). There the Host and Origin are that address, and every
request needs a paired device's session cookie, except the few that pair a device.
The device's name is kept in `remote_device` while its request runs, for auditing.
"""
from __future__ import annotations

import json
import secrets
from contextvars import ContextVar
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler
from importlib.resources import files
from urllib.parse import urlsplit

from shared.lan_access import LanAccess, LanError, clean_device_name, fingerprint

PANEL_CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
# The paired device behind the current request; None when it comes from this computer.
remote_device: ContextVar[str | None] = ContextVar("remote_device", default=None)
AUTHORITY_PATH = "/certificado-red-de-casa.crt"
# What an unpaired device may reach on the network server: the pairing page and its calls.
PAIRING_GETS = {"/emparejar", "/api/pairing/info", AUTHORITY_PATH}
PAIRING_POSTS = {"/api/pairing/request", "/api/pairing/confirm"}


class LocalPanelHandler(BaseHTTPRequestHandler):
    """Base request handler: a panel sets csrf and max_body and writes its own routes."""

    csrf = ""
    max_body = 32_000

    def log_message(self, format: str, *args: object) -> None:
        pass

    @property
    def route(self) -> str:
        """The request path without its query string."""
        return urlsplit(self.path).path

    @property
    def lan(self) -> LanAccess | None:
        """The network access of this request's server; None on the 127.0.0.1 server."""
        return getattr(self.server, "lan_access", None)

    def valid_host(self) -> bool:
        return self.headers.get("Host") == f"{self.server.server_address[0]}:{self.server.server_port}"

    def valid_origin(self) -> bool:
        scheme = "https" if self.lan else "http"
        return self.headers.get("Origin") == f"{scheme}://{self.server.server_address[0]}:{self.server.server_port}"

    def paired_device(self) -> str | None:
        """The device whose unexpired session cookie this request carries."""
        try:
            morsel = SimpleCookie(self.headers.get("Cookie", "")).get(self.lan.cookie)
        except CookieError:
            return None
        return self.lan.devices.device_for(morsel.value if morsel else "")

    def respond(
        self, status: int, payload: bytes, content_type: str,
        csp: str = PANEL_CSP, extra: dict | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", csp)
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(payload)

    def json_response(self, status: int, value: dict) -> None:
        self.respond(status, json.dumps(value, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def allowed_get(self) -> bool:
        """True when the panel should answer; otherwise the request is already answered.

        On the network server, an unpaired device only reaches the pairing page.
        """
        if not self.valid_host():
            self.json_response(403, {"error": "Host no autorizado."})
            return False
        if self.lan is None:
            remote_device.set(None)
            return True
        if self.route in PAIRING_GETS:
            self.pairing_get()
            return False
        device = self.paired_device()
        if device is not None:
            remote_device.set(device)
            return True
        if self.route == "/":
            self.respond(303, b"", "text/plain; charset=utf-8", extra={"Location": "/emparejar"})
        else:
            self.json_response(401, {"error": "Empareja este dispositivo con el panel."})
        return False

    def allowed_post(self) -> bool:
        """True for a POST with exact Host and Origin, the CSRF token and a plain JSON body.

        On the network server it must also carry a paired device's session; only the
        pairing calls go without one (and without the token, which needs a session).
        Otherwise answer 403 after reading and dropping the body: closing a socket with
        unread data makes Windows reset the connection, and the client would see a network
        error instead of the refusal.
        """
        plain = (
            self.valid_host() and self.valid_origin()
            and self.headers.get("Content-Type", "").split(";")[0] == "application/json"
            and self.headers.get("Transfer-Encoding") is None
        )
        if plain and self.lan is not None and self.route in PAIRING_POSTS:
            self.pairing_post()
            return False
        device = self.paired_device() if plain and self.lan is not None else None
        if (
            plain and (self.lan is None or device is not None)
            # A panel that forgot its token must refuse everything, not accept an empty one.
            and bool(self.csrf)
            and secrets.compare_digest(self.headers.get("X-CSRF-Token", "").encode(), self.csrf.encode())
        ):
            remote_device.set(device)
            return True
        self.close_connection = True
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if 0 < length <= self.max_body and self.headers.get("Transfer-Encoding") is None:
            self.rfile.read(length)
        self.json_response(403, {"error": "Solicitud local no autorizada."})
        return False

    def read_json(self) -> dict:
        """The request's JSON object, within the size limit; ValueError otherwise."""
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length <= self.max_body:
            raise ValueError("Tamaño de solicitud inválido.")
        raw = json.loads(self.rfile.read(length))
        if not isinstance(raw, dict):
            raise ValueError("Solicitud JSON inválida.")
        return raw

    # -------------------------------------------------------------- pairing (network server)

    def pairing_get(self) -> None:
        if self.route == "/emparejar":
            self.respond(200, files("shared").joinpath("pairing.html").read_bytes(), "text/html; charset=utf-8")
        elif self.route == AUTHORITY_PATH:
            self.respond(
                200, self.lan.authority_pem, "application/x-x509-ca-cert",
                extra={"Content-Disposition": "attachment; filename=red-de-casa.crt"},
            )
        else:
            self.json_response(200, {"panel": self.lan.panel, "fingerprint": fingerprint(self.lan.authority)})

    def pairing_post(self) -> None:
        """Ask for a code shown in this computer's terminal, or trade it for a session."""
        client = self.client_address[0]
        try:
            raw = self.read_json()
            if self.route == "/api/pairing/request":
                self.lan.pairing.request(clean_device_name(raw.get("name")), client, self.lan.panel)
                self.json_response(200, {"ok": True})
                return
            token = self.lan.devices.pair(self.lan.pairing.confirm(raw.get("code"), client))
        except (LanError, ValueError) as error:
            self.json_response(400, {"error": str(error)})
            return
        self.respond(
            200, json.dumps({"ok": True}).encode(), "application/json; charset=utf-8",
            extra={"Set-Cookie": self.lan.session_cookie(token)},
        )
