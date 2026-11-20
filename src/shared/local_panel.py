"""Security every local web panel shares, so both features enforce exactly the same checks.

A panel listens on 127.0.0.1 only. Every request must name that exact Host; every POST
must also come from that exact Origin, carry the run's CSRF token, send a JSON body
within the panel's size limit and use no chunked encoding. Responses forbid caching,
type sniffing, referrers, framing and any external resource.
"""
from __future__ import annotations

import json
import secrets
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlsplit

PANEL_CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


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

    def valid_host(self) -> bool:
        return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

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
        """True for a request to this panel's exact Host; otherwise answer 403."""
        if self.valid_host():
            return True
        self.json_response(403, {"error": "Host no autorizado."})
        return False

    def allowed_post(self) -> bool:
        """True for a POST with exact Host and Origin, the CSRF token and a plain JSON body.

        Otherwise answer 403 after reading and dropping the body: closing a socket with
        unread data makes Windows reset the connection, and the client would see a network
        error instead of the refusal.
        """
        if (
            self.valid_host() and self.headers.get("Origin") == f"http://127.0.0.1:{self.server.server_port}"
            # A panel that forgot its token must refuse everything, not accept an empty one.
            and bool(self.csrf)
            and secrets.compare_digest(self.headers.get("X-CSRF-Token", "").encode(), self.csrf.encode())
            and self.headers.get("Content-Type", "").split(";")[0] == "application/json"
            and self.headers.get("Transfer-Encoding") is None
        ):
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
