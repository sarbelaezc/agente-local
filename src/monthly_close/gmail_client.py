from __future__ import annotations

import os
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from monthly_close.graph_client import GraphClient, GraphError

GMAIL_ROOT = "https://gmail.googleapis.com/gmail/v1/users/me"
# Read-only: this scope cannot send, label, archive or delete messages.
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


class GmailError(GraphError):
    """Gmail failure; the CLI stops the same way as for Microsoft Graph."""


def validate_gmail_url(url: str) -> str:
    """Never send the bearer token anywhere except the Gmail API over HTTPS."""
    parsed = urlparse(url)
    if (
        parsed.scheme != "https" or parsed.hostname != "gmail.googleapis.com"
        or parsed.port not in (None, 443) or parsed.username or parsed.password
        or not parsed.path.startswith("/gmail/v1/users/me/")
    ):
        raise GmailError("Solo se permite la API de Gmail por HTTPS; no se envia el token a otros destinos.")
    return url


class GmailClient(GraphClient):
    """Read-only Gmail API client: GET only, bounded retries, Gmail host allow-list."""

    service = "Gmail"
    error = GmailError

    @staticmethod
    def validate(url: str) -> str:
        return validate_gmail_url(url)


def _google():
    try:
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise GmailError("Falta el paquete google-auth-oauthlib. Ejecuta: pip install -e \".[dev]\"") from exc
    return Credentials, Request, RefreshError, InstalledAppFlow


def _save(credentials, cache_path: Path) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = cache_path.with_suffix(".tmp")
    temporary.write_text(credentials.to_json(), encoding="utf-8")
    os.replace(temporary, cache_path)


def _read_only(credentials) -> None:
    granted = set(getattr(credentials, "granted_scopes", None) or credentials.scopes or [])
    if granted != set(SCOPES):
        raise GmailError("Google concedio permisos distintos de gmail.readonly; no se guarda la sesion.")


def silent_token(cache_path: Path) -> Callable[[], str]:
    """Return tokens from the saved session only; never opens a browser unattended."""
    credentials_type, request_type, refresh_error, _flow = _google()
    if not cache_path.is_file():
        raise GmailError("Sesion de Google ausente. Ejecuta el comando login con ese buzon.")
    try:
        credentials = credentials_type.from_authorized_user_file(str(cache_path), SCOPES)
    except ValueError as exc:
        raise GmailError("Sesion de Google ilegible. Ejecuta el comando login con ese buzon.") from exc

    def token() -> str:
        if not credentials.valid:
            if not credentials.refresh_token:
                raise GmailError("Sesion de Google vencida. Ejecuta el comando login con ese buzon.")
            try:
                credentials.refresh(request_type())
            except refresh_error as exc:
                raise GmailError(
                    "Google no renovo la sesion (vencida o revocada). Ejecuta el comando login con ese buzon."
                ) from exc
            _save(credentials, cache_path)
        return credentials.token

    return token


def interactive_login(client_secrets: Path, cache_path: Path) -> str:
    """Sign in through the person's browser; the agent never handles the password.

    client_secrets is the OAuth desktop client downloaded from Google Cloud; it lives
    in the local data folder, outside Git.
    """
    _credentials, _request, _refresh, flow_type = _google()
    if not client_secrets.is_file():
        raise GmailError(f"Falta {client_secrets.name} en la carpeta de datos local (cliente OAuth de escritorio).")
    flow = flow_type.from_client_secrets_file(str(client_secrets), SCOPES)
    credentials = flow.run_local_server(
        host="localhost", port=0, open_browser=True,
        authorization_prompt_message="Abre este enlace si el navegador no se abrio: {url}",
        success_message="Sesion de Gmail guardada. Puedes cerrar esta pestana.",
    )
    _read_only(credentials)
    _save(credentials, cache_path)
    client = GmailClient(lambda: credentials.token)
    try:
        address = client.get_json(f"{GMAIL_ROOT}/profile").get("emailAddress")
    finally:
        client.close()
    return address if isinstance(address, str) else "cuenta sin identificar"
