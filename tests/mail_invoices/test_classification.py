from datetime import date
from decimal import Decimal

import pytest
from openpyxl import Workbook

from mail_invoices.classification import classification_ready
from mail_invoices.workbook import InvoiceWorkbook


@pytest.mark.parametrize(
    ("statuses", "errors", "ready"),
    [
        (["registered"], [], True),
        (["registered", "registered"], [], True),
        ([], [], False),
        (["registered", "not_registered"], [], False),
        (["historical_review"], [], False),
        (["conflict"], [], False),
        (["incomplete"], [], False),
        (["registered"], ["encrypted PDF"], False),
    ],
)
def test_only_fully_registered_messages_without_errors_are_ready(statuses, errors, ready):
    assert classification_ready(statuses, errors) is ready


@pytest.mark.parametrize(
    ("number", "issued", "amount", "expected"),
    [
        ("TEST-1", date(2026, 10, 31), Decimal("100"), "registered"),
        ("TEST-1", date(2026, 10, 31), Decimal("200"), "conflict"),
        ("NEW", date(2026, 10, 31), Decimal("100"), "historical_review"),
        ("NEW", date(2026, 10, 30), Decimal("100"), "not_registered"),
        ("", date(2026, 10, 31), Decimal("100"), "incomplete"),
    ],
)
def test_registration_checks_are_readonly(tmp_path, number, issued, amount, expected):
    path = tmp_path / "synthetic.xlsx"
    book = Workbook()
    sheet = book.active
    sheet.title = "Hoja1"
    for column, header in enumerate(("Empresa", "Razón Social", "Fecha", "Valor", "Número de factura"), 2):
        sheet.cell(2, column, header)
    for row, num in ((3, "TEST-1"), (4, None)):
        for col, value in enumerate(("Salud", "Synthetic SAS", date(2026, 10, 31), 100, num), 2):
            sheet.cell(row, col, value)
    book.save(path)
    book.close()
    original = path.read_bytes()
    store = InvoiceWorkbook(path, tmp_path / "backups")
    assert store.registration_status(number, "Synthetic SAS", issued, amount, "COP") == expected
    assert path.read_bytes() == original
    assert not (tmp_path / "backups").exists()
