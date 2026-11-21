"""Destination of an outgoing movement, read from the bank's fixed sentence without the model.

The key groups payments to the same destination, like Manuela's exported keys:
``tr:`` account, ``bb:`` Bre-B key, ``qr:`` QR payment key and ``fd:`` investment fund.
Account numbers and numeric keys keep only their last four digits.
"""
from __future__ import annotations

import re

from monthly_close.domain import amount_minor

QR_NOT_OWN_REASON = (
    "Pago por QR a una llave que no es una cuenta tuya registrada: confirma si es gasto personal o familiar."
)
AMOUNT = r"(?P<amount>\$\s?\d[\d.,]*\d|\$\s?\d)"
ACCOUNT = r"\*?\s?\d[\d\s]*?"
PATTERNS = (
    ("tr", re.compile(
        r"(?:transferiste|enviaste)\s+" + AMOUNT + r"(?:\s+por\s+qr)?\s+desde\s+tu\s+cuenta\s+"
        + ACCOUNT + r"\s+a\s+la\s+cuenta\s+(?P<key>\*?\s?\d[\d\s]*\d)(?=[\s,.]|$)",
        re.IGNORECASE,
    )),
    ("bb", re.compile(
        r"transferiste\s+" + AMOUNT + r"\s+a\s+la\s+llave\s+(?P<key>@[\w.\-]+|\d+)\s+desde\s+tu\s+cuenta",
        re.IGNORECASE,
    )),
    ("qr", re.compile(
        r"pagaste\s+" + AMOUNT + r"\s+por\s+c[oó]digo\s+qr\s+desde\s+tu\s+cuenta\s+" + ACCOUNT
        + r"\s+a\s+la\s+llave\s+(?P<key>@[\w.\-]+|\d+)",
        re.IGNORECASE,
    )),
    ("fd", re.compile(
        r"retiraste\s+" + AMOUNT + r"\s+de\s+tu\s+cuenta\s+de\s+\w+\s*\*\s*(?P<key>\w+)",
        re.IGNORECASE,
    )),
)


def _key(prefix: str, raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("@"):
        return f"{prefix}:{raw.lower()}"
    if re.fullmatch(r"[\d\s*]+", raw):
        return f"{prefix}:{re.sub(r'\D', '', raw)[-4:]}"
    return f"{prefix}:{re.sub(r'\W', '', raw)[-4:].lower()}"


def notice_destinations(source: str) -> list[tuple[int, str]]:
    """Every (amount in minor units, destination key) the notice states literally."""
    text = re.sub(r"\s+", " ", source)
    found: list[tuple[int, str]] = []
    for prefix, pattern in PATTERNS:
        for match in pattern.finditer(text):
            try:
                found.append((amount_minor(match["amount"]), _key(prefix, match["key"])))
            except ValueError:
                continue  # not a readable amount: no destination rather than a guess
    return found


def assign_destinations(events: list[dict], source: str) -> list[dict]:
    """Set group_key on each event whose amount matches exactly one stated destination.

    Anything ambiguous (two destinations with the same amount, or none) stays without
    a key and is grouped by its merchant as before.
    """
    destinations = notice_destinations(source)
    for event in events:
        keys = {key for amount, key in destinations if amount == event["amount_minor"]}
        event["group_key"] = keys.pop() if len(keys) == 1 else event.get("group_key")
    return events
