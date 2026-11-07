from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from playwright.sync_api import Page

ALLOWED_HOSTS = {"outlook.live.com", "outlook.office.com", "outlook.office365.com"}


class OutlookError(RuntimeError):
    pass


@dataclass(frozen=True)
class Candidate:
    item_id: str
    sender: str
    subject: str


def validate_url(url: str) -> str:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS
        or parsed.port not in (None, 443) or parsed.username or parsed.password
        or not parsed.path.startswith("/mail/")
    ):
        raise ValueError("Solo se permite Outlook Web HTTPS, sin credenciales en la URL.")
    return url


def guard_page(page: Page, origin: str) -> None:
    """Stop if the browser leaves the expected mailbox or requests authentication."""
    parsed = urlparse(page.url)
    if parsed.scheme != "https" or parsed.netloc != origin or not parsed.path.startswith("/mail/"):
        raise OutlookError("Autenticacion o pantalla inesperada. Detenido; inicia sesion manualmente.")
    if page.locator(
        "input[type='password'], iframe[src*='captcha'], input[name='otc'], #idDiv_SAOTCS_Proofs"
    ).count():
        raise OutlookError("Autenticacion, MFA o captcha detectado. Detenido.")
