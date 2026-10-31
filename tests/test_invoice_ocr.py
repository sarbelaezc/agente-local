import os
from datetime import date
from decimal import Decimal
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from mail_invoices.documents import DocumentError, parse_document
from mail_invoices.local_model import validate_local_url
from mail_invoices.ocr import TesseractOcr

TESSERACT = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")
TESSDATA = Path(os.environ.get("LOCALAPPDATA", "")) / "MailInvoiceAgent" / "tessdata"
FONT = Path(r"C:\Windows\Fonts\arial.ttf")


def _scanned_pdf(lines):
    image = Image.new("L", (1700, 900), 255)
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(str(FONT), 44) if FONT.exists() else ImageFont.load_default()
    for index, line in enumerate(lines):
        draw.text((80, 80 + index * 90), line, fill=0, font=font)
    stream = BytesIO()
    image.save(stream, format="PDF", resolution=150)
    return stream.getvalue()


PDF_LINES = [
    "Factura No. OCR-2024-77",
    "Fecha de expedicion: 15/03/2024",
    "Total a pagar: COP $250.000",
]


class FakeOcr:
    def pdf_text(self, data):
        return "\n".join(PDF_LINES)


def test_scanned_pdf_without_ocr_requires_manual_review():
    with pytest.raises(DocumentError, match="manual review"):
        parse_document(_scanned_pdf(PDF_LINES), "scan.pdf")


def test_scanned_pdf_uses_ocr_text_and_requires_review():
    candidate = parse_document(_scanned_pdf(PDF_LINES), "scan.pdf", FakeOcr())[0]
    assert candidate.method == "ocr"
    assert candidate.number == "OCR-2024-77"
    assert candidate.issued == date(2024, 3, 15)
    assert candidate.amount == Decimal("250000")
    assert candidate.requires_review


def test_missing_tesseract_fails_safely(tmp_path):
    ocr = TesseractOcr(tmp_path / "missing.exe")
    with pytest.raises(DocumentError, match="not found"):
        parse_document(_scanned_pdf(PDF_LINES), "scan.pdf", ocr)


def test_ocr_languages_are_validated():
    with pytest.raises(ValueError):
        TesseractOcr(TESSERACT, languages="spa; del")


@pytest.mark.parametrize("url", ["http://10.0.0.5:11434", "https://127.0.0.1:11434"])
def test_remote_ollama_urls_are_rejected(url):
    with pytest.raises(ValueError):
        validate_local_url(url)


@pytest.mark.skipif(
    not TESSERACT.is_file() or not (TESSDATA / "spa.traineddata").is_file(),
    reason="Local Tesseract with Spanish data is not installed",
)
def test_real_tesseract_reads_a_scanned_invoice():
    ocr = TesseractOcr(TESSERACT, TESSDATA, "spa+eng")
    candidate = parse_document(_scanned_pdf(PDF_LINES), "scan.pdf", ocr)[0]
    assert candidate.method == "ocr"
    assert candidate.number == "OCR-2024-77"
    assert candidate.issued == date(2024, 3, 15)
    assert candidate.amount == Decimal("250000")
