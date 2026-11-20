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


class GmailSessionError(GmailError):
    """The saved Google session is missing, expired or revoked: only a new sign-in fixes it."""


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
        from google.auth.exceptions import RefreshError, TransportError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow, WSGITimeoutError
    except ImportError as exc:
        raise GmailError("Falta el paquete google-auth-oauthlib. Ejecuta: pip install -e \".[dev]\"") from exc
    return Credentials, Request, (RefreshError, TransportError), (InstalledAppFlow, WSGITimeoutError)


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
    credentials_type, request_type, (refresh_error, transport_error), _flow = _google()
    if not cache_path.is_file():
        raise GmailSessionError("Sesion de Google ausente. Ejecuta el comando login con ese buzon.")
    try:
        credentials = credentials_type.from_authorized_user_file(str(cache_path), SCOPES)
    except ValueError as exc:
        raise GmailSessionError("Sesion de Google ilegible. Ejecuta el comando login con ese buzon.") from exc

    def token() -> str:
        if not credentials.valid:
            if not credentials.refresh_token:
                raise GmailSessionError("Sesion de Google vencida. Ejecuta el comando login con ese buzon.")
            try:
                credentials.refresh(request_type())
            except refresh_error as exc:
                raise GmailSessionError(
                    "Google no renovo la sesion (vencida o revocada). Ejecuta el comando login con ese buzon."
                ) from exc
            except transport_error as exc:
                raise GmailError("No se pudo contactar con Google para renovar la sesion.") from exc
            _save(credentials, cache_path)
        return credentials.token

    return token


def interactive_login(
    client_secrets: Path, cache_path: Path, timeout_seconds: int | None = None,
    expected: str | None = None,
) -> str:
    """Sign in through the person's browser; the agent never handles the password.

    client_secrets is the OAuth desktop client downloaded from Google Cloud; it lives
    in the local data folder, outside Git. With timeout_seconds the wait for the
    sign-in ends instead of blocking an unattended run. With expected, a session of
    another Google account is refused and never saved.
    """
    _credentials, _request, _errors, (flow_type, timeout_error) = _google()
    if not client_secrets.is_file():
        raise GmailError(f"Falta {client_secrets.name} en la carpeta de datos local (cliente OAuth de escritorio).")
    flow = flow_type.from_client_secrets_file(str(client_secrets), SCOPES)
    try:
        credentials = flow.run_local_server(
            host="localhost", port=0, open_browser=True, timeout_seconds=timeout_seconds,
            authorization_prompt_message="Abre este enlace si el navegador no se abrio: {url}",
            success_message="Sesion de Gmail guardada. Puedes cerrar esta pestana.",
        )
    except timeout_error as exc:
        raise GmailSessionError("Nadie inicio sesion en Google a tiempo; la sesion sigue vencida.") from exc
    _read_only(credentials)
    client = GmailClient(lambda: credentials.token)
    try:
        address = client.get_json(f"{GMAIL_ROOT}/profile").get("emailAddress")
    finally:
        client.close()
    if not isinstance(address, str):
        raise GmailError("Google no devolvio la cuenta de la sesion; no se guarda.")
    if expected is not None and address.lower() != expected.lower():
        raise GmailSessionError("Se inicio sesion con otra cuenta de Google; no se guarda la sesion.")
    _save(credentials, cache_path)
    return address
