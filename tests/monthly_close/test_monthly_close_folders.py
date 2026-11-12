from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from datetime import date

import pytest
import httpx
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

from mail_invoices.local_model import LocalModelError
from monthly_close import main as app
from monthly_close.domain import CATEGORIES, Preferences
from monthly_close.extraction import Mail
from monthly_close.outlook import OutlookError
from monthly_close.source_reader import FOLDERS, ROWS, OutlookSourceReader
from monthly_close.store import Store

URL = "https://outlook.live.com/mail/0/"
SENDER = "avisos@example.test"
DATA = {
    "bandeja de entrada": [
        {"id": "in-0", "sender": SENDER},
        {"id": "in-1", "sender": "other@example.test"},
        {"id": "in-2", "sender": SENDER},
        {"id": "in-3", "sender": "other@example.test"},
    ],
    "correo no deseado": [],
    "elementos eliminados": [{"id": "del-0", "sender": SENDER}],
    "dinero": [],
}


def folder_html() -> str:
    return """<!doctype html><html><meta charset="utf-8"><body>
<input id="topSearchInput" role="combobox">
<div data-app-section="NavigationPane">
 <div role="group">
  <div><div draggable="true"><div role="treeitem" data-folder-name="bandeja de entrada"
    aria-level="2" title="Favorito" onclick="throw new Error('No usar favorito')">Favorito</div></div></div>
 </div>
 <div id="mailbox" role="group"></div>
</div>
<div data-app-section="MessageList"></div>
<script>
window.data = """ + json.dumps(DATA) + """;
const names = """ + json.dumps(dict(FOLDERS) | {"dinero": "Dinero"}) + """;
window.currentFolder = null;
window.pageIndex = 0;
const pane = document.querySelector('[data-app-section="MessageList"]');
for (const [key,label] of Object.entries(names)) {
  const wrapper = document.createElement('div');wrapper.draggable=true;
  const folder=document.createElement('div');
  folder.setAttribute('role','treeitem');folder.setAttribute('data-folder-name',key);
  folder.setAttribute('data-test-folder',key);
  folder.setAttribute('aria-level','2');folder.setAttribute('aria-selected','false');
  folder.title=label+' : Elementos '+data[key].length+' ';
  folder.textContent=label;folder.onclick=()=>openFolder(key);
  wrapper.append(folder);document.querySelector('#mailbox').append(wrapper);
}
window.addSubfolder = (parentId,id,label,items=[]) => {
  const parent=document.querySelector('[data-test-folder="'+parentId+'"]');
  let container=parent.parentElement.nextElementSibling;
  if(parent.getAttribute('aria-expanded')===null){
    container=document.createElement('div');container.hidden=true;
    container.append(document.createElement('div'));parent.parentElement.after(container);
    parent.setAttribute('aria-expanded','false');
    const button=document.createElement('button');button.textContent='Expandir';
    button.onclick=event=>{
      event.stopPropagation();
      const expanded=parent.getAttribute('aria-expanded')==='true';
      parent.setAttribute('aria-expanded',String(!expanded));container.hidden=expanded;
    };
    parent.append(button);
  }
  data[id]=items;names[id]=label;
  const wrapper=document.createElement('div');wrapper.draggable=true;
  const child=document.createElement('div');child.setAttribute('role','treeitem');
  child.setAttribute('data-folder-name',label.toLowerCase());child.setAttribute('data-test-folder',id);
  child.setAttribute('aria-level',String(Number(parent.getAttribute('aria-level'))+1));
  child.setAttribute('aria-selected','false');child.title=label+' : Elementos '+items.length+' ';
  child.textContent=label;child.onclick=()=>openFolder(id);
  wrapper.append(child);container.firstElementChild.append(wrapper);
};
window.renderRows = () => {
  const scroller = pane.querySelector('[data-testid="virtuoso-scroller"]');
  const list=scroller.querySelector('[role="listbox"]');list.replaceChildren();
  list.style.paddingTop=(pageIndex*40)+'px';
  for (let index=pageIndex*2; index<Math.min(data[currentFolder].length,(pageIndex+1)*2); index++) {
    const item=data[currentFolder][index];const wrapper=document.createElement('div');
    wrapper.setAttribute('data-item-index',String(index));
    const row=document.createElement('div');row.setAttribute('role','option');
    row.id=item.id;
    row.style.height='20px';
    row.setAttribute('data-convid',item.id);row.setAttribute('aria-selected','false');
    row.setAttribute('aria-posinset','0');row.setAttribute('aria-setsize','0');
    const sender=document.createElement('span');sender.title=item.sender;sender.textContent='Remitente';
    const subject=document.createElement('span');subject.className='TtcXM';subject.textContent='Compra Tienda';
    row.append(sender,subject);wrapper.append(row);list.append(wrapper);
  }
};
window.openFolder = key => {
  currentFolder=key;pageIndex=0;
  for(const e of document.querySelectorAll('#mailbox [role="treeitem"]')) {
    e.setAttribute('aria-selected',String(e.getAttribute('data-test-folder')===key));
  }
  pane.replaceChildren();
  const heading=document.createElement('span');heading.setAttribute('aria-label',names[key]);
  heading.textContent=names[key];pane.append(heading);
  const sort=document.createElement('button');
  sort.setAttribute('aria-label','Ordenado: Más recientes en la parte superior');
  pane.append(sort);
  if (!data[key].length) {
    const empty=document.createElement('span');empty.className='ksePc';
    empty.textContent=['bandeja de entrada','correo no deseado','elementos eliminados'].includes(key)
      ? 'Nada en '+names[key] : 'Nada en la carpeta';
    pane.append(empty);return;
  }
  const scroller=document.createElement('div');scroller.setAttribute('data-testid','virtuoso-scroller');
  scroller.style='height:80px;overflow:auto';
  const list=document.createElement('div');list.setAttribute('role','listbox');scroller.append(list);
  const spacer=document.createElement('div');spacer.style.height='800px';scroller.append(spacer);
  pane.append(scroller);renderRows();
  scroller.onscroll=()=>{
    const index=Math.min(Math.floor(scroller.scrollTop/40),Math.ceil(data[key].length/2)-1);
    if(index>pageIndex){pageIndex=index;renderRows();}
  };
};
openFolder('bandeja de entrada');
</script></body></html>"""


@pytest.fixture
def page():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        context.route("**/*", lambda route: route.fulfill(body=folder_html(), content_type="text/html"))
        page = context.new_page()
        page.set_default_timeout(500)
        page.goto(URL)
        try:
            yield page
        finally:
            browser.close()


def test_walk_visits_all_three_folders_and_only_authorized_candidates(page):
    reader = OutlookSourceReader(page, URL, [SENDER])
    visited = []
    reports = reader.walk_folders(5, lambda candidate: visited.append(candidate))
    assert [row["folder"] for row in reports] == [label for _, label in FOLDERS] + ["Dinero"]
    assert [row["rows"] for row in reports] == [4, 0, 1, 0]
    assert [row["authorized"] for row in reports] == [2, 0, 1, 0]
    assert [candidate.item_id for candidate in visited] == ["in-0", "in-2", "del-0"]
    assert all(candidate.sender == SENDER for candidate in visited)
    assert page.get_by_role("dialog").count() == 0


def test_scan_allows_read_only_preflight_without_enabling_or_persisting(page, tmp_path, monkeypatch):
    store = Store(tmp_path / "test.sqlite3")
    prefs = Preferences(
        "Prueba", "America/Bogota", "COP", ["Tarjeta A"], [], "Familiar",
        True, True, [SENDER], CATEGORIES, True,
    )

    @contextmanager
    def fake_browser(data_dir, settings):
        yield page

    monkeypatch.setattr(app, "browser", fake_browser)
    monkeypatch.setattr(app, "OllamaJsonModel", lambda *args: pytest.fail("No debe usarse Ollama."))
    monkeypatch.setattr(
        app.OutlookSourceReader, "read", lambda *args: pytest.fail("No debe abrirse el origen."),
    )
    settings = {
        "outlook_url": URL, "ollama_url": "http://127.0.0.1:11434",
        "ollama_model": "local-test", "max_messages": 5,
        "folder_scan_verified": False,
    }
    app.scan(
        store, tmp_path, settings, prefs, date(2026, 2, 1), date(2026, 3, 1), read_only=True,
    )
    assert store.state()["transactions"] == []
    assert store.state()["coverage"] == []
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM processed").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM audit").fetchone()[0] == 0
    assert settings["folder_scan_verified"] is False


def test_source_dialog_reading_works_during_virtual_traversal(page):
    raw = (
        "Received: from sender.test by outlook.test; Sun, 01 Mar 2026 02:15:00 +0000\n"
        "From: Avisos <avisos@example.test>\n"
        "Date: Sat, 28 Feb 2026 21:14:00 -0500\n"
        "Message-ID: <transaction-1@example.test>\n"
        "Subject: Compra Tienda\n"
        "Content-Type: text/plain; charset=utf-8\n\nCompraste en Tienda."
    )
    page.evaluate(
        """raw => {
          const menu=document.createElement('div');menu.id='context';menu.hidden=true;
          menu.setAttribute('role','menu');
          const view=document.createElement('div');view.setAttribute('role','menuitem');
          view.textContent='Ver';menu.append(view);
          const source=document.createElement('button');source.setAttribute('role','menuitem');
          source.textContent='Ver origen del mensaje';source.hidden=true;menu.append(source);
          view.onmouseover=()=>source.hidden=false;
          view.tabIndex=0;
          view.onkeydown=event=>{if(event.key==='ArrowRight')source.hidden=false;};
          source.onclick=()=>{
            menu.hidden=true;
            const dialog=document.createElement('div');dialog.setAttribute('role','dialog');
            const content=document.createElement('div');
            content.className='fui-DialogContent allowTextSelection';content.textContent=raw;
            const close=document.createElement('button');close.textContent='Cerrar';
            close.onclick=()=>dialog.remove();dialog.append(content,close);document.body.append(dialog);
          };
          document.body.append(menu);
          const actions=document.createElement('button');
          actions.setAttribute('aria-label','More items');document.body.append(actions);
          document.addEventListener('keydown',event=>{
            const row=event.target.closest('[data-convid]');
            if(row && event.shiftKey && event.key==='F10'){
              event.preventDefault();
              menu.hidden=false;
            }
          });
          document.addEventListener('click',event=>{
            const row=event.target.closest('[data-convid]');
            if(!row)return;
            row.tabIndex=0;
            for(const e of document.querySelectorAll('[data-convid]')){
              e.setAttribute('aria-selected',String(e===row));
            }
            const close=document.createElement('button');close.textContent='Cerrar';
            close.onclick=()=>close.remove();document.body.append(close);
          });
          document.addEventListener('contextmenu',event=>{
            const row=event.target.closest('[data-convid]');
            if(!row)return;
            event.preventDefault();
            for(const e of document.querySelectorAll('[data-convid]')){
              e.setAttribute('aria-selected',String(e===row));
            }
            menu.hidden=false;
          });
        }""", raw,
    )
    reader = OutlookSourceReader(page, URL, [SENDER])
    messages = []
    reports = reader.walk_folders(5, lambda candidate: messages.append(reader.read(candidate)))
    assert len(messages) == 3
    assert len({mail.id for mail in messages}) == 1
    assert [row["rows"] for row in reports] == [4, 0, 1, 0]
    assert all(mail.body.strip() == "Compraste en Tienda." for mail in messages)
    assert page.get_by_role("dialog").count() == 0


def test_normal_scan_requires_its_own_gate_before_starting_browser(tmp_path, monkeypatch):
    store = Store(tmp_path / "test.sqlite3")
    prefs = Preferences(
        "Prueba", "America/Bogota", "COP", ["Tarjeta A"], [], "Familiar",
        True, True, [SENDER], CATEGORIES, True,
    )
    monkeypatch.setattr(app, "browser", lambda *args: pytest.fail("No debe abrirse Outlook."))
    with pytest.raises(ValueError, match="Recorrido de carpetas"):
        app.scan(store, tmp_path, {}, prefs, date(2026, 2, 1), date(2026, 3, 1))


def test_scan_checks_ollama_before_opening_outlook(tmp_path, monkeypatch):
    store = Store(tmp_path / "test.sqlite3")
    prefs = Preferences(
        "Prueba", "America/Bogota", "COP", ["Tarjeta A"], [], "Familiar",
        True, True, [SENDER], CATEGORIES, True,
    )
    monkeypatch.setattr(app, "browser", lambda *args: pytest.fail("No debe abrirse Outlook."))
    original_client = httpx.Client
    monkeypatch.setattr(app.httpx, "Client", lambda **kwargs: original_client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"models": []})),
    ))
    settings = {
        "folder_scan_verified": True, "ollama_url": "http://127.0.0.1:11434",
        "ollama_model": "local-test",
    }
    with pytest.raises(LocalModelError, match="no esta instalado"):
        app.scan(store, tmp_path, settings, prefs, date(2026, 2, 1), date(2026, 3, 1))
    assert store.state()["coverage"] == []


def test_removed_discover_command_is_rejected(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["monthly_close", "discover"])
    with pytest.raises(SystemExit) as exc:
        app.main()
    assert exc.value.code == 2


def test_date_cutoff_stops_only_current_folder_and_requires_newest_first(page):
    reader = OutlookSourceReader(page, URL, [SENDER])
    visited = []

    def visit(candidate):
        visited.append(candidate.item_id)
        return False

    reports = reader.walk_folders(5, visit)
    assert visited == ["in-0", "del-0"]
    assert [report["rows"] for report in reports] == [1, 0, 1, 0]
    assert reports[0]["stopped_before_start"] is True
    def wrong_order(candidate):
        page.get_by_role("button", name="Ordenado: Más recientes en la parte superior").evaluate(
            "el => el.setAttribute('aria-label','Ordenado: Más antiguos en la parte superior')",
        )
        return False

    with pytest.raises(OutlookError, match="orden de fecha descendente"):
        reader.walk_folders(5, wrong_order)


def test_date_cutoff_does_not_hide_unvisited_positions(page):
    reader = OutlookSourceReader(page, URL, [SENDER])
    page.evaluate(
        """() => {
          document.querySelector('[data-item-index="0"] span[title]').title = 'other@example.test';
          const next = document.querySelector('[data-item-index="1"]');
          next.setAttribute('data-item-index','2');
          next.querySelector('span[title]').title = 'avisos@example.test';
        }"""
    )
    with pytest.raises(OutlookError, match="posiciones sin verificar"):
        reader.walk_folders(5, lambda candidate: False)


@pytest.mark.parametrize("processed_boundary", [False, True])
def test_scan_skips_newer_mail_and_stops_before_start_in_each_folder(
    page, tmp_path, monkeypatch, processed_boundary,
):
    store = Store(tmp_path / "date-range.sqlite3")
    prefs = Preferences(
        "Prueba", "America/Bogota", "COP", ["Tarjeta A"], [], "Familiar",
        True, True, [SENDER], CATEGORIES, True,
    )
    store.configure(prefs.to_dict())
    if processed_boundary:
        store.import_message("older", [])
    page.evaluate(
        """() => {
          data['bandeja de entrada'] = ['newer','end','inside','start','older','never']
            .map(id => ({id,sender:'avisos@example.test'}));
          for (const folder of document.querySelectorAll('#mailbox [data-folder-name]')) {
            const key = folder.getAttribute('data-folder-name');
            folder.title = names[key] + ' : Elementos ' + data[key].length + ' ';
          }
          openFolder('bandeja de entrada');
          const list = pane.querySelector('[role="listbox"]');
          const template = list.firstElementChild.cloneNode(true);
          list.replaceChildren();
          data['bandeja de entrada'].forEach((item, index) => {
            const wrapper = template.cloneNode(true);
            wrapper.setAttribute('data-item-index', String(index));
            const row = wrapper.querySelector('[data-convid]');
            row.id = item.id; row.setAttribute('data-convid', item.id);
            list.append(wrapper);
          });
        }"""
    )
    received = {
        "newer": "2026-03-02T12:00:00Z", "end": "2026-03-01T05:00:00Z",
        "inside": "2026-03-01T04:59:00Z", "start": "2026-02-01T05:00:00Z",
        "older": "2026-02-01T04:59:00Z", "del-0": "2026-02-15T12:00:00Z",
    }
    reads = []
    model_calls = []

    @contextmanager
    def browser(*args):
        yield page

    def read(reader, candidate):
        reader.guard()
        reads.append(candidate.item_id)
        return Mail(candidate.item_id, SENDER, "Tienda", received[candidate.item_id], "Tienda COP 10,05")

    class Model:
        def __init__(self, *args):
            pass

        def complete_json(self, system, user):
            model_calls.append(user)
            return {"events": [{
                "merchant": "Tienda", "account": "Tarjeta A", "amount_text": "COP 10,05",
                "currency": "COP", "date_text": "", "category": "Compras",
                "kind": "purchase", "savings": False,
            }]}

    monkeypatch.setattr(app, "browser", browser)
    monkeypatch.setattr(app.OutlookSourceReader, "read", read)
    monkeypatch.setattr(app, "OllamaJsonModel", Model)
    monkeypatch.setattr(app, "require_model", lambda settings: None)
    app.scan(
        store, tmp_path, {
            "outlook_url": URL, "ollama_url": "http://127.0.0.1:11434",
            "ollama_model": "local-test", "max_messages": 7, "folder_scan_verified": True,
        }, prefs, date(2026, 2, 1), date(2026, 3, 1),
    )
    assert reads == ["newer", "end", "inside", "start", "older", "del-0"]
    assert len(model_calls) == 3
    assert len(store.state()["transactions"]) == 3
    assert not store.seen("newer")
    assert not store.seen("end")
    assert store.seen("older") is processed_boundary


def test_real_scan_covers_spam_deleted_filters_dates_and_preserves_choices(page, tmp_path, monkeypatch):
    store = Store(tmp_path / "test.sqlite3")
    prefs = Preferences(
        "Prueba", "America/Bogota", "COP", ["Tarjeta A"], [], "Familiar",
        True, True, [SENDER], CATEGORIES, True,
    )
    store.configure(prefs.to_dict())
    # Include an authorized notice in Spam; only bodies of authorized senders can be read.
    page.evaluate(
        """() => {
          data['correo no deseado']=[{id:'spam-0',sender:'avisos@example.test'}];
          document.querySelector('#mailbox [data-folder-name="correo no deseado"]').title=
            'Correo no deseado : Elementos 1 ';
        }"""
    )
    reads = []
    calls = []

    @contextmanager
    def fake_browser(data_dir, settings):
        yield page

    def read(reader, candidate):
        assert candidate.sender == SENDER
        reader.guard()
        reads.append(candidate.item_id)
        # A duplicate in Deleted Items shares the RFC Message-ID of an inbox notice.
        identity = "in-0" if candidate.item_id == "del-0" else candidate.item_id
        received = "2026-03-01T04:30:00Z" if identity == "in-0" else "2026-03-01T06:00:00Z"
        return Mail(identity, SENDER, "Compra Tienda", received, "Tienda COP 10,05.")

    class Model:
        def __init__(self, *args):
            pass

        def complete_json(self, system, user):
            calls.append(user)
            return {"events": [{
                "merchant": "Tienda", "account": "Tarjeta A", "amount_text": "COP 10,05",
                "currency": "COP", "date_text": "", "category": "Compras",
                "kind": "purchase", "savings": False,
            }]}

    monkeypatch.setattr(app, "browser", fake_browser)
    monkeypatch.setattr(app.OutlookSourceReader, "read", read)
    monkeypatch.setattr(app, "OllamaJsonModel", Model)
    monkeypatch.setattr(app, "require_model", lambda settings: None)
    settings = {
        "outlook_url": URL, "ollama_url": "http://127.0.0.1:11434",
        "ollama_model": "local-test", "max_messages": 6,
        "folder_scan_verified": True,
    }
    app.scan(store, tmp_path, settings, prefs, date(2026, 2, 1), date(2026, 3, 1))
    assert reads == ["in-0", "in-2", "spam-0", "del-0"]
    assert len(calls) == 1
    state = store.state()
    assert len(state["transactions"]) == 1
    row = state["transactions"][0]
    assert row["day"] == "2026-02-28"
    assert row["amount_minor"] == 1005
    assert row["decision"] is None
    assert state["coverage"][0]["status"] == "partial"
    store.decide(row["id"], "family")
    app.scan(store, tmp_path, settings, prefs, date(2026, 2, 1), date(2026, 3, 1))
    assert len(calls) == 1
    assert len(store.state()["transactions"]) == 1
    assert store.state()["transactions"][0]["decision"] == "family"


def test_limit_is_global_and_never_skips_deleted_silently(page):
    visited = []
    with pytest.raises(OutlookError, match="Limite total"):
        OutlookSourceReader(page, URL, [SENDER]).walk_folders(
            4, lambda candidate: visited.append(candidate.item_id),
        )
    assert visited == ["in-0", "in-2"]


def test_missing_required_folder_stops(page):
    page.locator('#mailbox [data-folder-name="correo no deseado"]').evaluate("el=>el.remove()")
    with pytest.raises(OutlookError, match="Carpeta ausente"):
        OutlookSourceReader(page, URL, [SENDER]).walk_folders(10, lambda candidate: None)


def test_duplicate_mailbox_control_stops_not_first_or_last_fallback(page):
    page.locator('#mailbox [data-folder-name="bandeja de entrada"]').evaluate(
        "el=>el.parentElement.append(el.cloneNode(true))",
    )
    with pytest.raises(OutlookError, match="ambigua"):
        OutlookSourceReader(page, URL, [SENDER]).walk_folders(10, lambda candidate: None)


def test_three_folders_from_different_mailboxes_are_rejected(page):
    page.locator('#mailbox [data-folder-name="elementos eliminados"]').evaluate(
        """el=>{
          const group=document.createElement('div');group.setAttribute('role','group');
          document.querySelector('[data-app-section="NavigationPane"]').append(group);
          group.append(el.parentElement);
        }""",
    )
    with pytest.raises(OutlookError, match="mismo buzon"):
        OutlookSourceReader(page, URL, [SENDER]).walk_folders(10, lambda candidate: None)


def test_unknown_folder_total_stops(page):
    page.locator('#mailbox [data-folder-name="bandeja de entrada"]').evaluate(
        "el=>el.title='No hay cantidad comprobable'",
    )
    with pytest.raises(OutlookError, match="cantidad total"):
        OutlookSourceReader(page, URL, [SENDER]).walk_folders(10, lambda candidate: None)


def test_no_false_success_when_empty_spam_has_no_explicit_marker(page):
    page.evaluate(
        """() => {
          const original=openFolder;
          window.openFolder=key=>{
            original(key);
            if(key==='correo no deseado') document.querySelector('span.ksePc').remove();
          };
        }"""
    )
    with pytest.raises(PlaywrightTimeout):
        OutlookSourceReader(page, URL, [SENDER]).walk_folders(10, lambda candidate: None)


def test_changed_folder_count_stops_during_visit(page):
    reader = OutlookSourceReader(page, URL, [SENDER])

    def change(candidate):
        page.locator('#mailbox [data-folder-name="bandeja de entrada"]').evaluate(
            "el=>el.title='Bandeja de entrada : Elementos 5 '",
        )

    with pytest.raises(OutlookError, match="cantidad de mensajes cambio"):
        reader.walk_folders(10, change)


def test_changed_active_folder_stops_before_reading_another_message(page):
    reader = OutlookSourceReader(page, URL, [SENDER])

    def change(candidate):
        page.evaluate("openFolder('elementos eliminados')")

    with pytest.raises(OutlookError, match="carpeta activa cambio"):
        reader.walk_folders(10, change)


def test_focused_inbox_tabs_stop_instead_of_counting_only_one_tab(page):
    page.locator('[data-app-section="MessageList"]').evaluate(
        """el=>{
          const tab=document.createElement('button');tab.setAttribute('role','tab');
          tab.textContent='Prioritarios';el.append(tab);
        }"""
    )
    with pytest.raises(OutlookError, match="pestanas"):
        OutlookSourceReader(page, URL, [SENDER]).walk_folders(10, lambda candidate: None)


def test_authorized_grouped_conversation_stops(page):
    page.locator(ROWS).first.evaluate("el=>el.setAttribute('aria-expanded','false')")
    with pytest.raises(OutlookError, match="agrupada"):
        OutlookSourceReader(page, URL, [SENDER]).walk_folders(10, lambda candidate: None)


def test_missing_virtual_index_stops(page):
    page.locator("[data-item-index='1']").evaluate("el=>el.removeAttribute('data-item-index')")
    with pytest.raises(OutlookError, match="Faltan posiciones"):
        OutlookSourceReader(page, URL, [SENDER]).walk_folders(10, lambda candidate: None)


def test_duplicate_message_rows_at_distinct_indexes_stop(page):
    page.locator(ROWS).nth(1).evaluate("el=>el.id='in-0'")
    with pytest.raises(OutlookError, match="repetido|ambiguo"):
        OutlookSourceReader(page, URL, [SENDER]).walk_folders(10, lambda candidate: None)


def test_pagination_stall_never_claims_full_coverage(page):
    page.locator('[data-testid="virtuoso-scroller"]').evaluate("el=>el.onscroll=null")
    with pytest.raises(PlaywrightTimeout):
        OutlookSourceReader(page, URL, [SENDER]).walk_folders(10, lambda candidate: None)


def test_changed_virtual_row_order_stops(page):
    page.evaluate(
        """() => {
          const original=renderRows;
          window.renderRows=()=>{
            original();
            if(pageIndex>0) document.querySelector('[data-item-index]').setAttribute('data-item-index','0');
          };
        }""",
    )
    with pytest.raises(OutlookError, match="cambio de orden"):
        OutlookSourceReader(page, URL, [SENDER]).walk_folders(10, lambda candidate: None)


def test_dinero_expands_all_depths_and_does_not_visit_other_trees(page):
    page.evaluate(
        """() => {
          addSubfolder('dinero','bank','Banco',[{id:'bank-0',sender:'avisos@example.test'}]);
          addSubfolder('bank','card','Tarjeta',[{id:'card-0',sender:'other@example.test'}]);
          addSubfolder('card','old','Historico',[{id:'old-0',sender:'avisos@example.test'}]);
          addSubfolder('dinero','wallet','Billetera');
          addSubfolder('wallet','other-old','Historico');
          addSubfolder('bandeja de entrada','outside','No autorizado',
            [{id:'outside-0',sender:'avisos@example.test'}]);
        }"""
    )
    visited = []
    reports = OutlookSourceReader(page, URL, [SENDER]).walk_folders(
        8, lambda candidate: visited.append(candidate.item_id),
    )
    assert [row["folder"] for row in reports] == [
        "Bandeja de entrada", "Correo no deseado", "Elementos eliminados", "Dinero",
        "Dinero / Banco", "Dinero / Banco / Tarjeta",
        "Dinero / Banco / Tarjeta / Historico", "Dinero / Billetera",
        "Dinero / Billetera / Historico",
    ]
    assert visited == ["in-0", "in-2", "del-0", "bank-0", "old-0"]
    assert sum(row["rows"] for row in reports) == 8
    assert page.locator('[data-test-folder="bandeja de entrada"]').get_attribute(
        "aria-expanded",
    ) == "false"
    assert page.locator('[data-test-folder="bank"]').get_attribute("aria-expanded") == "true"
    assert page.locator('[data-test-folder="wallet"]').get_attribute("aria-expanded") == "true"


def test_missing_dinero_stops_before_reading_any_rows(page):
    page.locator('[data-test-folder="dinero"]').evaluate("el=>el.remove()")
    with pytest.raises(OutlookError, match="ausente"):
        OutlookSourceReader(page, URL, [SENDER]).walk_folders(
            10, lambda candidate: pytest.fail("No debe procesar un recorrido incompleto."),
        )


def test_ambiguous_sibling_names_stop_before_reading_rows(page):
    page.evaluate(
        """() => {
          addSubfolder('dinero','first','Banco');
          addSubfolder('dinero','second','Banco');
        }"""
    )
    with pytest.raises(OutlookError, match="jerarquia"):
        OutlookSourceReader(page, URL, [SENDER]).walk_folders(
            10, lambda candidate: pytest.fail("No debe elegir un duplicado arbitrario."),
        )


def test_child_with_inconsistent_depth_is_rejected(page):
    page.evaluate(
        """() => {
          addSubfolder('dinero','bank','Banco');
          document.querySelector('[data-test-folder="bank"]').setAttribute('aria-level','4');
        }"""
    )
    with pytest.raises(OutlookError, match="jerarquia"):
        OutlookSourceReader(page, URL, [SENDER]).folders()


def test_expanded_parent_without_children_stops_not_silently_leaf(page):
    page.locator('[data-test-folder="dinero"]').evaluate(
        "el=>el.setAttribute('aria-expanded','true')",
    )
    with pytest.raises(OutlookError, match="contenedor"):
        OutlookSourceReader(page, URL, [SENDER]).folders()


def test_new_subfolder_during_scan_invalidates_completion(page):
    reader = OutlookSourceReader(page, URL, [SENDER])

    def visit(candidate):
        if candidate.item_id == "in-0":
            page.evaluate("addSubfolder('dinero','new','Nueva')")

    with pytest.raises(OutlookError, match="arbol"):
        reader.walk_folders(10, visit)


def test_global_limit_includes_dinero_descendants(page):
    page.evaluate(
        "addSubfolder('dinero','bank','Banco',[{id:'bank-0',sender:'avisos@example.test'}])",
    )
    with pytest.raises(OutlookError, match="Limite total"):
        OutlookSourceReader(page, URL, [SENDER]).walk_folders(5, lambda candidate: None)


@pytest.mark.parametrize("suffix,expected", [
    ("6.603 (2.507 no le\u00eddos)", 6603),
    ("123 (17 no le\u00eddos)", 123), ("0 ", 0),
])
def test_localized_folder_totals(page, suffix, expected):
    control = page.locator('[data-test-folder="dinero"]')
    control.evaluate("(el,suffix)=>el.title='Dinero : Elementos '+suffix", suffix)
    assert OutlookSourceReader._folder_count(control) == expected


def test_malformed_thousands_separator_is_rejected(page):
    control = page.locator('[data-test-folder="dinero"]')
    control.evaluate("el=>el.title='Dinero : Elementos 6.60 '")
    with pytest.raises(OutlookError, match="cantidad total"):
        OutlookSourceReader._folder_count(control)


def test_distinct_messages_can_share_conversation_id(page):
    page.locator(ROWS).nth(1).evaluate("el=>el.setAttribute('data-convid','in-0')")
    reports = OutlookSourceReader(page, URL, [SENDER]).walk_folders(5, lambda candidate: None)
    assert sum(row["rows"] for row in reports) == 5


def test_missing_dom_message_identity_stops(page):
    page.locator(ROWS).nth(1).evaluate("el=>el.removeAttribute('id')")
    with pytest.raises(OutlookError, match="Faltan posiciones"):
        OutlookSourceReader(page, URL, [SENDER]).walk_folders(5, lambda candidate: None)


@pytest.mark.parametrize("limit,valid", [(10000, True), (10001, False), (0, False), (True, False)])
def test_runtime_bounds_for_large_folder_trees(tmp_path, limit, valid):
    settings = {
        "ollama_url": "http://127.0.0.1:11434", "ollama_model": "local-test",
        "browser_executable": sys.executable, "outlook_url": URL,
        "max_messages": limit,
    }
    (tmp_path / "runtime.json").write_text(json.dumps(settings), encoding="utf-8")
    if valid:
        assert app.runtime(tmp_path)["max_messages"] == limit
    else:
        with pytest.raises(ValueError, match="max_messages"):
            app.runtime(tmp_path)


@pytest.mark.parametrize(("port", "valid"), [(11434, True), (11435, False), (11436, False)])
def test_runtime_requires_shared_invoice_ollama_endpoint(tmp_path, port, valid):
    settings = {
        "ollama_url": f"http://127.0.0.1:{port}", "ollama_model": "local-test",
        "browser_executable": sys.executable, "outlook_url": URL,
        "max_messages": 10,
    }
    (tmp_path / "runtime.json").write_text(json.dumps(settings), encoding="utf-8")

    if valid:
        assert app.runtime(tmp_path)["ollama_url"] == "http://127.0.0.1:11434"
    else:
        with pytest.raises(ValueError, match="11434"):
            app.runtime(tmp_path)


def test_custom_empty_marker_cannot_hide_a_nonempty_subfolder(page):
    page.evaluate(
        """() => {
          addSubfolder('dinero','bank','Banco',[{id:'bank-0',sender:'avisos@example.test'}]);
          const original=openFolder;
          window.openFolder=key=>{
            original(key);
            if(key==='bank'){
              pane.querySelector('[data-testid="virtuoso-scroller"]').remove();
              const empty=document.createElement('span');empty.className='ksePc';
              empty.textContent='Nada en la carpeta';pane.append(empty);
            }
          };
        }"""
    )
    with pytest.raises(OutlookError, match="Lista vacia"):
        OutlookSourceReader(page, URL, [SENDER]).walk_folders(10, lambda candidate: None)


def test_virtual_row_disappearing_mid_snapshot_never_claims_full_coverage(page):
    def visit(candidate):
        if candidate.item_id == "in-0":
            page.locator("[data-item-index='1']").evaluate("el=>el.remove()")

    with pytest.raises(PlaywrightTimeout):
        OutlookSourceReader(page, URL, [SENDER]).walk_folders(10, visit)
