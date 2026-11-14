from __future__ import annotations

import hashlib
import json
import logging
import re
from email import policy
from email.message import EmailMessage
from email.parser import Parser
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser
from typing import Callable
from urllib.parse import urlparse

from playwright.sync_api import Locator, Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from monthly_close.domain import EMAIL, clean_text
from monthly_close.extraction import Mail
from monthly_close.outlook import Candidate, OutlookError, guard_page, validate_url

ROWS = '[data-app-section="MessageList"] [role="option"][data-convid]'
SEARCH = '#topSearchInput[role="combobox"]'
SUBJECT = "span.TtcXM"
SOURCE = ".fui-DialogContent.allowTextSelection"
MAX_SOURCE_CHARS = 2_000_000
MESSAGE_LIST = '[data-app-section="MessageList"]'
NAV_FOLDERS = '[data-app-section="NavigationPane"] [role="treeitem"][data-folder-name]'
FOLDER_TREE = (
    '[data-app-section="NavigationPane"] [role="group"] > div[draggable] > '
    '[role="treeitem"][data-folder-name]'
)
SCROLLER = MESSAGE_LIST + ' [data-testid="virtuoso-scroller"]'
FOLDERS = (
    ("bandeja de entrada", "Bandeja de entrada"),
    ("correo no deseado", "Correo no deseado"),
    ("elementos eliminados", "Elementos eliminados"),
)


class _BodyText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"head", "script", "style", "noscript", "iframe", "object"}:
            self.hidden.append(tag)
        if not self.hidden and tag in {"br", "p", "div", "tr", "li", "h1", "h2", "td"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if self.hidden and self.hidden[-1] == tag:
            self.hidden.pop()
        if not self.hidden and tag in {"p", "div", "tr", "li", "td"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def parse_source(raw: str, candidate: Candidate, origin: str) -> Mail:
    """Validate RFC headers and decode only the message text, never attachments."""
    if not raw or len(raw) > MAX_SOURCE_CHARS:
        raise OutlookError("Origen ausente o demasiado grande para el piloto.")
    message = Parser(_class=EmailMessage, policy=policy.default).parsestr(raw)
    if message.defects:
        raise OutlookError("Origen MIME defectuoso; requiere revision manual.")
    for name in ("From", "Subject", "Message-ID", "Date"):
        headers = message.get_all(name, [])
        if len(headers) != 1 or getattr(headers[0], "defects", ()):
            raise OutlookError(f"Encabezado {name} ausente, defectuoso o ambiguo.")
    addresses = getaddresses([str(message["From"])])
    if len(addresses) != 1 or addresses[0][1].lower() != candidate.sender:
        raise OutlookError("El remitente del origen no corresponde al resultado autorizado.")
    subject = clean_text(str(message["Subject"]), 300)
    if subject != candidate.subject:
        raise OutlookError("El asunto del origen no corresponde al resultado seleccionado.")
    message_id = str(message["Message-ID"]).strip()
    if not re.fullmatch(r"<[^<>\s]+@[^<>\s]+>", message_id):
        raise OutlookError("Message-ID no valido; no se usa el identificador de conversacion.")
    received = message.get_all("Received", [])
    if not received:
        raise OutlookError("Falta la fecha de recepcion del origen.")
    try:
        sent = parsedate_to_datetime(str(message["Date"]))
        stamp = parsedate_to_datetime(str(received[0]).rsplit(";", 1)[-1].strip())
    except (ValueError, TypeError, IndexError) as exc:
        raise OutlookError("Fecha RFC no reconocida; no se supone UTC.") from exc
    if sent.tzinfo is None or stamp.tzinfo is None:
        raise OutlookError("Las fechas del origen requieren zona horaria explicita.")
    part = message.get_body(preferencelist=("plain", "html"))
    if part is None or part.get_content_disposition() == "attachment":
        raise OutlookError("No hay cuerpo de texto sin adjuntos para procesar.")
    try:
        content = part.get_content(errors="strict")
    except (LookupError, UnicodeError, ValueError) as exc:
        raise OutlookError("No se pudo decodificar el texto del aviso.") from exc
    if not isinstance(content, str) or part.defects:
        raise OutlookError("Cuerpo MIME defectuoso o no textual.")
    if part.get_content_type() == "text/html":
        parser = _BodyText()
        parser.feed(content)
        parser.close()
        content = "".join(parser.parts)
    body = clean_text(content, 40000)
    identifier = hashlib.sha256((origin + "\n" + message_id).encode()).hexdigest()
    return Mail(identifier, candidate.sender, subject, stamp.isoformat(), body)


class OutlookSourceReader:
    """Read authorized messages through Outlook's Spanish source dialog."""

    def __init__(self, page: Page, url: str, approved_senders: list[str]) -> None:
        self.page = page
        self.origin = urlparse(validate_url(url)).netloc
        if not approved_senders or any(not EMAIL.fullmatch(sender) for sender in approved_senders):
            raise ValueError("El piloto requiere remitentes exactos aprobados.")
        self.approved_senders = [sender.lower() for sender in approved_senders]
        self.active_folder: tuple[str, ...] | None = None
        self.folder_total: int | None = None
        self.empty_label: str | None = None

    def guard(self) -> None:
        guard_page(self.page, self.origin)
        if self.active_folder is not None:
            selected = self._folder(self.active_folder)
            if not selected.evaluate(
                """(el, selector) => {
                  const selected = document.querySelectorAll(selector);
                  return selected.length === 1 && selected[0] === el;
                }""", NAV_FOLDERS + '[aria-selected="true"]',
            ):
                raise OutlookError("La carpeta activa cambio durante el recorrido.")
            if self.folder_total is not None and self._folder_count(selected) != self.folder_total:
                raise OutlookError("La cantidad de mensajes cambio durante el recorrido.")

    @staticmethod
    def _folder_count(control: Locator) -> int:
        title = control.get_attribute("title") or ""
        number = r"(?:[1-9]\d{0,2}(?:\.\d{3})+|\d+)"
        count = re.fullmatch(
            rf".+ : Elementos ({number})(?: \({number} no le\u00eddos\))?\s*", title,
        )
        if count is None:
            raise OutlookError("La carpeta no expone una cantidad total verificable.")
        return int(count[1].replace(".", ""))

    def _folder(self, path: tuple[str, ...]) -> Locator:
        controls = self.page.locator(FOLDER_TREE)
        if not controls.evaluate_all(
            """(els, keys) => {
              const folders = els.filter(e => keys.includes(e.getAttribute('data-folder-name')));
              return keys.every(key => folders.filter(
                e => e.getAttribute('data-folder-name') === key).length === 1)
                && new Set(folders.map(e => e.closest('[role="group"]'))).size === 1;
            }""", [folder for folder, _ in FOLDERS] + ["dinero"],
        ):
            raise OutlookError("Carpeta ausente o ambigua; deben pertenecer al mismo buzon.")
        for depth, key in enumerate(path):
            matching = controls.evaluate_all(
                """(els, {key, level}) => els.flatMap((el, index) =>
                  el.getAttribute('data-folder-name') === key
                    && el.getAttribute('aria-level') === level ? [index] : [])""",
                {"key": key, "level": str(depth + 2)},
            )
            if len(matching) != 1 or not controls.nth(matching[0]).is_visible():
                raise OutlookError("Carpeta ausente o ambigua; no se omite del escaneo.")
            target = controls.nth(matching[0])
            if depth < len(path) - 1:
                controls = self._children(target)
        return target

    @staticmethod
    def _children(target: Locator) -> Locator:
        container = target.locator("..").locator("xpath=following-sibling::*[1]")
        if (
            target.get_attribute("aria-expanded") != "true" or container.count() != 1
            or container.get_attribute("draggable") is not None
        ):
            raise OutlookError("Subcarpetas sin contenedor verificable.")
        return container.locator(':scope > div > div[draggable] > [role="treeitem"][data-folder-name]')

    def folders(self) -> list[tuple[tuple[str, ...], str]]:
        """Expand only Dinero and enumerate its descendants by unambiguous full path."""
        self.guard()
        self.page.locator(FOLDER_TREE).first.wait_for(state="visible", timeout=30000)
        result = [((key,), label) for key, label in FOLDERS]
        pending = [(("dinero",), "Dinero")]
        while pending:
            path, label = pending.pop()
            target = self._folder(path)
            result.append((path, label))
            expanded = target.get_attribute("aria-expanded")
            if expanded is None:
                continue
            if expanded == "false":
                button = target.locator("button")
                if button.count() != 1:
                    raise OutlookError("Control para expandir subcarpetas ausente o ambiguo.")
                button.click()
                self.page.wait_for_function(
                    "el => el.isConnected && el.getAttribute('aria-expanded') === 'true'",
                    arg=target.element_handle(),
                )
            elif expanded != "true":
                raise OutlookError("Estado de subcarpetas no reconocido.")
            children = self._children(target)
            children.first.wait_for(state="visible")
            entries = []
            keys: set[str] = set()
            for index in range(children.count()):
                child = children.nth(index)
                key = child.get_attribute("data-folder-name")
                title = child.get_attribute("title") or ""
                self._folder_count(child)
                name = title.rsplit(" : Elementos ", 1)[0]
                if (
                    not key or key in keys or name.lower() != key
                    or child.get_attribute("aria-level") != str(len(path) + 2)
                ):
                    raise OutlookError("Identidad o jerarquia de subcarpetas ambigua.")
                keys.add(key)
                entries.append((path + (key,), label + " / " + name))
            pending.extend(reversed(entries))
        return result

    def open_folder(self, key: str | tuple[str, ...], label: str) -> None:
        """Require a full folder path, never an arbitrary same-named folder."""
        self.guard()
        path = (key,) if isinstance(key, str) else key
        target = self._folder(path)
        key = path[-1]
        heading = (target.get_attribute("title") or "").rsplit(" : Elementos ", 1)[0]
        empty_label = "Nada en la carpeta" if path[0] == "dinero" else "Nada en " + heading
        total = self._folder_count(target)
        if target.get_attribute("aria-selected") != "true":
            old_rows = self.page.locator(ROWS)
            old_first = old_rows.first.element_handle() if old_rows.count() else None
            target.click()
            if old_first is not None:
                self.page.wait_for_function(
                    "row => !row.isConnected", arg=old_first, timeout=15000,
                )
        self.page.wait_for_function(
            """({tree,list,key,label}) => {
              const selected = [...document.querySelectorAll(tree)]
                .filter(e => e.getAttribute('aria-selected') === 'true');
              const panes = document.querySelectorAll(list);
              return selected.length === 1 && selected[0].getAttribute('data-folder-name') === key
                && panes.length === 1 && [...panes[0].querySelectorAll('span[aria-label]')]
                  .some(e => !e.closest('[role="option"]') && e.getAttribute('aria-label') === label);
            }""", arg={"tree": NAV_FOLDERS, "list": MESSAGE_LIST, "key": key, "label": heading},
        )
        self.active_folder = path
        self.folder_total = total
        self.empty_label = empty_label
        self.guard()
        pane = self.page.locator(MESSAGE_LIST)
        if pane.locator('[role="tab"]').count():
            raise OutlookError("Bandeja con pestanas o filtros; requiere vista sin Prioritarios/Otros.")
        self.page.wait_for_function(
            """({list,rows,empty}) => {
              const pane = document.querySelector(list);
              if (!pane || pane.querySelector('[role="progressbar"],[aria-busy="true"]')) return false;
              return document.querySelectorAll(rows).length > 0
                || [...pane.querySelectorAll('span.ksePc')]
                  .some(e => e.textContent.trim() === empty);
            }""", arg={"list": MESSAGE_LIST, "rows": ROWS, "empty": empty_label},
        )
        if self.page.locator(ROWS).count():
            scroller = self.page.locator(SCROLLER)
            if scroller.count() != 1 or not scroller.is_visible():
                raise OutlookError("Contenedor virtual ausente o ambiguo.")
            scroller.evaluate("el => { el.scrollTop = 0; }")
            self.page.wait_for_function(
                """selector => [...document.querySelectorAll(selector)]
                  .some(e => e.closest('[data-item-index]')?.getAttribute('data-item-index') === '0')""",
                arg=ROWS,
            )
        self.guard()

    def _candidate_from_row(self, row: Locator) -> Candidate | None:
        titles = row.locator("span[title]").evaluate_all(
            "els => els.map(el => el.getAttribute('title') || '')"
        )
        emails = [title.strip().lower() for title in titles if EMAIL.fullmatch(title.strip())]
        if not any(sender in self.approved_senders for sender in emails):
            return None
        if len(emails) != 1:
            raise OutlookError("Remitente ambiguo en el resultado.")
        subject = row.locator(SUBJECT)
        if subject.count() != 1:
            raise OutlookError("Asunto ausente o ambiguo en el resultado.")
        self._ensure_individual(row)
        item_id = row.get_attribute("id")
        if not item_id:
            raise OutlookError("Falta identificador de fila.")
        return Candidate(item_id, emails[0], clean_text(subject.text_content(), 300))

    def walk_folders(
        self, limit: int, visit: Callable[[Candidate], bool | None],
    ) -> list[dict]:
        """Visit folders; False ends only the current verified newest-first folder."""
        if type(limit) is not int or limit < 1:
            raise ValueError("El limite de filas debe ser positivo.")
        reports: list[dict] = []
        examined = 0
        folders = self.folders()
        for key, label in folders:
            self.open_folder(key, label)
            positions: dict[int, str] = {}
            ids: set[str] = set()
            total = self.folder_total
            if total is None:
                raise OutlookError("Cantidad de carpeta no verificada.")
            authorized = 0
            stopped_before_start = False
            while True:
                self.guard()
                rows = self.page.locator(ROWS)
                if rows.count() == 0:
                    empty = self.page.locator(MESSAGE_LIST + " span.ksePc")
                    if (
                        total != 0 or positions or empty.count() != 1
                        or empty.inner_text().strip() != self.empty_label
                    ):
                        raise OutlookError("Lista vacia sin confirmacion explicita; cobertura no demostrada.")
                    break
                # Read positions atomically: Outlook can resize its overscan while we iterate.
                snapshot = rows.evaluate_all(
                    """els => els.map(el => ({
                      id: el.id,
                      position: el.closest('[data-item-index]')?.getAttribute('data-item-index')
                    }))"""
                )
                page_ids = [item["id"] for item in snapshot]
                visited = False
                for item in snapshot:
                    position, item_id = item.get("position"), item["id"]
                    if not isinstance(position, str) or not position.isdigit() or not item_id:
                        raise OutlookError("Faltan posiciones o cantidad verificable en la lista.")
                    pos = int(position)
                    if not 0 <= pos < total:
                        raise OutlookError("Posicion fuera del total anunciado por la carpeta.")
                    if pos in positions:
                        if positions[pos] != item_id:
                            raise OutlookError("La lista cambio de orden durante el recorrido.")
                        continue
                    if item_id in ids:
                        raise OutlookError("Identificador de fila repetido en otra posicion.")
                    if examined >= limit:
                        raise OutlookError(
                            "Limite total de filas alcanzado; recorrido parcial, no se omiten carpetas."
                        )
                    row = self._message_row(item_id)
                    if row.count() == 0:
                        continue
                    if row.count() != 1:
                        raise OutlookError("Identificador de fila ausente o ambiguo.")
                    examined += 1
                    candidate = self._candidate_from_row(row)
                    positions[pos] = item_id
                    ids.add(item_id)
                    if candidate is not None:
                        keep_reading = visit(candidate)
                        authorized += 1
                        self.guard()
                        if keep_reading is False:
                            newest = self.page.locator(MESSAGE_LIST).get_by_role(
                                "button", name="Ordenado: Más recientes en la parte superior",
                                exact=True,
                            )
                            if newest.count() != 1 or not newest.is_visible():
                                raise OutlookError("No se confirma el orden de fecha descendente.")
                            if set(positions) != set(range(pos + 1)):
                                raise OutlookError("Hay posiciones sin verificar antes del corte por fecha.")
                            stopped_before_start = True
                            break
                        # Reading moves Outlook's list; scrolling on from a stale snapshot skips rows.
                        visited = True
                        break
                if stopped_before_start:
                    break
                if len(positions) == total:
                    if set(positions) != set(range(total)):
                        raise OutlookError("Hay posiciones sin verificar en la carpeta.")
                    break
                if visited:
                    continue
                scroller = self.page.locator(SCROLLER)
                if scroller.count() != 1 or not scroller.is_visible():
                    raise OutlookError("Contenedor virtual ausente o ambiguo.")
                scroller.evaluate("el => { el.scrollTop += el.clientHeight * 0.6; }")
                self.page.wait_for_function(
                    """({rows,before}) => {
                      const now = [...document.querySelectorAll(rows)]
                        .map(el => el.id);
                      return JSON.stringify(now) !== JSON.stringify(before);
                    }""", arg={"rows": ROWS, "before": page_ids},
                )
            report = {"folder": label, "rows": len(positions), "authorized": authorized}
            if stopped_before_start:
                report["stopped_before_start"] = True
            reports.append(report)
        if self.folders() != folders:
            raise OutlookError("El arbol de subcarpetas cambio durante el recorrido.")
        return reports

    @staticmethod
    def _ensure_individual(row: Locator) -> None:
        count = re.search(r"\b(\d+)\s+mensajes?\b", row.get_attribute("aria-label") or "", re.I)
        if row.get_attribute("aria-expanded") is not None or (count and int(count[1]) > 1):
            raise OutlookError("Conversacion agrupada; el piloto requiere un mensaje individual.")

    def candidate(self) -> Candidate:
        """Choose one visible authorized row, without reading unrelated subjects."""
        self.guard()
        self.page.locator(SEARCH).wait_for(state="visible", timeout=30000)
        if self.page.locator(SEARCH).count() != 1:
            raise OutlookError("Buscador ausente o ambiguo.")
        self.page.locator(f"{ROWS} span[title]").first.wait_for(state="visible")
        self.guard()
        rows = self.page.locator(ROWS)
        for sender in self.approved_senders:
            for index in range(rows.count()):
                row = rows.nth(index)
                candidate = self._candidate_from_row(row)
                if candidate is not None and candidate.sender == sender:
                    return candidate
        raise OutlookError(
            "No hay avisos de remitentes autorizados visibles. "
            "No se buscan ni abren mensajes fuera de la lista."
        )

    def _row(self, candidate: Candidate) -> Locator:
        row = self._message_row(candidate.item_id)
        if row.count() != 1:
            raise OutlookError("Identificador de fila ausente o ambiguo.")
        self._ensure_individual(row)
        titles = row.locator("span[title]").evaluate_all("els => els.map(el => el.title)")
        emails = [title.strip().lower() for title in titles if EMAIL.fullmatch(title.strip())]
        subject = row.locator(SUBJECT)
        if (
            candidate.sender not in self.approved_senders
            or emails != [candidate.sender]
            or subject.count() != 1
            or clean_text(subject.text_content(), 300) != candidate.subject
        ):
            raise OutlookError("El resultado cambio antes de abrir el origen.")
        return row

    def _message_row(self, item_id: str) -> Locator:
        if not item_id or re.search(r"[\x00-\x1f\x7f]", item_id):
            raise OutlookError("Identificador de fila no valido.")
        return self.page.locator(ROWS + "[id=" + json.dumps(item_id, ensure_ascii=False) + "]")

    def checkpoint_key(self, candidate: Candidate) -> str:
        """Hash folder and exact row metadata; never persist message text."""
        self.guard()
        row = self._row(candidate)
        metadata = row.evaluate(
            """el => ({
              label: el.getAttribute('aria-label'),
              titles: [...el.querySelectorAll('[title]')].map(n=>n.getAttribute('title')),
              dates: [...el.querySelectorAll('time')].map(n=>({
                datetime:n.getAttribute('datetime'),text:n.textContent
              }))
            })"""
        )
        return hashlib.sha256(json.dumps(
            [self.active_folder, candidate.item_id, candidate.sender, candidate.subject, metadata],
            sort_keys=True, ensure_ascii=False,
        ).encode()).hexdigest()

    def _open_source_menu(self, candidate: Candidate) -> Locator:
        for attempt in range(2):
            self.guard()
            row = self._row(candidate)
            if row.get_attribute("aria-selected") != "true":
                raise OutlookError("La seleccion cambio al abrir el menu.")
            if self.page.locator('[role="dialog"]').count():
                raise OutlookError("Hay un dialogo previo al abrir el menu.")
            try:
                if attempt:
                    row.click(button="right")
                else:
                    row.press("Shift+F10")
                self.guard()
                if self._row(candidate).get_attribute("aria-selected") != "true":
                    raise OutlookError("La seleccion cambio al abrir el menu.")
                view = self.page.get_by_role("menuitem", name="Ver", exact=True)
                view.wait_for(state="visible")
                if view.count() != 1:
                    raise OutlookError("Menu Ver ambiguo.")
                view.press("ArrowRight")
                source = self.page.get_by_role(
                    "menuitem", name="Ver origen del mensaje", exact=True,
                )
                source.wait_for(state="visible")
                if source.count() != 1:
                    raise OutlookError("Accion de origen ambigua.")
                self.guard()
                if self._row(candidate).get_attribute("aria-selected") != "true":
                    raise OutlookError("La seleccion cambio al abrir el submenu.")
                return source
            except PlaywrightTimeoutError:
                self.guard()
                if attempt or self.page.locator(
                    '[role="dialog"], [role="menu"]:visible',
                ).count():
                    raise
                if self._row(candidate).get_attribute("aria-selected") != "true":
                    raise OutlookError("La seleccion cambio; no se reabre el menu.") from None
                logging.getLogger(__name__).warning(
                    "El menu de origen se cerro; un unico reintento reabriendo el mismo aviso autorizado.",
                )
                self._select_message(candidate)
        raise AssertionError("El menu debe abrirse o detenerse.")

    def _close_message_view(self, candidate: Candidate) -> None:
        self.guard()
        row = self._row(candidate)
        if row.get_attribute("aria-selected") != "true":
            raise OutlookError("La seleccion cambio antes de cerrar la vista del mensaje.")
        close_message = self.page.get_by_role("button", name="Cerrar", exact=True)
        if close_message.count() == 0 and row.locator(SUBJECT).inner_text().strip():
            return
        if close_message.count() != 1 or self.page.locator('[role="dialog"]').count():
            raise OutlookError("No se reconoce el cierre de la vista del mensaje.")
        actions = self.page.get_by_role("button", name="More items", exact=True)
        actions.wait_for(state="visible", timeout=30000)
        if actions.count() != 1:
            raise OutlookError("Barra de acciones del mensaje ambigua.")
        self.guard()
        if self._row(candidate).get_attribute("aria-selected") != "true":
            raise OutlookError("La seleccion cambio al cargar la vista del mensaje.")
        close_message.click()
        close_message.wait_for(state="hidden")
        self.page.wait_for_function(
            """({selector,id,subject}) => {
              const rows = [...document.querySelectorAll(selector)].filter(el=>el.id===id);
              return rows.length===1 && rows[0].innerText.includes(subject);
            }""", arg={"selector": ROWS, "id": candidate.item_id, "subject": candidate.subject},
        )
        self.guard()
        if self._row(candidate).get_attribute("aria-selected") != "true":
            raise OutlookError("La seleccion cambio al cerrar la vista del mensaje.")

    def _select_message(self, candidate: Candidate) -> None:
        self.guard()
        if self.page.locator('[role="dialog"]').count() or self.page.get_by_role("menu").count():
            raise OutlookError("Hay un dialogo o menu previo; se detiene para evitar contenido obsoleto.")
        row = self._row(candidate)
        if self.page.get_by_role("button", name="Cerrar", exact=True).count():
            self._close_message_view(candidate)
            row = self._row(candidate)
        # Outlook enlarges rows lazily; a lower row gets pushed out and unmounted mid-action.
        row.evaluate("el => el.scrollIntoView({block: 'start'})")
        row.locator(SUBJECT).click()
        self.page.wait_for_function(
            """({selector,id}) => {
              const rows = [...document.querySelectorAll(selector)]
                .filter(row => row.id === id);
              return rows.length === 1 && rows[0].getAttribute('aria-selected') === 'true';
            }""", arg={"selector": ROWS, "id": candidate.item_id},
        )
        self.guard()
        row = self._row(candidate)
        if row.get_attribute("aria-selected") != "true":
            raise OutlookError("La fila autorizada no quedo seleccionada.")
        if row.locator(SUBJECT).inner_text().strip():
            self.page.get_by_role("button", name="Cerrar", exact=True).wait_for(state="visible")
        self._close_message_view(candidate)

    def read(self, candidate: Candidate) -> Mail:
        self._select_message(candidate)
        source = self._open_source_menu(candidate)
        source.press("Enter")
        # Outlook can toggle aria-hidden while the source dialog remains on screen.
        dialog = self.page.locator('[role="dialog"]')
        dialog.wait_for(state="visible")
        if dialog.count() != 1:
            raise OutlookError("Dialogo de origen ambiguo.")
        content = dialog.locator(SOURCE)
        content.wait_for(state="visible", timeout=30000)
        if content.count() != 1:
            raise OutlookError("Contenido de origen ambiguo.")
        self.page.wait_for_function(
            """selector => {
              const dialogs = document.querySelectorAll('[role="dialog"]');
              if (dialogs.length !== 1) return false;
              const contents = dialogs[0].querySelectorAll(selector);
              return contents.length === 1
                && /^[A-Za-z0-9-]+:/m.test(contents[0].textContent || '');
            }""", arg=SOURCE, timeout=30000,
        )
        self.guard()
        if self._row(candidate).get_attribute("aria-selected") != "true":
            raise OutlookError("La seleccion cambio al abrir el origen.")
        raw = content.text_content()
        if raw is None:
            raise OutlookError("Origen vacio.")
        mail = parse_source(raw, candidate, self.origin)
        close = dialog.get_by_role("button", name="Cerrar", exact=True, include_hidden=True)
        if close.count() != 1:
            raise OutlookError("Cierre del dialogo ambiguo.")
        close.click()
        dialog.wait_for(state="hidden")
        # Outlook keeps the app aria-hidden briefly after the dialog; role lookups would miss it.
        self.page.wait_for_function(
            """list => { const pane = document.querySelector(list);
              return !!pane && !pane.closest('[aria-hidden="true"]'); }""",
            arg=MESSAGE_LIST,
        )
        self._close_message_view(candidate)
        return mail
