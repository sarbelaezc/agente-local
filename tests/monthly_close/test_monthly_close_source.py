from __future__ import annotations

from contextlib import contextmanager
from email.message import EmailMessage

import httpx
import pytest

from monthly_close.outlook import Candidate, OutlookError
from monthly_close.source_reader import parse_source
from monthly_close import main as app
from monthly_close.domain import CATEGORIES
from monthly_close.store import Store

CANDIDATE = Candidate("row-1", "avisos@example.test", "Compra Tienda")


def source(*, html: bool = False) -> str:
    message = EmailMessage()
    message["Received"] = "from sender.test by outlook.test; Sun, 01 Mar 2026 02:15:00 +0000"
    message["From"] = "Avisos <avisos@example.test>"
    message["To"] = "test@example.test"
    message["Date"] = "Sat, 28 Feb 2026 21:14:00 -0500"
    message["Message-ID"] = "<transaction-1@example.test>"
    message["Subject"] = "Compra Tienda"
    if html:
        message.set_content(
            "<html><head><title>No extraer</title><style>oculto</style></head>"
            "<body><p>Compraste en Tienda</p><p>$ 1.234,56</p>"
            "<script>throw new Error('no ejecutar');</script></body></html>", subtype="html",
        )
    else:
        message.set_content("Compraste en Tienda $ 1.234,56.")
    message.add_attachment(b"do not decode", maintype="application", subtype="pdf", filename="test.pdf")
    return message.as_string()


def test_source_checks_sender_subject_message_id_and_received_timezone():
    mail = parse_source(source(), CANDIDATE, "outlook.live.com")
    assert mail.sender == CANDIDATE.sender
    assert mail.subject == CANDIDATE.subject
    assert mail.received == "2026-03-01T02:15:00+00:00"
    assert mail.body.strip() == "Compraste en Tienda $ 1.234,56."
    assert "do not decode" not in mail.body
    assert len(mail.id) == 64


def test_conversation_id_is_not_used_as_persistent_message_identity():
    first = parse_source(source(), CANDIDATE, "outlook.live.com")
    moved = parse_source(
        source(), Candidate("another-row-id", CANDIDATE.sender, CANDIDATE.subject),
        "outlook.live.com",
    )
    other_message = parse_source(
        source().replace("<transaction-1@example.test>", "<transaction-2@example.test>"),
        CANDIDATE, "outlook.live.com",
    )
    assert first.id == moved.id
    assert first.id != other_message.id


def test_html_is_converted_without_loading_or_executing_resources():
    mail = parse_source(source(html=True), CANDIDATE, "outlook.live.com")
    assert "Compraste en Tienda" in mail.body
    assert "$ 1.234,56" in mail.body
    assert "no ejecutar" not in mail.body
    assert "No extraer" not in mail.body
    assert "oculto" not in mail.body


@pytest.mark.parametrize(("old", "new", "error"), [
    ("Avisos <avisos@example.test>", "Other <other@example.test>", "remitente"),
    ("Subject: Compra Tienda", "Subject: Otro asunto", "asunto"),
    ("Message-ID: <transaction-1@example.test>\n", "", "Message-ID"),
    ("Message-ID: <transaction-1@example.test>", "Message-ID: invalid", "Message-ID"),
    ("Date: Sat, 28 Feb 2026 21:14:00 -0500", "Date: invalid", "Date"),
    ("02:15:00 +0000", "02:15:00 -0000", "zona horaria"),
    ("Date: Sat, 28 Feb 2026 21:14:00 -0500", "Date: Sat, 28 Feb 2026 21:14:00 -0000", "zona horaria"),
])
def test_source_identity_or_dates_cannot_be_guessed(old, new, error):
    with pytest.raises(OutlookError, match=error):
        parse_source(source().replace(old, new), CANDIDATE, "outlook.live.com")


@pytest.mark.parametrize("header", ["From", "Subject", "Message-ID", "Date"])
def test_duplicate_critical_headers_stop(header):
    raw = source()
    line = next(line for line in raw.splitlines() if line.startswith(header + ":"))
    with pytest.raises(OutlookError, match=header):
        parse_source(line + "\n" + raw, CANDIDATE, "outlook.live.com")


@pytest.fixture
def pilot(tmp_path, monkeypatch):
    store = Store(tmp_path / "pilot.sqlite3")
    store.configure({
        "location": "Prueba", "timezone": "America/Bogota", "currency": "COP",
        "accounts": ["Tarjeta A"], "excluded_accounts": [],
        "transfer_label": "Familiar", "ask_savings": True, "ask_people": True,
        "approved_senders": [CANDIDATE.sender], "categories": CATEGORIES,
        "confidential_confirmed": True,
    })

    class Reader:
        def candidate(self):
            return CANDIDATE

        def read(self, candidate):
            return parse_source(source(), candidate, "outlook.live.com")

    @contextmanager
    def fake_source(data_dir, settings, approved_senders):
        assert approved_senders == [CANDIDATE.sender]
        yield Reader()

    monkeypatch.setattr(app, "open_source", fake_source)
    settings = {"ollama_url": "http://127.0.0.1:11434", "ollama_model": "local-test"}
    return store, settings, tmp_path


def test_read_only_pilot_uses_one_notice_and_never_calls_model_or_imports(pilot, monkeypatch):
    store, settings, tmp_path = pilot
    monkeypatch.setattr(
        app, "OllamaJsonModel", lambda *args: pytest.fail("No debe usarse Ollama en solo lectura."),
    )
    app.run_pilot(store, tmp_path, settings, True)
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM processed").fetchone()[0] == 0
    assert store.state()["transactions"] == []


def test_full_pilot_persists_only_unapproved_proposals(pilot, monkeypatch):
    store, settings, tmp_path = pilot
    original_client = httpx.Client
    monkeypatch.setattr(
        app.httpx, "Client",
        lambda **kwargs: original_client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"models": [{"name": "local-test"}]}),
        )),
    )

    class Model:
        def __init__(self, *args):
            pass

        def complete_json(self, system, user):
            assert 'devuelve {"events":[]}' in system
            return {"events": [{
                "merchant": "Tienda", "account": "Tarjeta A", "amount_text": "$ 1.234,56",
                "currency": "COP", "date_text": "", "category": "Compras",
                "kind": "purchase", "savings": False,
            }]}

    monkeypatch.setattr(app, "OllamaJsonModel", Model)
    app.run_pilot(store, tmp_path, settings, False)
    state = store.state()
    assert len(state["transactions"]) == 1
    row = state["transactions"][0]
    assert row["amount_minor"] == 123456
    assert row["day"] == "2026-02-28"
    assert row["decision"] is None
    assert state["coverage"][0]["status"] == "partial"
    store.decide(row["id"], "personal")
    app.run_pilot(store, tmp_path, settings, False)
    assert len(store.state()["transactions"]) == 1
    assert store.state()["transactions"][0]["decision"] == "personal"


def test_missing_model_stops_before_opening_mail(pilot, monkeypatch):
    store, settings, tmp_path = pilot
    original_client = httpx.Client
    monkeypatch.setattr(
        app.httpx, "Client",
        lambda **kwargs: original_client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"models": []}),
        )),
    )
    monkeypatch.setattr(app, "open_source", lambda *args: pytest.fail("No debe abrirse el correo."))
    with pytest.raises(app.LocalModelError, match="no esta instalado"):
        app.run_pilot(store, tmp_path, settings, False)
