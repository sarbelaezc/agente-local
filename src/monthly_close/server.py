from __future__ import annotations

import json
import secrets
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from importlib.resources import files
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from shared.local_model import LocalModelError
from monthly_close.rule_suggestions import CategoryModel, suggest_pending_categories, suggest_rules
from monthly_close.store import Store

# Changes that can complete or reopen a month, so its reconciliation is checked again.
RECONCILE_AFTER = {"/api/decision", "/api/decision-group", "/api/failure/dismiss", "/api/card-series"}


def required_texts(raw: dict, *names: str, error: str) -> tuple:
    """The named fields of a request, all text; otherwise the request is refused."""
    values = tuple(raw.get(name) for name in names)
    if not all(isinstance(value, str) for value in values):
        raise ValueError(error)
    return values


def optional_text(raw: dict, name: str, error: str) -> str | None:
    """An optional request field: missing, or text."""
    value = raw.get(name)
    if value is not None and not isinstance(value, str):
        raise ValueError(error)
    return value


def create_server(store: Store, port: int = 8766, model: CategoryModel | None = None) -> HTTPServer:
    """Serve the dashboard on loopback only, with strict Host/Origin and CSRF checks.

    `model` is the optional local Ollama client used only to suggest rules; it never writes.
    """
    csrf = secrets.token_urlsafe(32)

    def configured_model() -> CategoryModel:
        if model is None:
            raise ValueError("Ollama no esta configurado: revisa runtime.json.")
        return model

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

        def discard_body(self) -> None:
            """Read and drop a rejected request's body, within the usual limit.

            Closing a socket with unread data makes Windows reset the connection, so the
            client would see a network error instead of the 403. Nothing read is used.
            """
            self.close_connection = True
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return
            if 0 < length <= 32000 and self.headers.get("Transfer-Encoding") is None:
                self.rfile.read(length)

        def json_response(self, status: int, value: dict) -> None:
            self.respond(
                status, json.dumps(value, ensure_ascii=False).encode(), "application/json; charset=utf-8",
            )

        def do_GET(self) -> None:
            if not self.valid_host():
                self.json_response(403, {"error": "Host no autorizado."})
                return
            path = urlsplit(self.path).path
            if path == "/":
                self.respond(
                    200, files("monthly_close").joinpath("dashboard.html").read_bytes(),
                    "text/html; charset=utf-8",
                )
            elif path == "/api/state":
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
                self.discard_body()
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
                    event_id, decision = required_texts(raw, "id", "decision", error="Decision invalida.")
                    store.decide(
                        event_id, decision, optional_text(raw, "category", "Categoria invalida."),
                        currency=optional_text(raw, "currency", "Moneda invalida."),
                        exchange_rate=optional_text(raw, "exchange_rate", "Tasa invalida: debe enviarse como texto decimal."),
                    )
                elif self.path == "/api/decision-group":
                    owner, group, decision = required_texts(
                        raw, "owner", "group", "decision", error="Decision por destino invalida.",
                    )
                    scope, remember = raw.get("scope", "pending"), raw.get("remember", False)
                    if scope not in {"pending", "all"} or type(remember) is not bool:
                        raise ValueError("Alcance de la decision invalido.")
                    store.decide_group(
                        owner, group, decision, pending_only=scope == "pending", remember=remember,
                        category=optional_text(raw, "category", "Categoria invalida."),
                    )
                elif self.path == "/api/between-owners":
                    owner, group = required_texts(raw, "owner", "group", error="Marca entre titulares invalida.")
                    remember = raw.get("remember", False)
                    if type(remember) is not bool:
                        raise ValueError("Marca entre titulares invalida.")
                    store.mark_between_owners(owner, group, raw.get("value"), remember=remember)
                elif self.path == "/api/group-rules/delete":
                    store.delete_group_rule(*required_texts(raw, "owner", "group", error="Regla de destino invalida."))
                elif self.path == "/api/rules":
                    store.add_rule(*required_texts(raw, "pattern", "category", "decision", error="Regla invalida."))
                elif self.path == "/api/rules/suggest":
                    self.json_response(200, {"suggestions": suggest_rules(store, configured_model())})
                    return
                elif self.path == "/api/category-suggestions":
                    suggestions = suggest_pending_categories(store, configured_model())
                    store.save_category_suggestions(suggestions, replace=True)
                    self.json_response(200, {"by_id": suggestions})
                    return
                elif self.path == "/api/rules/delete":
                    if type(raw.get("id")) is not int:
                        raise ValueError("Regla invalida.")
                    store.delete_rule(raw["id"])
                elif self.path == "/api/failure/dismiss":
                    store.dismiss_failure(*required_texts(raw, "message_id", error="Aviso invalido."))
                elif self.path == "/api/card-series":
                    error = "Respuesta de tarjeta invalida."
                    owner, product, suffix = required_texts(raw, "owner", "product", "suffix", error=error)
                    store.decide_card_series(owner, product, suffix, optional_text(raw, "replaces", error))
                elif self.path == "/api/coverage":
                    if raw.get("status") != "reviewed":
                        raise ValueError("Solo se permite confirmar una revision manual.")
                    store.coverage(raw.get("month", ""), "reviewed", raw.get("note", ""))
                else:
                    self.json_response(404, {"error": "Ruta inexistente."})
                    return
                if self.path in RECONCILE_AFTER:
                    store.update_reconciled_coverage(
                        datetime.now(ZoneInfo(store.preferences().timezone)).date(),
                    )
            except (ValueError, TypeError, ZoneInfoNotFoundError, LocalModelError) as exc:
                self.json_response(400, {"error": str(exc)})
                return
            self.json_response(200, {"ok": True})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.timeout = 1
    return server
