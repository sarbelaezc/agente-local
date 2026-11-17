"""Bank statements: read, check and reconcile them with the movements read from notices.

Statements arrive as password-protected PDFs. They are decrypted and read in memory
only; nothing of the PDF is stored except the movements and balances extracted here.
Every parser is deterministic (no model) and refuses lines it cannot read exactly.
"""
from __future__ import annotations

import hashlib
import io
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from pypdf import PdfReader
from pypdf.errors import PdfReadError

FILE_NAME = re.compile(r"Extracto_\d+_(?P<period>\d{6})_(?P<product>[A-Z_]+?)_\w{4}\.pdf")
# Protección names its attachments "<id>_PensionVoluntaria_202610.pdf".
PROTECCION_FILE = re.compile(r"[0-9A-Za-z]+_(?P<kind>PensionVoluntaria|PensionesCesantias)_(?P<period>\d{6})\.pdf")
# Any statement attachment the reader may download: Bancolombia's or Protección's.
STATEMENT_NAME = re.compile(
    r"(?:Extracto_\d+_\d{6}_[A-Z_]+?_\w{4}|[0-9A-Za-z]+_(?:PensionVoluntaria|PensionesCesantias)_\d{6})\.pdf"
)
PROTECCION_PRODUCTS = {"PensionVoluntaria": "PROTECCION_VOLUNTARIA", "PensionesCesantias": "PROTECCION_CESANTIAS"}
# Protección's statement e-mails: their PDF is read here, so the mail itself is no notice.
PROTECCION_SENDER = "afp_proteccion@proteccion.com"
PROTECCION_SUBJECT = re.compile(r"^Extracto (?:Pensiones Voluntarias|de Cesantias) Protecci[oó]n", re.I)
PRODUCTS = {
    "CTA_AHORROS": "Cuenta de ahorros",
    "TARJETA_MASTERCARD": "Tarjeta de crédito Master",
    "TARJETA_AMEX": "Tarjeta de crédito Amex",
    "TARJETA_VISA": "Tarjeta de crédito Visa",
    "FIDUCUENTA": "Fiducuenta",
    "CREDITO": "Crédito de consumo",
    "HIPOTECARIO": "Crédito hipotecario",
    "PROTECCION_VOLUNTARIA": "Pensión voluntaria Protección",
    "PROTECCION_CESANTIAS": "Cesantías Protección",
}
MAX_PDF_BYTES = 10 * 1024 * 1024
# Protección's PDFs make pypdf warn about every embedded font (fontTools is not
# installed). The text is read correctly; those warnings only flood the terminal.
logging.getLogger("pypdf").setLevel(logging.ERROR)


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
    due_minor: int | None = None  # credit instalment due for the next period
    paid_minor: int | None = None  # payments a credit statement lists, kept even if its lines are not imported
    # Investment figures of the period (Protección): money put in, taken out (including
    # transfers to other funds) and everything else that changed the balance (net returns).
    opening_minor: int | None = None
    contributions_minor: int | None = None
    withdrawals_minor: int | None = None
    returns_minor: int | None = None

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
    # "RECARGA DE TARJETA CIVICA": topping up a prepaid card, such as the metro pass.
    if outflow and re.search(r"recarga\s+(?:de\s+)?tarjeta|tarjeta\s+civica", text):
        return "purchase"
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
        # The bank glues a reference to the merchant: a letter and five digits
        # ("VIRTUALC24280") or six or more digits ("MANEJO000000");
        # without it, payments to the same merchant group together.
        description = re.sub(r"\s*(?:[A-Z]\d{5}|\d{6,})$", "", re.sub(r"^\d{4,}\s+", "", match["desc"])) or match["desc"]
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
    statement = Statement(
        "CREDITO", number[1][-4:], end.replace(day=1), end,
        balance_minor=_us_amount(balance[1]), due_minor=_us_amount(total[2]),
    )
    paid = _us_amount(total[1])  # "abono anterior": what was paid since the last statement
    if paid:
        statement.movements.append(Movement(end, "Cuota de crédito pagada", paid, "COP", "loan"))
    statement.paid_minor = paid
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
    statement.paid_minor = sum(m.amount_minor for m in statement.movements)
    return statement


def identify(name: str) -> tuple[str, str] | None:
    """Product and period (YYYYMM) of a statement attachment name, or None."""
    match = FILE_NAME.fullmatch(name)
    if match:
        return match["product"], match["period"]
    match = PROTECCION_FILE.fullmatch(name)
    if match:
        return PROTECCION_PRODUCTS[match["kind"]], match["period"]
    return None


def is_statement_notice(sender: str, subject: str) -> bool:
    """A Protección statement e-mail: its PDF is the source, the mail has no movement."""
    return sender.lower() == PROTECCION_SENDER and bool(PROTECCION_SUBJECT.match(subject.strip()))


SPANISH_MONTHS = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7,
    "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
}
# "$ 12,345,678", "-$ 1,234", "+$ 0" or "$ -99.999": whole pesos with grouped thousands.
PESOS = r"[+-]?\s?\$\s?-?\d{1,3}(?:[.,]\d{3})*"
AMOUNT_ONLY = re.compile(rf"^(?P<amount>{PESOS})$")
# Protección prints every figure rounded to whole pesos, so their sum may differ from
# the printed final balance by a few pesos. A larger gap means a misread statement.
ROUNDING_MINOR = 10 * 100


def _adds_up(total: int, final: int) -> bool:
    return abs(total - final) <= ROUNDING_MINOR


def _pesos(text: str) -> int:
    """Whole pesos as Protección writes them, in minor units; anything else is refused."""
    clean = re.sub(r"\s", "", text)
    match = re.fullmatch(r"([+-]?)\$(-?)(\d{1,3}(?:[.,]\d{3})*)", clean)
    if not match or len(set(re.findall(r"[.,]", match[3]))) > 1:
        raise StatementError("Importe no reconocido en el extracto de Protección.")
    value = int(re.sub(r"[.,]", "", match[3])) * 100
    return -value if "-" in (match[1], match[2]) else value


def _spanish_day(text: str) -> date:
    """'30 de Octubre 2026' or 'Octubre 30 de 2026'."""
    found = re.fullmatch(r"(\d{1,2}) de ([a-z]+) (\d{4})", text.strip().lower()) or re.fullmatch(
        r"([a-z]+) (\d{1,2}) de (\d{4})", text.strip().lower(),
    )
    if not found:
        raise StatementError("Fecha del extracto de Protección no reconocida.")
    if found[1].isdigit():
        day, month, year = found[1], found[2], found[3]
    else:
        month, day, year = found[1], found[2], found[3]
    if month not in SPANISH_MONTHS:
        raise StatementError("Mes del extracto de Protección no reconocido.")
    return date(int(year), SPANISH_MONTHS[month], int(day))


def _labelled(lines: list[str], pattern: str, last: bool = False) -> int | None:
    """Amount written on the same line as a label."""
    found = [re.search(pattern + rf"\s*(?P<amount>{PESOS})\s*$", line, re.I) for line in lines]
    found = [match for match in found if match]
    if not found:
        return None
    return _pesos((found[-1] if last else found[0])["amount"])


def _amount_after(lines: list[str], index: int, limit: int = 4) -> int:
    """First line holding only an amount within a few lines after index."""
    for line in lines[index + 1:index + 1 + limit]:
        match = AMOUNT_ONLY.match(line)
        if match:
            return _pesos(match["amount"])
    raise StatementError("Saldo del extracto de Protección no encontrado.")


def _index(lines: list[str], pattern: str, start: int = 0) -> int:
    for index in range(start, len(lines)):
        if re.search(pattern, lines[index], re.I):
            return index
    raise StatementError("Sección del extracto de Protección no encontrada.")


def _figures(statement: Statement, opening: int, contributions: int, withdrawals: int, final: int) -> Statement:
    statement.opening_minor = opening
    statement.contributions_minor = contributions
    statement.withdrawals_minor = withdrawals
    statement.balance_minor = final
    statement.returns_minor = final - opening - contributions + withdrawals
    return statement


def _voluntary_period(lines: list[str]) -> tuple[date, date]:
    index = _index(lines, r"^Periodo:")
    text = lines[index].split(":", 1)[1].strip()
    if text.endswith(" a") and index + 1 < len(lines):  # the period ends on the next line
        text += " " + lines[index + 1]
    parts = re.split(r"\s+a\s+", text)
    if len(parts) != 2:
        raise StatementError("Periodo del extracto de Protección no reconocido.")
    return _spanish_day(parts[0]), _spanish_day(parts[1])


def _parse_voluntary_current(lines: list[str], statement: Statement) -> Statement:
    """Format since mid-2026: every figure of the summary sits next to its label."""
    opening = _labelled(lines, r"Saldo inicial del periodo:")
    contributions = _labelled(lines, r"El dinero que ingreso a tu cuenta\.")
    withdrawals = _labelled(lines, r"Dinero que sacaste de tu cuenta\.")
    transfers = _labelled(lines, r"Movimientos que realizaste hacia otras entidades\.")
    refund = _labelled(lines, r"Tu beneficio por mantener una inversion constante con Proteccion\.")
    net = _amount_after(lines, _index(lines, r"costo de administracion del mes"), 2)
    final = _amount_after(lines, _index(lines, r"^Saldo final del periodo$"), 2)
    figures = (opening, contributions, withdrawals, transfers, refund)
    if any(value is None for value in figures):
        raise StatementError("Resumen del extracto de Protección incompleto.")
    out = abs(withdrawals) + abs(transfers)
    if not _adds_up(opening + abs(contributions) - out + net + abs(refund), final):
        raise StatementError("El resumen del extracto de Protección no cuadra con su saldo final.")
    return _figures(statement, opening, abs(contributions), out, final)


def _parse_voluntary_previous(lines: list[str], statement: Statement) -> Statement:
    """Format until mid-2026: the summary prints out of order, so figures come from the
    labelled detail section and must add up exactly to the final balance."""
    start = _index(lines, r"^Saldo inicial$")
    opening = _amount_after(lines, start + 1, 2)
    final = _amount_after(lines, _index(lines, r"^Saldo final$", start), 2)
    # The detail section is printed after an empty template with the same labels, so
    # the last labelled line is the real one.
    contributions = _labelled(lines, r"^Aportes", last=True) or 0
    withdrawals = _labelled(lines, r"^Retiros", last=True) or 0
    net = _labelled(lines, r"^Rendimiento neto")  # after fees, as the first page states it
    refund = _labelled(lines, r"^Devolucion de comision", last=True) or 0
    transfers = _labelled(lines, r"^Traslados de recursos de/hacia fondos de pensiones", last=True) or 0
    if net is None:
        raise StatementError("Extracto de Protección sin rendimiento neto.")
    if not _adds_up(opening + abs(contributions) - abs(withdrawals) + net + abs(refund) + transfers, final):
        raise StatementError("El detalle del extracto de Protección no cuadra con su saldo final.")
    out = abs(withdrawals) + max(-transfers, 0)
    return _figures(statement, opening, abs(contributions) + max(transfers, 0), out, final)


def parse_voluntary(text: str) -> Statement:
    """Protección voluntary pension: balance, contributions, withdrawals and returns."""
    lines = _lines(text)
    start, end = _voluntary_period(lines)
    statement = Statement("PROTECCION_VOLUNTARIA", "", start, end)
    if any(re.search(r"Saldo inicial del periodo:", line, re.I) for line in lines):
        return _parse_voluntary_current(lines, statement)
    return _parse_voluntary_previous(lines, statement)


def parse_severance(text: str) -> Statement:
    """Protección severance fund (cesantías): one summary line for the half year."""
    lines = _lines(text)
    period = _first(lines, r"comprendido entre:\s*(\d{4})/(\d{2})/(\d{2}) a (\d{4})/(\d{2})/(\d{2})")
    if not period:
        raise StatementError("Periodo del extracto de cesantías no reconocido.")
    values = None
    for index in range(_index(lines, r"^Saldo Final$"), min(len(lines), _index(lines, r"^Saldo Final$") + 4)):
        found = re.findall(PESOS, lines[index])
        if len(found) == 5 and re.fullmatch(rf"(?:{PESOS}\s*){{5}}", lines[index]):
            values = [_pesos(item) for item in found]
            break
    if values is None:
        raise StatementError("Resumen del extracto de cesantías no encontrado.")
    opening, contributions, withdrawals, valuation, final = values
    if not _adds_up(opening + abs(contributions) - abs(withdrawals) + valuation, final):
        raise StatementError("El resumen del extracto de cesantías no cuadra con su saldo final.")
    statement = Statement(
        "PROTECCION_CESANTIAS", "",
        date(int(period[1]), int(period[2]), int(period[3])), date(int(period[4]), int(period[5]), int(period[6])),
    )
    return _figures(statement, opening, abs(contributions), abs(withdrawals), final)


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
    if product == "PROTECCION_VOLUNTARIA":
        return parse_voluntary(text)
    if product == "PROTECCION_CESANTIAS":
        return parse_severance(text)
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
                "due_minor": statement.due_minor, "paid_minor": statement.paid_minor,
                "opening_minor": statement.opening_minor,
                "contributions_minor": statement.contributions_minor,
                "withdrawals_minor": statement.withdrawals_minor,
                "returns_minor": statement.returns_minor,
            })
        return rows


def reconcile(statements: list[Statement], rows: list[dict], start: date) -> Reconciliation:
    """Compare statements with known movements from start on; nothing is decided here."""
    candidates = [c for c in pair_internal(statements) if c.movement.day >= start]
    matched, missing = match_existing(candidates, rows)
    return Reconciliation(statements, matched, missing)
