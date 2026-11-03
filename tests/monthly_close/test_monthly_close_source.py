from __future__ import annotations

import json
from contextlib import contextmanager
from email.message import EmailMessage

import httpx
import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from monthly_close.outlook import Candidate, OutlookError
from monthly_close.source_reader import OutlookSourceReader, parse_source
from monthly_close import main as app
from monthly_close.domain import CATEGORIES
from monthly_close.store import Store

URL = "https://outlook.live.com/mail/0/"
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


def fixture_html(raw: str) -> str:
    return """<!doctype html><html><body>
<input id="topSearchInput" role="combobox" placeholder="Buscar">
<button aria-label="More items"></button>
<div data-app-section="MessageList" role="listbox">
 <div id="row-1" role="option" data-convid="conversation-1" aria-selected="false"
 onclick="this.setAttribute('aria-selected','true');openMessage()"
 tabindex="0"
 onkeydown="if(event.shiftKey && event.key==='F10'){event.preventDefault();document.querySelector('#context').hidden=false}"
 oncontextmenu="event.preventDefault();window.pointerRetries=(window.pointerRetries||0)+1;this.onkeydown({shiftKey:true,key:'F10',preventDefault:()=>{},currentTarget:this})">
  <span title="avisos@example.test">Avisos</span><span title="Nombre mostrado">Nombre mostrado</span>
  <span class="TtcXM">Compra Tienda</span>
 </div>
</div>
<div id="context" role="menu" hidden>
 <div role="menuitem" tabindex="0" aria-label="Ver"
 onkeydown="if(event.key==='ArrowRight')document.querySelector('#submenu').hidden=false"
 onmouseover="document.querySelector('#submenu').hidden=false">Ver</div>
 <div id="submenu" hidden>
  <button role="menuitem" aria-label="Ver origen del mensaje" onclick="openSource()">Ver origen del mensaje</button>
 </div>
</div>
<script>
const raw = """ + json.dumps(raw) + """;
function openMessage() {
 const close=document.createElement('button');close.textContent='Cerrar';
 close.onclick=()=>close.remove();document.body.append(close);
}
function openSource() {
 document.querySelector('#context').hidden=true;
 const dialog=document.createElement('div');dialog.setAttribute('role','dialog');
 const content=document.createElement('div');content.className='fui-DialogContent allowTextSelection';
 content.textContent=raw;dialog.append(content);
 const close=document.createElement('button');close.textContent='Cerrar';
 close.onclick=()=>dialog.remove();dialog.append(close);document.body.append(dialog);
}
</script></body></html>"""


@pytest.fixture
def page():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        context.route(
            "**/*", lambda route: route.fulfill(body=fixture_html(source()), content_type="text/html"),
        )
        page = context.new_page()
        page.set_default_timeout(500)
        page.goto(URL)
        try:
            yield page
        finally:
            browser.close()


def test_source_reader_uses_targeted_context_menu_not_stale_reading_pane(page):
    reader = OutlookSourceReader(page, URL, [CANDIDATE.sender])
    assert reader.candidate() == CANDIDATE
    mail = reader.read(reader.candidate())
    assert mail.sender == CANDIDATE.sender
    assert mail.body.strip() == "Compraste en Tienda $ 1.234,56."
    assert page.get_by_role("dialog").count() == 0


def test_checkpoint_hash_changes_with_folder_identity_or_row_metadata(page):
    reader = OutlookSourceReader(page, URL, [CANDIDATE.sender])
    candidate = reader.candidate()
    initial = reader.checkpoint_key(candidate)
    assert len(initial) == 64
    assert reader.checkpoint_key(candidate) == initial
    page.locator("[data-convid]").evaluate("el=>el.setAttribute('aria-label','changed date')")
    assert reader.checkpoint_key(candidate) != initial
    with pytest.raises(OutlookError):
        reader.checkpoint_key(Candidate(candidate.item_id, "other@example.test", candidate.subject))

@pytest.mark.parametrize("stage", ["menu", "submenu"])
@pytest.mark.parametrize("calendar_item", [False, True])
def test_closed_source_menu_reopens_once_for_same_authorized_row(
    page, caplog, stage, calendar_item,
):
    if calendar_item:
        page.evaluate(
            """() => {
              const appointment = document.createElement('button');
              appointment.setAttribute('role', 'menuitem');
              appointment.textContent = 'Evento del calendario';
              document.body.append(appointment);
            }"""
        )
    page.evaluate(
        """stage => {
          window.opens = 0;
          const row = document.querySelector('[data-convid]');
          row.onkeydown = event => {
            if (!event.shiftKey || event.key !== 'F10') return;
            event.preventDefault();
            window.opens++;
            document.querySelector('#context').hidden = stage === 'menu' && window.opens === 1;
          };
          if (stage === 'submenu') {
            document.querySelector('[aria-label="Ver"]').onkeydown = event => {
              if (event.key !== 'ArrowRight') return;
              if (window.opens === 1) document.querySelector('#context').hidden = true;
              else document.querySelector('#submenu').hidden = false;
            };
          }
        }""", stage,
    )
    reader = OutlookSourceReader(page, URL, [CANDIDATE.sender])
    assert reader.read(reader.candidate()).sender == CANDIDATE.sender
    assert page.evaluate("window.opens") == 2
    assert page.evaluate("window.pointerRetries") == 1
    assert "un unico reintento" in caplog.text


def test_menu_retry_closes_open_notice_and_reopens_same_message(page):
    page.evaluate(
        """() => {
          window.opens = 0; window.messageOpens = 0; window.messageCloses = 0;
          const row = document.querySelector('[data-convid]');
          const subject = row.querySelector('.TtcXM');
          const text = subject.textContent;
          function openMessage() {
            const hidden = document.createElement('span');
            hidden.style.display = 'none'; hidden.textContent = text;
            subject.replaceChildren(hidden);
            const close = document.createElement('button'); close.textContent = 'Cerrar';
            close.onclick = () => {
              window.messageCloses++;
              subject.textContent = text; close.remove();
            };
            document.body.append(close);
          }
          row.onclick = () => {
            row.setAttribute('aria-selected', 'true');
            window.messageOpens++; openMessage();
          };
          row.onkeydown = event => {
            if (!event.shiftKey || event.key !== 'F10') return;
            event.preventDefault(); window.opens++;
            if (window.opens === 1) openMessage();
            else document.querySelector('#context').hidden = false;
          };
        }"""
    )
    reader = OutlookSourceReader(page, URL, [CANDIDATE.sender])
    assert reader.read(reader.candidate()).sender == CANDIDATE.sender
    assert page.evaluate("window.opens") == 2
    assert page.evaluate("window.messageOpens") == 2
    assert page.evaluate("window.messageCloses") == 3


@pytest.mark.parametrize("loading", [False, True])
def test_message_view_closes_even_when_background_subject_is_still_rendered(page, loading):
    page.evaluate(
        """loading => {
          const row = document.querySelector('[data-convid]');
          row.onclick = () => {
            row.setAttribute('aria-selected', 'true');
            const close = document.createElement('button');
            close.textContent = 'Cerrar';
            const actions = document.querySelector('[aria-label="More items"]');
            if (loading) {
              actions.hidden = true;
              setTimeout(() => actions.hidden = false, 100);
            }
            close.onclick = () => {
              if (actions.hidden) throw new Error('Vista todavia cargando');
              close.remove();
            };
            document.body.append(close);
          };
          row.addEventListener('keydown', event => {
            if (event.shiftKey && event.key === 'F10'
                && [...document.querySelectorAll('button')]
                  .some(button => button.textContent === 'Cerrar')) {
              document.querySelector('#context').hidden = true;
            }
          });
        }""", loading,
    )
    reader = OutlookSourceReader(page, URL, [CANDIDATE.sender])
    assert reader.read(reader.candidate()).sender == CANDIDATE.sender
    assert page.get_by_role("button", name="Cerrar", exact=True).count() == 0


def test_source_read_restores_list_when_reading_view_appears_late(page):
    page.evaluate(
        """() => {
          const open = window.openSource;
          window.openSource = () => {
            open();
            const close = document.createElement('button');
            close.textContent = 'Cerrar';
            close.onclick = () => close.remove();
            document.body.append(close);
          };
        }"""
    )
    reader = OutlookSourceReader(page, URL, [CANDIDATE.sender])
    assert reader.read(reader.candidate()).sender == CANDIDATE.sender
    assert page.get_by_role("button", name="Cerrar", exact=True).count() == 0


@pytest.mark.parametrize("block", ["always_closed", "selection", "sender", "dialog", "mfa", "open_menu"])
def test_menu_retry_is_bounded_and_does_not_bypass_safety(page, block):
    page.evaluate(
        """block => {
          window.opens = 0;
          document.querySelector('[data-convid]').onkeydown = event => {
            if (!event.shiftKey || event.key !== 'F10') return;
            event.preventDefault();
            window.opens++;
            const row = event.currentTarget;
            if (block === 'selection') row.setAttribute('aria-selected', 'false');
            if (block === 'sender') row.querySelector('span[title]').title = 'other@example.test';
            if (block === 'dialog') {
              const dialog = document.createElement('div');
              dialog.setAttribute('role','dialog');
              dialog.setAttribute('aria-hidden','true');
              document.body.append(dialog);
            }
            if (block === 'mfa') {
              const input = document.createElement('input'); input.name = 'otc';
              document.body.append(input);
            }
            if (block === 'open_menu') {
              document.querySelector('#context').hidden = false;
              document.querySelector('[aria-label="Ver"]').remove();
              const other = document.createElement('button');
              other.setAttribute('role','menuitem'); other.textContent = 'Otra accion';
              document.querySelector('#context').append(other);
            }
          };
        }""", block,
    )
    reader = OutlookSourceReader(page, URL, [CANDIDATE.sender])
    with pytest.raises((OutlookError, PlaywrightTimeoutError)):
        reader.read(reader.candidate())
    assert page.evaluate("window.opens") == (2 if block == "always_closed" else 1)


def test_source_reader_handles_visible_dialog_temporarily_hidden_from_accessibility(
    page, monkeypatch,
):
    reader = OutlookSourceReader(page, URL, [CANDIDATE.sender])
    original_row = reader._row

    def checked_row(candidate):
        row = original_row(candidate)
        if page.locator('[role="dialog"]').count():
            page.locator('[role="dialog"]').evaluate(
                "el => el.setAttribute('aria-hidden', 'true')",
            )
        return row

    monkeypatch.setattr(reader, "_row", checked_row)
    mail = reader.read(reader.candidate())
    assert mail.body.strip() == "Compraste en Tienda $ 1.234,56."
    assert page.locator('[role="dialog"]').count() == 0


def test_source_reader_waits_until_dialog_is_physically_closed(page):
    page.evaluate(
        """() => {
          const original = window.openSource;
          window.openSource = () => {
            original();
            const dialog = document.querySelector('[role="dialog"]');
            dialog.querySelector('button').onclick = () => {
              dialog.setAttribute('aria-hidden', 'true');
              setTimeout(() => dialog.remove(), 100);
            };
          };
        }"""
    )
    reader = OutlookSourceReader(page, URL, [CANDIDATE.sender])
    reader.read(reader.candidate())
    assert page.locator('[role="dialog"]').count() == 0


def test_existing_accessibility_hidden_dialog_stops_before_opening_menu(page):
    reader = OutlookSourceReader(page, URL, [CANDIDATE.sender])
    candidate = reader.candidate()
    page.evaluate(
        """() => {
          const dialog = document.createElement('div');
          dialog.setAttribute('role', 'dialog');
          dialog.setAttribute('aria-hidden', 'true');
          document.body.append(dialog);
        }"""
    )
    with pytest.raises(OutlookError, match="dialogo o menu previo"):
        reader.read(candidate)
    assert page.locator("#context").is_hidden()


def test_source_menu_is_activated_without_pointer_dismissing_the_submenu(page):
    page.get_by_role(
        "menuitem", name="Ver origen del mensaje", exact=True, include_hidden=True,
    ).evaluate(
        """el => el.onpointerover = () => {
          document.querySelector('#context').hidden = true;
        }"""
    )
    reader = OutlookSourceReader(page, URL, [CANDIDATE.sender])
    assert reader.read(reader.candidate()).body.strip() == "Compraste en Tienda $ 1.234,56."


def test_dom_row_identity_is_escaped_without_using_conversation_id(page):
    identifier = 'row-"quoted"\\with space'
    page.locator("[data-convid]").evaluate("(el,id)=>el.id=id", identifier)
    reader = OutlookSourceReader(page, URL, [CANDIDATE.sender])
    candidate = reader.candidate()
    assert candidate.item_id == identifier
    assert reader.read(candidate).body.strip() == "Compraste en Tienda $ 1.234,56."


def test_selection_uses_subject_not_empty_row_area(page):
    page.locator("[data-convid]").evaluate(
        """el => {
          el.style.width='900px';
          el.oncontextmenu=event=>{
            event.preventDefault();
            if(!event.target.closest('.TtcXM'))return;
            el.setAttribute('aria-selected','true');
            document.querySelector('#context').hidden=false;
          };
        }"""
    )
    reader = OutlookSourceReader(page, URL, [CANDIDATE.sender])
    assert reader.read(reader.candidate()).body.strip() == "Compraste en Tienda $ 1.234,56."


def test_context_menu_opens_only_after_explicit_selection(page):
    page.locator("[data-convid]").evaluate(
        """el => el.onkeydown = event => {
          event.preventDefault();
          if (!event.shiftKey || event.key !== 'F10') return;
          if (el.getAttribute('aria-selected') !== 'true') return;
          document.querySelector('#context').hidden = false;
        }"""
    )
    reader = OutlookSourceReader(page, URL, [CANDIDATE.sender])
    assert reader.read(reader.candidate()).body.strip() == "Compraste en Tienda $ 1.234,56."


def test_selection_identity_change_stops_before_context_menu(page):
    page.locator("[data-convid]").evaluate(
        """el => el.onclick = () => {
          el.setAttribute('aria-selected', 'true');
          el.querySelector('span[title]').title = 'other@example.test';
        }"""
    )
    reader = OutlookSourceReader(page, URL, [CANDIDATE.sender])
    with pytest.raises(OutlookError, match="resultado cambio"):
        reader.read(reader.candidate())
    assert page.locator("#context").is_hidden()


def test_selected_subject_uses_dom_text_when_rendered_inner_text_is_empty(page):
    page.locator("[data-convid]").evaluate(
        """el => el.onclick = () => {
          el.setAttribute('aria-selected', 'true');
          const subject = el.querySelector('.TtcXM');
          const text = subject.textContent;
          subject.replaceChildren();
          const hidden = document.createElement('span');
          hidden.style.display = 'none';
          hidden.textContent = text;
          subject.append(hidden);
          const close = document.createElement('button');
          close.textContent = 'Cerrar';
          close.onclick = () => {
            subject.textContent = text;
            close.remove();
          };
          document.body.append(close);
        }"""
    )
    reader = OutlookSourceReader(page, URL, [CANDIDATE.sender])
    assert reader.read(reader.candidate()).subject == CANDIDATE.subject


def test_unrecognized_full_message_view_stops_before_opening_menu(page):
    page.locator("[data-convid]").evaluate(
        """el => el.onclick = () => {
          el.setAttribute('aria-selected', 'true');
          const subject = el.querySelector('.TtcXM');
          const hidden = document.createElement('span');
          hidden.style.display = 'none';
          hidden.textContent = subject.textContent;
          subject.replaceChildren(hidden);
        }"""
    )
    reader = OutlookSourceReader(page, URL, [CANDIDATE.sender])
    with pytest.raises(OutlookError, match="No se reconoce el cierre"):
        reader.read(reader.candidate())
    assert page.locator("#context").is_hidden()


def test_unapproved_row_never_opens_context_menu(page):
    reader = OutlookSourceReader(page, URL, ["other@example.test"])
    with pytest.raises(OutlookError, match="No hay avisos"):
        reader.candidate()
    assert page.locator("#context").is_hidden()
    assert page.locator("[aria-selected=true]").count() == 0


def test_changed_sender_is_rechecked_before_click(page):
    reader = OutlookSourceReader(page, URL, [CANDIDATE.sender])
    candidate = reader.candidate()
    page.locator("span[title='avisos@example.test']").evaluate("el=>el.title='other@example.test'")
    with pytest.raises(OutlookError, match="resultado cambio"):
        reader.read(candidate)
    assert page.locator("[aria-selected=true]").count() == 0


def test_grouped_conversation_stops(page):
    reader = OutlookSourceReader(page, URL, [CANDIDATE.sender])
    page.locator("[data-convid]").evaluate("el=>el.setAttribute('aria-label','12 mensajes')")
    with pytest.raises(OutlookError, match="agrupada"):
        reader.candidate()


def test_grouping_change_after_selection_stops_before_opening(page):
    reader = OutlookSourceReader(page, URL, [CANDIDATE.sender])
    candidate = reader.candidate()
    page.locator("[data-convid]").evaluate("el=>el.setAttribute('aria-expanded','false')")
    with pytest.raises(OutlookError, match="agrupada"):
        reader.read(candidate)
    assert page.locator("#context").is_hidden()


def test_ambiguous_sender_added_after_selection_stops_before_opening(page):
    reader = OutlookSourceReader(page, URL, [CANDIDATE.sender])
    candidate = reader.candidate()
    page.locator("[data-convid]").evaluate(
        """el=>{
          const sender=document.createElement('span');sender.title='other@example.test';
          el.append(sender);
        }"""
    )
    with pytest.raises(OutlookError, match="resultado cambio"):
        reader.read(candidate)
    assert page.locator("#context").is_hidden()


def test_duplicate_row_id_stops_before_opening(page):
    reader = OutlookSourceReader(page, URL, [CANDIDATE.sender])
    candidate = reader.candidate()
    page.locator("[data-convid]").evaluate("el=>el.parentElement.append(el.cloneNode(true))")
    with pytest.raises(OutlookError, match="ambiguo"):
        reader.read(candidate)


def test_source_reader_stops_on_mfa(page):
    page.locator("body").evaluate(
        "el=>{const i=document.createElement('input');i.name='otc';el.append(i)}",
    )
    with pytest.raises(OutlookError, match="MFA"):
        OutlookSourceReader(page, URL, [CANDIDATE.sender]).candidate()


@pytest.mark.parametrize("view_delay", [0, 100])
def test_source_reader_waits_for_asynchronous_selection_and_source_content(page, view_delay):
    page.locator("[data-convid]").evaluate(
        """(el, delay) => el.onclick = event => {
          event.preventDefault();
          setTimeout(() => {
            el.setAttribute('aria-selected','true');
            setTimeout(openMessage, delay);
          }, 50);
        }""", view_delay,
    )
    page.evaluate(
        """() => {
          const original = window.openSource;
          window.openSource = () => {
            original();
            const node = document.querySelector('.allowTextSelection');
            const text = node.textContent;node.textContent='';
            setTimeout(() => node.textContent=text,50);
          };
        }"""
    )
    reader = OutlookSourceReader(page, URL, [CANDIDATE.sender])
    assert reader.read(reader.candidate()).body.strip() == "Compraste en Tienda $ 1.234,56."


@pytest.fixture
def pilot(page, tmp_path, monkeypatch):
    store = Store(tmp_path / "pilot.sqlite3")
    store.configure({
        "location": "Prueba", "timezone": "America/Bogota", "currency": "COP",
        "accounts": ["Tarjeta A"], "excluded_accounts": [],
        "transfer_label": "Familiar", "ask_savings": True, "ask_people": True,
        "approved_senders": [CANDIDATE.sender], "categories": CATEGORIES,
        "confidential_confirmed": True,
    })

    @contextmanager
    def fake_browser(data_dir, settings):
        yield page

    monkeypatch.setattr(app, "browser", fake_browser)
    settings = {
        "outlook_url": URL, "ollama_url": "http://127.0.0.1:11436",
        "ollama_model": "local-test", "selectors_verified": False,
    }
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
    assert settings["selectors_verified"] is False


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
    monkeypatch.setattr(app, "browser", lambda *args: pytest.fail("No debe abrirse el correo."))
    with pytest.raises(app.LocalModelError, match="no esta instalado"):
        app.run_pilot(store, tmp_path, settings, False)


def test_playwright_error_does_not_log_financial_aria_labels(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["monthly-close", "pilot"])

    def broken(args):
        raise PlaywrightError("aria-label=private-financial-canary")

    monkeypatch.setattr(app, "run", broken)
    with pytest.raises(SystemExit) as exc:
        app.main()
    assert exc.value.code == 1
    assert "private-financial-canary" not in capsys.readouterr().err
