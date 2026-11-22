"""Synthetic data for the monthly close tests: each test states only what matters to it."""

from __future__ import annotations

import json

SENDER = "avisos@example.test"

# The answers of step zero that most tests share; tests override only what they check.
SETTINGS = {
    "location": "Ciudad de prueba", "timezone": "America/Bogota", "currency": "COP",
    "accounts": ["Tarjeta A"], "excluded_accounts": [],
    "transfer_label": "Familiar", "ask_savings": True, "ask_people": True,
    "approved_senders": [SENDER], "confidential_confirmed": True,
}

# A card payment in the bank's fixed wording, which is read without the model.
CARD_NOTICE = (
    "Bancolombia: Pagaste $218,699 en la tarjeta de credito *8243 desde la cuenta *0111, "
    "el 14/10/2026 11:47. Dudas? Llamanos."
)


def settings(categories: list[str], **overrides) -> dict:
    """Step-zero answers with the given categories and any field changed."""
    return {**SETTINGS, "categories": categories, **overrides}


def movement(event_id: str, merchant: str = "TIENDA FICTICIA", **overrides) -> dict:
    """A pending purchase as a notice import stores it."""
    return {
        "id": event_id, "message_id": "m-" + event_id, "day": "2026-10-05",
        "merchant": merchant, "account": "Tarjeta A", "amount_minor": 5000000,
        "currency": "COP", "category": "Compras", "kind": "purchase",
        "decision": None, "reason": "Propuesta de Ollama.", **overrides,
    }


def statement_line(event_id: str, merchant: str, **overrides) -> dict:
    """A pending movement that only a bank statement showed (its id starts with stmt:)."""
    return {
        "id": f"stmt:{event_id}", "message_id": "statement:x", "day": "2026-01-10",
        "merchant": merchant, "account": "Tarjeta de crédito Visa", "amount_minor": 1000,
        "currency": "COP", "category": "Otros gastos", "kind": "purchase",
        "decision": None, "owner": "santiago", "reason": "Leído del extracto.", **overrides,
    }



FILE_IMPORT_COLUMNS = (
    "id", "message_id", "owner", "day", "time", "merchant", "account", "account_label", "amount_minor",
    "currency", "category", "kind", "decision", "reason", "group_key", "note", "category_uncertain",
)


def file_imported(store, rows: list[dict]) -> None:
    """Movements as the one-off import of Manuela's exported files left them in the database."""
    defaults = {"time": None, "account_label": None, "group_key": None, "note": None, "category_uncertain": 0}
    with store.connect() as db:
        for row in rows:
            values = {**defaults, **row}
            db.execute(
                f"INSERT INTO transactions ({','.join(FILE_IMPORT_COLUMNS)}) "
                f"VALUES ({','.join('?' * len(FILE_IMPORT_COLUMNS))})",
                [values[column] for column in FILE_IMPORT_COLUMNS],
            )

class FakeExtractionModel:
    """Stands in for Ollama's notice extraction and checks the prompt keeps the email untrusted."""

    def __init__(self, **updates) -> None:
        self.calls = 0
        self.event = {
            "merchant": "Tienda", "account": "Tarjeta A", "amount_text": "$ 1.234,56",
            "currency": "COP", "date_text": "", "category": "Compras",
            "kind": "purchase", "savings": False,
        }
        self.event.update(updates)

    def complete_json(self, system, user):
        self.calls += 1
        assert "no confiable" in system
        assert "untrusted_email" in json.loads(user)
        return {"events": [self.event]}
