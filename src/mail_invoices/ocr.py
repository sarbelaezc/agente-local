from __future__ import annotations

import re
import subprocess
from io import BytesIO
from pathlib import Path

import pypdfium2 as pdfium

from mail_invoices.documents import MAX_PDF_TEXT, DocumentError

MAX_OCR_PAGES = 5
MAX_PAGE_PIXELS = 40_000_000
RENDER_SCALE = 300 / 72


def render_pdf_pages(
    data: bytes, max_pages: int = MAX_OCR_PAGES, scale: float = RENDER_SCALE
) -> list[bytes]:
    """Render PDF pages to grayscale PNG bytes in memory, without writing files."""
    try:
        pdf = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as exc:
        raise DocumentError("Scanned PDF cannot be rendered for OCR") from exc
    images = []
    try:
        if len(pdf) > max_pages:
            raise DocumentError(f"Scanned PDF exceeds the {max_pages}-page OCR limit")
        for index in range(len(pdf)):
            page = pdf[index]
            try:
                width, height = page.get_size()
                if width * scale * height * scale > MAX_PAGE_PIXELS:
                    raise DocumentError("Scanned PDF page is too large for OCR")
                image = page.render(scale=scale, grayscale=True).to_pil()
                buffer = BytesIO()
                image.save(buffer, format="PNG")
                images.append(buffer.getvalue())
            finally:
                page.close()
    finally:
        pdf.close()
    return images


class TesseractOcr:
    """Local OCR through the Tesseract CLI; images are passed through stdin."""

    def __init__(
        self,
        executable: Path,
        tessdata_dir: Path | None = None,
        languages: str = "spa+eng",
        timeout_seconds: float = 60.0,
        max_pages: int = MAX_OCR_PAGES,
    ) -> None:
        if not re.fullmatch(r"[a-z_]+(?:\+[a-z_]+)*", languages):
            raise ValueError("OCR languages must look like 'spa+eng'")
        self._executable = executable
        self._tessdata_dir = tessdata_dir
        self._languages = languages
        self._timeout = timeout_seconds
        self._max_pages = max_pages

    def _command(self) -> list[str]:
        command = [str(self._executable), "stdin", "stdout", "-l", self._languages]
        if self._tessdata_dir is not None:
            command += ["--tessdata-dir", str(self._tessdata_dir)]
        return command + ["--psm", "3"]

    def image_text(self, png: bytes) -> str:
        if not self._executable.is_file():
            raise DocumentError("Tesseract executable not found; scanned PDF requires manual review")
        try:
            completed = subprocess.run(
                self._command(), input=png, capture_output=True,
                timeout=self._timeout, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise DocumentError("OCR timed out; scanned PDF requires manual review") from exc
        except OSError as exc:
            raise DocumentError("Tesseract could not be started") from exc
        if completed.returncode != 0:
            raise DocumentError("Tesseract failed; check the OCR languages and tessdata_dir")
        return completed.stdout.decode("utf-8", errors="replace")

    def pdf_text(self, data: bytes) -> str:
        texts = []
        size = 0
        for png in render_pdf_pages(data, self._max_pages):
            text = self.image_text(png)
            size += len(text)
            if size > MAX_PDF_TEXT:
                raise DocumentError("OCR text exceeds the pilot limit")
            texts.append(text)
        return "\n".join(texts)
