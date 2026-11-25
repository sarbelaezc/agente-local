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
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from zipfile import BadZipFile

from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException

LEDGERS_FILE = "credit_ledgers.json"
HEADERS = {
    "day": "fecha",
    "capital": "pago capital",
    "interest": "pago intereses",
    "insurance": "seguros y cargos adicionales",
    "total": "pago total",
}
OPTIONAL_HEADERS = {"extra": "abono capital"}
HEADER_SEARCH_ROWS = 10
# Workbooks with formulas carry binary fractions: a row may differ from its parts by
# a couple of cents, and a period may differ from the bank by less than one peso.
ROW_TOLERANCE_MINOR = 2
PERIOD_TOLERANCE_MINOR = 100
PRODUCTS = {"CREDITO", "HIPOTECARIO"}
CONTRASTS = {"capital", "payments"}


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
    product: str = "CREDITO"
    payroll: bool = False
    contrast: str = "capital"

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
        product, payroll, contrast_mode = raw.get("product", "CREDITO"), raw.get("payroll", False), raw.get("contrast", "capital")
        if product not in PRODUCTS or type(payroll) is not bool or contrast_mode not in CONTRASTS:
            raise LedgerError("product debe ser CREDITO o HIPOTECARIO, payroll true o false y contrast capital o payments.")
        return cls(
            owner, suffix, name.strip()[:60], Path(path), sheet, tuple(months), category,
            product, payroll, contrast_mode,
        )


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
    if isinstance(value, float):
        return int((Decimal(repr(value)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    raise LedgerError(f"Fila {row}: valor no numerico en {field}.")


def read_payments(config: LedgerConfig, since: date | None = None) -> list[Payment]:
    """Payments with a total above zero from since on; rows still to be paid (total 0) are skipped.

    Older rows are neither imported nor validated: only the period the bank's
    statements can confirm is used.
    """
    try:
        book = load_workbook(config.path, read_only=True, data_only=True)
    except (OSError, InvalidFileException, BadZipFile) as exc:
        raise LedgerError(f"No se pudo abrir el registro del credito *{config.suffix}.") from exc
    try:
        if config.sheet not in book.sheetnames:
            raise LedgerError(f"El libro no tiene la hoja {config.sheet}.")
        rows = book[config.sheet].iter_rows(values_only=True)
        columns, number = None, 0
        for number, row in enumerate(rows, start=1):
            header = [str(cell).strip().lower() if cell is not None else "" for cell in row]
            if all(title in header for title in HEADERS.values()):
                columns = {key: header.index(title) for key, title in HEADERS.items()}
                columns.update({key: header.index(title) for key, title in OPTIONAL_HEADERS.items() if title in header})
                break
            if number >= HEADER_SEARCH_ROWS:
                break
        if columns is None:
            raise LedgerError("La hoja no tiene los encabezados esperados.")
        payments = []
        for number, row in enumerate(rows, start=number + 1):
            when = row[columns["day"]] if len(row) > columns["day"] else None
            if not isinstance(when, datetime) or (since is not None and when.date() < since):
                continue  # opening balance, empty rows or a period without statements
            values = {key: _minor(row[index], key, number) for key, index in columns.items() if key != "day"}
            if values["total"] <= 0:
                continue
            capital = values["capital"] + values.get("extra", 0)
            if abs(capital + values["interest"] + values["insurance"] - values["total"]) > ROW_TOLERANCE_MINOR:
                raise LedgerError(f"Fila {number}: capital, intereses y seguros no suman el total.")
            # Without an "Abono Capital" column, a row of capital only is a capital payment.
            extra = values["extra"] > 0 if "extra" in values else (
                capital > 0 and values["interest"] == 0 and values["insurance"] == 0
            )
            payments.append(Payment(
                when.date(), capital, values["interest"], values["insurance"], values["total"], extra,
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
        if abs(capital - (before - after)) >= PERIOD_TOLERANCE_MINOR:
            problems.append(f"{end}: el registro abona {capital / 100:,.0f} y el saldo bajo {(before - after) / 100:,.0f}")
    return problems


def contrast_payments(payments: list[Payment], paid: list[tuple[str, int]]) -> list[str]:
    """Periods where the register's payments differ from the payments the statement lists.

    For credits whose statement shows every payment of the period but a balance that
    includes accrued interest (a mortgage), the payments are what can be compared.
    """
    problems = []
    for (start, _), (end, listed) in zip(paid, paid[1:]):
        low, high = date.fromisoformat(start), date.fromisoformat(end)
        total = sum(p.total_minor for p in payments if low < p.day <= high)
        if abs(total - listed) >= PERIOD_TOLERANCE_MINOR:
            problems.append(f"{end}: el registro paga {total / 100:,.0f} y el extracto lista {listed / 100:,.0f}")
    return problems


def events(config: LedgerConfig, payments: list[Payment], category: str) -> list[dict]:
    """One pending loan movement per payment, marked as payroll or own payment.

    Fortnightly instalments are payroll deductions; a capital payment is payroll only in
    the configured bonus months, otherwise it came from the person's own account.
    """
    result = []
    for payment in payments:
        payroll = config.payroll and (not payment.extra or payment.day.month in config.bonus_months)
        source = "own" if not payroll else ("bonus" if payment.extra else "payroll")
        identity = f"{config.suffix}|{payment.day.isoformat()}|{payment.total_minor}|{int(payment.extra)}"
        merchant = (
            f"Abono a capital crédito {config.name} *{config.suffix}" if payment.extra
            else f"Cuota {'quincenal ' if config.payroll else ''}crédito {config.name} *{config.suffix}"
        )
        origin = "descontado de la prima" if payment.extra and payroll else (
            "descontado de la nómina" if payroll else "pagado desde tu cuenta"
        )
        result.append({
            "id": "ledger:" + hashlib.sha256(identity.encode()).hexdigest(),
            "message_id": f"ledger:{config.product}:{config.suffix}:{source}",
            "day": payment.day.isoformat(), "merchant": merchant, "account": f"Crédito {config.name}",
            "amount_minor": payment.total_minor, "currency": "COP", "category": category, "kind": "loan",
            "decision": None, "group_key": f"cr:{config.suffix}",
            "note": (
                f"Capital {payment.capital_minor / 100:,.0f} · intereses {payment.interest_minor / 100:,.0f} · "
                f"seguros {payment.insurance_minor / 100:,.0f}"
            ).replace(",", "."),
            "reason": f"Tomado de tu registro de pagos del crédito *{config.suffix} ({origin}); "
                      "cuadra con los extractos del banco.",
        })
    return result
