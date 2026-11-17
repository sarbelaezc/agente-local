from __future__ import annotations

import json
import sys
from argparse import Namespace
from datetime import date
from email.message import EmailMessage
from urllib.parse import parse_qs, quote, unquote, urlparse

import httpx
import pytest

from monthly_close import main as app
from monthly_close import graph_client
from monthly_close.domain import Preferences
from monthly_close.graph_client import GraphClient, GraphError, validate_graph_url
from monthly_close.graph_reader import GraphSourceReader
from monthly_close.outlook import Candidate, OutlookError
from monthly_close.source_reader import parse_source
from monthly_close.store import Store

SENDER = "avisos@example.test"
CLIENT_ID = "11111111-2222-3333-4444-555555555555"
ROOT = "https://graph.microsoft.com/v1.0"


@pytest.fixture
def preferences(categories) -> Preferences:
    return Preferences.parse({
        "location": "Ciudad de prueba", "timezone": "America/Bogota", "currency": "COP",
        "accounts": ["Tarjeta A"], "excluded_accounts": ["Cuenta B"],
        "transfer_label": "Familiar", "ask_savings": True, "ask_people": True,
        "approved_senders": [SENDER], "categories": categories,
        "confidential_confirmed": True,
    })


def msg(identifier: str, received: str, sender: str = SENDER, subject: str = "Compra Tienda") -> dict:
    return {
        "id": identifier, "subject": subject, "receivedDateTime": received,
        "from": {"emailAddress": {"address": sender}},
    }


def mime(
    identifier: str, sender: str = SENDER, subject: str = "Compra Tienda",
    received: str = "Fri, 13 Nov 2026 10:00:00 +0000",
) -> bytes:
    message = EmailMessage()
    message["Received"] = "from sender.test by outlook.test; " + received
    message["From"] = f"Avisos <{sender}>"
    message["To"] = "test@example.test"
    message["Date"] = "Fri, 13 Nov 2026 04:59:00 -0500"
    message["Message-ID"] = f"<{identifier}@example.test>"
    message["Subject"] = subject
    message.set_content("Compraste en Tienda $ 1.234,56.")
    return message.as_bytes()


class Mailbox:
    """Scripted Microsoft Graph that mimics the routes the reader uses, with paging."""

    def __init__(self) -> None:
        self.folders: dict[str, dict] = {}
        self.top: list[str] = []
        self.alias = {"inbox": "inbox", "junkemail": "junk", "deleteditems": "deleted"}
        self.totals: dict[str, list[int]] = {}
        self.raw: dict[str, bytes] = {}
        self.page_size = 2
        self.reads: list[str] = []
        self.closed = False

    def add(self, folder_id: str, name: str, parent: str | None = None, messages=()) -> None:
        self.folders[folder_id] = {"name": name, "children": [], "messages": list(messages)}
        if parent:
            self.folders[parent]["children"].append(folder_id)
        else:
            self.top.append(folder_id)

    def _folder(self, folder_id: str) -> dict:
        folder = self.folders[folder_id]
        return {"id": folder_id, "displayName": folder["name"], "childFolderCount": len(folder["children"])}

    def _page(self, values: list[dict], url: str) -> dict:
        query = parse_qs(urlparse(url).query)
        skip = int(query.get("skip", ["0"])[0])
        page = values[skip:skip + self.page_size]
        data: dict = {"value": page}
        if skip + self.page_size < len(values):
            data["@odata.nextLink"] = url.split("?")[0] + f"?skip={skip + self.page_size}"
        return data

    def get_json(self, url: str, params: dict | None = None) -> dict:
        validate_graph_url(url)
        path = urlparse(url).path.removeprefix("/v1.0/me/mailFolders").strip("/")
        if not path:
            return self._page([self._folder(folder_id) for folder_id in self.top], url)
        parts = path.split("/")
        folder_id = self.alias.get(unquote(parts[0]), unquote(parts[0]))
        if len(parts) == 1:
            queue = self.totals.get(folder_id, [])
            total = queue.pop(0) if queue else len(self.folders[folder_id]["messages"])
            return {**self._folder(folder_id), "totalItemCount": total}
        if parts[1] == "childFolders":
            return self._page([self._folder(child) for child in self.folders[folder_id]["children"]], url)
        assert parts[1] == "messages"
        return self._page(self.folders[folder_id]["messages"], url)

    def get_bytes(self, url: str) -> bytes:
        validate_graph_url(url)
        identifier = unquote(urlparse(url).path.split("/")[-2])
        self.reads.append(identifier)
        return self.raw[identifier]

    def close(self) -> None:
        self.closed = True


def mailbox() -> Mailbox:
    box = Mailbox()
    box.add("inbox", "Bandeja de entrada", messages=[
        msg("m1", "2026-11-13T10:00:00Z"),
        msg("m2", "2026-11-12T10:00:00Z", sender="otro@example.test"),
        msg("m3", "2026-11-11T10:00:00Z"),
    ])
    box.add("junk", "Correo no deseado")
    box.add("deleted", "Elementos eliminados", messages=[msg("m4", "2026-11-10T10:00:00Z", sender="otro@example.test")])
    box.add("compras", "Compras", messages=[msg("never", "2026-11-09T10:00:00Z")])
    box.add("din", "Dinero")
    box.add("banco", "Banco", parent="din", messages=[
        msg("m5", "2026-11-09T10:00:00Z"), msg("m6", "2026-11-08T10:00:00Z"),
    ])
    box.add("puntos", "Puntos", parent="banco", messages=[msg("m7", "2026-11-07T10:00:00Z")])
    for identifier in ("m1", "m3", "m5", "m6", "m7"):
        box.raw[identifier] = mime(identifier)
    return box


def reader(box: Mailbox, senders=(SENDER,), origin: str = "outlook.live.com") -> GraphSourceReader:
    return GraphSourceReader(box, list(senders), origin)


def test_walks_inbox_junk_deleted_then_the_whole_dinero_tree_and_nothing_else():
    box, visited, paths = mailbox(), [], []
    graph = reader(box)

    def visit(candidate):
        visited.append(candidate.item_id)
        paths.append(graph.active_folder)

    reports = graph.walk_folders(100, visit)

    assert [report["folder"] for report in reports] == [
        "Bandeja de entrada", "Correo no deseado", "Elementos eliminados",
        "Dinero", "Dinero / Banco", "Dinero / Banco / Puntos",
    ]
    assert [report["rows"] for report in reports] == [3, 0, 1, 0, 2, 1]
    assert [report["authorized"] for report in reports] == [2, 0, 0, 0, 2, 1]
    assert visited == ["m1", "m3", "m5", "m6", "m7"]
    assert paths[-1] == ("dinero", "banco", "puntos")
    assert "never" not in visited and box.reads == []


def test_unauthorized_senders_are_neither_visited_nor_read():
    box = mailbox()
    seen = []
    reader(box).walk_folders(100, lambda candidate: seen.append(candidate.sender))
    assert set(seen) == {SENDER}
    assert box.reads == []


def test_visit_false_ends_only_the_current_folder():
    box, visited = mailbox(), []

    def visit(candidate):
        visited.append(candidate.item_id)
        return False if candidate.item_id == "m1" else None

    reports = reader(box).walk_folders(100, visit)

    assert reports[0] == {"folder": "Bandeja de entrada", "rows": 1, "authorized": 1, "stopped_before_start": True}
    assert visited == ["m1", "m5", "m6", "m7"]


def test_count_that_never_matches_the_folder_total_stops_with_both_numbers():
    box = mailbox()
    box.totals["inbox"] = [5, 5, 5, 5, 5, 5]
    with pytest.raises(OutlookError, match=r"Cobertura no demostrada en Bandeja de entrada.*3.*5"):
        reader(box).walk_folders(100, lambda candidate: None)


def test_mail_arriving_during_the_snapshot_retries_instead_of_failing():
    box = mailbox()
    box.totals["inbox"] = [2, 3]  # first attempt sees a total that moved while listing
    reports = reader(box).walk_folders(100, lambda candidate: None)
    assert reports[0]["rows"] == 3


def test_new_mail_after_the_snapshot_does_not_disturb_the_run():
    box, graph = mailbox(), None
    graph = reader(box)

    def visit(candidate):
        if candidate.item_id == "m1":
            box.folders["inbox"]["messages"].insert(0, msg("late", "2026-11-14T10:00:00Z"))

    reports = graph.walk_folders(100, visit)
    assert reports[0]["rows"] == 3


def test_dates_out_of_order_stop():
    box = mailbox()
    box.folders["inbox"]["messages"][1]["receivedDateTime"] = "2026-11-14T10:00:00Z"
    with pytest.raises(OutlookError, match="orden de fecha descendente"):
        reader(box).walk_folders(100, lambda candidate: None)


def test_repeated_message_id_stops():
    box = mailbox()
    box.folders["inbox"]["messages"][2]["id"] = "m1"
    with pytest.raises(OutlookError, match="repetido"):
        reader(box).walk_folders(100, lambda candidate: None)


def test_row_limit_is_an_error_not_a_silent_partial_scan():
    with pytest.raises(OutlookError, match="Limite total"):
        reader(mailbox()).walk_folders(2, lambda candidate: None)
    with pytest.raises(ValueError):
        reader(mailbox()).walk_folders(0, lambda candidate: None)


def test_missing_or_duplicate_dinero_folder_stops():
    box = mailbox()
    box.add("din2", "dinero")
    with pytest.raises(GraphError, match="Dinero"):
        reader(box).folders()
    box = mailbox()
    del box.folders["din"], box.folders["banco"], box.folders["puntos"]
    box.top = [name for name in box.top if name not in {"din"}]
    with pytest.raises(GraphError, match="Dinero"):
        reader(box).folders()


def test_ambiguous_subfolder_names_stop():
    box = mailbox()
    box.add("banco2", "BANCO", parent="din")
    with pytest.raises(GraphError, match="ambigua"):
        reader(box).folders()


def test_folder_tree_changing_during_the_walk_stops():
    box = mailbox()

    def visit(candidate):
        if candidate.item_id == "m1":
            box.add("nueva", "Nueva", parent="din")

    with pytest.raises(OutlookError, match="arbol de subcarpetas cambio"):
        reader(box).walk_folders(100, visit)


def test_unusable_folder_or_message_payloads_stop():
    box = mailbox()
    box.folders["inbox"]["messages"][0].pop("id")
    with pytest.raises(OutlookError, match="identificador"):
        reader(box).walk_folders(100, lambda candidate: None)
    box = mailbox()
    box.folders["inbox"]["messages"][0]["receivedDateTime"] = "ayer"
    with pytest.raises(OutlookError, match="Fecha"):
        reader(box).walk_folders(100, lambda candidate: None)
    box = mailbox()
    box.folders["inbox"]["messages"][0]["subject"] = ""
    with pytest.raises(OutlookError, match="Asunto"):
        reader(box).walk_folders(100, lambda candidate: None)


def test_requires_exact_approved_senders():
    with pytest.raises(ValueError, match="remitentes"):
        GraphSourceReader(mailbox(), [])
    with pytest.raises(ValueError, match="remitentes"):
        GraphSourceReader(mailbox(), ["no-es-un-correo"])


def test_read_validates_mime_and_keeps_the_stable_message_identity():
    box = mailbox()
    graph = reader(box)
    candidate = Candidate("m1", SENDER, "Compra Tienda")
    mail = graph.read(candidate)
    assert mail == parse_source(mime("m1").decode(), candidate, "outlook.live.com")
    assert mail.id != reader(box, origin="otro.example").read(candidate).id
    assert box.reads == ["m1", "m1"]


def test_read_stops_on_mismatched_sender_or_subject_or_invalid_utf8():
    box = mailbox()
    graph = reader(box)
    with pytest.raises(OutlookError, match="remitente"):
        graph.read(Candidate("m1", "otro@example.test", "Compra Tienda"))
    with pytest.raises(OutlookError, match="asunto"):
        graph.read(Candidate("m1", SENDER, "Otro asunto"))
    box.raw["m1"] = b"\xff\xfe"
    with pytest.raises(OutlookError, match="UTF-8"):
        graph.read(Candidate("m1", SENDER, "Compra Tienda"))


def test_message_ids_are_encoded_in_the_url():
    box = mailbox()
    box.raw["a/b+c="] = mime("x")
    reader(box).read(Candidate("a/b+c=", SENDER, "Compra Tienda"))
    assert box.reads == ["a/b+c="]
    assert quote("a/b+c=", safe="") == "a%2Fb%2Bc%3D"


def test_checkpoint_key_is_stable_per_folder_and_requires_a_listed_notice():
    box = mailbox()
    graph = reader(box)
    keys: dict[str, str] = {}

    def visit(candidate):
        keys[candidate.item_id] = graph.checkpoint_key(candidate)
        assert graph.checkpoint_key(candidate) == keys[candidate.item_id]

    graph.walk_folders(100, visit)
    assert len(set(keys.values())) == len(keys) == 5
    with pytest.raises(OutlookError, match="desconocido"):
        graph.checkpoint_key(Candidate("nunca-listado", SENDER, "Compra Tienda"))


def test_pilot_candidate_is_the_newest_authorized_notice_and_reports_its_folder():
    graph = reader(mailbox())
    candidate = graph.candidate()
    assert (candidate.item_id, candidate.sender, graph.active_folder) == ("m1", SENDER, ("bandeja de entrada",))
    with pytest.raises(OutlookError, match="No hay avisos"):
        reader(mailbox(), senders=["nadie@example.test"]).candidate()


# --- Graph HTTP client -----------------------------------------------------------------

class Response:
    def __init__(self, status: int = 200, payload=None, content: bytes = b"", headers=None) -> None:
        self.status_code, self._payload, self.content = status, payload, content
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class Http:
    def __init__(self, *responses) -> None:
        self.responses, self.requests, self.closed = list(responses), [], False

    def get(self, url, params=None, headers=None):
        self.requests.append((url, params, headers))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize("url", [
    "https://evil.example/v1.0/me/messages",
    "http://graph.microsoft.com/v1.0/me/messages",
    "https://graph.microsoft.com.evil.example/v1.0/me/messages",
    "https://graph.microsoft.com:8443/v1.0/me/messages",
    "https://user:pass@graph.microsoft.com/v1.0/me/messages",
    "https://graph.microsoft.com/beta/me/messages",
])
def test_token_is_never_sent_outside_graph_v1(url):
    http = Http(Response(payload={"value": []}))
    with pytest.raises(GraphError):
        GraphClient(lambda: "secret", http).get_json(url)
    assert http.requests == []


def test_client_sends_bearer_token_and_returns_json_or_bytes():
    http = Http(Response(payload={"value": [1]}), Response(content=b"raw"))
    client = GraphClient(lambda: "tok", http)
    assert client.get_json(ROOT + "/me/mailFolders", {"$top": 1}) == {"value": [1]}
    assert client.get_bytes(ROOT + "/me/messages/x/$value") == b"raw"
    assert http.requests[0][2] == {"Authorization": "Bearer tok"}
    assert http.requests[0][1] == {"$top": 1}
    client.close()
    assert http.closed


def test_throttling_is_retried_with_a_capped_wait_and_then_reported():
    sleeps: list[float] = []
    http = Http(Response(429, headers={"Retry-After": "3"}), Response(503, headers={"Retry-After": "999"}),
                Response(payload={"ok": 1}))
    assert GraphClient(lambda: "t", http, sleeps.append).get_json(ROOT + "/me") == {"ok": 1}
    assert sleeps == [3.0, 60.0]
    http = Http(*[Response(429) for _ in range(4)])
    with pytest.raises(GraphError, match="429"):
        GraphClient(lambda: "t", http, lambda seconds: None).get_json(ROOT + "/me")
    assert len(http.requests) == 4


def test_api_errors_report_code_and_message_without_the_token():
    http = Http(Response(403, payload={"error": {"code": "ErrorAccessDenied", "message": "Access is denied."}}))
    with pytest.raises(GraphError, match="403.*ErrorAccessDenied.*Access is denied") as raised:
        GraphClient(lambda: "super-secret", http).get_json(ROOT + "/me")
    assert "super-secret" not in str(raised.value)


def test_unexpected_payloads_and_network_failures_become_graph_errors():
    with pytest.raises(GraphError, match="JSON"):
        GraphClient(lambda: "t", Http(Response(content=b"<html>"))).get_json(ROOT + "/me")
    with pytest.raises(GraphError, match="inesperada"):
        GraphClient(lambda: "t", Http(Response(payload=[1]))).get_json(ROOT + "/me")
    waits: list[float] = []
    with pytest.raises(GraphError, match="contactar"):
        GraphClient(
            lambda: "t", Http(*[httpx.HTTPError("boom")] * 4), sleep=waits.append,
        ).get_json(ROOT + "/me")
    assert len(waits) == 3


def test_momentary_network_and_gateway_failures_are_retried():
    waits: list[float] = []
    client = GraphClient(
        lambda: "t",
        Http(httpx.HTTPError("reset"), Response(502), Response(payload={"ok": True})),
        sleep=waits.append,
    )
    assert client.get_json(ROOT + "/me") == {"ok": True}
    assert len(waits) == 2


def test_next_link_to_another_host_is_refused_by_the_reader():
    box = mailbox()
    original = box.get_json

    def hostile(url, params=None):
        data = original(url, params)
        if "@odata.nextLink" in data:
            data["@odata.nextLink"] = "https://evil.example/v1.0/me/mailFolders/inbox/messages?skip=2"
        return data

    box.get_json = hostile
    box.page_size = 1
    with pytest.raises(GraphError, match="Solo se permite Microsoft Graph"):
        reader(box).walk_folders(100, lambda candidate: None)


# --- Microsoft sign-in (msal replaced by a fake) -----------------------------------------

class FakeCache:
    has_state_changed = True

    def deserialize(self, text):
        self.text = text

    def serialize(self):
        return '{"cache": "saved"}'


def fake_msal(accounts, result=None):
    calls = {"scopes": []}

    class App:
        def __init__(self, client_id, authority, token_cache):
            calls["init"] = (client_id, authority)

        def get_accounts(self):
            return accounts

        def acquire_token_silent(self, scopes, account):
            calls["scopes"].append(scopes)
            return result

        def acquire_token_interactive(self, scopes):
            calls["scopes"].append(scopes)
            return result

    module = type(sys)("msal")
    module.SerializableTokenCache = FakeCache
    module.PublicClientApplication = App
    return module, calls


def test_silent_token_is_read_only_cached_and_never_interactive(tmp_path, monkeypatch):
    module, calls = fake_msal([{"username": "yo@example.test"}], {"access_token": "tok"})
    monkeypatch.setitem(sys.modules, "msal", module)
    token = graph_client.silent_token(CLIENT_ID, "consumers", tmp_path / "cache.bin")
    assert token() == "tok"
    assert calls["scopes"] == [["Mail.Read"]]
    assert calls["init"] == (CLIENT_ID, "https://login.microsoftonline.com/consumers")
    assert (tmp_path / "cache.bin").read_text(encoding="utf-8") == '{"cache": "saved"}'
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("accounts,result,message", [
    ([], None, "login"),
    ([{"username": "a"}, {"username": "b"}], None, "ambigua"),
    ([{"username": "a"}], {"error": "invalid_grant"}, "vencida"),
    ([{"username": "a"}], None, "vencida"),
])
def test_silent_token_asks_for_login_instead_of_prompting(tmp_path, monkeypatch, accounts, result, message):
    module, _ = fake_msal(accounts, result)
    monkeypatch.setitem(sys.modules, "msal", module)
    with pytest.raises(GraphError, match=message):
        graph_client.silent_token(CLIENT_ID, "consumers", tmp_path / "cache.bin")()


def test_invalid_graph_settings_and_missing_msal_are_reported(tmp_path, monkeypatch):
    module, _ = fake_msal([])
    monkeypatch.setitem(sys.modules, "msal", module)
    with pytest.raises(GraphError, match="invalidos"):
        graph_client.silent_token("no-guid", "consumers", tmp_path / "c.bin")
    with pytest.raises(GraphError, match="invalidos"):
        graph_client.silent_token(CLIENT_ID, "organizations", tmp_path / "c.bin")
    monkeypatch.setitem(sys.modules, "msal", None)
    with pytest.raises(GraphError, match="msal"):
        graph_client.silent_token(CLIENT_ID, "consumers", tmp_path / "c.bin")


def test_interactive_login_saves_the_cache_and_returns_the_account(tmp_path, monkeypatch):
    module, calls = fake_msal([{"username": "yo@example.test"}], {"access_token": "tok"})
    monkeypatch.setitem(sys.modules, "msal", module)
    assert graph_client.interactive_login(CLIENT_ID, "common", tmp_path / "c.bin") == "yo@example.test"
    assert calls["scopes"] == [["Mail.Read"]] and (tmp_path / "c.bin").is_file()
    module, _ = fake_msal([], {"error": "access_denied"})
    monkeypatch.setitem(sys.modules, "msal", module)
    with pytest.raises(GraphError, match="access_denied"):
        graph_client.interactive_login(CLIENT_ID, "common", tmp_path / "c2.bin")


# --- Configuration and wiring in main --------------------------------------------------

def graph_settings(**updates) -> dict:
    settings = {
        "ollama_url": "http://127.0.0.1:11434", "ollama_model": "local-test",
        "source": "graph", "graph_client_id": CLIENT_ID, "max_messages": 50,
    }
    settings.update(updates)
    return settings


def write_runtime(tmp_path, settings: dict) -> None:
    (tmp_path / "runtime.json").write_text(json.dumps(settings), encoding="utf-8")


def test_runtime_defaults_to_personal_accounts_and_needs_no_source_key(tmp_path):
    write_runtime(tmp_path, graph_settings())
    runtime = app.runtime(tmp_path)
    assert runtime["graph_authority"] == "consumers"
    settings = graph_settings()
    settings.pop("source", None)
    write_runtime(tmp_path, settings)
    assert app.runtime(tmp_path)["graph_client_id"] == settings["graph_client_id"]


@pytest.mark.parametrize("updates,message", [
    ({"graph_client_id": "x"}, "graph_client_id"),
    ({"graph_client_id": None}, "graph_client_id"),
    ({"graph_authority": "organizations"}, "graph_authority"),
    ({"source": "otra"}, "source"),
    ({"source": "browser"}, "navegador se retiro"),
    ({"ollama_url": "http://127.0.0.1:11435"}, "11434"),
    ({"max_messages": 0}, "max_messages"),
])
def test_graph_runtime_rejects_invalid_settings(tmp_path, updates, message):
    write_runtime(tmp_path, graph_settings(**updates))
    with pytest.raises(ValueError, match=message):
        app.runtime(tmp_path)


def test_open_source_uses_graph_keeps_the_identifier_origin_and_closes_the_client(
    tmp_path, monkeypatch,
):
    box = Mailbox()
    monkeypatch.setattr(app, "silent_token", lambda *args: (lambda: "t"))
    monkeypatch.setattr(app, "GraphClient", lambda token: box)
    with app.open_source(tmp_path, graph_settings(graph_authority="consumers"), [SENDER]) as source:
        # Changing this origin would change every message identifier and re-import notices.
        assert isinstance(source, GraphSourceReader) and source.origin == "outlook.live.com"
    assert box.closed


def test_login_signs_in_with_graph_and_saves_the_session(tmp_path, preferences, monkeypatch, capsys):
    write_runtime(tmp_path, graph_settings())
    Store(tmp_path / "close.sqlite3").configure(preferences.to_dict())
    calls = []
    monkeypatch.setattr(app, "interactive_login", lambda *args: calls.append(args) or "yo@example.test")
    app.run(Namespace(
        command="login", data_dir=tmp_path, port=0, month=None, start=None, end=None, read_only=False,
    ))
    assert calls == [(CLIENT_ID, "consumers", tmp_path / "graph_token_cache.bin")]
    assert "yo@example.test" in capsys.readouterr().out


def verified_settings(**updates) -> dict:
    return graph_settings(folder_scan_verified=True, **updates)


def test_read_only_scan_through_graph_lists_folders_without_opening_any_message(
    tmp_path, preferences, monkeypatch, capsys,
):
    box = mailbox()
    monkeypatch.setattr(app, "silent_token", lambda *args: (lambda: "t"))
    monkeypatch.setattr(app, "GraphClient", lambda token: box)
    store = Store(tmp_path / "close.sqlite3")
    store.configure(preferences.to_dict())
    app.scan(
        store, tmp_path, graph_settings(), preferences, date(2026, 11, 1), date(2026, 12, 1), read_only=True,
    )
    output = capsys.readouterr().out
    assert "Dinero / Banco: 2 filas verificadas; 2 de remitentes autorizados." in output
    assert "Bandeja de entrada: 3 filas verificadas; 2 de remitentes autorizados." in output
    assert box.reads == [] and store.state()["transactions"] == []


class Model:
    def __init__(self) -> None:
        self.calls = 0

    def complete_json(self, system, user):
        self.calls += 1
        return {"events": [{
            "merchant": "Tienda", "account": "Tarjeta A", "amount_text": "$ 1.234,56",
            "currency": "COP", "date_text": "", "category": "Compras", "kind": "purchase", "savings": False,
        }]}


def test_full_scan_through_graph_imports_notices_stops_before_the_start_date_and_resumes(
    tmp_path, preferences, monkeypatch,
):
    box = Mailbox()
    box.add("inbox", "Bandeja de entrada", messages=[
        msg("n1", "2026-11-13T10:00:00Z"), msg("n2", "2026-11-12T10:00:00Z"),
        msg("old", "2026-10-20T10:00:00Z"), msg("older", "2026-10-19T10:00:00Z"),
    ])
    box.add("junk", "Correo no deseado")
    box.add("deleted", "Elementos eliminados")
    box.add("din", "Dinero")
    arrivals = {
        "n1": "Fri, 13 Nov 2026 10:00:00 +0000", "n2": "Thu, 12 Nov 2026 10:00:00 +0000",
        "old": "Tue, 20 Oct 2026 10:00:00 +0000", "older": "Mon, 19 Oct 2026 10:00:00 +0000",
    }
    for identifier, received in arrivals.items():
        box.raw[identifier] = mime(identifier, received=received)
    model = Model()
    monkeypatch.setattr(app, "silent_token", lambda *args: (lambda: "t"))
    monkeypatch.setattr(app, "GraphClient", lambda token: box)
    monkeypatch.setattr(app, "OllamaJsonModel", lambda *args: model)
    monkeypatch.setattr(app, "require_model", lambda settings: None)
    store = Store(tmp_path / "close.sqlite3")
    store.configure(preferences.to_dict())
    args = (store, tmp_path, verified_settings(), preferences, date(2026, 11, 1), date(2026, 12, 1))

    app.scan(*args)

    assert box.reads == ["n1", "n2", "old"]
    assert len(store.state()["transactions"]) == 2 and model.calls == 2
    box.reads.clear()
    app.scan(*args)
    assert model.calls == 2 and len(store.state()["transactions"]) == 2


def test_the_monthly_close_client_can_only_read():
    """The Azure app is shared with invoices (Mail.ReadWrite): reading-only lives in this code."""
    writes = ("post", "put", "patch", "delete", "post_json", "request", "send")
    assert not any(hasattr(GraphClient, name) for name in writes)
    http = Http(Response(payload={"value": []}))
    GraphClient(lambda: "token", http=http, sleep=lambda seconds: None).get_json(f"{ROOT}/me/mailFolders")
    assert len(http.requests) == 1  # Http only implements get: any other verb would fail
