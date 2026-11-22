from __future__ import annotations

import base64
import json
from datetime import date, datetime, timezone
from email.message import EmailMessage
from urllib.parse import unquote, urlparse

import pytest

from monthly_close import gmail_client
from monthly_close import main as app
from monthly_close.gmail_client import GmailClient, GmailError, validate_gmail_url
from monthly_close.gmail_reader import GmailSourceReader
from monthly_close.graph_client import GraphError, validate_graph_url
from monthly_close.graph_reader import GraphSourceReader
from monthly_close.mailboxes import load_mailboxes, primary_graph
from monthly_close.outlook import Candidate, OutlookError
from monthly_close.store import Store

SENDER = "avisos@example.test"
CLIENT_ID = "11111111-2222-3333-4444-555555555555"
GRAPH = "https://graph.microsoft.com/v1.0"
GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
BOGOTA = datetime(2026, 11, 1, 5, tzinfo=timezone.utc)  # 2026-11-01 00:00 in Bogota


def mime(identifier: str, received: str, amount: str = "$ 1.234,56", subject: str = "Compra Tienda") -> bytes:
    message = EmailMessage()
    message["Received"] = "from sender.test by mx.test; " + received
    message["From"] = f"Avisos <{SENDER}>"
    message["To"] = "persona@example.test"
    message["Date"] = received
    message["Message-ID"] = f"<{identifier}@example.test>"
    message["Subject"] = subject
    message.set_content(f"Compraste en Tienda {amount}.")
    return message.as_bytes()


# ---------------------------------------------------------------- configuration


def graph_settings(**updates) -> dict:
    settings = {
        "ollama_url": "http://127.0.0.1:11434", "ollama_model": "local-test",
        "graph_client_id": CLIENT_ID, "max_messages": 50, "folder_scan_verified": True,
    }
    settings.update(updates)
    return settings


GMAIL_BOX = {"name": "manuela", "provider": "gmail", "owner": "manuela", "verified": True}
OUTLOOK_BOX = {"name": "santiago", "provider": "graph", "owner": "santiago"}


def test_without_a_mailbox_list_only_santiagos_outlook_is_read_with_its_existing_session():
    [box] = load_mailboxes(graph_settings())
    # The origin and session file of existing installations must not change.
    assert (box.provider, box.owner, box.origin, box.token_cache) == (
        "graph", "santiago", "outlook.live.com", "graph_token_cache.bin",
    )
    assert box.verified


def test_gmail_mailbox_defaults_keep_its_own_identifiers_and_files():
    boxes = load_mailboxes(graph_settings(mailboxes=[OUTLOOK_BOX, GMAIL_BOX]))
    outlook, gmail = boxes
    assert primary_graph(boxes) is outlook and outlook.token_cache == "graph_token_cache.bin"
    assert (gmail.origin, gmail.token_cache, gmail.client_secrets) == (
        "mail.google.com", "gmail_token_manuela.json", "gmail_client_secret.json",
    )


@pytest.mark.parametrize("boxes,message", [
    ([OUTLOOK_BOX, {**GMAIL_BOX, "origin": "outlook.live.com"}], "origin"),
    ([OUTLOOK_BOX, {**GMAIL_BOX, "name": "santiago"}], "name"),
    ([OUTLOOK_BOX, {**GMAIL_BOX, "token_cache": "graph_token_cache.bin"}], "token_cache"),
    ([OUTLOOK_BOX, {**GMAIL_BOX, "token_cache": "..\\fuera.json"}], "token_cache"),
    ([OUTLOOK_BOX, {**GMAIL_BOX, "client_secrets": "C:/x.json"}], "client_secrets"),
    ([OUTLOOK_BOX, {**GMAIL_BOX, "provider": "imap"}], "provider"),
    ([OUTLOOK_BOX, {**GMAIL_BOX, "owner": "otra"}], "Titular"),
    ([OUTLOOK_BOX, {**GMAIL_BOX, "verified": "si"}], "verified"),
    ([OUTLOOK_BOX, {**GMAIL_BOX, "password": "x"}], "admite solo"),
    ([{**OUTLOOK_BOX, "verified": True}], "folder_scan_verified"),
    ([], "1 a 5"),
])
def test_invalid_mailbox_lists_are_rejected(boxes, message):
    with pytest.raises(ValueError, match=message):
        load_mailboxes(graph_settings(mailboxes=boxes))


def test_runtime_requires_santiagos_outlook_among_the_mailboxes(tmp_path):
    (tmp_path / "runtime.json").write_text(json.dumps(graph_settings(mailboxes=[GMAIL_BOX])), encoding="utf-8")
    with pytest.raises(ValueError, match="Outlook de Santiago"):
        app.runtime(tmp_path)


def test_mailbox_aliases_replace_the_step_zero_accounts_for_that_owner(preferences):
    [_, gmail] = load_mailboxes(graph_settings(mailboxes=[
        OUTLOOK_BOX, {**GMAIL_BOX, "accounts": ["Tarjeta M"], "excluded_accounts": []},
    ]))
    own = gmail.preferences(preferences)
    assert own.accounts == ["Tarjeta M"] and own.excluded_accounts == []
    assert own.approved_senders == preferences.approved_senders


# ---------------------------------------------------------------- Graph delta


def row(identifier: str, received: str, sender: str = SENDER) -> dict:
    return {
        "id": identifier, "subject": "Compra Tienda", "receivedDateTime": received,
        "from": {"emailAddress": {"address": sender}},
    }


class DeltaGraph:
    """Scripted Graph with one inbox and only the routes the incremental walk uses."""

    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.changes: list[dict] = []
        self.rounds = 0
        self.expired = False
        self.requests: list[str] = []
        self.raw: dict[str, bytes] = {}
        self.closed = False
        self.searches: list[dict | None] = []
        self.searchable: set[str] = set()

    def get_json(self, url: str, params: dict | None = None) -> dict:
        validate_graph_url(url)
        self.requests.append(url)
        if urlparse(url).path == "/v1.0/me/messages":  # search used to retry the review queue
            self.searches.append(params)
            return {"value": [message for message in self.messages if message["id"] in self.searchable]}
        path = urlparse(url).path.removeprefix("/v1.0/me/mailFolders").strip("/")
        if not path:
            return {"value": [
                {"id": name, "displayName": label, "childFolderCount": 0}
                for name, label in (
                    ("inbox", "Bandeja de entrada"), ("junk", "Correo no deseado"),
                    ("deleted", "Elementos eliminados"), ("din", "Dinero"),
                )
            ]}
        folder, *rest = path.split("/")
        if not rest:  # well-known folders are fetched by name
            folder = {"junkemail": "junk", "deleteditems": "deleted"}.get(folder, folder)
            return {"id": folder, "displayName": folder, "childFolderCount": 0}
        assert rest == ["messages", "delta"]
        if folder != "inbox":
            return {"value": [], "@odata.deltaLink": f"{GRAPH}/me/mailFolders/{folder}/messages/delta?t=0"}
        second_page = "page=2" in url
        if "t=" in url or "src=changes" in url:
            if self.expired and not second_page:
                raise GraphError("Graph respondio 400", 400, "syncStateNotFound")
            source, values = "changes", self.changes
        else:
            # Only the first request carries the query; links already encode it.
            assert params == (None if second_page else {"$select": "id,subject,from,receivedDateTime"})
            source, values = "messages", self.messages
        if not second_page and len(values) > 1:
            return {
                "value": values[:1],
                "@odata.nextLink": f"{GRAPH}/me/mailFolders/inbox/messages/delta?src={source}&page=2",
            }
        self.rounds += 1
        return {
            "value": values[1:] if "page=2" in url else values,
            "@odata.deltaLink": f"{GRAPH}/me/mailFolders/inbox/messages/delta?t={self.rounds}",
        }

    def get_bytes(self, url: str) -> bytes:
        validate_graph_url(url)
        return self.raw[unquote(urlparse(url).path.split("/")[-2])]

    def close(self) -> None:
        self.closed = True


def walk(reader, state: dict, visit=lambda candidate: None, limit: int = 50):
    saved: dict[str, str] = {}
    reports = reader.walk_changes(state, BOGOTA, limit, visit, saved.__setitem__)
    return reports, saved


def test_first_delta_round_lists_the_folder_and_saves_its_link_after_visiting():
    box = DeltaGraph()
    box.messages = [
        row("new", "2026-11-13T10:00:00Z"), row("old", "2026-10-31T10:00:00Z"),
        row("other", "2026-11-12T10:00:00Z", sender="otro@example.test"),
    ]
    visited = []
    reports, saved = walk(GraphSourceReader(box, [SENDER]), {}, lambda c: visited.append(c.item_id))
    assert visited == ["new"]  # before the start date and unauthorized senders are not visited
    assert saved["inbox"].endswith("t=1") and set(saved) == {"inbox", "junk", "deleted", "din"}
    assert reports[0] == {
        "folder": "Bandeja de entrada", "rows": 2, "authorized": 1, "incremental": True, "initial": True,
    }


def test_next_delta_round_reads_only_changes_and_ignores_removed_messages():
    box = DeltaGraph()
    box.changes = [row("later", "2026-11-14T10:00:00Z"), {"id": "gone", "@removed": {"reason": "deleted"}}]
    visited = []
    state = {"inbox": f"{GRAPH}/me/mailFolders/inbox/messages/delta?t=7"}
    reports, saved = walk(GraphSourceReader(box, [SENDER]), state, lambda c: visited.append(c.item_id))
    assert visited == ["later"] and "initial" not in reports[0]
    assert state["inbox"] in box.requests


def test_an_expired_delta_link_synchronises_that_folder_again():
    box = DeltaGraph()
    box.expired = True
    box.messages = [row("new", "2026-11-13T10:00:00Z")]
    visited = []
    reports, saved = walk(
        GraphSourceReader(box, [SENDER]), {"inbox": f"{GRAPH}/me/mailFolders/inbox/messages/delta?t=7"},
        lambda c: visited.append(c.item_id),
    )
    assert visited == ["new"] and reports[0]["initial"] and saved["inbox"].endswith("t=1")


def test_other_graph_errors_stop_and_a_failed_visit_keeps_the_previous_link():
    box = DeltaGraph()
    box.messages = [row("new", "2026-11-13T10:00:00Z")]

    def fail(candidate):
        raise OutlookError("detenido")

    saved: dict[str, str] = {}
    with pytest.raises(OutlookError):
        GraphSourceReader(box, [SENDER]).walk_changes({}, BOGOTA, 50, fail, saved.__setitem__)
    assert "inbox" not in saved

    class Broken(DeltaGraph):
        def get_json(self, url, params=None):
            if "t=" in url:
                raise GraphError("Graph respondio 503", 503, "")
            return super().get_json(url, params)

    with pytest.raises(GraphError):
        walk(GraphSourceReader(Broken(), [SENDER]), {"inbox": f"{GRAPH}/me/mailFolders/inbox/messages/delta?t=7"})


def test_delta_row_limit_is_an_error_not_a_silent_partial_scan():
    box = DeltaGraph()
    box.messages = [row("a", "2026-11-13T10:00:00Z"), row("b", "2026-11-12T10:00:00Z")]
    with pytest.raises(OutlookError, match="Limite total"):
        walk(GraphSourceReader(box, [SENDER]), {}, limit=1)


def test_delta_answers_without_a_usable_link_stop():
    class NoLink(DeltaGraph):
        def get_json(self, url, params=None):
            data = super().get_json(url, params)
            if "messages/delta" in url:
                data.pop("@odata.deltaLink", None)
            return data

    with pytest.raises(GraphError, match="enlace"):
        walk(GraphSourceReader(NoLink(), [SENDER]), {})


# ---------------------------------------------------------------- Gmail


def encoded(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


class FakeGmail:
    """Scripted Gmail API: list, metadata, raw source, history and profile."""

    def __init__(self) -> None:
        self.messages: dict[str, dict] = {}
        self.listed: list[str] = []
        self.history: list[str] | None = []
        self.history_id = "900"
        self.queries: list[dict] = []
        self.raw_reads: list[str] = []
        self.closed = False

    def add(self, identifier: str, received: datetime, sender: str = SENDER, labels=("INBOX",), raw=None) -> None:
        self.messages[identifier] = {
            "id": identifier, "labelIds": list(labels), "internalDate": str(int(received.timestamp() * 1000)),
            "payload": {"headers": [
                {"name": "From", "value": f"Avisos <{sender}>"}, {"name": "Subject", "value": "Compra Tienda"},
            ]},
            "raw": encoded(raw if raw is not None else mime(identifier, received.strftime("%a, %d %b %Y %H:%M:%S +0000"))),
        }
        self.listed.append(identifier)

    def get_json(self, url: str, params: dict | None = None) -> dict:
        validate_gmail_url(url)
        path = urlparse(url).path.removeprefix("/gmail/v1/users/me/")
        params = params or {}
        if path == "profile":
            return {"emailAddress": "persona@example.test", "historyId": self.history_id}
        if path == "messages":
            self.queries.append(params)
            return {"messages": [{"id": item, "threadId": "t"} for item in self.listed]}
        if path == "history":
            if self.history is None:
                raise GmailError("Gmail respondio 404", 404, "404")
            return {"history": [{"messagesAdded": [{"message": {"id": item}}]} for item in self.history],
                    "historyId": "950"}
        identifier = unquote(path.removeprefix("messages/"))
        if identifier not in self.messages:
            raise GmailError("Gmail respondio 404", 404, "404")
        message = self.messages[identifier]
        if params.get("format") == "raw":
            self.raw_reads.append(identifier)
            return {"id": identifier, "raw": message["raw"]}
        assert params == {"format": "metadata", "metadataHeaders": ["From", "Subject"]}
        return {key: value for key, value in message.items() if key != "raw"}

    def close(self) -> None:
        self.closed = True


def gmail_box() -> FakeGmail:
    box = FakeGmail()
    box.add("g1", datetime(2026, 11, 13, 15, tzinfo=timezone.utc))
    box.add("g2", datetime(2026, 11, 12, 15, tzinfo=timezone.utc), sender="otro@example.test")
    box.add("sent", datetime(2026, 11, 11, 15, tzinfo=timezone.utc), labels=("SENT",))
    box.add("old", datetime(2026, 10, 20, 15, tzinfo=timezone.utc))
    return box


def test_gmail_lists_exact_senders_with_spam_and_trash_and_reads_only_authorized_notices():
    box = gmail_box()
    visited = []
    reader = GmailSourceReader(box, [SENDER])
    reports, saved = walk(reader, {}, lambda c: visited.append(c.item_id))
    query = box.queries[0]
    assert query["q"].startswith(f'from:("{SENDER}") after:') and query["includeSpamTrash"] == "true"
    assert visited == ["g1"] and saved == {"history": "900"}
    assert reports == [{"folder": "Gmail", "rows": 3, "authorized": 1, "incremental": True, "initial": True}]
    assert box.raw_reads == []  # listing never opens a message
    mail = reader.read(Candidate("g1", SENDER, "Compra Tienda"))
    assert mail.sender == SENDER and "Compraste en Tienda" in mail.body and box.raw_reads == ["g1"]


def test_gmail_message_identity_is_hashed_with_its_own_origin():
    box = gmail_box()
    reader = GmailSourceReader(box, [SENDER])
    walk(reader, {})
    gmail_id = reader.read(Candidate("g1", SENDER, "Compra Tienda")).id
    other = GmailSourceReader(box, [SENDER], origin="outlook.live.com")
    assert gmail_id != other.read(Candidate("g1", SENDER, "Compra Tienda")).id


def test_gmail_history_reads_only_added_messages_and_saves_the_newest_id():
    box = gmail_box()
    box.add("g3", datetime(2026, 11, 14, 15, tzinfo=timezone.utc))
    box.history = ["g3", "deleted-meanwhile"]
    visited = []
    reports, saved = walk(GmailSourceReader(box, [SENDER]), {"history": "900"}, lambda c: visited.append(c.item_id))
    assert visited == ["g3"] and saved == {"history": "950"} and box.queries == []
    assert "initial" not in reports[0]


def test_expired_gmail_history_falls_back_to_a_full_listing():
    box = gmail_box()
    box.history = None
    visited = []
    reports, saved = walk(GmailSourceReader(box, [SENDER]), {"history": "1"}, lambda c: visited.append(c.item_id))
    assert visited == ["g1"] and reports[0]["initial"] and saved == {"history": "900"}


def test_gmail_full_walk_is_newest_first_and_stops_when_asked():
    box = gmail_box()
    box.add("g0", datetime(2026, 11, 2, 15, tzinfo=timezone.utc))
    visited = []

    def visit(candidate):
        visited.append(candidate.item_id)
        return False

    [report] = GmailSourceReader(box, [SENDER]).walk_folders(50, visit, since=BOGOTA)
    assert visited == ["g1"] and report["stopped_before_start"]
    assert "after:" in box.queries[0]["q"]


@pytest.mark.parametrize("senders", [[], ["no-es-correo"], ['a"b@example.test'], ["a)b@example.test"]])
def test_gmail_reader_requires_exact_quotable_senders(senders):
    with pytest.raises(ValueError):
        GmailSourceReader(FakeGmail(), senders)


def test_gmail_source_with_another_sender_or_invalid_encoding_stops():
    box = FakeGmail()
    box.add("x", datetime(2026, 11, 13, 15, tzinfo=timezone.utc), raw=b"\xff\xfe")
    with pytest.raises(OutlookError, match="UTF-8"):
        GmailSourceReader(box, [SENDER]).read(Candidate("x", SENDER, "Compra Tienda"))
    box.add("y", datetime(2026, 11, 13, 15, tzinfo=timezone.utc))
    with pytest.raises(OutlookError, match="remitente"):
        GmailSourceReader(box, [SENDER, "otro@example.test"]).read(Candidate("y", "otro@example.test", "Compra Tienda"))


@pytest.mark.parametrize("url", [
    "https://evil.example/gmail/v1/users/me/messages",
    "http://gmail.googleapis.com/gmail/v1/users/me/messages",
    "https://gmail.googleapis.com.evil.example/gmail/v1/users/me/messages",
    "https://gmail.googleapis.com/gmail/v1/users/otra@example.test/messages",
    "https://graph.microsoft.com/v1.0/me/messages",
])
def test_gmail_token_is_never_sent_outside_the_gmail_api(url):
    sent = []

    class Http:
        def get(self, *args, **kwargs):
            sent.append(args)

        def close(self):
            pass

    with pytest.raises(GmailError, match="Solo se permite la API de Gmail"):
        GmailClient(lambda: "token", http=Http()).get_json(url)
    assert sent == []


class Answer:
    def __init__(self, status: int, payload: dict | None = None, headers: dict | None = None) -> None:
        self.status_code, self._payload, self.headers = status, payload or {}, headers or {}

    def json(self):
        return self._payload


class Answers:
    def __init__(self, *answers: Answer) -> None:
        self.answers, self.requests = list(answers), 0

    def get(self, url, params=None, headers=None):
        self.requests += 1
        return self.answers.pop(0)

    def close(self):
        pass


QUOTA = Answer(403, {"error": {
    "code": 403, "message": "Quota exceeded for quota metric 'Queries' and limit 'Units per minute per user'.",
    "errors": [{"reason": "rateLimitExceeded"}],
}})
PROFILE = "https://gmail.googleapis.com/gmail/v1/users/me/profile"


def test_gmail_waits_out_its_per_minute_quota_and_says_so():
    sleeps, told = [], []
    http = Answers(QUOTA, Answer(429), Answer(200, {"historyId": "1"}))
    client = GmailClient(lambda: "token", http=http, sleep=sleeps.append)
    client.on_wait = told.append
    assert client.get_json(PROFILE) == {"historyId": "1"}
    assert sleeps == told == [15.0, 30.0]

    http = Answers(*[QUOTA] * 5)
    with pytest.raises(GmailError, match="403"):
        GmailClient(lambda: "token", http=http, sleep=lambda seconds: None).get_json(PROFILE)
    assert http.requests == 5  # four waits of the minute's quota, then it stops


def test_a_gmail_permission_refusal_is_never_repeated():
    denied = Answer(403, {"error": {"code": 403, "message": "Request had insufficient authentication scopes.",
                                     "errors": [{"reason": "insufficientPermissions"}]}})
    http = Answers(denied)
    with pytest.raises(GmailError, match="insufficient"):
        GmailClient(lambda: "token", http=http, sleep=lambda seconds: pytest.fail("no wait")).get_json(PROFILE)
    assert http.requests == 1


def test_gmail_session_is_required_and_only_read_only_scopes_are_kept(tmp_path):
    with pytest.raises(GmailError, match="login"):
        gmail_client.silent_token(tmp_path / "gmail_token_manuela.json")

    class Credentials:
        granted_scopes = ["https://www.googleapis.com/auth/gmail.readonly", "https://mail.google.com/"]
        scopes = granted_scopes

    with pytest.raises(GmailError, match="gmail.readonly"):
        gmail_client._read_only(Credentials())
    Credentials.granted_scopes = list(gmail_client.SCOPES)
    gmail_client._read_only(Credentials())


def test_login_with_a_gmail_mailbox_uses_its_client_and_session_files(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        gmail_client, "interactive_login", lambda *args, **kwargs: calls.append((args, kwargs)) or "persona@example.test",
    )
    message = app.login(tmp_path, graph_settings(mailboxes=[OUTLOOK_BOX, {**GMAIL_BOX, "address": "Persona@Example.test"}]), "manuela")
    # The new session must belong to the configured account.
    assert calls == [(
        (tmp_path / "gmail_client_secret.json", tmp_path / "gmail_token_manuela.json"),
        {"expected": "persona@example.test"},
    )]
    assert "solo lectura de Gmail" in message
    with pytest.raises(ValueError, match="no configurado"):
        app.login(tmp_path, graph_settings(), "manuela")


# ---------------------------------------------------------------- store


def insert_imported(
    store: Store, owner: str, day: str, amount: int, legacy: str = "x",
    kind: str = "purchase", category: str = "Compras",
) -> str:
    """A movement as the one-off import of a member's exported files left it."""
    identifier = f"{owner}:{day}:{amount}:{legacy}"
    with store.connect() as db:
        db.execute(
            """INSERT INTO transactions (id,message_id,day,merchant,account,amount_minor,currency,
            category,kind,decision,reason,owner,note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (identifier, f"{owner}-import:{day}:{amount}:{legacy}", day, "Comercio del archivo",
             "TC *0001", amount, "COP", category, kind, "family", "importado", owner, "Nota de ella"),
        )
    return identifier


def sources(store: Store) -> dict[str, str]:
    """Which notice (or file) each movement comes from."""
    with store.connect() as db:
        return {row["id"]: row["message_id"] for row in db.execute("SELECT id, message_id FROM transactions")}


def event(identifier: str, day: str, amount: int) -> dict:
    return {
        "id": identifier, "message_id": "m-" + identifier, "day": day, "merchant": "Tienda", "account": "Tarjeta A",
        "amount_minor": amount, "currency": "COP", "category": "Compras", "kind": "purchase",
        "decision": None, "reason": "Propuesta",
    }


def test_a_notice_completes_its_only_file_twin_and_flags_several_without_deciding_them(
    tmp_path, preferences,
):
    store = Store(tmp_path / "close.sqlite3")
    store.configure(preferences.to_dict())
    store.add_rule("TIENDA", "Compras", "personal")
    twin = insert_imported(store, "manuela", "2026-11-05", 123456)
    first, second = (insert_imported(store, "manuela", "2026-11-07", 500, legacy) for legacy in "ab")
    store.import_message("m-a", [
        {**event("a", "2026-11-05", 123456), "time": "10:15"}, event("b", "2026-11-05", 999),
    ], owner="manuela")
    store.import_message("m-c", [event("c", "2026-11-05", 123456)])
    store.import_message("m-d", [event("d", "2026-11-07", 500)], owner="manuela")
    rows = {row["id"]: row for row in store.state()["transactions"]}
    source = sources(store)

    # The notice becomes the source of her movement: its fields, her decision and note.
    assert "a" not in rows
    completed = rows[twin]
    assert (source[twin], completed["merchant"], completed["account"], completed["time"]) == (
        "m-a", "Tienda", "Tarjeta A", "10:15",
    )
    assert (completed["decision"], completed["category"], completed["note"]) == ("family", "Compras", "Nota de ella")
    assert rows["b"]["owner"] == "manuela" and rows["b"]["decision"] == "personal"  # no twin: the rule decides
    assert rows["c"]["owner"] == "santiago" and rows["c"]["note"] is None  # another owner's file movement
    # Several twins: a person compares them; no rule decides it and both stay from the file.
    assert rows["d"]["decision"] is None and rows["d"]["note"].startswith("Posible duplicado de 2 movimientos")
    assert source[first].startswith("manuela-import:") and source[second].startswith("manuela-import:")
    with store.connect() as db:
        audit = dict(db.execute(
            "SELECT action, target FROM audit WHERE action LIKE 'mailbox-%' ORDER BY id",
        ).fetchall())
    assert audit == {"mailbox-complete-file-import": twin, "mailbox-possible-duplicate": "d"}


def test_reprocessing_a_notice_never_deletes_the_file_movement_it_completed(tmp_path, preferences):
    store = Store(tmp_path / "close.sqlite3")
    store.configure(preferences.to_dict())
    twin = insert_imported(store, "manuela", "2026-11-05", 7000, kind="card_payment", category="Otros gastos")
    guessed = {**event("g", "2026-11-05", 7000), "kind": "card_payment", "category": "Otros gastos"}
    store.import_message("m-g", [guessed], owner="manuela")
    store.import_message("m-g", [], reprocess=True, owner="manuela")
    assert [row["id"] for row in store.state()["transactions"]] == [twin]


def test_sync_state_is_saved_per_mailbox_and_pruned(tmp_path):
    store = Store(tmp_path / "close.sqlite3")
    store.save_sync("santiago", "inbox", "a")
    store.save_sync("santiago", "gone", "b")
    store.save_sync("manuela", "history", "9")
    store.save_sync("santiago", "inbox", "c")
    store.prune_sync("santiago", {"inbox"})
    assert store.sync_state("santiago") == {"inbox": "c"} and store.sync_state("manuela") == {"history": "9"}


# ---------------------------------------------------------------- end to end


class Model:
    def __init__(self) -> None:
        self.calls = 0

    def complete_json(self, system, user):
        self.calls += 1
        return {"events": [{
            "merchant": "Tienda", "account": "Desconocida", "amount_text": "$ 1.234,56",
            "currency": "COP", "date_text": "", "category": "Compras", "kind": "purchase", "savings": False,
        }]}


def two_mailboxes(tmp_path, preferences, monkeypatch, gmail_verified: bool = True):
    outlook, gmail = DeltaGraph(), FakeGmail()
    outlook.messages = [row("o1", "2026-11-13T10:00:00Z")]
    outlook.raw["o1"] = mime("o1", "Fri, 13 Nov 2026 10:00:00 +0000")
    gmail.add("before", datetime(2026, 11, 3, 15, tzinfo=timezone.utc))
    gmail.add("cutoff", datetime(2026, 11, 5, 15, tzinfo=timezone.utc))
    gmail.add("after", datetime(2026, 11, 6, 15, tzinfo=timezone.utc))
    model = Model()
    monkeypatch.setattr(app, "silent_token", lambda *args: (lambda: "t"))
    monkeypatch.setattr(app, "GraphClient", lambda token: outlook)
    monkeypatch.setattr(app.gmail_client, "silent_token", lambda path: (lambda: "t"))
    monkeypatch.setattr(app, "GmailClient", lambda token: gmail)
    monkeypatch.setattr(app, "OllamaJsonModel", lambda *args: model)
    monkeypatch.setattr(app, "require_model", lambda settings: None)
    store = Store(tmp_path / "close.sqlite3")
    store.configure(preferences.to_dict())
    settings = graph_settings(mailboxes=[OUTLOOK_BOX, {**GMAIL_BOX, "verified": gmail_verified}])
    return store, settings, outlook, gmail, model


def test_incremental_scan_reads_both_mailboxes_from_the_same_start_and_completes_file_movements(
    tmp_path, preferences, monkeypatch, capsys,
):
    store, settings, outlook, gmail, model = two_mailboxes(tmp_path, preferences, monkeypatch)
    twin = insert_imported(store, "manuela", "2026-11-05", 123456)
    args = (store, tmp_path, settings, preferences, date(2026, 1, 1), date(2026, 11, 17))

    app.scan(*args, incremental=True)

    rows = store.state()["transactions"]
    # Both mailboxes are read from the same start; every movement now comes from a notice.
    assert sorted((row["owner"], row["day"]) for row in rows) == [
        ("manuela", "2026-11-03"), ("manuela", "2026-11-05"), ("manuela", "2026-11-06"), ("santiago", "2026-11-13"),
    ]
    assert not any(origin.startswith("manuela-import:") for origin in sources(store).values())
    completed = next(row for row in rows if row["id"] == twin)
    assert completed["decision"] == "family" and completed["note"] == "Nota de ella"
    assert gmail.raw_reads == ["before", "cutoff", "after"]
    assert store.sync_state("manuela") == {"history": "900"} and "inbox" in store.sync_state("santiago")
    output = capsys.readouterr().out
    assert "Buzon manuela (gmail, titular manuela) desde 2026-01-01." in output
    assert "primera sincronizacion" in output

    calls = model.calls
    gmail.history, outlook.changes = [], []
    app.scan(*args, incremental=True)
    assert model.calls == calls and len(store.state()["transactions"]) == len(rows)
    assert "solo cambios desde la anterior" in capsys.readouterr().out


def test_an_unverified_gmail_mailbox_blocks_imports_but_allows_a_read_only_listing(
    tmp_path, preferences, monkeypatch, capsys,
):
    store, settings, outlook, gmail, model = two_mailboxes(tmp_path, preferences, monkeypatch, gmail_verified=False)
    with pytest.raises(ValueError, match="manuela"):
        app.scan(store, tmp_path, settings, preferences, date(2026, 1, 1), date(2026, 11, 17), incremental=True)
    assert model.calls == 0

    class Listing(DeltaGraph):
        def get_json(self, url, params=None):
            if "messages/delta" in url:
                raise AssertionError("read-only never uses delta")
            if url.endswith("/messages") or "/messages?" in url:
                return {"value": []}
            if "/mailFolders/" in url and "childFolders" in url:
                return {"value": []}
            if "/mailFolders/" in url:
                return {"id": "x", "displayName": "x", "childFolderCount": 0, "totalItemCount": 0}
            return super().get_json(url, params)

    monkeypatch.setattr(app, "GraphClient", lambda token: Listing())
    app.scan(store, tmp_path, settings, preferences, date(2026, 11, 1), date(2026, 12, 1), read_only=True)
    assert gmail.raw_reads == [] and store.sync_state("manuela") == {}
    assert "Gmail: 3 filas verificadas; 3 de remitentes autorizados" in capsys.readouterr().out


def test_incremental_mode_refuses_read_only_and_reprocessing(tmp_path, preferences):
    store = Store(tmp_path / "close.sqlite3")
    store.configure(preferences.to_dict())
    for flags in ({"read_only": True}, {"reprocess_empty": True}):
        with pytest.raises(ValueError, match="incremental"):
            app.scan(store, tmp_path, graph_settings(), preferences, date(2026, 1, 1), date(2026, 2, 1),
                     incremental=True, **flags)


# ---------------------------------------------------------------- review queue


class InventingModel(Model):
    """Proposes a merchant missing from the notice until told to stop."""

    def __init__(self) -> None:
        super().__init__()
        self.invent = True

    def complete_json(self, system, user):
        result = super().complete_json(system, user)
        if self.invent:
            result["events"][0]["merchant"] = "Inventado"
        return result


def test_incremental_runs_retry_queued_notices_that_graph_no_longer_reports(
    tmp_path, preferences, monkeypatch, capsys,
):
    outlook = DeltaGraph()
    outlook.messages = [row("o1", "2026-11-13T10:00:00Z")]
    outlook.raw["o1"] = mime("o1", "Fri, 13 Nov 2026 10:00:03 +0000")
    model = InventingModel()
    monkeypatch.setattr(app, "silent_token", lambda *args: (lambda: "t"))
    monkeypatch.setattr(app, "GraphClient", lambda token: outlook)
    monkeypatch.setattr(app, "OllamaJsonModel", lambda *args: model)
    monkeypatch.setattr(app, "require_model", lambda settings: None)
    store = Store(tmp_path / "close.sqlite3")
    store.configure(preferences.to_dict())
    args = (store, tmp_path, graph_settings(), preferences, date(2026, 1, 1), date(2026, 11, 17))

    app.scan(*args, incremental=True)
    assert [item["sender"] for item in store.queued_failures()] == [SENDER]
    assert store.state()["transactions"] == [] and outlook.searches == []

    # Next run: Graph reports no change, yet the queued notice is found and read again.
    model.invent = False
    outlook.searchable = {"o1"}
    app.scan(*args, incremental=True)
    assert store.queued_failures() == []
    assert [row["merchant"] for row in store.state()["transactions"]] == ["Tienda"]
    [search] = outlook.searches
    assert f"from/emailAddress/address eq '{SENDER}'" in search["$filter"]
    assert "receivedDateTime ge 2026-11-13T09:45:03Z" in search["$filter"]
    assert "Cola de revision: 1 aviso(s) leidos de nuevo" in capsys.readouterr().out

    # Once imported, nothing is searched again.
    app.scan(*args, incremental=True)
    assert len(outlook.searches) == 1


def test_gmail_finds_a_queued_notice_only_from_its_sender_near_its_time():
    box = gmail_box()
    reader = GmailSourceReader(box, [SENDER])
    moment = datetime(2026, 11, 13, 15, 2, tzinfo=timezone.utc)
    window = app.QUEUE_WINDOW
    assert [c.item_id for c in reader.locate(SENDER, moment, window)] == ["g1"]
    assert 'from:("avisos@example.test")' in box.queries[0]["q"] and "before:" in box.queries[0]["q"]
    assert reader.locate(SENDER, moment.replace(hour=17), window) == []
    with pytest.raises(ValueError):
        reader.locate("otro@example.test", moment, window)


def test_graph_search_answering_another_sender_stops():
    outlook = DeltaGraph()
    outlook.messages = [row("x", "2026-11-13T10:00:00Z", sender="otro@example.test")]
    outlook.searchable = {"x"}
    reader = GraphSourceReader(outlook, [SENDER])
    with pytest.raises(OutlookError, match="otro remitente"):
        reader.locate(SENDER, datetime(2026, 11, 13, 10, tzinfo=timezone.utc), app.QUEUE_WINDOW)
