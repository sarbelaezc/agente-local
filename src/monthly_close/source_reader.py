from __future__ import annotations

import hashlib
import re
from email import policy
from email.message import EmailMessage
from email.parser import Parser
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser

from monthly_close.domain import clean_text
from monthly_close.extraction import Mail
from monthly_close.outlook import Candidate, OutlookError

MAX_SOURCE_CHARS = 2_000_000


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
