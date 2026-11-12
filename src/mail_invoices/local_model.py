from __future__ import annotations

import json
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
MAX_RESPONSE_CHARS = 20000


class LocalModelError(RuntimeError):
    pass


class JsonModel(Protocol):
    def complete_json(self, system: str, user: str) -> dict[str, Any]:
        """Return the model answer as a JSON object."""
        ...


def validate_local_url(url: str) -> str:
    cleaned = url.rstrip("/")
    parsed = urlparse(cleaned)
    if parsed.scheme != "http" or parsed.hostname not in LOCAL_HOSTS:
        raise ValueError("Ollama URL must be local (127.0.0.1, localhost, or ::1)")
    return cleaned


class OllamaJsonModel:
    """Local Ollama client. It has no tools and its output is treated as untrusted."""

    def __init__(self, url: str, model: str, timeout_seconds: float = 120.0) -> None:
        self._url = validate_local_url(url)
        self._model = model
        self._timeout = timeout_seconds

    def complete_json(self, system: str, user: str) -> dict[str, Any]:
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "format": "json",
            "options": {"temperature": 0, "num_ctx": 8192},
        }
        try:
            # trust_env=False keeps invoice text away from any configured HTTP proxy.
            with httpx.Client(timeout=self._timeout, trust_env=False) as client:
                response = client.post(f"{self._url}/api/chat", json=payload)
                response.raise_for_status()
            content = response.json().get("message", {}).get("content")
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            raise LocalModelError("Local Ollama request failed; is Ollama running?") from exc
        if not isinstance(content, str) or len(content) > MAX_RESPONSE_CHARS:
            raise LocalModelError("Local model returned an invalid response")
        try:
            value = json.loads(content)
        except json.JSONDecodeError as exc:
            raise LocalModelError("Local model did not return JSON") from exc
        if not isinstance(value, dict):
            raise LocalModelError("Local model did not return a JSON object")
        return value
