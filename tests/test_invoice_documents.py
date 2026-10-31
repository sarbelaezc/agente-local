from datetime import date
from decimal import Decimal
from io import BytesIO
from zipfile import ZipFile

import pytest
from pypdf import PdfWriter
from pypdf.generic import (
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
)

from mail_invoices.documents import DocumentError, parse_document, parse_money

XML = b"""<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"
xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2">
<cbc:ID>TEST-001</cbc:ID><cbc:IssueDate>2024-01-02</cbc:IssueDate>
<cac:AccountingSupplierParty><cac:Party><cac:PartyLegalEntity>
<cbc:RegistrationName>Example supplier SAS</cbc:RegistrationName>
</cac:PartyLegalEntity></cac:Party></cac:AccountingSupplierParty>
<cac:LegalMonetaryTotal><cbc:PayableAmount currencyID="COP">1234.56</cbc:PayableAmount>
</cac:LegalMonetaryTotal></Invoice>"""


def test_xml_supplier_total_and_all_years_are_preserved():
    invoice = parse_document(XML, "invoice.xml")[0]
    assert invoice.number == "TEST-001"
    assert invoice.legal_name == "Example supplier SAS"
    assert invoice.issued == date(2024, 1, 2)
    assert invoice.amount == Decimal("1234.56")
    assert invoice.currency == "COP"
    assert not invoice.missing_fields


def test_zip_xml_is_read_without_extracting_files():
    stream = BytesIO()
    with ZipFile(stream, "w") as archive:
        archive.writestr("documents/invoice.xml", XML)
    assert parse_document(stream.getvalue(), "invoice.zip")[0].number == "TEST-001"


def test_unsafe_archive_path_is_rejected():
    stream = BytesIO()
    with ZipFile(stream, "w") as archive:
        archive.writestr("../invoice.xml", XML)
    with pytest.raises(DocumentError, match="unsafe path"):
        parse_document(stream.getvalue(), "invoice.zip")


def test_xml_entity_expansion_is_rejected():
    with pytest.raises(DocumentError, match="unsafe"):
        parse_document(b'<!DOCTYPE foo [<!ENTITY x "secret">]><foo>&x;</foo>', "bad.xml")


@pytest.mark.parametrize("value", ["1.234,56", "1,234.56", "1234.56"])
def test_pdf_amount_separators(value):
    assert parse_money(value) == Decimal("1234.56")


def test_pdf_text_is_extracted_and_missing_supplier_requires_review():
    writer = PdfWriter()
    page = writer.add_blank_page(width=600, height=800)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
    })
    content = DecodedStreamObject()
    content.set_data(
        b"BT /F1 12 Tf 30 760 Td (Factura No. PDF-001) Tj "
        b"0 -20 Td (Fecha de expedicion: 2024-01-02) Tj "
        b"0 -20 Td (Total a pagar: COP $1.234,56) Tj ET"
    )
    page[NameObject("/Contents")] = writer._add_object(content)
    stream = BytesIO()
    writer.write(stream)
    candidate = parse_document(stream.getvalue(), "invoice.pdf")[0]
    assert candidate.number == "PDF-001"
    assert candidate.issued == date(2024, 1, 2)
    assert candidate.amount == Decimal("1234.56")
    assert candidate.currency == "COP"
    assert "legal_name" in candidate.missing_fields
