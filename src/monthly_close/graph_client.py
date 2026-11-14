from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import httpx

from monthly_close.outlook import OutlookError

GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
# Read-only: this scope cannot change, move, delete or mark messages.
SCOPES = ["Mail.Read"]
AUTHORITIES = {"consumers", "common"}
CLIENT_ID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
CACHE_NAME = "graph_token_cache.bin"
RETRYABLE = {429, 503, 504}


class GraphError(OutlookError):
    """Graph failure; subclasses OutlookError so the CLI stops the same way."""


def validate_graph_url(url: str) -> str:
    """Never send the bearer token anywhere except Microsoft Graph v1.0 over HTTPS."""
    parsed = urlparse(url)
    if (
        parsed.scheme != "https" or parsed.hostname != "graph.microsoft.com"
        or parsed.port not in (None, 443) or parsed.username or parsed.password
        or not parsed.path.startswith("/v1.0/")
    ):
        raise GraphError("Solo se permite Microsoft Graph v1.0 por HTTPS; no se envia el token a otros destinos.")
    return url


def _retry_after(response) -> float:
    try:
        return max(0.0, min(float(response.headers.get("Retry-After", "5")), 60.0))
    except (TypeError, ValueError):
        return 5.0


def _describe(response) -> str:
    """Graph error code and message; these describe the API call, never message content."""
    try:
        error = response.json().get("error", {})
        code, text = str(error.get("code", "")), str(error.get("message", ""))
    except (ValueError, AttributeError):
        code, text = "", ""
    return f"Graph respondio {response.status_code}" + (f" ({code}: {text[:200]})" if code or text else ".")


class GraphClient:
    """Minimal read-only Graph client: GET only, bounded retries, host allow-list."""

    def __init__(
        self, token: Callable[[], str], http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep, retries: int = 3,
    ) -> None:
        self._token = token
        self._http = http or httpx.Client(timeout=30, follow_redirects=False)
        self._sleep = sleep
        self._retries = retries

    def close(self) -> None:
        self._http.close()

    def _get(self, url: str, params: dict | None):
        validate_graph_url(url)
        for attempt in range(self._retries + 1):
            try:
                response = self._http.get(
                    url, params=params, headers={"Authorization": "Bearer " + self._token()},
                )
            except httpx.HTTPError as exc:
                raise GraphError("No se pudo contactar con Microsoft Graph.") from exc
            if response.status_code in RETRYABLE and attempt < self._retries:
                self._sleep(_retry_after(response))
                continue
            break
        if response.status_code != 200:
            raise GraphError(_describe(response))
        return response

    def get_json(self, url: str, params: dict | None = None) -> dict:
        try:
            data = self._get(url, params).json()
        except ValueError as exc:
            raise GraphError("Graph devolvio una respuesta que no es JSON.") from exc
        if not isinstance(data, dict):
            raise GraphError("Graph devolvio una respuesta JSON inesperada.")
        return data

    def get_bytes(self, url: str) -> bytes:
        return self._get(url, None).content


def _msal_app(client_id: str, authority: str, cache_path: Path):
    try:
        import msal
    except ImportError as exc:
        raise GraphError("Falta el paquete msal. Ejecuta: pip install -e \".[dev]\"") from exc
    if not CLIENT_ID.fullmatch(client_id) or authority not in AUTHORITIES:
        raise GraphError("graph_client_id o graph_authority invalidos.")
    cache = msal.SerializableTokenCache()
    if cache_path.is_file():
        cache.deserialize(cache_path.read_text(encoding="utf-8"))
    app = msal.PublicClientApplication(
        client_id, authority=f"https://login.microsoftonline.com/{authority}", token_cache=cache,
    )
    return app, cache


def _save(cache, cache_path: Path) -> None:
    if cache.has_state_changed:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = cache_path.with_suffix(".tmp")
        temporary.write_text(cache.serialize(), encoding="utf-8")
        os.replace(temporary, cache_path)


def silent_token(client_id: str, authority: str, cache_path: Path) -> Callable[[], str]:
    """Return tokens from the local cache only; never opens a browser unattended."""
    app, cache = _msal_app(client_id, authority, cache_path)

    def token() -> str:
        accounts = app.get_accounts()
        if len(accounts) != 1:
            raise GraphError("Sesion de Microsoft ausente o ambigua. Ejecuta el comando login.")
        result = app.acquire_token_silent(SCOPES, account=accounts[0])
        _save(cache, cache_path)
        if not result or "access_token" not in result:
            raise GraphError("Sesion de Microsoft vencida. Ejecuta el comando login.")
        return result["access_token"]

    return token


def interactive_login(client_id: str, authority: str, cache_path: Path) -> str:
    """Interactive sign-in in the user's browser; the agent never handles the password."""
    app, cache = _msal_app(client_id, authority, cache_path)
    result = app.acquire_token_interactive(SCOPES)
    _save(cache, cache_path)
    if "access_token" not in result:
        raise GraphError(f"No se completo el inicio de sesion: {result.get('error', 'desconocido')}.")
    accounts = app.get_accounts()
    return accounts[0]["username"] if len(accounts) == 1 else "cuenta sin identificar"
