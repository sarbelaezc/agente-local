"""Invoice review panel: approval rules, local server and the page in a real browser.

Only synthetic workbooks, messages and PDFs in temporary folders are used.
"""
from __future__ import annotations

import json
import threading
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from openpyxl import Workbook

from mail_invoices import batch_review as br
from mail_invoices.batch_state import BatchMessage, BatchStateStore
from mail_invoices.classification_decisions import ClassificationDecisionStore
from mail_invoices.documents import InvoiceCandidate
from mail_invoices.folders import IncomingMessage
from mail_invoices.review import ReviewItem
from mail_invoices.review_server import ReviewPanel
from mail_invoices.settings import MailSettings
from mail_invoices.workbook import InvoiceWorkbook, ReviewedInvoice

CLIENT_ID = "11111111-2222-3333-4444-555555555555"
FOLDERS = ["Salud", "Hogar/Servicios"]


def candidate(number: str, source: str = "classification-1/FE.xml") -> InvoiceCandidate:
    return InvoiceCandidate(
        source, "digest", number, "Proveedor SAS", date(2026, 11, 10), Decimal("119000"), "COP", method="xml",
    )


def message(identity: str = "m1", numbers=("FE-1", "FE-2"), **changes) -> BatchMessage:
    items = [ReviewItem(candidate(number), f"Factura {identity}") for number in numbers]
    base = BatchMessage(
        identity, "facturas@proveedor.example", f"Factura {identity}", items,
        IncomingMessage(
            "facturas@proveedor.example", f"Factura {identity}", "Proveedor SAS", "Cuerpo",
            tuple({"supplier": "Proveedor SAS", "descriptions": "Consulta", "evidence": ""} for _ in numbers),
        ),
        folder="Salud", reason="historial del proveedor",
    )
    return replace(base, **changes)


def fields(number: str, **changes) -> dict:
    values = {
        "company": "Proveedor", "legal_name": "Proveedor SAS", "issued": "2026-11-10",
        "amount": "119000", "number": number, "currency": "COP",
    }
    return {**values, **changes}


@pytest.fixture
def book(tmp_path) -> InvoiceWorkbook:
    path = tmp_path / "book.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Hoja1"
    for column, header in enumerate(("Empresa", "Razón Social", "Fecha", "Valor"), 2):
        sheet.cell(2, column, header)
    workbook.save(path)
    workbook.close()
    return InvoiceWorkbook(path, tmp_path / "backups")


@pytest.fixture
def journal(tmp_path) -> BatchStateStore:
    return BatchStateStore(tmp_path / "state.sqlite3", "test")


def submission(*messages, approver="Santi", legacy=None) -> dict:
    return {"approver": approver, "messages": list(messages), "legacy": legacy or {}}


# ---------------------------------------------------------------- approval rules


def test_an_approval_saves_the_whole_message_before_any_side_effect(book, journal):
    messages = [message()]
    original = book.path.read_bytes()
    staged = br.approve_batch(messages, FOLDERS, journal, book, submission(
        {"id": "m1", "folder": "Hogar/Servicios", "invoices": [fields("FE-1"), fields("FE-2", company="Otra")]},
    ))
    [saved] = journal.load()
    assert staged[0] is messages[0] and saved.state == "approved"
    assert (saved.folder, saved.approver) == ("Hogar/Servicios", "Santi")
    assert [invoice.company for invoice in saved.invoices] == ["Proveedor", "Otra"]
    assert saved.approved_at.endswith("+00:00")
    assert book.path.read_bytes() == original and not book.backup_dir.exists()  # Excel untouched


@pytest.mark.parametrize("chosen,approver,error", [
    ([], "Santi", "al menos un correo"),
    ([{"id": "m1", "folder": "Salud", "invoices": [fields("FE-1"), fields("FE-2")]}], "  ", "quién aprueba"),
    ([{"id": "m1", "folder": "No existe", "invoices": [fields("FE-1"), fields("FE-2")]}], "Santi", "carpeta existente"),
    ([{"id": "m1", "folder": "Salud", "invoices": [fields("FE-1")]}], "Santi", "todas las facturas"),
    ([{"id": "m1", "folder": "Salud", "invoices": [fields("FE-1"), fields("FE-2", amount="cero")]}], "Santi", "valor"),
    ([{"id": "otro", "folder": "Salud", "invoices": []}], "Santi", "desconocido"),
])
def test_incomplete_approvals_save_nothing(book, journal, chosen, approver, error):
    with pytest.raises(ValueError, match=error):
        br.approve_batch([message()], FOLDERS, journal, book, submission(*chosen, approver=approver))
    assert journal.load() == []


def test_one_invalid_message_blocks_the_whole_batch(book, journal):
    messages = [message("m1"), message("m2")]
    with pytest.raises(ValueError):
        br.approve_batch(messages, FOLDERS, journal, book, submission(
            {"id": "m1", "folder": "Salud", "invoices": [fields("FE-1"), fields("FE-2")]},
            {"id": "m2", "folder": "", "invoices": [fields("FE-1"), fields("FE-2")]},
        ))
    assert journal.load() == [] and all(m.state == "pending" for m in messages)


def test_messages_with_errors_or_in_progress_cannot_be_approved(book, journal):
    for blocked in (message(errors=["Adjunto ilegible"]), message(state="registered")):
        with pytest.raises(ValueError, match="no se puede aprobar"):
            br.approve_batch([blocked], FOLDERS, journal, book, submission(
                {"id": "m1", "folder": "Salud", "invoices": [fields("FE-1"), fields("FE-2")]},
            ))


def test_previous_approvals_resume_without_a_new_selection(book, journal):
    previous = message(state="approved")
    assert br.approve_batch([previous], FOLDERS, journal, book, submission(approver="")) == []


def test_a_historical_match_needs_an_explicit_decision(book, journal, monkeypatch):
    monkeypatch.setattr(book, "legacy_matches", lambda invoice: (7,) if invoice.number == "FE-2" else ())
    chosen = {"id": "m1", "folder": "Salud", "invoices": [fields("FE-1"), fields("FE-2")]}
    with pytest.raises(br.LegacyDecisionRequired) as asked:
        br.approve_batch([message()], FOLDERS, journal, book, submission(chosen))
    assert [(q.key, q.row, q.number) for q in asked.value.questions] == [("m1#1", 7, "FE-2")]
    assert journal.load() == []
    br.approve_batch([message()], FOLDERS, journal, book, submission(chosen, legacy={"m1#1": "link"}))
    assert journal.load()[0].invoices[1].legacy_row == 7
    br.approve_batch([message()], FOLDERS, journal, book, submission(chosen, legacy={"m1#1": "new"}))
    assert journal.load()[0].invoices[1].confirmed_new is True


def test_an_ambiguous_historical_match_stops(book, journal, monkeypatch):
    monkeypatch.setattr(book, "legacy_matches", lambda invoice: (4, 9))
    with pytest.raises(ValueError, match="varias filas"):
        br.approve_batch([message()], FOLDERS, journal, book, submission(
            {"id": "m1", "folder": "Salud", "invoices": [fields("FE-1"), fields("FE-2")]},
        ))


def test_a_blocked_message_is_approved_again_with_its_corrections(book, journal):
    blocked = message(state="blocked", last_error="Conflicto de empresa", approver="Antes")
    br.approve_batch([blocked], FOLDERS, journal, book, submission(
        {"id": "m1", "folder": "Salud", "invoices": [fields("FE-1", company="Corregida"), fields("FE-2")]},
    ))
    [saved] = journal.load()
    assert (saved.state, saved.last_error, saved.approver) == ("approved", "", "Santi")
    assert saved.invoices[0].company == "Corregida"


# ---------------------------------------------------------------- what the panel shows


def settings(tmp_path, book) -> MailSettings:
    return MailSettings(CLIENT_ID, book.path, book.backup_dir, "Hoja1", 3, approver="Santi")


def test_the_batch_view_offers_documents_and_hints_but_no_paths(tmp_path, book):
    run = tmp_path / "run"
    (run / "classification-1").mkdir(parents=True)
    (run / "classification-1" / "FE.pdf").write_bytes(b"%PDF-1.4 sintetico")
    (run / "classification-1" / "FE.xml").write_bytes(b"<xml/>")
    view, pdfs = br.batch_view([message(numbers=("FE-1",))], FOLDERS, settings(tmp_path, book), run, book)
    [shown] = view["messages"]
    invoice = shown["invoices"][0]
    assert shown["selectable"] and shown["editable"] and shown["state_text"] == "Pendiente de aprobación"
    assert invoice["values"]["company"] == "Proveedor SAS" and invoice["company_hint"] == "legal_name"
    assert pdfs[invoice["pdf"]] == (run / "classification-1" / "FE.pdf").resolve()
    assert str(run) not in json.dumps(view)  # the page gets a token, never a path


def test_progress_and_result_follow_the_messages(tmp_path, book):
    messages = [message("m1"), message("m2")]
    view, _ = br.batch_view(messages, FOLDERS, settings(tmp_path, book), None, book)
    messages[0].state = "moved"
    messages[1].state, messages[1].last_error = "blocked", "Excel abierto"
    refreshed = br.refresh_view(view, messages)
    assert [m["state_text"] for m in refreshed["messages"]] == ["Archivado y verificado", "Bloqueado: requiere una nueva aprobación"]
    assert br.batch_result(messages, True)["summary"] == "1 archivado y verificado; 1 pendiente o con error"


def test_classification_saves_only_confirmed_examples(tmp_path):
    store = ClassificationDecisionStore(tmp_path / "decisions.sqlite3")
    context = {"sender": "a@b.example", "subject": "Factura", "supplier": "Proveedor", "body": "Cuerpo", "invoices": []}
    report = {"folders": FOLDERS, "messages": [
        {"subject": "Factura", "sender": "a@b.example", "errors": [], "eligible": True, "folder": "Salud",
         "invoices": [{"number": "FE-1", "status": "registered"}], "classification_context": context},
        {"subject": "", "sender": "c@d.example", "errors": ["Sin adjuntos"], "eligible": False, "folder": None, "invoices": []},
    ]}
    view = br.classification_view(report, store.path, "Santi")
    assert [m["confirmable"] for m in view["messages"]] == [True, False]
    assert view["messages"][0]["invoices"][0]["status"] == "Registrada en Excel"
    assert br.save_classification(report, store, {"approver": "Santi", "confirmed": [{"id": "0", "folder": "Salud"}]}) == 1
    assert store.load()[0].folder == "Salud" and report["messages"][0]["confirmed_folder"] == "Salud"
    with pytest.raises(ValueError, match="contexto"):
        br.save_classification(report, store, {"approver": "Santi", "confirmed": [{"id": "1", "folder": "Salud"}]})
    br.record_reviewed_folders(report, {"0": "Hogar/Servicios", "1": "Inventada"})
    assert [m["reviewed_folder"] for m in report["messages"]] == ["Hogar/Servicios", None]


# ---------------------------------------------------------------- local server


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def started(view=None, actions=None, pdfs=None, clock=None) -> ReviewPanel:
    panel = ReviewPanel(
        view or (lambda: {"mode": "batch", "messages": []}), actions or {}, pdfs,
        open_browser=lambda url: None, clock=clock or Clock(),
    )
    panel.start()
    return panel


def post(panel, path, body, **headers):
    base = {"Origin": panel.url.rstrip("/"), "X-CSRF-Token": panel._csrf, "Content-Type": "application/json"}
    return httpx.post(panel.url + path.lstrip("/"), content=json.dumps(body), headers={**base, **headers})


def test_the_server_answers_only_loopback_requests_with_its_token(tmp_path):
    panel = started(actions={"/api/approve": lambda raw: {"finish": True}})
    try:
        page = httpx.get(panel.url)
        assert page.status_code == 200 and "Facturas" in page.text
        assert "default-src 'none'" in page.headers["Content-Security-Policy"]
        assert httpx.get(panel.url + "api/view", headers={"Host": "evil.example"}).status_code == 403
        assert post(panel, "/api/approve", {}, **{"X-CSRF-Token": "otro"}).status_code == 403
        assert post(panel, "/api/approve", {}, Origin="http://evil.example").status_code == 403
        assert httpx.post(panel.url + "api/approve", content="{}", headers={
            "Origin": panel.url.rstrip("/"), "X-CSRF-Token": panel._csrf, "Content-Type": "text/plain",
        }).status_code == 403
        assert post(panel, "/api/desconocida", {}).status_code == 404
        assert not panel._decided.is_set()
    finally:
        panel.stop()


def test_rejected_requests_with_a_body_always_get_their_403():
    # The body of a rejected request is read and dropped: otherwise Windows can reset the
    # connection and the client sees a network error instead of the refusal.
    panel = started(actions={"/api/approve": lambda raw: {"finish": True}})
    try:
        with httpx.Client(trust_env=False) as client:
            headers = {"Origin": "http://evil.example", "X-CSRF-Token": panel._csrf, "Content-Type": "application/json"}
            body = json.dumps({"relleno": "x" * 20000})
            statuses = [
                client.post(panel.url + "api/approve", content=body, headers=headers).status_code
                for _ in range(300)
            ]
        assert statuses == [403] * 300
        assert not panel._decided.is_set()
    finally:
        panel.stop()


def test_an_approval_ends_the_review_and_later_actions_are_refused():
    panel = started(actions={"/api/approve": lambda raw: {"finish": True}})
    try:
        assert post(panel, "/api/approve", {"x": 1}).json() == {"ok": True}
        assert panel.wait_for_decision(poll=0.01) is True
        panel.executing()
        assert httpx.get(panel.url + "api/view").json()["phase"] == "executing"
        assert post(panel, "/api/approve", {"x": 1}).status_code == 409
    finally:
        panel.stop()


def test_validation_errors_and_their_details_reach_the_page():
    def approve(raw):
        error = ValueError("Decide cada posible duplicado")
        error.payload = {"legacy": [{"key": "m1#0", "row": 7}]}
        raise error

    panel = started(actions={"/api/approve": approve})
    try:
        answer = post(panel, "/api/approve", {"x": 1})
        assert answer.status_code == 400
        assert answer.json() == {"error": "Decide cada posible duplicado", "legacy": [{"key": "m1#0", "row": 7}]}
        assert not panel._decided.is_set()
    finally:
        panel.stop()


def test_closing_the_panel_or_its_tab_never_approves():
    clock = Clock()
    panel = started(clock=clock)
    try:
        assert post(panel, "/api/close", {"folders": {}}).json() == {"ok": True}
        assert panel.wait_for_decision(poll=0.01) is False
        assert panel.result["message"].startswith("Cerraste el panel")
    finally:
        panel.stop()
    panel = started(clock=clock)
    try:
        httpx.get(panel.url + "api/view")  # the page checked in once, then the tab was closed
        clock.now += 1000
        assert panel.wait_for_decision(poll=0.01) is False
        assert "sin aprobar" in panel.result["message"]
    finally:
        panel.stop()


def test_pdfs_are_served_only_by_their_token(tmp_path):
    pdf = tmp_path / "factura.pdf"
    pdf.write_bytes(b"%PDF-1.4 sintetico")
    panel = started(pdfs={"abc": pdf})
    try:
        shown = httpx.get(panel.url + "api/pdf/abc")
        assert shown.status_code == 200 and shown.content == pdf.read_bytes()
        assert shown.headers["Content-Type"] == "application/pdf"
        assert "sandbox" not in shown.headers["Content-Security-Policy"]
        assert httpx.get(panel.url + "api/pdf/otro").status_code == 404
        assert httpx.get(panel.url + "api/pdf/..%2Ffactura.pdf").status_code == 404
    finally:
        panel.stop()


def test_finishing_waits_for_the_page_to_close_the_panel():
    panel = started()
    threading.Timer(0.2, lambda: post(panel, "/api/close", {})).start()
    httpx.get(panel.url + "api/view")
    panel.finish({"summary": "1 archivado y verificado"}, poll=0.05)
    with pytest.raises(httpx.HTTPError):
        httpx.get(panel.url + "api/view", timeout=1)


# ---------------------------------------------------------------- the page in a browser


def test_the_page_requires_a_complete_approval_and_shows_progress(tmp_path, book, journal, new_page):
    messages = [message("m1"), message("m2", errors=["Adjunto ilegible: FE.pdf"])]
    view, pdfs = br.batch_view(messages, FOLDERS, settings(tmp_path, book), None, book)
    approvals = []

    def approve(raw):
        approvals.append(raw)
        br.approve_batch(messages, FOLDERS, journal, book, raw)
        return {"finish": True}

    panel = started(view=lambda: br.refresh_view(view, messages), actions={"/api/approve": approve}, pdfs=pdfs)
    try:
        page = new_page()
        page.goto(panel.url)
        page.get_by_role("heading", name="Facturas por aprobar").wait_for()
        cards = page.locator("article.card")
        assert cards.count() == 2
        assert cards.nth(1).locator(".choose").is_disabled()  # extraction errors block it
        assert not cards.nth(0).locator(".choose").is_checked()  # nothing is preselected
        page.get_by_role("button", name="Aprobar y ejecutar").click()
        page.get_by_role("alert").get_by_text("Selecciona al menos un correo apto").wait_for()
        assert approvals == []

        cards.nth(0).locator(".choose").check()
        page.get_by_text("1 correo y 2 facturas seleccionados").wait_for()
        amount = cards.nth(0).get_by_label("Valor").first
        amount.fill("")
        page.get_by_role("button", name="Aprobar y ejecutar 1 correo").click()
        page.get_by_role("link", name="Valor: Completa este dato.").wait_for()
        assert amount.get_attribute("aria-invalid") == "true" and approvals == []

        amount.fill("119.000")
        cards.nth(0).get_by_label("Carpeta destino").select_option("Hogar/Servicios")
        page.get_by_role("button", name="Aprobar y ejecutar 1 correo").click()
        page.get_by_text("Ejecutando el lote").wait_for()
        assert approvals[0]["messages"][0]["folder"] == "Hogar/Servicios"
        assert journal.load()[0].state == "approved"

        messages[0].state = "moved"
        panel.finish({"summary": "1 archivado y verificado", "failed": False}, wait=False)
    finally:
        panel._closed.set()


# ---------------------------------------------------------------- the panels the commands open


def drive(target, *args):
    """Run a command that opens a panel; return the thread, the panel URL and its token."""
    import time

    from mail_invoices import review_server

    urls = []
    thread = threading.Thread(target=target, args=args, daemon=True)
    original = review_server.webbrowser.open
    review_server.webbrowser.open = urls.append
    try:
        thread.start()
        deadline = time.monotonic() + 10
        while not urls and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        review_server.webbrowser.open = original
    url = urls[0]
    csrf = httpx.get(url + "api/view").json()["csrf"]
    return thread, url, csrf


def send(url, csrf, path, body):
    return httpx.post(url + path.lstrip("/"), content=json.dumps(body), headers={
        "Origin": url.rstrip("/"), "X-CSRF-Token": csrf, "Content-Type": "application/json",
    })


def test_the_batch_panel_asks_for_legacy_decisions_then_approves(tmp_path, book, journal, monkeypatch):
    from mail_invoices.batch_workflow import BatchReviewPanel

    monkeypatch.setattr(InvoiceWorkbook, "legacy_matches", lambda self, invoice: (5,) if invoice.number == "FE-2" else ())
    messages = [message()]
    outcome = []
    panel_settings = settings(tmp_path, book)

    def review():
        outcome.append(BatchReviewPanel(messages, FOLDERS, panel_settings, journal, None).run())

    thread, url, csrf = drive(review)
    chosen = {"id": "m1", "folder": "Salud", "invoices": [fields("FE-1"), fields("FE-2")]}
    asked = send(url, csrf, "/api/approve", submission(chosen))
    assert asked.status_code == 400 and asked.json()["legacy"][0]["row"] == 5
    assert send(url, csrf, "/api/approve", submission(chosen, legacy={"m1#1": "new"})).json() == {"ok": True}
    thread.join(10)
    assert outcome == [True] and journal.load()[0].invoices[1].confirmed_new


def test_a_storage_failure_while_approving_is_reported_not_crashed(tmp_path, book, journal, monkeypatch):
    import sqlite3

    from mail_invoices.batch_workflow import BatchReviewPanel

    def broken(self, messages):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(BatchStateStore, "save", broken)
    panel = BatchReviewPanel([message()], FOLDERS, settings(tmp_path, book), journal, None)
    panel.panel._open_browser = lambda url: None
    panel.panel.start()
    try:
        csrf = httpx.get(panel.panel.url + "api/view").json()["csrf"]
        answer = send(panel.panel.url, csrf, "/api/approve", submission(
            {"id": "m1", "folder": "Salud", "invoices": [fields("FE-1"), fields("FE-2")]},
        ))
        assert answer.status_code == 400 and "database is locked" in answer.json()["error"]
    finally:
        panel.panel.stop()


def test_the_classification_panel_saves_examples_and_records_the_folders(tmp_path):
    from mail_invoices import workflow

    store = ClassificationDecisionStore(tmp_path / "decisions.sqlite3")
    context = {"sender": "a@b.example", "subject": "Factura", "supplier": "Proveedor", "body": "Cuerpo", "invoices": []}
    report = {"folders": FOLDERS, "messages": [
        {"subject": "Factura", "sender": "a@b.example", "errors": [], "eligible": True, "folder": "Salud",
         "invoices": [], "classification_context": context},
    ]}
    thread, url, csrf = drive(workflow.review_classification, report, store, "Santi")
    saved = send(url, csrf, "/api/save-decisions", {"approver": "Santi", "confirmed": [{"id": "0", "folder": "Salud"}]})
    assert saved.json() == {"ok": True, "saved": 1}
    assert send(url, csrf, "/api/close", {"folders": {"0": "Hogar/Servicios"}}).json() == {"ok": True}
    thread.join(10)
    assert not thread.is_alive()
    assert report["messages"][0]["reviewed_folder"] == "Hogar/Servicios"
    assert store.load()[0].folder == "Salud"
