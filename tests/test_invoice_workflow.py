from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from mail_invoices import main, workflow
from mail_invoices.documents import DocumentError, InvoiceCandidate
from mail_invoices.local_model import LocalModelError
from mail_invoices.ocr import TesseractOcr
from mail_invoices.settings import MailSettings


@pytest.fixture
def settings(tmp_path):
    return MailSettings(
        tmp_path / "opera.exe", tmp_path / "profile", tmp_path / "book.xlsx",
        tmp_path / "backups", "Hoja1", 1,
    )


@pytest.mark.parametrize(
    ("flag", "handler"),
    [
        ("--login", "login"),
        ("--scan-inbox", "scan_inbox"),
        ("--download-attachments", "download_and_analyze"),
        ("--review-invoices", "review_invoices"),
        ("--analyze-file", "analyze_file"),
    ],
)
def test_cli_routes_to_workflow_with_unchanged_arguments(monkeypatch, settings, flag, handler):
    arguments = ["mail-invoices", flag]
    if flag == "--analyze-file":
        arguments.append("invoice.xml")
    arguments.extend(["--days", "7", "--no-model"])
    monkeypatch.setattr("sys.argv", arguments)
    monkeypatch.setattr(main, "load_settings", lambda path: settings)
    calls = []

    def run(*args):
        calls.append(args)
        return 1

    monkeypatch.setattr(workflow, handler, run)
    assert main.main() == 1
    if flag == "--login":
        assert calls == [(settings,)]
    elif flag == "--scan-inbox":
        assert calls == [(settings, 7)]
    elif flag == "--analyze-file":
        assert calls == [(settings, Path("invoice.xml"), False)]
    else:
        assert calls == [(settings, 7, False)]


def test_review_workflow_preserves_document_and_model_errors(monkeypatch, tmp_path, settings):
    attachment = tmp_path / "invoice.pdf"
    attachment.write_bytes(b"synthetic")
    candidate = InvoiceCandidate(
        source="invoice.pdf", digest="test", number="TEST-1",
        legal_name="Synthetic SAS", issued=date(2026, 10, 31),
        amount=Decimal("100"), currency="COP", method="pdf_text",
    )
    issue = {"source": "other.pdf", "error": "encrypted", "requires_review": True}
    monkeypatch.setattr(workflow, "_download_batch", lambda *args: ([("message", attachment)], [issue]))
    monkeypatch.setattr(workflow, "parse_document", lambda *args: [candidate])

    def unavailable_model(*args):
        raise LocalModelError("Model unavailable")

    monkeypatch.setattr(workflow, "complete_candidate", unavailable_model)
    reviews = []

    def review(items, issues, actual_settings):
        reviews.append((items, issues, actual_settings))
        return False

    monkeypatch.setattr(workflow, "run_invoice_review", review)
    assert workflow.review_invoices(settings, 30, True) == 1
    items, issues, actual_settings = reviews[0]
    assert items[0].candidate == candidate
    assert items[0].model_error == "Model unavailable"
    assert items[0].message == "message"
    assert issues == [issue]
    assert actual_settings == settings


def test_failed_extraction_keeps_readonly_json_shape(monkeypatch, tmp_path):
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"synthetic")

    def invalid_document(*args):
        raise DocumentError("Encrypted PDF")

    monkeypatch.setattr(workflow, "parse_document", invalid_document)
    results = workflow._summarize_file(path, TesseractOcr(tmp_path / "tesseract.exe"), None)
    assert results == [{
        "source": "broken.pdf", "error": "Encrypted PDF", "requires_review": True,
    }]
