from __future__ import annotations

import re
from dataclasses import dataclass, replace

from monthly_close.domain import EMAIL, Preferences, owner_id, string_list
from monthly_close.gmail_reader import DEFAULT_ORIGIN as GMAIL_ORIGIN
from monthly_close.graph_client import CACHE_NAME
from monthly_close.graph_reader import DEFAULT_ORIGIN as GRAPH_ORIGIN

PROVIDERS = {"graph", "gmail"}
NAME = re.compile(r"[a-z][a-z0-9-]{1,30}")
FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}")
ORIGIN = re.compile(r"[a-z0-9.-]+(?:/[a-z0-9-]+)?")
KEYS = {
    "name", "provider", "owner", "origin", "token_cache", "client_secrets", "verified", "accounts",
    "excluded_accounts", "address",
}
DEFAULT_CLIENT_SECRETS = "gmail_client_secret.json"


@dataclass(frozen=True)
class Mailbox:
    """One mailbox the close reads, and whose movements belong to one household member.

    origin is hashed into every message identifier: changing it would import notices
    again. accounts and excluded_accounts replace the step-zero aliases for this owner.
    """

    name: str
    provider: str
    owner: str
    origin: str
    token_cache: str
    verified: bool
    client_secrets: str | None = None
    accounts: tuple[str, ...] | None = None
    excluded_accounts: tuple[str, ...] | None = None
    # Gmail only: the account a new sign-in must belong to (required to sign in again
    # from the scheduled close, so another person's account is never imported).
    address: str | None = None

    def preferences(self, base: Preferences) -> Preferences:
        """Step-zero answers with this mailbox's own account aliases, when it has them."""
        changes: dict = {}
        if self.accounts is not None:
            changes["accounts"] = list(self.accounts)
        if self.excluded_accounts is not None:
            changes["excluded_accounts"] = list(self.excluded_accounts)
        return replace(base, **changes) if changes else base


def _file(raw: dict, key: str, default: str) -> str:
    value = raw.get(key, default)
    if not isinstance(value, str) or not FILE_NAME.fullmatch(value) or ".." in value:
        raise ValueError(f"{key} debe ser un nombre de archivo dentro de la carpeta de datos local.")
    return value


def _aliases(raw: dict, key: str) -> tuple[str, ...] | None:
    if key not in raw:
        return None
    return tuple(string_list(raw[key], key, allow_empty=key == "excluded_accounts"))


def _mailbox(raw: object, settings: dict) -> Mailbox:
    if not isinstance(raw, dict) or set(raw) - KEYS:
        raise ValueError("Cada buzon admite solo: " + ", ".join(sorted(KEYS)) + ".")
    name, provider = raw.get("name"), raw.get("provider")
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ValueError("name del buzon: minusculas, numeros y guiones.")
    if provider not in PROVIDERS:
        raise ValueError(f"provider del buzon {name} debe ser graph o gmail.")
    origin = raw.get("origin", GRAPH_ORIGIN if provider == "graph" else GMAIL_ORIGIN)
    if not isinstance(origin, str) or not ORIGIN.fullmatch(origin):
        raise ValueError(f"origin del buzon {name} no es valido.")
    address = raw.get("address")
    if address is not None and (provider != "gmail" or not isinstance(address, str) or not EMAIL.fullmatch(address)):
        raise ValueError(f"address del buzon {name} debe ser el correo exacto de su cuenta de Gmail.")
    if provider == "graph":
        if "client_secrets" in raw or "verified" in raw:
            raise ValueError(f"El buzon {name} de Graph usa graph_client_id y folder_scan_verified.")
        verified = settings.get("folder_scan_verified") is True
        secrets = None
    else:
        if type(raw.get("verified", False)) is not bool:
            raise ValueError(f"verified del buzon {name} debe ser true o false.")
        verified = raw.get("verified", False)
        secrets = _file(raw, "client_secrets", DEFAULT_CLIENT_SECRETS)
    return Mailbox(
        name, provider, owner_id(raw.get("owner")), origin,
        _file(raw, "token_cache", CACHE_NAME if provider == "graph" else f"gmail_token_{name}.json"),
        verified, secrets, _aliases(raw, "accounts"), _aliases(raw, "excluded_accounts"),
        address.lower() if address else None,
    )


def load_mailboxes(settings: dict) -> list[Mailbox]:
    """Mailboxes configured in runtime.json; without a list, Santiago's Outlook alone."""
    raw = settings.get("mailboxes")
    if raw is None:
        return [Mailbox(
            "santiago", "graph", "santiago", GRAPH_ORIGIN, CACHE_NAME,
            settings.get("folder_scan_verified") is True,
        )]
    if not isinstance(raw, list) or not 1 <= len(raw) <= 5:
        raise ValueError("mailboxes debe ser una lista de 1 a 5 buzones.")
    boxes = [_mailbox(item, settings) for item in raw]
    for field in ("name", "origin", "token_cache"):
        values = [getattr(box, field) for box in boxes]
        if len(set(values)) != len(values):
            raise ValueError(f"Dos buzones comparten {field}; cada uno necesita el suyo.")
    return boxes


def primary_graph(boxes: list[Mailbox]) -> Mailbox:
    """Santiago's Outlook: statements, pilot and destination backfill read only this one."""
    for box in boxes:
        if box.provider == "graph" and box.owner == "santiago":
            return box
    raise ValueError("Falta el buzon de Outlook de Santiago (provider graph, owner santiago).")


def find(boxes: list[Mailbox], name: str | None) -> Mailbox:
    if name is None:
        return primary_graph(boxes)
    for box in boxes:
        if box.name == name:
            return box
    raise ValueError("Buzon no configurado: " + ", ".join(box.name for box in boxes) + ".")
