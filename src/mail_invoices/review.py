from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from mail_invoices.documents import InvoiceCandidate, parse_money
from mail_invoices.workbook import InvoiceWorkbook, RecordResult, ReviewedInvoice


@dataclass(frozen=True)
class ReviewItem:
    candidate: InvoiceCandidate
    message: str
    rejected_model_fields: tuple[str, ...] = ()
    model_error: str | None = None


def reviewed_invoice_from_fields(fields: dict[str, str]) -> ReviewedInvoice:
    try:
        issued = date.fromisoformat(fields["issued"].strip())
        amount: Decimal = parse_money(fields["amount"])
    except ValueError as error:
        raise ValueError(f"Fecha o valor inválido: {error}") from error
    invoice = ReviewedInvoice(
        company=fields["company"].strip(),
        legal_name=fields["legal_name"].strip(),
        issued=issued,
        amount=amount,
        number=fields["number"].strip(),
        currency=fields["currency"].strip().upper(),
    )
    invoice.validate()
    return invoice


def suggested_company(suggestions: dict[str, str], legal_name: str) -> str:
    """Uses a unique historical company or falls back to the document legal name."""
    normalized_name = " ".join(legal_name.split())
    return suggestions.get(normalized_name.casefold(), "") or normalized_name


def record_approved_batch(
    store: InvoiceWorkbook,
    invoices: list[ReviewedInvoice],
    *,
    approved: bool,
) -> list[RecordResult] | None:
    if not approved:
        return None
    return store.record(invoices)
