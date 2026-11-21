from __future__ import annotations

import secrets
from datetime import datetime
from http.server import HTTPServer, ThreadingHTTPServer
from importlib.resources import files
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from shared.local_model import LocalModelError
from shared.local_panel import LocalPanelHandler
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
    """Serve the dashboard on loopback only, with the checks every local panel shares.

    `model` is the optional local Ollama client used only to suggest rules; it never writes.
    """
    token = secrets.token_urlsafe(32)

    def configured_model() -> CategoryModel:
        if model is None:
            raise ValueError("Ollama no esta configurado: revisa runtime.json.")
        return model

    class Handler(LocalPanelHandler):
        csrf = token

        def do_GET(self) -> None:
            if not self.allowed_get():
                return
            path = self.route
            if path == "/":
                self.respond(
                    200, files("monthly_close").joinpath("dashboard.html").read_bytes(),
                    "text/html; charset=utf-8",
                )
            elif path == "/api/state":
                self.json_response(200, {**store.state(), "csrf": token})
            else:
                self.json_response(404, {"error": "Ruta inexistente."})

        def do_POST(self) -> None:
            if not self.allowed_post():
                return
            try:
                raw = self.read_json()
                if self.route == "/api/config":
                    store.configure(raw)
                elif self.route == "/api/decision":
                    event_id, decision = required_texts(raw, "id", "decision", error="Decision invalida.")
                    store.decide(
                        event_id, decision, optional_text(raw, "category", "Categoria invalida."),
                        currency=optional_text(raw, "currency", "Moneda invalida."),
                        exchange_rate=optional_text(raw, "exchange_rate", "Tasa invalida: debe enviarse como texto decimal."),
                    )
                elif self.route == "/api/decision-group":
                    owner, group, decision = required_texts(
                        raw, "owner", "group", "decision", error="Decision por destino invalida.",
                    )
                    scope, remember = raw.get("scope", "pending"), raw.get("remember", False)
                    if scope not in {"pending", "all"} or type(remember) is not bool:
                        raise ValueError("Alcance de la decision invalido.")
                    store.decide_group(
                        owner, group, decision, pending_only=scope == "pending", remember=remember,
                        category=optional_text(raw, "category", "Categoria invalida."),
                        exchange_rate=optional_text(raw, "exchange_rate", "Tasa invalida: debe enviarse como texto decimal."),
                    )
                elif self.route == "/api/between-owners":
                    owner, group = required_texts(raw, "owner", "group", error="Marca entre titulares invalida.")
                    remember = raw.get("remember", False)
                    if type(remember) is not bool:
                        raise ValueError("Marca entre titulares invalida.")
                    store.mark_between_owners(owner, group, raw.get("value"), remember=remember)
                elif self.route == "/api/group-rules/delete":
                    store.delete_group_rule(*required_texts(raw, "owner", "group", error="Regla de destino invalida."))
                elif self.route == "/api/rules":
                    store.add_rule(*required_texts(raw, "pattern", "category", "decision", error="Regla invalida."))
                elif self.route == "/api/rules/suggest":
                    self.json_response(200, {"suggestions": suggest_rules(store, configured_model())})
                    return
                elif self.route == "/api/category-suggestions":
                    suggestions = suggest_pending_categories(store, configured_model())
                    store.save_category_suggestions(suggestions, replace=True)
                    self.json_response(200, {"by_id": suggestions})
                    return
                elif self.route == "/api/rules/delete":
                    if type(raw.get("id")) is not int:
                        raise ValueError("Regla invalida.")
                    store.delete_rule(raw["id"])
                elif self.route == "/api/failure/dismiss":
                    store.dismiss_failure(*required_texts(raw, "message_id", error="Aviso invalido."))
                elif self.route == "/api/card-series":
                    error = "Respuesta de tarjeta invalida."
                    owner, product, suffix = required_texts(raw, "owner", "product", "suffix", error=error)
                    store.decide_card_series(owner, product, suffix, optional_text(raw, "replaces", error))
                elif self.route == "/api/coverage":
                    if raw.get("status") != "reviewed":
                        raise ValueError("Solo se permite confirmar una revision manual.")
                    store.coverage(raw.get("month", ""), "reviewed", raw.get("note", ""))
                else:
                    self.json_response(404, {"error": "Ruta inexistente."})
                    return
                if self.route in RECONCILE_AFTER:
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
