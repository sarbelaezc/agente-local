"""A person's own register of a credit's payments, used as complementary evidence.

The bank's credit statement shows only the last payment of each period; the register
lists every payment (fortnightly payroll deductions and capital payments) with its
capital, interest and insurance. Its capital must match the statement balances
exactly, otherwise nothing is imported.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException

LEDGERS_FILE = "credit_ledgers.json"
HEADERS = {
    "day": "fecha",
    "extra": "abono capital",
    "capital": "pago capital",
    "interest": "pago intereses",
    "insurance": "seguros y cargos adicionales",
    "total": "pago total",
}


class LedgerError(ValueError):
    """A register that cannot be read or does not match the bank: nothing is imported."""


@dataclass(frozen=True)
class LedgerConfig:
    owner: str
    suffix: str
    name: str
    path: Path
    sheet: str
    bonus_months: tuple[int, ...]
    category: str | None = None

    @classmethod
    def parse(cls, raw: object) -> LedgerConfig:
        if not isinstance(raw, dict):
            raise LedgerError("Cada registro de credito debe ser un objeto.")
        suffix, path, sheet = raw.get("suffix"), raw.get("path"), raw.get("sheet")
        owner, name, months = raw.get("owner"), raw.get("name"), raw.get("bonus_months", [6, 12])
        if not isinstance(suffix, str) or not re.fullmatch(r"\d{4}", suffix):
            raise LedgerError("suffix debe tener los ultimos cuatro digitos del credito.")
        if not isinstance(path, str) or not path.lower().endswith(".xlsx"):
            raise LedgerError("path debe apuntar a un libro .xlsx.")
        if not isinstance(sheet, str) or not sheet or not isinstance(owner, str) or not isinstance(name, str):
            raise LedgerError("Faltan owner, name o sheet en el registro de credito.")
        if not isinstance(months, list) or any(type(m) is not int or not 1 <= m <= 12 for m in months):
            raise LedgerError("bonus_months debe ser una lista de meses entre 1 y 12.")
        category = raw.get("category")
        if category is not None and (not isinstance(category, str) or not category.strip()):
            raise LedgerError("category debe ser una categoria de gasto configurada.")
        return cls(owner, suffix, name.strip()[:60], Path(path), sheet, tuple(months), category)


def load_configs(data_dir: Path) -> list[LedgerConfig]:
    """Registers configured in the local data folder; none when the file does not exist."""
    path = data_dir / LEDGERS_FILE
    if not path.is_file():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise LedgerError(f"{LEDGERS_FILE} debe ser una lista.")
    return [LedgerConfig.parse(item) for item in raw]


@dataclass(frozen=True)
class Payment:
    day: date
    capital_minor: int  # ordinary capital plus any capital payment
    interest_minor: int
    insurance_minor: int
    total_minor: int
    extra: bool  # a capital payment outside the fortnightly instalment


def _minor(value: object, field: str, row: int) -> int:
    if value is None:
        return 0
    if type(value) is int:
        return value * 100
    if isinstance(value, float) and value == round(value, 2):
        return round(value * 100)
    raise LedgerError(f"Fila {row}: valor no numerico en {field}.")


def read_payments(config: LedgerConfig, since: date | None = None) -> list[Payment]:
    """Payments with a total above zero from since on; rows still to be paid (total 0) are skipped.

    Older rows are neither imported nor validated: only the period the bank's
    statements can confirm is used.
    """
    try:
        book = load_workbook(config.path, read_only=True, data_only=True)
    except (OSError, InvalidFileException) as exc:
        raise LedgerError(f"No se pudo abrir el registro del credito *{config.suffix}.") from exc
    try:
        if config.sheet not in book.sheetnames:
            raise LedgerError(f"El libro no tiene la hoja {config.sheet}.")
        rows = book[config.sheet].iter_rows(values_only=True)
        header = [str(cell).strip().lower() if cell is not None else "" for cell in next(rows, ())]
        try:
            columns = {key: header.index(title) for key, title in HEADERS.items()}
        except ValueError as exc:
            raise LedgerError("La hoja no tiene los encabezados esperados.") from exc
        payments = []
        for number, row in enumerate(rows, start=2):
            when = row[columns["day"]] if len(row) > columns["day"] else None
            if not isinstance(when, datetime) or (since is not None and when.date() < since):
                continue  # opening balance, empty rows or a period without statements
            values = {key: _minor(row[index], key, number) for key, index in columns.items() if key != "day"}
            if values["total"] <= 0:
                continue
            capital = values["capital"] + values["extra"]
            if capital + values["interest"] + values["insurance"] != values["total"]:
                raise LedgerError(f"Fila {number}: capital, intereses y seguros no suman el total.")
            payments.append(Payment(
                when.date(), capital, values["interest"], values["insurance"], values["total"],
                values["extra"] > 0,
            ))
        return payments
    finally:
        book.close()


def contrast(payments: list[Payment], balances: list[tuple[str, int]]) -> list[str]:
    """Periods where the register's capital differs from the statement's capital decrease."""
    problems = []
    for (start, before), (end, after) in zip(balances, balances[1:]):
        low, high = date.fromisoformat(start), date.fromisoformat(end)
        capital = sum(p.capital_minor for p in payments if low < p.day <= high)
        if capital != before - after:
            problems.append(f"{end}: el registro abona {capital / 100:,.0f} y el saldo bajo {(before - after) / 100:,.0f}")
    return problems


def events(config: LedgerConfig, payments: list[Payment], category: str) -> list[dict]:
    """One pending loan movement per payment, marked as payroll or own payment.

    Fortnightly instalments are payroll deductions; a capital payment is payroll only in
    the configured bonus months, otherwise it came from the person's own account.
    """
    result = []
    for payment in payments:
        payroll = not payment.extra or payment.day.month in config.bonus_months
        source = "own" if not payroll else ("bonus" if payment.extra else "payroll")
        identity = f"{config.suffix}|{payment.day.isoformat()}|{payment.total_minor}|{int(payment.extra)}"
        merchant = (
            f"Abono a capital crédito {config.name} *{config.suffix}" if payment.extra
            else f"Cuota quincenal crédito {config.name} *{config.suffix}"
        )
        origin = "descontado de la prima" if payment.extra and payroll else (
            "pagado desde tu cuenta" if payment.extra else "descontado de la nómina"
        )
        result.append({
            "id": "ledger:" + hashlib.sha256(identity.encode()).hexdigest(),
            "message_id": f"ledger:CREDITO:{config.suffix}:{source}",
            "day": payment.day.isoformat(), "merchant": merchant, "account": f"Crédito {config.name}",
            "amount_minor": payment.total_minor, "currency": "COP", "category": category, "kind": "loan",
            "decision": None, "group_key": f"cr:{config.suffix}",
            "note": (
                f"Capital {payment.capital_minor / 100:,.0f} · intereses {payment.interest_minor / 100:,.0f} · "
                f"seguros {payment.insurance_minor / 100:,.0f}"
            ).replace(",", "."),
            "reason": f"Tomado de tu registro de pagos del crédito *{config.suffix} ({origin}); "
                      "su capital cuadra con los extractos del banco.",
        })
    return result
