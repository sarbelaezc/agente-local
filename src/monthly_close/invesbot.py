"""Invesbot plan reports: read the PDFs the user downloads by hand and check their figures.

Reports are read in memory only; nothing of the PDF is kept except the figures extracted
here. The parser is deterministic (no model) and refuses a report whose figures do not
add up exactly. Errors name the label or the file, never an amount.
"""
from __future__ import annotations

import calendar
import io
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from .statements import MAX_PDF_BYTES

INVESBOT_DIR = "invesbot"
INVESBOT_PRODUCT = "INVESBOT"
OWNERS = ("santiago", "manuela")

MONTHS = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7,
    "agosto": 8, "septiembre": 9, "sptiembre": 9,  # the bank writes "Sptiembre"
    "octubre": 10, "noviembre": 11, "diciembre": 12,
}
START_MONTHS = {
    "ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6, "jul": 7, "ago": 8,
    "sep": 9, "oct": 10, "nov": 11, "dec": 12, "dic": 12,
}
MONTH_NAMES = {number: name for name, number in MONTHS.items() if name != "sptiembre"}
SAVINGS_TRANSFER = "TRASLADO A FONDO DE INVERSION"
AMOUNT = re.compile(r"(-?)\s*\$\s*(-?)\s*(\d[\d.]*,\d{2})")


class ReportError(ValueError):
    """A report that cannot be read exactly; it is reported, never guessed."""


@dataclass(frozen=True)
class Report:
    """Figures are cumulative since the plan started, except the balance at month end."""

    owner: str
    plan: str
    period_end: date
    plan_start: date
    balance_minor: int
    opening_minor: int
    additional_minor: int
    additional_count: int
    withdrawals_minor: int
    returns_minor: int


def _pdf_text(content: bytes) -> str:
    """Text of pages 1 and 2 of a report PDF, read in memory."""
    if len(content) > MAX_PDF_BYTES:
        raise ReportError("Informe demasiado grande.")
    try:
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted:
            raise ReportError("El informe está cifrado.")
        return "\n".join(page.extract_text() or "" for page in reader.pages[:2])
    except PdfReadError as exc:
        raise ReportError("PDF de informe ilegible.") from exc


def _lines(text: str) -> list[str]:
    """Lines without ligatures, accents or repeated spaces."""
    plain = "".join(
        char for char in unicodedata.normalize("NFD", unicodedata.normalize("NFKC", text))
        if unicodedata.category(char) != "Mn"
    )
    return [re.sub(r"\s+", " ", line).strip() for line in plain.splitlines() if line.strip()]


def _minor(sign: str, inner_sign: str, digits: str) -> int:
    value = int(Decimal(digits.replace(".", "").replace(",", ".")) * 100)
    return -value if sign or inner_sign else value


def _find(lines: list[str], pattern: str, label: str) -> int:
    regex = re.compile(pattern, re.I)
    for index, line in enumerate(lines):
        if regex.search(line):
            return index
    raise ReportError(f"Falta el rótulo {label} en el informe.")


def _amount(lines: list[str], pattern: str, label: str) -> tuple[int, int]:
    """Amount written after a label, on its line or the next one; also its line index."""
    index = _find(lines, pattern, label)
    tail = re.split(pattern, lines[index], maxsplit=1, flags=re.I)[-1]
    for text in (tail, lines[index + 1] if index + 1 < len(lines) else ""):
        match = AMOUNT.search(text)
        if match:
            return _minor(*match.groups()), index
    raise ReportError(f"El rótulo {label} no trae un importe legible.")


def _month_end(month_name: str, year: str) -> date:
    month = MONTHS.get(month_name.lower())
    if month is None:
        raise ReportError("No se reconoce el periodo del informe.")
    return date(int(year), month, calendar.monthrange(int(year), month)[1])


def parse_report(text: str, owner: str) -> Report:
    """Figures of one report; raises ReportError unless they add up exactly."""
    lines = _lines(text)
    title = _find(lines, r"^Invesbot \| Reporte plan inversion", "Invesbot | Reporte plan inversión")
    if title + 1 >= len(lines):
        raise ReportError("Falta el nombre del plan en el informe.")
    plan = lines[title + 1]

    index = _find(lines, r"Reporte plan inversion - ", "Reporte plan inversión")
    period = re.search(r"Reporte plan inversion - (\w+) de (\d{4})", lines[index], re.I)
    if not period:
        raise ReportError("No se reconoce el periodo del informe.")
    period_end = _month_end(period.group(1), period.group(2))

    index = _find(lines, r"Fecha de inicio", "Fecha de inicio")
    start = re.search(
        r"(\w{3}) (\d{1,2}) de (\d{4})", " ".join(lines[index:index + 2]).split("inicio", 1)[-1],
    )
    if not start or start.group(1).lower() not in START_MONTHS:
        raise ReportError("No se reconoce la Fecha de inicio del informe.")
    try:
        plan_start = date(int(start.group(3)), START_MONTHS[start.group(1).lower()], int(start.group(2)))
    except ValueError as exc:
        raise ReportError("No se reconoce la Fecha de inicio del informe.") from exc

    balance, _ = _amount(lines, r"TOTAL DEL PLAN SALDO AL CIERRE DEL MES", "TOTAL DEL PLAN SALDO AL CIERRE DEL MES")
    opening, _ = _amount(lines, r"MONTO DE APERTURA", "MONTO DE APERTURA")
    additional, at = _amount(lines, r"APORTES ADICIONALES", "APORTES ADICIONALES")
    count = re.search(r"(\d+) aportes?\b", " ".join(lines[at:at + 3]), re.I)
    if not count:
        raise ReportError("Falta el número de aportes en APORTES ADICIONALES.")
    withdrawals, _ = _amount(lines, r"RETIROS ?REALIZADOS", "RETIROS REALIZADOS")
    returns, _ = _amount(lines, r"RENDIMIENTOS AL CIERRE DEL MES", "RENDIMIENTOS AL CIERRE DEL MES")

    if opening + additional - withdrawals + returns != balance:
        raise ReportError("Las cifras del informe no suman el saldo al cierre.")
    return Report(owner, plan, period_end, plan_start, balance, opening, additional,
                  int(count.group(1)), withdrawals, returns)


def read_reports(data_dir: Path) -> list[Report]:
    """Reports in invesbot/<owner>/*.pdf, once each, ordered by (owner, plan, period)."""
    found: dict[tuple[str, str, date], tuple[Report, str]] = {}
    for owner in OWNERS:
        folder = data_dir / INVESBOT_DIR / owner
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.pdf")):
            try:
                content = path.read_bytes()
                report = parse_report(_pdf_text(content), owner)
            except (OSError, ReportError) as exc:
                raise ReportError(f"{path.name}: {exc}") from exc
            key = (owner, report.plan, report.period_end)
            if key in found and found[key][0] != report:
                raise ReportError(f"{path.name}: difiere de {found[key][1]}, del mismo plan y mes.")
            found.setdefault(key, (report, path.name))
    return [found[key][0] for key in sorted(found)]


def period_checks(reports: list[Report]) -> list[dict]:
    """One statement check per report, with the figures of its own period.

    The period figures are the difference with the previous report of the same plan (a
    gap of months is covered by the difference); without one, the plan starts at balance 0.
    """
    checks: list[dict] = []
    previous: dict[tuple[str, str], Report] = {}
    for report in sorted(reports, key=lambda r: (r.owner, r.plan, r.period_end)):
        before = previous.get((report.owner, report.plan))
        opening = before.balance_minor if before else 0
        contributions = report.opening_minor + report.additional_minor - (
            before.opening_minor + before.additional_minor if before else 0)
        withdrawals = report.withdrawals_minor - (before.withdrawals_minor if before else 0)
        previous[(report.owner, report.plan)] = report
        checks.append({
            "identity": report_identity(report),
            "product": INVESBOT_PRODUCT,
            "suffix": report.plan,
            "owner": report.owner,
            "period_start": report.period_end.replace(day=1).isoformat(),
            "period_end": report.period_end.isoformat(),
            "movements": 0,
            "matched": 0,
            "missing": 0,
            "balance_minor": report.balance_minor,
            "opening_minor": opening,
            "contributions_minor": contributions,
            "withdrawals_minor": withdrawals,
            "returns_minor": report.balance_minor - opening - contributions + withdrawals,
        })
    return checks


def report_identity(report: Report) -> str:
    return f"{INVESBOT_PRODUCT}:{report.plan}:{report.period_end}:{report.owner}"


@dataclass(frozen=True)
class Outcome:
    """Movements to count as investment, and per report identity (expected, recorded).

    justified_by names, for each movement to restore, the report identity that shows it.
    """

    restore: list[str]
    differences: dict[str, tuple[int, int]]
    justified_by: dict[str, str] = field(default_factory=dict)


def _is_plan_movement(movement: dict, report: Report, plans_by_owner: dict[str, set[str]]) -> bool:
    """«Invesbot <plan>», or bare «Invesbot» when it is the owner's only plan."""
    merchant = movement["merchant"]
    return merchant == f"Invesbot {report.plan}" or (
        merchant == "Invesbot" and plans_by_owner.get(report.owner) == {report.plan})


def plan_outcome(
    reports: list[Report], movements: list[dict], plans_by_owner: dict[str, set[str]],
    restored: set[str],
) -> Outcome:
    """Compare each report's contributions with the recorded ones and propose corrections.

    Per plan, oldest report first: what a report misses is filled first with savings
    transfers of its month decided as investment, then with excluded contributions of the
    plan of its month, each only when it fits in what is still missing. What counted for
    one report keeps counting for the next. Movements in restored, and those whose
    restorable is false (Store._decide would refuse them as investment), are never proposed.
    """
    restore: list[str] = []
    differences: dict[str, tuple[int, int]] = {}
    justified_by: dict[str, str] = {}
    counted_transfers: dict[str, str] = {}  # transfer id -> the plan it counted for
    ordered = sorted(movements, key=lambda m: (m["day"], m["id"]))
    for report in sorted(reports, key=lambda r: (r.owner, r.plan, r.period_end)):
        start, end = report.plan_start.isoformat(), report.period_end.isoformat()
        month = end[:7]
        own = [m for m in ordered if m["owner"] == report.owner and start <= m["day"] <= end]
        plan = [m for m in own if _is_plan_movement(m, report, plans_by_owner)]
        recorded = sum(
            m["amount_minor"] if m["decision"] == "investment" or m["id"] in justified_by
            else -m["amount_minor"] if m["decision"] == "investment_withdrawal" else 0
            for m in plan
        ) + sum(m["amount_minor"] for m in own if counted_transfers.get(m["id"]) == report.plan)
        expected = report.opening_minor + report.additional_minor - report.withdrawals_minor
        identity = report_identity(report)
        for movement in own:
            missing = expected - recorded
            if missing <= 0 or movement["day"][:7] != month:
                continue
            if (movement["merchant"] == SAVINGS_TRANSFER and movement["decision"] == "investment"
                    and movement["id"] not in counted_transfers and movement["amount_minor"] <= missing):
                counted_transfers[movement["id"]] = report.plan
                recorded += movement["amount_minor"]
        for movement in plan:
            missing = expected - recorded
            if missing <= 0 or movement["day"][:7] != month:
                continue
            if (movement["decision"] == "exclude" and movement["restorable"]
                    and movement["id"] not in restored
                    and movement["id"] not in justified_by and movement["amount_minor"] <= missing):
                restore.append(movement["id"])
                justified_by[movement["id"]] = identity
                recorded += movement["amount_minor"]
        differences[identity] = (expected, recorded)
    return Outcome(restore, differences, justified_by)


def restore_note(identity: str) -> str:
    """Note of a movement counted as a contribution because of the report identity."""
    period_end = date.fromisoformat(identity.rsplit(":", 2)[1])
    return (f"Contado como aporte: el informe de Invesbot de {MONTH_NAMES[period_end.month]} "
            f"de {period_end.year} muestra el aporte.")
