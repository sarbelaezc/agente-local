from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

CATEGORIES = [
    "Domicilios y restaurantes", "Compras", "Salud y farmacia", "Transporte",
    "Suscripciones", "Mercado y tiendas", "Otros gastos",
]
KINDS = {
    "purchase", "pse", "transfer", "own_transfer", "loan", "income",
    "card_payment", "rejected", "promotion", "verification", "unknown", "investment",
}
NON_EXPENSES = {"loan", "income", "card_payment", "rejected", "promotion", "verification"}
EMAIL = re.compile(r"[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+")


def clean_text(value: object, limit: int = 300) -> str:
    if not isinstance(value, str):
        raise ValueError("Se esperaba texto.")
    text = "".join(
        char for char in value
        if char in "\n\t" or not unicodedata.category(char).startswith("C")
    ).strip()
    if not text or len(text) > limit:
        raise ValueError(f"Texto vacio o demasiado largo (maximo {limit}).")
    return text


def string_list(value: object, label: str, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or len(value) > 100:
        raise ValueError(f"{label} debe ser una lista de hasta 100 elementos.")
    result = list(dict.fromkeys(clean_text(item, 150) for item in value))
    if not result and not allow_empty:
        raise ValueError(f"Completa {label}.")
    return result


@dataclass(frozen=True)
class Preferences:
    location: str
    timezone: str
    currency: str
    accounts: list[str]
    excluded_accounts: list[str]
    transfer_label: str
    ask_savings: bool
    ask_people: bool
    approved_senders: list[str]
    categories: list[str]
    confidential_confirmed: bool

    @classmethod
    def parse(cls, raw: dict) -> Preferences:
        """Validate all step-zero answers before authorizing mail access."""
        for field in ("ask_savings", "ask_people", "confidential_confirmed"):
            if type(raw.get(field)) is not bool:
                raise ValueError(f"Responde {field}.")
        if raw["confidential_confirmed"] is not True:
            raise ValueError("Debes autorizar el procesamiento local de contenido confidencial.")
        timezone = clean_text(raw.get("timezone"), 100)
        ZoneInfo(timezone)
        currency = clean_text(raw.get("currency"), 3).upper()
        if not re.fullmatch(r"[A-Z]{3}", currency):
            raise ValueError("La moneda debe ser un codigo de tres letras.")
        senders = [
            item.lower() for item in string_list(
                raw.get("approved_senders"), "remitentes", allow_empty=True,
            )
        ]
        if any(not EMAIL.fullmatch(sender) for sender in senders):
            raise ValueError("Usa direcciones exactas de remitentes, sin comodines.")
        return cls(
            clean_text(raw.get("location")), timezone, currency,
            string_list(raw.get("accounts"), "cuentas"),
            string_list(raw.get("excluded_accounts"), "cuentas excluidas", allow_empty=True),
            clean_text(raw.get("transfer_label"), 100),
            raw["ask_savings"], raw["ask_people"], senders,
            string_list(raw.get("categories"), "categorias"),
            True,
        )

    def to_dict(self) -> dict:
        return asdict(self)


def amount_minor(text: str) -> int:
    """Parse a source amount, never binary floating point or model arithmetic."""
    text = clean_text(text, 80)
    if not re.fullmatch(r"(?:[A-Z]{3}\s*)?[$€£]?\s*\d[\d., ]*", text):
        raise ValueError("Monto no reconocido; requiere revision del correo.")
    number = re.sub(r"[^\d.,]", "", text)
    separators = [index for index, char in enumerate(number) if char in ",."]
    if separators:
        last = separators[-1]
        decimals = len(number) - last - 1
        if decimals in (1, 2):
            whole = number[:last]
            fraction = number[last + 1:]
            groups = re.split(r"[.,]", whole)
            if len(groups) > 1 and (
                not 1 <= len(groups[0]) <= 3 or any(len(g) != 3 for g in groups[1:])
            ):
                raise ValueError("Separadores de monto invalidos.")
            if len(groups) > 1 and number[last] in whole:
                raise ValueError("Separador decimal ambiguo.")
            number = "".join(groups) + "." + fraction
        elif decimals == 3:
            groups = re.split(r"[.,]", number)
            if len(set(number[i] for i in separators)) != 1 or (
                not 1 <= len(groups[0]) <= 3 or any(len(g) != 3 for g in groups[1:])
            ):
                raise ValueError("Separadores de miles invalidos.")
            number = "".join(groups)
        else:
            raise ValueError("Precision monetaria no soportada.")
    result = int(Decimal(number) * 100)
    if not 0 < result <= 10**14:
        raise ValueError("Monto fuera del rango permitido.")
    return result


def received_local(value: str, timezone: str) -> datetime:
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("Outlook no expuso una fecha con zona horaria; no se supone UTC.")
    return stamp.astimezone(ZoneInfo(timezone))


def decision_reason(kind: str, account: str, savings: bool, preferences: Preferences) -> str:
    if kind in NON_EXPENSES:
        return "No es gasto: " + kind
    if account in preferences.excluded_accounts or kind == "own_transfer":
        return "Revisar como " + preferences.transfer_label
    if kind == "investment":
        return "Confirmar aporte de inversion; no se suma al gasto de consumo."
    if savings and preferences.ask_savings:
        return "Confirmar si se paga con ahorros."
    if kind == "transfer" and preferences.ask_people:
        return "Confirmar transferencia a una persona."
    return "Propuesta de Ollama: confirma datos, categoria y si es gasto personal."
