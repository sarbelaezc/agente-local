"""Bank statements: read, check and reconcile them with the movements read from notices.

Statements arrive as password-protected PDFs. They are decrypted and read in memory
only; nothing of the PDF is stored except the movements and balances extracted here.
Every parser is deterministic (no model) and refuses lines it cannot read exactly.
"""
from __future__ import annotations

import hashlib
import io
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from pypdf import PdfReader
from pypdf.errors import PdfReadError

FILE_NAME = re.compile(r"Extracto_\d+_(?P<period>\d{6})_(?P<product>[A-Z_]+?)_\w{4}\.pdf")
PRODUCTS = {
    "CTA_AHORROS": "Cuenta de ahorros",
    "TARJETA_MASTERCARD": "Tarjeta de crédito Master",
    "TARJETA_AMEX": "Tarjeta de crédito Amex",
    "TARJETA_VISA": "Tarjeta de crédito Visa",
    "FIDUCUENTA": "Fiducuenta",
    "CREDITO": "Crédito de consumo",
    "HIPOTECARIO": "Crédito hipotecario",
}
MAX_PDF_BYTES = 10 * 1024 * 1024


class StatementError(ValueError):
    """A statement that cannot be read exactly; it is reported, never guessed."""


@dataclass(frozen=True)
class Movement:
    day: date
    description: str
    amount_minor: int  # positive: money out (charge); negative: money in (credit)
    currency: str
    hint: str  # purchase | card_payment | transfer | income | investment | investment_withdrawal | loan | unknown


@dataclass
class Statement:
    product: str
    suffix: str
    period_start: date
    period_end: date
    movements: list[Movement] = field(default_factory=list)
    balance_minor: int | None = None  # investment value or outstanding credit

    @property
    def identity(self) -> str:
        return f"{self.product}:{self.suffix}:{self.period_end.isoformat()}"


def _us_amount(text: str) -> int:
    """'-1,234.56' or '. 25' (as the PDF writes small amounts) in minor units."""
    clean = re.sub(r"\s", "", text).replace(",", "")
    if clean.startswith("-."):
        clean = "-0" + clean[1:]
    elif clean.startswith("."):
        clean = "0" + clean
    if not re.fullmatch(r"-?\d+\.\d{2}", clean):
        raise StatementError("Importe no reconocido en el extracto.")
    return int(Decimal(clean) * 100)


def _co_amount(text: str) -> int:
    """'-1.234,56' (Colombian format) in minor units."""
    clean = re.sub(r"\s", "", text).replace(".", "").replace(",", ".")
    if not re.fullmatch(r"-?\d+\.\d{2}", clean):
        raise StatementError("Importe no reconocido en el extracto.")
    return int(Decimal(clean) * 100)


def decrypt_text(content: bytes, password: str) -> str:
    """Text of a PDF decrypted in memory; the PDF is never written to disk."""
    if len(content) > MAX_PDF_BYTES:
        raise StatementError("Extracto demasiado grande.")
    try:
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted and not reader.decrypt(password):
            raise StatementError("La contraseña configurada no abre el extracto.")
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except PdfReadError as exc:
        raise StatementError("PDF de extracto ilegible.") from exc


def _lines(text: str) -> list[str]:
    """Lines without accents or repeated spaces, so 'Número' and 'numero' read alike."""
    plain = "".join(
        char for char in unicodedata.normalize("NFD", text) if unicodedata.category(char) != "Mn"
    )
    return [re.sub(r"\s+", " ", line).strip() for line in plain.splitlines() if line.strip()]


def _hint(description: str, outflow: bool) -> str:
    text = description.lower()
    if re.search(r"fondo de inversion|traslado a fondo|traslado a fiducuenta", text):
        return "investment" if outflow else "investment_withdrawal"
    if re.search(r"traslado de fondo", text):
        return "investment_withdrawal"
    # "PAGO SUC VIRT TC MASTER DOLAR": paying a card's dollar balance from savings.
    if re.search(r"tarjeta|abono sucursal|pago tc|pago tarj|\btc\b", text):
        return "card_payment"
    if re.search(r"credito|prestamo|hipotec", text) and outflow:
        return "loan"
    if not outflow:
        return "income"
    if re.search(r"transferencia|transf|bre-?b|llave|qr", text):
        return "transfer"
    return "unknown"


SAVINGS_PERIOD = re.compile(r"desde\s*:\s*(\d{4})/(\d{2})/(\d{2})\s*hasta\s*:\s*(\d{4})/(\d{2})/(\d{2})", re.I)
SAVINGS_LINE = re.compile(
    r"^(?P<day>\d{1,2})/(?P<month>\d{2}) (?P<desc>.+?) (?P<amount>-?(?:[\d,]+)?\.\s?\d{2}) (?P<balance>-?[\d,]+\.\d{2})$"
)


def _year_for(month: int, start: date, end: date) -> int:
    return start.year if month >= start.month and start.year != end.year else end.year


def parse_savings(text: str) -> Statement:
    lines = _lines(text)
    period = next((SAVINGS_PERIOD.search(line) for line in lines if SAVINGS_PERIOD.search(line)), None)
    number = next((re.search(r"^numero (\d{6,})$", line, re.I) for line in lines if re.search(r"^numero \d{6,}$", line, re.I)), None)
    if not period or not number:
        raise StatementError("Extracto de ahorros sin periodo o número de cuenta.")
    start = date(int(period[1]), int(period[2]), int(period[3]))
    end = date(int(period[4]), int(period[5]), int(period[6]))
    statement = Statement("CTA_AHORROS", number[1][-4:], start, end)
    previous: int | None = None
    for line in lines:
        match = SAVINGS_LINE.match(line)
        if not match:
            continue
        amount, balance = _us_amount(match["amount"]), _us_amount(match["balance"])
        # The running balance proves each line was read whole: previous + amount = balance.
        if previous is not None and previous + amount != balance:
            raise StatementError("El saldo corrido del extracto de ahorros no cuadra.")
        previous = balance
        month = int(match["month"])
        day = date(_year_for(month, start, end), month, int(match["day"]))
        description = match["desc"]
        statement.movements.append(Movement(
            day, description, -amount, "COP", _hint(description, amount < 0),
        ))
    return statement


CARD_PERIOD = re.compile(r"(\d{2}) (\w{3}) - (\d{2}) (\w{3})\s*\.?\s*(\d{4})", re.I)
CARD_LINE = re.compile(r"^(?P<desc>.+?) (?P<day>\d{2}/\d{2}/\d{4}) \$ (?P<amount>-?[\d.]+,\d{2})\b")
CARD_CURRENCY = re.compile(r"cuenta en\s*:\s*(pesos|d[oó]lares)", re.I)
INTERNAL_CARD = re.compile(r"saldo a diferir|traslado saldo", re.I)


def parse_card(product: str, text: str, period: str) -> Statement:
    lines = _lines(text)
    suffix = next((re.search(r"\*\s*(\d{4})\b", line)[1] for line in lines if re.search(r"tarjeta\s*:.*\*\s*\d{4}", line, re.I)), None)
    if not suffix:
        raise StatementError("Extracto de tarjeta sin número de tarjeta.")
    year, month = int(period[:4]), int(period[4:])
    end = date(year, month, 28) + timedelta(days=4)
    end = end - timedelta(days=end.day)  # last day of the statement month
    statement = Statement(product, suffix, end.replace(day=1), end)
    currency = None
    seen: set[tuple] = set()
    for line in lines:
        found = CARD_CURRENCY.search(line)
        if found:
            currency = "COP" if found[1].lower() == "pesos" else "USD"
        match = CARD_LINE.match(line)
        if not match or INTERNAL_CARD.search(match["desc"]):
            continue
        if currency is None:
            raise StatementError("Movimiento de tarjeta sin sección de moneda.")
        amount = _co_amount(match["amount"])
        day_text = match["day"]
        day = date(int(day_text[6:]), int(day_text[3:5]), int(day_text[:2]))
        # The bank glues a reference to the merchant ("ABONO SUCURSAL VIRTUALC24280");
        # without it, payments to the same merchant group together.
        description = re.sub(r"\s*[A-Z]?\d{5,}$", "", re.sub(r"^\d{4,}\s+", "", match["desc"])) or match["desc"]
        key = (day, description, amount, currency)
        if key in seen:
            continue  # the PDF repeats some rows across its three columns
        seen.add(key)
        hint = "card_payment" if amount < 0 else "purchase"
        statement.movements.append(Movement(day, description, amount, currency, hint))
    if not statement.movements and not any("movimientos" in line.lower() for line in lines):
        raise StatementError("Formato de extracto de tarjeta no reconocido.")
    return statement


FUND_PERIOD = re.compile(r"desde\s*:\s*(\d{8})\s*hasta\s*:\s*(\d{8})", re.I)
FUND_LINE = re.compile(r"^(?P<day>\d{8}) (?P<desc>.+?) (?P<amount>-?[\d.]+,\d{2}) [\d.,]+ (?P<balance>[\d.]+,\d{2})$")


def _ymd(text: str) -> date:
    return date(int(text[:4]), int(text[4:6]), int(text[6:]))


def parse_fund(text: str) -> Statement:
    lines = _lines(text)
    period = next((FUND_PERIOD.search(line) for line in lines if FUND_PERIOD.search(line)), None)
    account = next((re.search(r"cuenta de inversion\s*:\s*(\d{4,})", line, re.I) for line in lines if re.search(r"cuenta de inversion\s*:\s*\d", line, re.I)), None)
    if not period or not account:
        raise StatementError("Extracto de Fiducuenta sin periodo o cuenta.")
    statement = Statement("FIDUCUENTA", account[1][-4:], _ymd(period[1]), _ymd(period[2]))
    for index, line in enumerate(lines):
        if re.search(r"retencion nuevo saldo", line, re.I) and index + 2 < len(lines):
            values = re.findall(r"-?[\d.]+,\d+", lines[index + 2])
            if len(values) >= 3:
                statement.balance_minor = _co_amount(values[2])
        match = FUND_LINE.match(line)
        if not match:
            continue
        description = match["desc"].lower()
        if description.startswith("adicion"):
            hint = "investment"
        elif description.startswith("retiro"):
            hint = "investment_withdrawal"
        else:
            continue  # returns and fees change the balance but are not money moved by the person
        amount = _co_amount(match["amount"])
        signed = amount if hint == "investment" else -amount
        statement.movements.append(Movement(_ymd(match["day"]), match["desc"], signed, "COP", hint))
    if statement.balance_minor is None:
        raise StatementError("Extracto de Fiducuenta sin nuevo saldo.")
    return statement


def _first(lines: list[str], pattern: str) -> re.Match | None:
    """First line match of a pattern, case-insensitive."""
    for line in lines:
        found = re.search(pattern, line, re.I)
        if found:
            return found
    return None


def parse_consumer_credit(text: str) -> Statement:
    lines = _lines(text)
    number = _first(lines, r"obligacion\s+\S+\s*:\s*(\d{4,})")
    # Month first: the cut-off dates of consecutive statements read 1/05, 2/05, 3/05...
    cut = _first(lines, r"fecha corte extracto (?P<month>\d{1,2})/(?P<day>\d{1,2})/(?P<year>\d{4})")
    total = _first(lines, r"^total ([\d,]*\.\s?\d{2}) ([\d,]*\.\s?\d{2})$")
    balance = _first(lines, r"saldo de capital (-?[\d,]*\.\s?\d{2})")
    if not number or not cut or not total or not balance:
        raise StatementError("Extracto de crédito con formato no reconocido.")
    end = date(int(cut["year"]), int(cut["month"]), int(cut["day"]))
    statement = Statement("CREDITO", number[1][-4:], end.replace(day=1), end, balance_minor=_us_amount(balance[1]))
    paid = _us_amount(total[1])  # "abono anterior": what was paid since the last statement
    if paid:
        statement.movements.append(Movement(end, "Cuota de crédito pagada", paid, "COP", "loan"))
    return statement


MORTGAGE_HEADER = re.compile(r"^(\d{4})/(\d{2})/(\d{2}) (\d{4})/(\d{2})/(\d{2}) \$ ([\d,]+\.\d{2}) \$ ([\d,]+\.\d{2})$")
MORTGAGE_LINE = re.compile(r"^(?P<day>\d{4}/\d{2}/\d{2}) (?P<desc>pago cuota|pago extraordinario|abono)\b(?P<rest>.*?) (?P<total>[\d,]+\.\d{2})$", re.I)


def parse_mortgage(text: str) -> Statement:
    lines = _lines(text)
    header = next((MORTGAGE_HEADER.match(line) for line in lines if MORTGAGE_HEADER.match(line)), None)
    number = next((re.fullmatch(r"\d{8,}", line) for line in lines if re.fullmatch(r"\d{8,}", line)), None)
    if not header or not number:
        raise StatementError("Extracto hipotecario con formato no reconocido.")
    end = date(int(header[4]), int(header[5]), int(header[6]))
    statement = Statement("HIPOTECARIO", number[0][-4:], end.replace(day=1), end, balance_minor=_us_amount(header[8]))
    for line in lines:
        match = MORTGAGE_LINE.match(line)
        if match:
            day = date(*map(int, match["day"].split("/")))
            statement.movements.append(Movement(day, match["desc"].capitalize(), _us_amount(match["total"]), "COP", "loan"))
    return statement


def parse(product: str, period: str, text: str) -> Statement:
    if product == "CTA_AHORROS":
        return parse_savings(text)
    if product.startswith("TARJETA_"):
        return parse_card(product, text, period)
    if product == "FIDUCUENTA":
        return parse_fund(text)
    if product == "CREDITO":
        return parse_consumer_credit(text)
    if product == "HIPOTECARIO":
        return parse_mortgage(text)
    raise StatementError("Producto de extracto no soportado.")


@dataclass
class Candidate:
    statement: Statement
    movement: Movement
    paired: bool = False  # the same money seen in another statement


def _close(a: Movement, b: Movement, days: int = 5) -> bool:
    return abs((a.day - b.day).days) <= days


def pair_internal(statements: list[Statement]) -> list[Candidate]:
    """One candidate per real money movement.

    A card payment appears as a savings debit and as a card credit; a fund addition as
    a savings debit and a fund addition; a loan instalment as a savings debit and in
    the credit statement. The savings side is dropped when the other side exists.
    """
    candidates, seen = [], set()
    for statement in statements:
        for movement in statement.movements:
            # Consecutive statements may repeat a movement near their cut-off date.
            key = (statement.product, statement.suffix, movement.day, movement.amount_minor, movement.currency)
            if movement.currency in {"COP", "USD"} and key not in seen:
                seen.add(key)
                candidates.append(Candidate(statement, movement))
    savings = [c for c in candidates if c.statement.product == "CTA_AHORROS"]
    others = [c for c in candidates if c.statement.product != "CTA_AHORROS"]
    for other in others:
        if other.movement.hint not in {"card_payment", "investment", "investment_withdrawal", "loan"}:
            continue
        for saving in savings:
            if not saving.paired and _same_money(saving.movement, other.movement):
                saving.paired = True
                break
    return [c for c in candidates if not c.paired]


def _same_money(saving: Movement, other: Movement) -> bool:
    """A savings line and another statement's line describing one transfer of money.

    Signs: positive is money leaving that product. Paying a card leaves savings
    (positive) and enters the card (negative); every other pair has the same sign.
    """
    if saving.currency != "COP" or other.currency != "COP" or not _close(saving, other):
        return False
    expected = -other.amount_minor if other.hint == "card_payment" else other.amount_minor
    return saving.amount_minor == expected


def match_existing(candidates: list[Candidate], rows: list[dict]) -> tuple[list[tuple[Candidate, str]], list[Candidate]]:
    """Pair each statement movement with one existing movement of the same amount and nearby date."""
    free = [
        row for row in rows
        if row["owner"] == "santiago" and row["currency"] in {"COP", "USD"}
    ]
    used: set[str] = set()
    matched, missing = [], []
    for candidate in sorted(candidates, key=lambda c: c.movement.day):
        movement = candidate.movement
        options = [
            row for row in free
            if row["id"] not in used and row["currency"] == movement.currency
            and row["amount_minor"] == abs(movement.amount_minor)
            and abs((date.fromisoformat(row["day"]) - movement.day).days) <= 5
        ]
        if not options:
            missing.append(candidate)
            continue
        best = min(options, key=lambda row: abs((date.fromisoformat(row["day"]) - movement.day).days))
        used.add(best["id"])
        matched.append((candidate, best["id"]))
    return matched, missing


KIND_FOR_HINT = {
    "purchase": "purchase", "card_payment": "card_payment", "transfer": "transfer",
    "income": "income", "investment": "investment", "investment_withdrawal": "unknown",
    "loan": "loan", "unknown": "unknown",
}


def missing_event(candidate: Candidate, category: str, income_category: str) -> dict:
    """A pending movement for something the statement shows and no notice did."""
    statement, movement = candidate.statement, candidate.movement
    identity = f"{statement.identity}|{movement.day}|{movement.description}|{movement.amount_minor}|{movement.currency}"
    digest = hashlib.sha256(identity.encode()).hexdigest()
    kind = KIND_FOR_HINT[movement.hint]
    return {
        "id": "stmt:" + digest, "message_id": "statement:" + statement.identity,
        "day": movement.day.isoformat(), "merchant": movement.description[:150],
        "account": PRODUCTS.get(statement.product, "Desconocida"),
        "amount_minor": abs(movement.amount_minor), "currency": movement.currency,
        "category": income_category if kind == "income" else category, "kind": kind,
        "decision": None,
        "reason": f"Leído del extracto {PRODUCTS.get(statement.product, statement.product)} "
        f"*{statement.suffix}; no llegó por aviso. Confirma qué es.",
    }


@dataclass
class Reconciliation:
    """What each statement showed, what was already known and what was missing."""

    statements: list[Statement]
    matched: list[tuple[Candidate, str]]
    missing: list[Candidate]

    def checks(self) -> list[dict]:
        rows = []
        for statement in self.statements:
            matched = sum(c.statement is statement for c, _ in self.matched)
            missing = sum(c.statement is statement for c in self.missing)
            rows.append({
                "identity": statement.identity, "product": statement.product,
                "suffix": statement.suffix, "period_start": statement.period_start.isoformat(),
                "period_end": statement.period_end.isoformat(), "movements": len(statement.movements),
                "matched": matched, "missing": missing, "balance_minor": statement.balance_minor,
            })
        return rows


def reconcile(statements: list[Statement], rows: list[dict], start: date) -> Reconciliation:
    """Compare statements with known movements from start on; nothing is decided here."""
    candidates = [c for c in pair_internal(statements) if c.movement.day >= start]
    matched, missing = match_existing(candidates, rows)
    return Reconciliation(statements, matched, missing)
