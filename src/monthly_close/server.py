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


def create_server(store: Store, port: int = 8766, model: CategoryModel | None = None) -> HTTPServer:
    """Serve the dashboard on loopback only, with strict Host/Origin and CSRF checks.

    `model` is the optional local Ollama client used only to suggest rules; it never writes.
    """
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
                    currency, exchange_rate = raw.get("currency"), raw.get("exchange_rate")
                    if currency is not None and not isinstance(currency, str):
                        raise ValueError("Moneda invalida.")
                    if exchange_rate is not None and not isinstance(exchange_rate, str):
                        raise ValueError("Tasa invalida: debe enviarse como texto decimal.")
                    store.decide(
                        event_id, decision, category,
                        currency=currency, exchange_rate=exchange_rate,
                    )
                elif self.path == "/api/decision-group":
                    owner, group, decision = raw.get("owner"), raw.get("group"), raw.get("decision")
                    if not all(isinstance(value, str) for value in (owner, group, decision)):
                        raise ValueError("Decision por destino invalida.")
                    scope, remember = raw.get("scope", "pending"), raw.get("remember", False)
                    if scope not in {"pending", "all"} or type(remember) is not bool:
                        raise ValueError("Alcance de la decision invalido.")
                    category = raw.get("category")
                    if category is not None and not isinstance(category, str):
                        raise ValueError("Categoria invalida.")
                    store.decide_group(
                        owner, group, decision, pending_only=scope == "pending", remember=remember,
                        category=category,
                    )
                elif self.path == "/api/between-owners":
                    owner, group = raw.get("owner"), raw.get("group")
                    value, remember = raw.get("value"), raw.get("remember", False)
                    if not isinstance(owner, str) or not isinstance(group, str) or type(remember) is not bool:
                        raise ValueError("Marca entre titulares invalida.")
                    store.mark_between_owners(owner, group, value, remember=remember)
                elif self.path == "/api/group-rules/delete":
                    owner, group = raw.get("owner"), raw.get("group")
                    if not isinstance(owner, str) or not isinstance(group, str):
                        raise ValueError("Regla de destino invalida.")
                    store.delete_group_rule(owner, group)
                elif self.path == "/api/rules":
                    pattern, category, decision = (
                        raw.get("pattern"), raw.get("category"), raw.get("decision"),
                    )
                    if not all(isinstance(value, str) for value in (pattern, category, decision)):
                        raise ValueError("Regla invalida.")
                    store.add_rule(pattern, category, decision)
                elif self.path in {"/api/rules/suggest", "/api/category-suggestions"}:
                    if model is None:
                        raise ValueError("Ollama no esta configurado: revisa runtime.json.")
                    if self.path == "/api/rules/suggest":
                        self.json_response(200, {"suggestions": suggest_rules(store, model)})
                    else:
                        self.json_response(200, {"by_id": suggest_pending_categories(store, model)})
                    return
                elif self.path == "/api/rules/delete":
                    if type(raw.get("id")) is not int:
                        raise ValueError("Regla invalida.")
                    store.delete_rule(raw["id"])
                elif self.path == "/api/failure/dismiss":
                    if not isinstance(raw.get("message_id"), str):
                        raise ValueError("Aviso invalido.")
                    store.dismiss_failure(raw["message_id"])
                elif self.path == "/api/card-series":
                    owner, product, suffix, replaces = (
                        raw.get("owner"), raw.get("product"), raw.get("suffix"), raw.get("replaces"),
                    )
                    if not all(isinstance(value, str) for value in (owner, product, suffix)) or not (
                        replaces is None or isinstance(replaces, str)
                    ):
                        raise ValueError("Respuesta de tarjeta invalida.")
                    store.decide_card_series(owner, product, suffix, replaces)
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
