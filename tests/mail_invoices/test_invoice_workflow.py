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


def test_extract_file_keeps_valid_zip_invoices_and_reports_failed_member(monkeypatch, tmp_path):
    path = tmp_path / "invoice.zip"
    path.write_bytes(b"synthetic")
    candidate = InvoiceCandidate(source="invoice.zip:a.xml", digest="test", method="pdf_text")

    def parse(data, source, ocr, member_errors):
        member_errors.append(("invoice.zip:other.pdf", "Scanned PDF"))
        return [candidate]

    def unavailable_model(*args):
        raise LocalModelError("Model unavailable")

    monkeypatch.setattr(workflow, "parse_document", parse)
    monkeypatch.setattr(workflow, "complete_candidate", unavailable_model)
    extracted, issues = workflow._extract_file(path, None, object())
    assert extracted == [(candidate, (), "Model unavailable")]
    assert issues == [
        {"source": "invoice.zip:other.pdf", "error": "Scanned PDF", "requires_review": True},
    ]


@pytest.mark.parametrize("flag", ["--process-one-invoice", "--review-invoices", "--download-attachments"])
def test_removed_legacy_modes_are_rejected(monkeypatch, flag):
    monkeypatch.setattr("sys.argv", ["mail-invoices", flag])
    with pytest.raises(SystemExit):
        main.main()
