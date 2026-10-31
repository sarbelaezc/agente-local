from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import PurePosixPath
from zipfile import BadZipFile, ZipFile

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException
from pypdf import PdfReader
from pypdf.errors import PdfReadError
from xml.etree.ElementTree import ParseError

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_ZIP_BYTES = 30 * 1024 * 1024
INVOICE_NS = "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"


class DocumentError(ValueError):
    pass


@dataclass(frozen=True)
class InvoiceCandidate:
    source: str
    digest: str
    number: str = ""
    legal_name: str = ""
    issued: date | None = None
    amount: Decimal | None = None
    currency: str = ""
    evidence: str = ""

    @property
    def missing_fields(self) -> tuple[str, ...]:
        values = {
            "number": self.number,
            "legal_name": self.legal_name,
            "issued": self.issued,
            "amount": self.amount,
            "currency": self.currency,
        }
        return tuple(key for key, value in values.items() if not value)


def _unique_match(pattern: str, text: str) -> str:
    matches = {match.strip() for match in re.findall(pattern, text, flags=re.I)}
    return matches.pop() if len(matches) == 1 else ""


def parse_money(text: str) -> Decimal:
    value = text.strip().replace("$", "").replace(" ", "").replace("\u00a0", "")
    if not re.fullmatch(r"\d+(?:[.,]\d+)*", value):
        raise DocumentError("Invalid monetary value")
    if "," in value and "." in value:
        decimal_separator = "," if value.rfind(",") > value.rfind(".") else "."
        thousands_separator = "." if decimal_separator == "," else ","
        value = value.replace(thousands_separator, "").replace(decimal_separator, ".")
    elif "," in value or "." in value:
        separator = "," if "," in value else "."
        parts = value.split(separator)
        if len(parts) > 2 or len(parts[-1]) == 3:
            if not all(len(part) == 3 for part in parts[1:]):
                raise DocumentError("Ambiguous monetary separators")
            value = "".join(parts)
        else:
            value = value.replace(separator, ".")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise DocumentError("Invalid monetary value") from exc
    if not result.is_finite() or result < 0:
        raise DocumentError("Invalid monetary value")
    return result


def _xml_invoice(data: bytes, source: str) -> InvoiceCandidate:
    try:
        root = ElementTree.fromstring(data, forbid_dtd=True)
        if root.tag.endswith("}AttachedDocument"):
            embedded = [
                node.text.strip()
                for node in root.iter()
                if node.tag.endswith("}Description")
                and node.text
                and "<Invoice" in node.text
            ]
            if len(embedded) != 1:
                raise DocumentError("AttachedDocument does not contain one unambiguous invoice")
            root = ElementTree.fromstring(embedded[0], forbid_dtd=True)
    except (ParseError, DefusedXmlException) as exc:
        raise DocumentError("Invalid or unsafe invoice XML") from exc
    if root.tag != f"{{{INVOICE_NS}}}Invoice":
        raise DocumentError("XML is not a supported UBL invoice")
    ns = {
        "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
        "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
    }
    number = root.findtext("cbc:ID", default="", namespaces=ns).strip()
    legal = root.findtext(
        "cac:AccountingSupplierParty/cac:Party/cac:PartyLegalEntity/cbc:RegistrationName",
        default="", namespaces=ns,
    ).strip()
    issued_text = root.findtext("cbc:IssueDate", default="", namespaces=ns)
    payable = root.find("cac:LegalMonetaryTotal/cbc:PayableAmount", ns)
    try:
        issued = date.fromisoformat(issued_text) if issued_text else None
        amount = Decimal(payable.text) if payable is not None and payable.text else None
    except (ValueError, InvalidOperation) as exc:
        raise DocumentError("Invalid XML invoice date or amount") from exc
    if amount is not None and (not amount.is_finite() or amount < 0):
        raise DocumentError("Invalid XML invoice amount")
    currency = payable.attrib.get("currencyID", "") if payable is not None else ""
    return InvoiceCandidate(
        source, hashlib.sha256(data).hexdigest(), number, legal, issued, amount,
        currency, "UBL Invoice: supplier, IssueDate and PayableAmount",
    )


def _pdf_invoice(data: bytes, source: str) -> InvoiceCandidate:
    try:
        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted:
            raise DocumentError("Encrypted PDF requires manual review")
        if len(reader.pages) > 30:
            raise DocumentError("PDF exceeds the 30-page pilot limit")
        pieces = []
        size = 0
        for page in reader.pages:
            text = page.extract_text() or ""
            size += len(text)
            if size > 60000:
                raise DocumentError("PDF text exceeds the pilot limit")
            pieces.append(text)
        text = "\n".join(pieces)
    except PdfReadError as exc:
        raise DocumentError("Unreadable PDF requires manual review") from exc
    if not text.strip():
        raise DocumentError("Scanned PDF has no extractable text; manual review required")
    if not re.search(r"\bfactura\b", text, flags=re.I):
        raise DocumentError("PDF is not identifiable as an invoice")
    number = _unique_match(
        r"factura(?:\s+electr[oó]nica)?(?:\s+de\s+venta)?\s*"
        r"(?:n[oú]mero|n[uú]m\.?|nro\.?|no\.?|n[°º]|#)\s*:?\s*([A-Z0-9][A-Z0-9-]*)",
        text,
    )
    issued_text = _unique_match(
        r"fecha(?:\s+de)?\s+(?:emisi[oó]n|expedici[oó]n|factura)\s*:?\s*"
        r"(\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4})", text,
    )
    total_text = _unique_match(
        r"(?:total\s+a\s+pagar|valor\s+total|total\s+factura)\s*:?\s*(?:COP\s*)?"
        r"\$?\s*(\d[\d.,]*)", text,
    )
    issued = None
    if issued_text:
        try:
            if "/" in issued_text:
                day, month, year = issued_text.split("/")
                issued = date(int(year), int(month), int(day))
            else:
                issued = date.fromisoformat(issued_text)
        except ValueError as exc:
            raise DocumentError("Invalid PDF invoice date") from exc
    amount = parse_money(total_text) if total_text else None
    currency = "COP" if re.search(r"\bCOP\b", text) else ""
    return InvoiceCandidate(
        source=source, digest=hashlib.sha256(data).hexdigest(), number=number,
        issued=issued, amount=amount, currency=currency, evidence=text[:2500],
    )


def parse_document(data: bytes, source: str) -> list[InvoiceCandidate]:
    """Parse local invoice bytes without extracting archives or following links."""
    if len(data) > MAX_FILE_BYTES:
        raise DocumentError("Attachment exceeds the 10 MB pilot limit")
    suffix = PurePosixPath(source.replace("\\", "/")).suffix.lower()
    if suffix == ".xml":
        return [_xml_invoice(data, source)]
    if suffix == ".pdf":
        return [_pdf_invoice(data, source)]
    if suffix != ".zip":
        raise DocumentError("Only XML, PDF and ZIP attachments are supported")
    candidates = []
    try:
        with ZipFile(BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > 100 or sum(entry.file_size for entry in entries) > MAX_ZIP_BYTES:
                raise DocumentError("ZIP exceeds the pilot limits")
            for entry in entries:
                if entry.is_dir():
                    continue
                path = PurePosixPath(entry.filename.replace("\\", "/"))
                if path.is_absolute() or ".." in path.parts:
                    raise DocumentError("ZIP contains an unsafe path")
                if path.suffix.lower() not in (".pdf", ".xml"):
                    continue
                if entry.flag_bits & 1 or entry.file_size > MAX_FILE_BYTES:
                    raise DocumentError("ZIP contains an encrypted or oversized document")
                if entry.file_size > max(entry.compress_size, 1) * 200:
                    raise DocumentError("ZIP compression ratio exceeds the pilot limit")
                with archive.open(entry) as stream:
                    content = stream.read(MAX_FILE_BYTES + 1)
                candidates.extend(parse_document(content, f"{source}:{entry.filename}"))
    except BadZipFile as exc:
        raise DocumentError("Invalid ZIP attachment") from exc
    if not candidates:
        raise DocumentError("ZIP contains no supported invoices")
    return candidates
