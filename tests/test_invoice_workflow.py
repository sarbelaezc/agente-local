from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from mail_invoices import classification, main, workflow
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
        ("--classify-invoices", "classify_invoices"),
    ],
)
def test_cli_routes_to_workflow_with_unchanged_arguments(monkeypatch, settings, flag, handler):
    arguments = ["mail-invoices", flag]
    if flag == "--analyze-file":
        arguments.append("invoice.xml")
    arguments.extend(["--days", "7", "--no-model"])
    if flag == "--classify-invoices":
        arguments.extend(["--candidate-text", "SYNTH-001"])
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
    elif flag == "--classify-invoices":
        assert calls == [(settings, 7, [], False, "SYNTH-001")]
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


def test_classification_cleans_run_attachments(tmp_path, monkeypatch):
    attachments = tmp_path / "attachments"
    settings = MailSettings(
        tmp_path / "opera.exe", tmp_path, tmp_path / "book.xlsx", tmp_path,
        "Hoja1", 1, attachments_dir=attachments,
    )
    created = []

    def simulate(run_settings, *args, **kwargs):
        path = run_settings.attachments_dir / "classification-1" / "invoice.zip"
        path.parent.mkdir()
        path.write_bytes(b"synthetic")
        created.append(path)
        return {"messages": []}

    monkeypatch.setattr(classification, "_simulate_classification", simulate)

    assert classification.simulate_classification(settings, 30, [], False) == {"messages": []}
    assert not created[0].exists()
    assert list(attachments.iterdir()) == []


def test_classification_report_uses_relative_archive_source(tmp_path):
    candidate = InvoiceCandidate(
        source="invoice.zip:invoice.xml", digest="synthetic", number="TEST-1",
        legal_name="Synthetic SAS", issued=date(2026, 10, 31),
        amount=Decimal("100"), currency="COP", method="xml",
    )

    result = classification._with_archive_path(
        candidate, tmp_path / "classification-1" / "invoice.zip",
    )

    assert result.source == "classification-1/invoice.zip:invoice.xml"


def test_download_analysis_cleans_only_its_run_files(tmp_path, monkeypatch, settings):
    attachments = tmp_path / "attachments"
    attachments.mkdir()
    unrelated = attachments / "keep.txt"
    unrelated.write_text("user file", encoding="utf-8")
    actual_settings = replace(settings, attachments_dir=attachments)
    created = []

    def download(_settings, _days, target_dir):
        path = target_dir / "invoice.xml"
        path.write_bytes(b"synthetic")
        created.append(path)
        return [("synthetic message", path)], []

    monkeypatch.setattr(workflow, "_download_batch", download)
    monkeypatch.setattr(workflow, "_build_ocr", lambda _settings: None)
    monkeypatch.setattr(workflow, "_summarize_file", lambda *args: [])

    assert workflow.download_and_analyze(actual_settings, 30, False) == 0
    assert not created[0].exists()
    assert unrelated.read_text(encoding="utf-8") == "user file"
    assert list(attachments.iterdir()) == [unrelated]


def test_review_cancel_cleans_run_attachments(tmp_path, monkeypatch, settings):
    attachments = tmp_path / "attachments"
    actual_settings = replace(settings, attachments_dir=attachments)
    created = []

    def download(_settings, _days, target_dir):
        path = target_dir / "invoice.xml"
        path.write_bytes(b"synthetic")
        created.append(path)
        return [("synthetic message", path)], []

    monkeypatch.setattr(workflow, "_download_batch", download)
    monkeypatch.setattr(workflow, "_build_ocr", lambda _settings: None)
    monkeypatch.setattr(workflow, "_extract_file", lambda *args: ([], []))

    assert workflow.review_invoices(actual_settings, 30, False) == 1
    assert not created[0].exists()
    assert list(attachments.iterdir()) == []
