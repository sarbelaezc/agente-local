from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any

import httpx

from mail_invoices.local_model import MAX_RESPONSE_CHARS, LocalModelError, validate_local_url
from monthly_close.domain import KINDS

EVENT_PROPERTIES = {
    **{key: {"type": "string"} for key in (
        "merchant", "account", "amount_text", "currency", "date_text", "category",
    )},
    "kind": {"type": "string", "enum": sorted(KINDS)},
    "savings": {"type": "boolean"},
}
SCHEMA = {
    "type": "object",
    "properties": {"events": {
        "type": "array", "maxItems": 30,
        "items": {
            "type": "object", "properties": EVENT_PROPERTIES,
            "required": list(EVENT_PROPERTIES), "additionalProperties": False,
        },
    }},
    "required": ["events"], "additionalProperties": False,
}


class OllamaJsonModel:
    """Constrain expense output at generation time; extraction still verifies every fact."""

    def __init__(self, url: str, model: str, timeout_seconds: float = 120.0) -> None:
        self._url = validate_local_url(url)
        self._model = model
        self._timeout = timeout_seconds

    def complete_json(self, system: str, user: str) -> dict[str, Any]:
        context = json.loads(user)
        schema = deepcopy(SCHEMA)
        properties = schema["properties"]["events"]["items"]["properties"]
        properties["category"]["enum"] = context["categories"]
        properties["account"]["enum"] = (
            context["accounts"] + context["excluded_accounts"] + ["Desconocida"]
        )
        properties["currency"]["enum"] = [context["default_currency"]]
        # Constrain explicitly marked amounts without rewriting the source evidence.
        amounts = sorted(set(re.findall(
            rf"(?<!\w)(?:{re.escape(context['default_currency'])}[ \t]*[$€£]?[ \t]*"
            r"|[$€£][ \t]*)\d+(?:[.,]\d+|[ \t]\d{3})*(?!\d)",
            context["untrusted_email"],
        )))
        if amounts:
            properties["amount_text"]["enum"] = amounts
        properties["date_text"]["enum"] = [""] + sorted(set(re.findall(
            r"(?<!\d)(?:\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4})(?!\d)",
            context["untrusted_email"],
        )))
        try:
            with httpx.Client(timeout=self._timeout, trust_env=False) as client:
                response = client.post(self._url + "/api/chat", json={
                    "model": self._model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "stream": False, "format": schema,
                    "options": {"temperature": 0, "num_ctx": 8192},
                })
                response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise LocalModelError("Ollama local no pudo generar la propuesta estructurada.") from exc
        message = payload.get("message") if isinstance(payload, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or len(content) > MAX_RESPONSE_CHARS:
            raise LocalModelError("Ollama devolvio una respuesta estructurada invalida.")
        try:
            value = json.loads(content)
        except ValueError as exc:
            raise LocalModelError("Ollama no devolvio JSON valido.") from exc
        if not isinstance(value, dict):
            raise LocalModelError("Ollama no devolvio un objeto JSON.")
        return value
