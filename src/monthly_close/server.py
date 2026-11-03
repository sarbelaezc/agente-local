from __future__ import annotations

import json
import secrets
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from importlib.resources import files
from zoneinfo import ZoneInfoNotFoundError

from monthly_close.store import Store


def create_server(store: Store, port: int = 8766) -> HTTPServer:
    """Serve the dashboard on loopback only, with strict Host/Origin and CSRF checks."""
    csrf = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def valid_host(self) -> bool:
            return self.headers.get("Host") == f"127.0.0.1:{server.server_port}"

        def respond(self, status: int, payload: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                "connect-src 'self'; img-src 'self' data:; base-uri 'none'; "
                "form-action 'none'; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(payload)

        def json_response(self, status: int, value: dict) -> None:
            self.respond(
                status, json.dumps(value, ensure_ascii=False).encode(), "application/json; charset=utf-8",
            )

        def do_GET(self) -> None:
            if not self.valid_host():
                self.json_response(403, {"error": "Host no autorizado."})
                return
            if self.path == "/":
                self.respond(
                    200, files("monthly_close").joinpath("dashboard.html").read_bytes(),
                    "text/html; charset=utf-8",
                )
            elif self.path == "/api/state":
                self.json_response(200, {**store.state(), "csrf": csrf})
            else:
                self.json_response(404, {"error": "Ruta inexistente."})

        def do_POST(self) -> None:
            origin = f"http://127.0.0.1:{server.server_port}"
            if (
                not self.valid_host() or self.headers.get("Origin") != origin
                or not secrets.compare_digest(
                    self.headers.get("X-CSRF-Token", "").encode(), csrf.encode(),
                )
                or self.headers.get("Content-Type", "").split(";")[0] != "application/json"
                or self.headers.get("Transfer-Encoding") is not None
            ):
                self.json_response(403, {"error": "Solicitud local no autorizada."})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 32000:
                    raise ValueError("Tamano de solicitud invalido.")
                raw = json.loads(self.rfile.read(length))
                if not isinstance(raw, dict):
                    raise ValueError("Solicitud JSON invalida.")
                if self.path == "/api/config":
                    store.configure(raw)
                elif self.path == "/api/decision":
                    event_id, decision = raw.get("id"), raw.get("decision")
                    if not isinstance(event_id, str) or not isinstance(decision, str):
                        raise ValueError("Decision invalida.")
                    category = raw.get("category")
                    if category is not None and not isinstance(category, str):
                        raise ValueError("Categoria invalida.")
                    store.decide(event_id, decision, category)
                elif self.path == "/api/coverage":
                    if raw.get("status") != "reviewed":
                        raise ValueError("Solo se permite confirmar una revision manual.")
                    store.coverage(raw.get("month", ""), "reviewed", raw.get("note", ""))
                else:
                    self.json_response(404, {"error": "Ruta inexistente."})
                    return
            except (ValueError, TypeError, ZoneInfoNotFoundError) as exc:
                self.json_response(400, {"error": str(exc)})
                return
            self.json_response(200, {"ok": True})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.timeout = 1
    return server
