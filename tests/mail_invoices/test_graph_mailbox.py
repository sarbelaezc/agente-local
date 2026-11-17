"""Invoice mailbox over Microsoft Graph, with a scripted Graph and fictitious data only."""
from __future__ import annotations

from datetime import date
from hashlib import sha256
from types import SimpleNamespace
from urllib.parse import unquote, urlparse

import httpx
import pytest

from mail_invoices import graph_mailbox as gm
from mail_invoices.documents import MAX_FILE_BYTES

ROOT = "https://graph.microsoft.com/v1.0"
SENDER = "facturas@proveedor.example"


def message(identifier, subject="Factura FE-1", preview="Adjuntamos su factura", sender=SENDER, received="2026-11-10T15:00:00Z"):
    return {
        "id": identifier, "subject": subject, "bodyPreview": preview, "isRead": False,
        "receivedDateTime": received, "from": {"emailAddress": {"address": sender}},
    }


class FakeGraph:
    """Scripted Graph routes used by the invoice mailbox; records every request."""

    def __init__(self) -> None:
        self.system = {"inbox": "id-inbox", "junkemail": "id-junk", "drafts": "id-drafts",
                       "sentitems": "id-sent", "deleteditems": "id-deleted", "conversationhistory": "id-conv"}
        self.root = [
            {"id": "id-inbox", "displayName": "Bandeja de entrada", "childFolderCount": 0},
            {"id": "id-salud", "displayName": "Salud", "childFolderCount": 1},
            {"id": "id-hogar", "displayName": "Hogar", "childFolderCount": 0},
            {"id": "id-notas", "displayName": "Notas", "childFolderCount": 0},
        ]
        self.children = {"id-salud": [{"id": "id-eps", "displayName": "EPS", "childFolderCount": 0}]}
        self.inbox = [message("m1"), message("m2", subject="Promocion", preview="Descuentos", sender="ofertas@tienda.example")]
        self.details = {"m1": {"folder": "id-inbox", "body": "Cuerpo de la factura FE-1", "subject": "Factura FE-1"}}
        self.attachments = {"m1": [
            {"@odata.type": "#microsoft.graph.fileAttachment", "id": "a1", "name": "FE-1.xml", "size": 10},
            {"@odata.type": "#microsoft.graph.fileAttachment", "id": "a2", "name": "..\\FE-1.pdf", "size": 10},
            {"@odata.type": "#microsoft.graph.fileAttachment", "id": "a3", "name": "logo.png", "size": 10},
            {"@odata.type": "#microsoft.graph.referenceAttachment", "id": "a4", "name": "nube.pdf", "size": 10},
            {"@odata.type": "#microsoft.graph.fileAttachment", "id": "a5", "name": "enorme.zip", "size": 3 * MAX_FILE_BYTES},
        ]}
        self.files = {"a1": b"<xml/>", "a2": b"%PDF-1.4"}
        self.history = {"id-salud": [message("h1", subject="Factura antigua")]}
        self.requests: list[tuple[str, str]] = []
        self.moves: list[tuple[str, str]] = []

    def get_json(self, url, params=None):
        gm.validate_request("GET", url)
        self.requests.append(("GET", url))
        path = unquote(urlparse(url).path).removeprefix("/v1.0/me/")
        parts = path.split("/")
        if parts[0] == "mailFolders":
            if len(parts) == 1:
                return {"value": self.root}
            folder = parts[1]
            if len(parts) == 2:
                if folder not in self.system:
                    raise gm.MailboxError("Graph respondio 404")
                return {"id": self.system[folder]}
            if parts[2] == "childFolders":
                return {"value": self.children.get(folder, [])}
            if folder == "inbox":
                return {"value": self.inbox}
            return {"value": self.history.get(folder, [])}
        identifier = parts[1]
        if len(parts) == 2:
            detail = self.details[identifier]
            return {
                "id": identifier, "subject": detail["subject"], "parentFolderId": detail["folder"],
                "from": {"emailAddress": {"address": SENDER}}, "body": {"content": detail["body"]},
            }
        return {"value": self.attachments.get(identifier, [])}

    def get_bytes(self, url):
        gm.validate_request("GET", url)
        return self.files[unquote(urlparse(url).path).split("/")[-2]]

    def post_json(self, url, payload):
        gm.validate_request("POST", url)
        identifier = unquote(urlparse(url).path).split("/")[-2]
        self.moves.append((identifier, payload["destinationId"]))
        self.details[identifier]["folder"] = payload["destinationId"]
        return {"id": identifier}

    def close(self):
        pass


@pytest.fixture
def graph():
    return FakeGraph()


@pytest.fixture
def mailbox(graph):
    return gm.GraphMailbox(graph)


# ---------------------------------------------------------------- allow-list and client


@pytest.mark.parametrize("method,url", [
    ("DELETE", f"{ROOT}/me/messages/m1"),
    ("PATCH", f"{ROOT}/me/messages/m1"),
    ("POST", f"{ROOT}/me/sendMail"),
    ("POST", f"{ROOT}/me/messages/m1/copy"),
    ("POST", f"{ROOT}/me/messages/m1/move?extra=1"),
    ("POST", f"{ROOT}/me/mailFolders"),
    ("GET", "https://evil.example/v1.0/me/messages"),
    ("GET", "http://graph.microsoft.com/v1.0/me/messages"),
    ("GET", f"{ROOT.replace('v1.0', 'beta')}/me/messages"),
    ("GET", f"{ROOT}/users/otra/messages"),
])
def test_only_reads_and_the_move_action_are_allowed(method, url):
    with pytest.raises(gm.MailboxError):
        gm.validate_request(method, url)


def test_reads_and_moving_one_message_are_allowed():
    assert gm.validate_request("GET", f"{ROOT}/me/mailFolders")
    assert gm.validate_request("POST", f"{ROOT}/me/messages/AAMk%3D/move")


class Response:
    def __init__(self, status=200, payload=None, content=b"", headers=None):
        self.status_code, self._payload, self.content = status, payload, content
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class Http:
    def __init__(self, *responses):
        self.responses, self.requests = list(responses), []

    def _next(self, method, url, headers, body):
        self.requests.append((method, url, headers, body))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def get(self, url, params=None, headers=None):
        return self._next("GET", url, headers, params)

    def post(self, url, json=None, headers=None):
        return self._next("POST", url, headers, json)

    def close(self):
        pass


def client(*responses):
    http = Http(*responses)
    return gm.InvoiceGraphClient(lambda: "token", http=http, sleep=lambda seconds: None), http


def test_requests_carry_the_token_and_ask_for_immutable_ids_and_text_bodies():
    graph_client, http = client(Response(payload={"value": []}))
    assert graph_client.get_json(f"{ROOT}/me/mailFolders") == {"value": []}
    headers = http.requests[0][2]
    assert headers["Authorization"] == "Bearer token"
    assert 'IdType="ImmutableId"' in headers["Prefer"] and 'body-content-type="text"' in headers["Prefer"]


def test_a_forbidden_request_is_refused_before_anything_is_sent():
    graph_client, http = client()
    with pytest.raises(gm.MailboxError, match="no permitida"):
        graph_client.post_json(f"{ROOT}/me/sendMail", {})
    assert http.requests == []


def test_reads_are_retried_but_an_ambiguous_move_is_not():
    graph_client, http = client(Response(503), Response(payload={"ok": True}))
    assert graph_client.get_json(f"{ROOT}/me/mailFolders") == {"ok": True}
    graph_client, http = client(Response(503), Response(payload={"id": "m1"}))
    with pytest.raises(gm.MailboxError, match="503"):
        graph_client.post_json(f"{ROOT}/me/messages/m1/move", {"destinationId": "x"})
    assert len(http.requests) == 1


def test_a_throttled_move_waits_because_graph_did_not_act():
    graph_client, http = client(Response(429, headers={"Retry-After": "1"}), Response(201, payload={"id": "m1"}))
    assert graph_client.post_json(f"{ROOT}/me/messages/m1/move", {"destinationId": "x"}) == {"id": "m1"}
    assert len(http.requests) == 2


def test_a_lost_move_answer_is_not_repeated_but_a_lost_read_is():
    graph_client, http = client(httpx.ConnectError("caido"))
    with pytest.raises(gm.MailboxError, match="contactar"):
        graph_client.post_json(f"{ROOT}/me/messages/m1/move", {"destinationId": "x"})
    assert len(http.requests) == 1
    graph_client, http = client(httpx.ConnectError("caido"), Response(payload={"value": []}))
    assert graph_client.get_json(f"{ROOT}/me/mailFolders") == {"value": []}


def test_graph_errors_report_code_and_message_and_non_json_is_refused():
    graph_client, _ = client(Response(403, payload={"error": {"code": "ErrorAccessDenied", "message": "Sin permiso"}}))
    with pytest.raises(gm.MailboxError, match="403.*ErrorAccessDenied"):
        graph_client.get_json(f"{ROOT}/me/mailFolders")
    graph_client, _ = client(Response(200, payload=None))
    with pytest.raises(gm.MailboxError, match="JSON"):
        graph_client.get_json(f"{ROOT}/me/mailFolders")
    graph_client, _ = client(Response(200, payload=["lista"]))
    with pytest.raises(gm.MailboxError, match="inesperada"):
        graph_client.get_json(f"{ROOT}/me/mailFolders")
    graph_client, _ = client(Response(200, content=b"bytes"))
    assert graph_client.get_bytes(f"{ROOT}/me/messages/m1/attachments/a1/$value") == b"bytes"


# ---------------------------------------------------------------- folders, candidates and messages


def test_folders_keep_full_paths_and_skip_system_folders(mailbox, graph):
    folders = mailbox.folders()
    assert [folder.path for folder in folders] == ["Hogar", "Salud", "Salud/EPS"]
    assert mailbox.folder("Salud/EPS").id == "id-eps"
    mailbox.folders()  # cached for the run: no second walk
    assert sum(1 for method, url in graph.requests if url.endswith("/me/mailFolders")) == 1


def test_ambiguous_or_unknown_folders_stop(mailbox, graph):
    graph.root.append({"id": "id-salud-2", "displayName": "Salud", "childFolderCount": 0})
    with pytest.raises(LookupError, match="ambiguous"):
        mailbox.folders()
    with pytest.raises(LookupError, match="missing or ambiguous"):
        gm.GraphMailbox(FakeGraph()).folder("No existe")


def test_candidates_are_invoice_like_messages_with_attachments(mailbox, graph):
    graph.inbox.append(message("m3", subject="Su comprobante", sender="no-reply@banco.example"))
    graph.inbox.append({"id": "m4", "subject": "Factura sin remitente", "receivedDateTime": "2026-11-10T15:00:00Z"})
    found = mailbox.candidates(30, date(2026, 11, 16))
    assert [candidate.id for candidate in found] == ["m1", "m3"]
    query = next(url for method, url in graph.requests if url.endswith("/mailFolders/inbox/messages"))
    assert query  # the filter itself is checked below on the client parameters


def test_candidate_query_filters_by_date_and_attachments():
    graph_client, http = client(Response(payload={"value": []}))
    gm.GraphMailbox(graph_client).candidates(30, date(2026, 11, 16))
    params = http.requests[0][3]
    assert params["$filter"] == "receivedDateTime ge 2026-10-17T00:00:00Z and hasAttachments eq true"
    assert params["$orderby"] == "receivedDateTime desc"


def test_candidate_text_filter_ignores_accents_and_case(mailbox):
    candidates = mailbox.candidates(30, date(2026, 11, 16))
    assert [c.id for c in gm.filter_candidates(candidates, "FE-1")] == ["m1"]
    assert [c.id for c in gm.filter_candidates(candidates, "SÚ FACTURA")] == ["m1"]
    assert gm.filter_candidates(candidates, "RECIBO DE LUZ") == []
    with pytest.raises(ValueError, match="empty"):
        gm.filter_candidates(candidates, "  ")


def test_message_detail_reads_the_text_body_and_its_digest(mailbox):
    detail = mailbox.message("m1")
    assert (detail.sender, detail.subject, detail.folder_id) == (SENDER, "Factura FE-1", "id-inbox")
    assert detail.body_digest == sha256("Cuerpo de la factura FE-1".encode()).hexdigest()


def test_only_safe_supported_file_attachments_are_saved(mailbox, tmp_path):
    paths, errors = mailbox.download_attachments("m1", tmp_path / "run")
    assert sorted(path.name for path in paths) == ["FE-1.pdf", "FE-1.xml"]
    assert all(path.parent == tmp_path / "run" for path in paths)  # "..\\" never escapes
    assert errors == ["enorme.zip: larger than 10 MB"]


def test_attachment_names_are_cleaned_and_unique():
    taken: set[str] = set()
    assert gm.safe_file_name("Factura:1?.PDF", taken) == "Factura_1_.pdf"
    assert gm.safe_file_name("factura_1_.pdf", taken) == "factura_1_ (2).pdf"
    assert gm.safe_file_name("../../", taken) == "adjunto"


def test_history_reads_only_authorized_folders(mailbox):
    assert gm.read_history(mailbox, []) == []
    history = gm.read_history(mailbox, ["Salud"])
    assert [(item.sender, item.subject, item.folder) for item in history] == [(SENDER, "Factura antigua", "Salud")]
    with pytest.raises(ValueError, match="limit"):
        gm.read_history(mailbox, ["Salud"], limit=6)


# ---------------------------------------------------------------- moving approved messages


def approved_data(**changes):
    data = {
        "conversation": "m1", "sender": SENDER, "subject": "Factura FE-1", "eligible": True,
        "body_digest": sha256("Cuerpo de la factura FE-1".encode()).hexdigest(),
    }
    return {**data, **changes}


@pytest.mark.parametrize("approved,eligible", [(False, True), (True, False), (False, False)])
def test_movement_gate_never_touches_the_mailbox_without_both_approvals(approved, eligible):
    with pytest.raises(ValueError, match="registration"):
        gm.move_registered_message(None, {"eligible": eligible}, "Salud", approved=approved)


def test_an_approved_message_is_moved_once_and_verified(mailbox, graph):
    calls = []
    data = approved_data()
    gm.move_registered_message(mailbox, data, "Salud/EPS", approved=True, before_move=lambda: calls.append(True))
    assert graph.moves == [("m1", "id-eps")] and calls == [True]
    assert data["movement_status"] == "verified"
    gm.verify_destination(mailbox, data, "Salud/EPS")


@pytest.mark.parametrize("changes,error", [
    ({"subject": "Otro asunto"}, "metadata changed"),
    ({"body_digest": "0" * 64}, "body changed after review"),
])
def test_a_message_that_changed_after_review_is_never_moved(mailbox, graph, changes, error):
    with pytest.raises(LookupError, match=error):
        gm.move_registered_message(mailbox, approved_data(**changes), "Salud", approved=True)
    assert graph.moves == []


def test_verification_fails_when_the_message_is_elsewhere(mailbox):
    with pytest.raises(LookupError, match="Destination verification failed"):
        gm.verify_destination(mailbox, approved_data(), "Salud")


def test_a_move_that_changes_the_identity_is_flagged(mailbox, graph):
    graph.post_json = lambda url, payload: {"id": "otro"}
    with pytest.raises(LookupError, match="identity"):
        gm.move_registered_message(mailbox, approved_data(), "Salud", approved=True)


# ---------------------------------------------------------------- session


def fake_msal(monkeypatch, accounts, result=None):
    class Cache:
        has_state_changed = True

        def deserialize(self, text):
            pass

        def serialize(self):
            return "cache"

    class App:
        def __init__(self, client_id, authority, token_cache):
            App.authority = authority

        def get_accounts(self):
            return accounts

        def acquire_token_silent(self, scopes, account):
            App.scopes = scopes
            return result

        def acquire_token_interactive(self, scopes):
            App.scopes = scopes
            return result or {"error": "cancelado"}

    monkeypatch.setitem(__import__("sys").modules, "msal", SimpleNamespace(
        SerializableTokenCache=Cache, PublicClientApplication=App,
    ))
    return App


CLIENT_ID = "11111111-2222-3333-4444-555555555555"


def test_the_saved_session_asks_only_for_mail_read_write(monkeypatch, tmp_path):
    app = fake_msal(monkeypatch, [{"username": "yo@example.test"}], {"access_token": "abc"})
    token = gm.silent_token(CLIENT_ID, "consumers", tmp_path / "cache.bin")
    assert token() == "abc" and app.scopes == ["Mail.ReadWrite"]
    assert app.authority == "https://login.microsoftonline.com/consumers"
    assert (tmp_path / "cache.bin").read_text(encoding="utf-8") == "cache"


@pytest.mark.parametrize("accounts,result,message", [
    ([], None, "ausente"),
    ([{"username": "a"}, {"username": "b"}], None, "ausente"),
    ([{"username": "a"}], {"error": "invalid_grant"}, "vencida"),
])
def test_a_missing_or_expired_session_asks_for_login(monkeypatch, tmp_path, accounts, result, message):
    fake_msal(monkeypatch, accounts, result)
    with pytest.raises(gm.MailboxError, match=message):
        gm.silent_token(CLIENT_ID, "consumers", tmp_path / "cache.bin")()


def test_login_returns_the_account_and_invalid_settings_stop(monkeypatch, tmp_path):
    fake_msal(monkeypatch, [{"username": "yo@example.test"}], {"access_token": "abc"})
    assert gm.interactive_login(CLIENT_ID, "consumers", tmp_path / "cache.bin") == "yo@example.test"
    fake_msal(monkeypatch, [], None)
    with pytest.raises(gm.MailboxError, match="cancelado"):
        gm.interactive_login(CLIENT_ID, "consumers", tmp_path / "cache.bin")
    with pytest.raises(gm.MailboxError, match="invalidos"):
        gm.silent_token("no-es-un-id", "consumers", tmp_path / "cache.bin")


def test_the_mailbox_session_always_closes_its_client(monkeypatch, tmp_path):
    closed = []
    monkeypatch.setattr(gm, "silent_token", lambda *args: (lambda: "t"))

    class Client:
        def __init__(self, token):
            pass

        def close(self):
            closed.append(True)

    monkeypatch.setattr(gm, "InvoiceGraphClient", Client)
    settings = SimpleNamespace(graph_client_id=CLIENT_ID, graph_authority="consumers")
    with pytest.raises(RuntimeError):
        with gm.open_mailbox(settings) as opened:
            assert isinstance(opened, gm.GraphMailbox)
            raise RuntimeError("fallo")
    assert closed == [True]


# ---------------------------------------------------------------- classification over Graph


def test_classification_reads_downloads_and_suggests_without_moving(graph, tmp_path, monkeypatch):
    from datetime import date as day
    from decimal import Decimal

    from openpyxl import Workbook

    from mail_invoices import classification, workflow
    from mail_invoices.documents import InvoiceCandidate
    from mail_invoices.settings import MailSettings

    book = tmp_path / "book.xlsx"
    workbook = Workbook()
    workbook.active.title = "Hoja1"
    for column, header in enumerate(("Empresa", "Razón Social", "Fecha", "Valor"), 2):
        workbook.active.cell(2, column, header)
    workbook.save(book)
    workbook.close()
    seen = []

    def extract(path, ocr, model):
        seen.append(path.name)
        candidate = InvoiceCandidate(
            path.name, "digest", "FE-1", "Proveedor SAS", day(2026, 11, 10), Decimal("100"), "COP", method="xml",
        )
        return [(candidate, (), None)], []

    monkeypatch.setattr(workflow, "_extract_file", extract)
    monkeypatch.setenv("AGENTE_LOCAL_DATA", str(tmp_path / "data"))
    settings = MailSettings(
        CLIENT_ID, book, tmp_path / "backups", "Hoja1", 5, attachments_dir=tmp_path / "tmp",
        tesseract_executable=tmp_path / "missing.exe", folder_rules={SENDER: "Salud"},
    )
    report = classification.simulate_classification(
        settings, 30, [], False, include_review_items=True, mailbox=gm.GraphMailbox(graph),
    )
    [result] = report["messages"]
    assert report["moved"] == 0 and graph.moves == []
    assert seen == ["FE-1.xml"]  # the loose PDF with the same name is the XML's rendering
    assert (result["conversation"], result["sender"], result["subject"]) == ("m1", SENDER, "Factura FE-1")
    # Sender rules never impose an invoice destination and the model is off: no suggestion.
    assert result["folder"] is None and result["errors"] == ["enorme.zip: larger than 10 MB"]
    assert result["body_digest"] == sha256("Cuerpo de la factura FE-1".encode()).hexdigest()
    assert [invoice["status"] for invoice in result["invoices"]] == ["not_registered"]
    assert not (tmp_path / "tmp").exists() or list((tmp_path / "tmp").iterdir()) == []


def test_a_graph_failure_on_one_message_is_reported_on_that_message(graph, tmp_path, monkeypatch):
    from mail_invoices import classification
    from mail_invoices.settings import MailSettings

    def unavailable(identifier):
        raise gm.MailboxError("Graph respondio 503")

    mailbox = gm.GraphMailbox(graph)
    monkeypatch.setattr(mailbox, "message", unavailable)
    monkeypatch.setenv("AGENTE_LOCAL_DATA", str(tmp_path / "data"))
    settings = MailSettings(
        CLIENT_ID, tmp_path / "book.xlsx", tmp_path, "Hoja1", 5, attachments_dir=tmp_path / "tmp",
        tesseract_executable=tmp_path / "missing.exe",
    )
    report = classification.simulate_classification(settings, 30, [], False, mailbox=mailbox)
    assert report["messages"][0]["errors"] == ["Graph respondio 503"]
    assert report["messages"][0]["eligible"] is False


# ---------------------------------------------------------------- unexpected Graph answers stop


@pytest.mark.parametrize("change,error", [
    (lambda g: setattr(g, "root", "no-es-lista"), "lista no reconocida"),
    (lambda g: g.root.append({"id": "x", "displayName": "", "childFolderCount": 0}), "recognizable name"),
    (lambda g: g.root.append({"id": "x", "displayName": "Raro", "childFolderCount": -1}), "subcarpetas"),
])
def test_an_unexpected_folder_answer_stops(graph, change, error):
    change(graph)
    with pytest.raises((gm.MailboxError, LookupError), match=error):
        gm.GraphMailbox(graph).folders()


@pytest.mark.parametrize("raw,error", [
    ({"subject": "Factura", "receivedDateTime": "2026-11-10T15:00:00Z"}, "identificador"),
    ({**message("x"), "receivedDateTime": "ayer"}, "Fecha"),
    ({**message("x"), "receivedDateTime": "2026-11-10T15:00:00"}, "zona horaria"),
])
def test_an_unexpected_candidate_answer_stops(graph, raw, error):
    graph.inbox = [raw]
    with pytest.raises(gm.MailboxError, match=error):
        gm.GraphMailbox(graph).candidates(30, date(2026, 11, 16))


def test_an_unbounded_candidate_list_stops(graph, monkeypatch):
    monkeypatch.setattr(gm, "CANDIDATE_SCAN_LIMIT", 1)
    graph.inbox = [message("m1"), message("m3")]
    with pytest.raises(gm.MailboxError, match="reduce --days"):
        gm.GraphMailbox(graph).candidates(30, date(2026, 11, 16))


def test_a_message_without_subject_is_skipped_as_candidate(graph):
    graph.inbox = [message("m1", subject="  "), message("m3")]
    assert [c.id for c in gm.GraphMailbox(graph).candidates(30, date(2026, 11, 16))] == ["m3"]


@pytest.mark.parametrize("detail,error", [
    ({"id": "otro"}, "another message"),
    ({"body": None}, "cuerpo"),
])
def test_an_unexpected_message_answer_stops(graph, detail, error):
    original = graph.get_json

    def answer(url, params=None):
        data = original(url, params)
        return {**data, **detail} if url.endswith("/me/messages/m1") else data

    graph.get_json = answer
    with pytest.raises((gm.MailboxError, LookupError), match=error):
        gm.GraphMailbox(graph).message("m1")


def test_a_bad_page_link_or_a_bad_retry_after_are_handled(graph):
    graph.root = []
    original = graph.get_json
    graph.get_json = lambda url, params=None: {"value": [], "@odata.nextLink": 7} if url.endswith("/me/mailFolders") else original(url, params)
    with pytest.raises(gm.MailboxError, match="Enlace"):
        gm.GraphMailbox(graph).folders()
    assert gm._retry_after(SimpleNamespace(headers={"Retry-After": "pronto"})) == 5.0
    graph_client, http = client()
    graph_client.close()


def test_a_missing_msal_package_is_reported(monkeypatch, tmp_path):
    monkeypatch.setitem(__import__("sys").modules, "msal", None)
    with pytest.raises(gm.MailboxError, match="msal"):
        gm.silent_token(CLIENT_ID, "consumers", tmp_path / "cache.bin")


# ---------------------------------------------------------------- read-only commands


def test_login_signs_in_with_the_invoice_app(monkeypatch, tmp_path, capsys):
    from mail_invoices import workflow
    from mail_invoices.settings import MailSettings

    calls = []
    monkeypatch.setattr(gm, "interactive_login", lambda *args: calls.append(args) or "yo@example.test")
    monkeypatch.setenv("AGENTE_LOCAL_DATA", str(tmp_path))
    settings = MailSettings(CLIENT_ID, tmp_path / "book.xlsx", tmp_path, "Hoja1", 1)
    assert workflow.login(settings) == 0
    assert calls == [(CLIENT_ID, "consumers", tmp_path / "MailInvoiceAgent" / "graph_token_cache.bin")]
    assert "yo@example.test" in capsys.readouterr().out


def test_scan_inbox_lists_candidates_without_opening_them(graph, monkeypatch, tmp_path, capsys):
    import json
    from contextlib import contextmanager

    from mail_invoices import workflow
    from mail_invoices.settings import MailSettings

    @contextmanager
    def session(settings):
        yield gm.GraphMailbox(graph)

    monkeypatch.setattr(gm, "open_mailbox", session)
    settings = MailSettings(CLIENT_ID, tmp_path / "book.xlsx", tmp_path, "Hoja1", 1)
    assert workflow.scan_inbox(settings, 30) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [item["subject"] for item in listed] == ["Factura FE-1"]
    assert not any("/me/messages/" in url for method, url in graph.requests)  # nothing opened


@pytest.mark.parametrize("names,read", [
    (["FE-1.xml", "FE-1.pdf"], ["FE-1.xml"]),                                # same name
    (["FE-1.xml", "Representacion grafica FE-1.pdf"], ["FE-1.xml"]),         # invoice number in the name
    (["FE-1.xml", "otro.pdf"], ["FE-1.xml", "otro.pdf"]),                    # no unambiguous pair: still read
    (["FE-1.xml", "FE-1.pdf", "FE-1 copia.pdf"], ["FE-1.xml", "FE-1 copia.pdf"]),  # one PDF per XML only
    (["FE-1.pdf"], ["FE-1.pdf"]),                                            # a PDF alone is the invoice
])
def test_loose_xml_and_pdf_are_paired_like_inside_a_zip(graph, tmp_path, monkeypatch, names, read):
    from mail_invoices import classification, workflow
    from mail_invoices.documents import InvoiceCandidate
    from mail_invoices.settings import MailSettings

    graph.attachments["m1"] = [
        {"@odata.type": "#microsoft.graph.fileAttachment", "id": f"a{i}", "name": name, "size": 10}
        for i, name in enumerate(names)
    ]
    graph.files = {f"a{i}": b"x" for i in range(len(names))}
    seen = []

    def extract(path, ocr, model):
        seen.append(path.name)
        if path.suffix == ".pdf" and path.name != "FE-1.pdf" and "copia" not in path.name:
            return [], [{"source": path.name, "error": "Scanned PDF", "requires_review": True}]
        return [(InvoiceCandidate(path.name, "d", "FE-1" if path.suffix == ".xml" else "", method=path.suffix[1:]), (), None)], []

    monkeypatch.setattr(workflow, "_extract_file", extract)
    monkeypatch.setattr(classification, "InvoiceWorkbook", lambda *a: type("Book", (), {"registration_status": lambda *x, **k: "not_registered"})())
    monkeypatch.setenv("AGENTE_LOCAL_DATA", str(tmp_path / "data"))
    settings = MailSettings(CLIENT_ID, tmp_path / "book.xlsx", tmp_path, "Hoja1", 5, attachments_dir=tmp_path / "tmp",
                            tesseract_executable=tmp_path / "missing.exe")
    report = classification.simulate_classification(settings, 30, [], False, mailbox=gm.GraphMailbox(graph))
    assert seen == read
    # A paired PDF that could not be read never blocks its valid XML.
    if names == ["FE-1.xml", "Representacion grafica FE-1.pdf"]:
        assert report["messages"][0]["errors"] == []


def test_excluded_senders_are_not_classified(graph, tmp_path, monkeypatch):
    from mail_invoices import classification
    from mail_invoices.settings import MailSettings

    monkeypatch.setenv("AGENTE_LOCAL_DATA", str(tmp_path / "data"))
    settings = MailSettings(CLIENT_ID, tmp_path / "book.xlsx", tmp_path, "Hoja1", 5, attachments_dir=tmp_path / "tmp",
                            tesseract_executable=tmp_path / "missing.exe", excluded_senders=("@proveedor.example",))
    report = classification.simulate_classification(settings, 30, [], False, mailbox=gm.GraphMailbox(graph))
    assert report["messages"] == [] and not any("/me/messages/m1" in url for _, url in graph.requests)
