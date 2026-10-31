from datetime import date
from decimal import Decimal

import pytest
from openpyxl import Workbook, load_workbook

from mail_invoices.documents import InvoiceCandidate
from mail_invoices.main import _prefer_xml_candidates
from mail_invoices.review_ui import (
    ReviewItem,
    record_approved_batch,
    reviewed_invoice_from_fields,
)
from mail_invoices.workbook import InvoiceWorkbook


def test_review_fields_are_converted_and_validated():
    invoice = reviewed_invoice_from_fields(review_fields())
    assert invoice.issued == date(2026, 10, 27)
    assert invoice.amount == Decimal("50900.00")
    assert invoice.currency == "COP"


def review_fields():
    return {
        "company": "Mercado",
        "legal_name": "Proveedor SAS",
        "issued": "2026-10-27",
        "amount": "50.900,00",
        "number": "FH4640622",
        "currency": "cop",
    }


def test_review_fields_reject_unsupported_currency():
    fields = review_fields()
    fields["currency"] = "USD"
    with pytest.raises(ValueError, match="Only confirmed COP"):
        reviewed_invoice_from_fields(fields)


def test_record_requires_explicit_batch_approval_and_uses_only_synthetic_workbook(tmp_path):
    path = tmp_path / "synthetic.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Hoja1"
    for column, header in enumerate(["Empresa", "Razón Social", "Fecha", "Valor"], 2):
        sheet.cell(2, column, header)
    workbook.save(path)
    workbook.close()
    store = InvoiceWorkbook(path, tmp_path / "backups")
    invoice = reviewed_invoice_from_fields(review_fields())
    original = path.read_bytes()

    assert record_approved_batch(store, [invoice], approved=False) is None
    assert path.read_bytes() == original

    results = record_approved_batch(store, [invoice], approved=True)
    assert results is not None
    assert results[0].inserted
    saved = load_workbook(path)
    assert saved["Hoja1"]["F3"].value == "FH4640622"
    saved.close()


def test_zip_pdf_duplicate_prefers_xml_candidate():
    pdf = ReviewItem(
        InvoiceCandidate(
            source="tmp/invoice.zip:invoice.pdf",
            digest="pdf",
            number="FE-123",
            method="pdf_text",
        ),
        "message",
    )
    xml = ReviewItem(
        InvoiceCandidate(
            source="tmp/invoice.zip:invoice.xml",
            digest="xml",
            number="FE-123",
            method="xml",
        ),
        "message",
    )
    other = ReviewItem(
        InvoiceCandidate(
            source="tmp/other.zip:other.xml",
            digest="other",
            number="FE-456",
            method="xml",
        ),
        "message",
    )
    assert _prefer_xml_candidates([pdf, xml, other]) == [xml, other]
