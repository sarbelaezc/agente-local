"""Review data and approval rules of the invoice panel, without any user interface.

The panel only shows what these functions return and sends back what a person
chose; every rule (whole e-mails only, existing folders, an approver, explicit
decisions on historical matches) is enforced here.
"""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from zipfile import BadZipFile, ZipFile

from shared.local_panel import remote_device
from mail_invoices.batch_state import BatchMessage, BatchStateStore
from mail_invoices.classification_decisions import ClassificationDecisionStore
from mail_invoices.documents import MAX_FILE_BYTES
from mail_invoices.folders import ConfirmedExample, IncomingMessage
from mail_invoices.review import reviewed_invoice_from_fields, suggested_company
from mail_invoices.settings import MailSettings
from mail_invoices.workbook import InvoiceWorkbook, ReviewedInvoice

STATE_TEXT = {
    "pending": "Pendiente de aprobación",
    "approved": "Aprobado; falta registrar en Excel",
    "registered": "Registrado en Excel; falta archivar",
    "moving": "Archivo sin confirmar: solo se verificará, no se repetirá",
    "moved": "Archivado y verificado",
    "blocked": "Bloqueado: requiere una nueva aprobación",
}
STATUS_TEXT = {
    "registered": "Registrada en Excel",
    "not_registered": "Sin registrar en Excel",
    "historical_review": "Coincide con una fila sin número; requiere revisión",
    "conflict": "En conflicto con Excel",
    "incomplete": "Datos incompletos",
}
FIELDS = ("company", "legal_name", "issued", "amount", "number", "currency")
# Messages produced in English by the extraction steps, shown in Spanish on the panel.
NOTICE_TEXT = (
    ("Invoice number differs between subject and attachment: ",
     "El número del asunto no coincide con el del documento ({}). Contrástalo con el PDF y corrígelo si hace falta."),
    ("No supported attachments downloaded", "El correo no trae adjuntos PDF, XML o ZIP{}."),
)


def readable(text: str) -> str:
    for prefix, spanish in NOTICE_TEXT:
        if text.startswith(prefix):
            return spanish.format(text[len(prefix):])
    return text.replace(": larger than 10 MB", ": supera 10\u00a0MB").replace(": Scanned PDF", ": PDF escaneado sin texto")
EXECUTABLE = {"approved", "registered", "moving"}


# ------------------------------------------------------------------ PDF previews


def _safe_pdf_entry(info) -> bool:
    path = PurePosixPath(info.filename.replace("\\", "/"))
    if info.is_dir() or path.suffix.casefold() != ".pdf":
        return False
    if path.is_absolute() or ".." in path.parts or info.flag_bits & 1:
        return False
    return info.file_size <= MAX_FILE_BYTES


def _matching_pdf_entry(entries, member: PurePosixPath):
    if member.suffix.casefold() == ".pdf":
        matches = [item for item in entries if PurePosixPath(item.filename.replace("\\", "/")) == member]
    else:
        matches = [
            item for item in entries
            if PurePosixPath(item.filename.replace("\\", "/")).stem.casefold() == member.stem.casefold()
            and PurePosixPath(item.filename.replace("\\", "/")).parent == member.parent
        ]
        if not matches and len(entries) == 1:
            matches = entries
    return matches[0] if len(matches) == 1 else None


def _zip_pdf_bytes(root: Path, source: str) -> bytes | None:
    zip_marker = source.casefold().find(".zip:")
    archive_path = (root / source[:zip_marker + 4]).resolve()
    member = PurePosixPath(source[zip_marker + 5:].replace("\\", "/"))
    if not archive_path.is_relative_to(root) or not archive_path.is_file():
        return None
    if member.is_absolute() or ".." in member.parts:
        return None
    try:
        with ZipFile(archive_path) as archive:
            entry = _matching_pdf_entry([item for item in archive.infolist() if _safe_pdf_entry(item)], member)
            if entry is None:
                return None
            with archive.open(entry) as stream:
                data = stream.read(MAX_FILE_BYTES + 1)
    except (BadZipFile, KeyError, OSError, RuntimeError):
        return None
    return data if len(data) <= MAX_FILE_BYTES else None


def _direct_pdf_preview(root: Path, source: str) -> Path | None:
    candidate = (root / source).resolve()
    if not candidate.is_relative_to(root):
        return None
    if candidate.suffix.casefold() == ".pdf":
        return candidate if candidate.is_file() and candidate.stat().st_size <= MAX_FILE_BYTES else None
    if candidate.suffix.casefold() != ".xml" or not candidate.is_file():
        return None
    pdfs = list(candidate.parent.glob("*.pdf"))
    matches = [path for path in pdfs if path.stem.casefold() == candidate.stem.casefold()] or pdfs
    if len(matches) != 1:
        return None
    preview = matches[0].resolve()
    return preview if preview.is_relative_to(root) and preview.stat().st_size <= MAX_FILE_BYTES else None


def prepare_pdf_preview(attachments_dir: Path | None, source: str) -> Path | None:
    """The PDF that unambiguously represents an invoice, inside this run's directory only."""
    if attachments_dir is None:
        return None
    root = attachments_dir.resolve()
    if ".zip:" not in source.casefold():
        return _direct_pdf_preview(root, source)
    data = _zip_pdf_bytes(root, source)
    if data is None:
        return None
    preview_dir = root / "pdf-preview"
    preview_dir.mkdir(parents=True, exist_ok=True)
    preview_path = preview_dir / f"{hashlib.sha256(source.encode('utf-8')).hexdigest()}.pdf"
    preview_path.write_bytes(data)
    return preview_path


# ------------------------------------------------------------------ batch view


def _invoice_values(message: BatchMessage, index: int, suggestions: dict[str, str]) -> tuple[dict, str]:
    """Field values to show: a previous approval, else the document with a company hint."""
    if len(message.invoices) == len(message.items):
        approved = message.invoices[index]
        return {
            "company": approved.company, "legal_name": approved.legal_name,
            "issued": approved.issued.isoformat(), "amount": str(approved.amount),
            "number": approved.number, "currency": approved.currency,
        }, "approved"
    candidate = message.items[index].candidate
    legal_name = " ".join(candidate.legal_name.split())
    hint = "history" if suggestions.get(legal_name.casefold()) else ("legal_name" if legal_name else "")
    return {
        "company": suggested_company(suggestions, candidate.legal_name),
        "legal_name": candidate.legal_name,
        "issued": candidate.issued.isoformat() if candidate.issued else "",
        "amount": str(candidate.amount) if candidate.amount is not None else "",
        "number": candidate.number, "currency": candidate.currency,
    }, hint


def batch_view(
    messages: list[BatchMessage], folders: list[str], settings: MailSettings,
    attachments_dir: Path | None, workbook: InvoiceWorkbook,
) -> tuple[dict, dict[str, Path]]:
    """What the panel shows for a batch, and the PDF files it may serve by token."""
    try:
        suggestions, suggestion_error = workbook.company_suggestions(), ""
    except (OSError, ValueError) as error:
        suggestions, suggestion_error = {}, str(error)
    pdfs: dict[str, Path] = {}
    view_messages = []
    for message in messages:
        invoices = []
        for index, item in enumerate(message.items):
            values, hint = _invoice_values(message, index, suggestions)
            pdf = prepare_pdf_preview(attachments_dir, item.candidate.source)
            token = None
            if pdf is not None:
                token = hashlib.sha256(f"{message.conversation}#{index}".encode()).hexdigest()[:32]
                pdfs[token] = pdf
            invoices.append({
                "source": item.candidate.source, "method": item.candidate.method,
                "requires_review": item.candidate.requires_review,
                "rejected_fields": list(item.rejected_model_fields), "model_error": item.model_error or "",
                "company_hint": hint, "values": values, "pdf": token,
            })
        view_messages.append({
            "id": message.conversation, "subject": message.subject, "sender": message.sender,
            "state": message.state, "state_text": STATE_TEXT[message.state],
            "reason": message.reason, "last_error": message.last_error,
            "warnings": [readable(text) for text in message.warnings],
            "errors": [readable(text) for text in message.errors],
            "editable": message.state in {"pending", "blocked"},
            "selectable": bool(message.items) and not message.errors,
            "folder": message.folder or "", "approver": message.approver, "invoices": invoices,
        })
    return {
        "mode": "batch", "workbook": str(settings.workbook), "approver": settings.approver,
        "folders": folders, "suggestion_error": suggestion_error, "messages": view_messages,
    }, pdfs


def refresh_view(view: dict, messages: list[BatchMessage]) -> dict:
    """The fixed view with each message's current state, for progress while executing."""
    by_id = {message.conversation: message for message in messages}
    refreshed = []
    for item in view["messages"]:
        message = by_id.get(item["id"])
        if message is not None:
            item = {
                **item, "state": message.state, "state_text": STATE_TEXT[message.state],
                "last_error": message.last_error, "folder": message.folder or item["folder"],
                "approver": message.approver, "editable": message.state in {"pending", "blocked"},
            }
        refreshed.append(item)
    return {**view, "messages": refreshed}


def batch_result(messages: list[BatchMessage], failed: bool) -> dict:
    """What the panel says when the batch ends."""
    moved = sum(message.state == "moved" for message in messages)
    others = len(messages) - moved
    summary = f"{moved} archivado{'s' if moved != 1 else ''} y verificado{'s' if moved != 1 else ''}"
    if others:
        summary += f"; {others} pendiente{'s' if others != 1 else ''} o con error"
    return {
        "summary": summary, "failed": failed,
        "messages": [
            {"id": message.conversation, "state": message.state, "error": message.last_error}
            for message in messages
        ],
    }


# ------------------------------------------------------------------ batch approval


@dataclass(frozen=True)
class LegacyQuestion:
    """An invoice that matches one historical row without a number: a person decides."""

    key: str
    row: int
    number: str
    legal_name: str


class LegacyDecisionRequired(ValueError):
    def __init__(self, questions: list[LegacyQuestion]) -> None:
        super().__init__("Decide cada posible duplicado histórico antes de aprobar")
        self.questions = questions


def _text(value: object, label: str, limit: int = 300) -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f"{label} invalido")
    return value


def _resolve_legacy(
    workbook: InvoiceWorkbook, key: str, invoice: ReviewedInvoice, choices: dict[str, str],
    questions: list[LegacyQuestion],
) -> ReviewedInvoice:
    matches = workbook.legacy_matches(invoice)
    if len(matches) > 1:
        raise ValueError(
            f"La factura {invoice.number} coincide con varias filas sin número ({matches}); "
            "resuélvelo en Excel antes de aprobar"
        )
    if not matches:
        return invoice
    choice = choices.get(key)
    if choice == "link":
        return replace(invoice, legacy_row=matches[0])
    if choice == "new":
        return replace(invoice, confirmed_new=True)
    questions.append(LegacyQuestion(key, matches[0], invoice.number, invoice.legal_name))
    return invoice


def approve_batch(
    messages: list[BatchMessage], folders: list[str], journal: BatchStateStore,
    workbook: InvoiceWorkbook, submission: dict,
) -> list[BatchMessage]:
    """Validate the person's choices and persist every approval together.

    Nothing is written to Excel or the mailbox here: approvals are saved atomically
    before any side effect, and the batch executes only after this returns.
    """
    if not isinstance(submission, dict):
        raise ValueError("Aprobacion invalida")
    approver = " ".join(_text(submission.get("approver", ""), "Aprobador", 100).split())
    # An approval from a paired device of the home network records which one.
    if approver and remote_device.get():
        approver = f"{approver} (desde {remote_device.get()})"
    chosen = submission.get("messages", [])
    choices = submission.get("legacy", {})
    if not isinstance(chosen, list) or not isinstance(choices, dict):
        raise ValueError("Aprobacion invalida")
    by_id = {message.conversation: message for message in messages}
    executable = [message for message in messages if message.state in EXECUTABLE]
    if not chosen and not executable:
        raise ValueError("Selecciona al menos un correo apto para aprobar")
    if chosen and not approver:
        raise ValueError("Escribe quién aprueba el lote")
    staged, questions, seen = [], [], set()
    approved_at = datetime.now(timezone.utc).isoformat()
    for raw in chosen:
        if not isinstance(raw, dict):
            raise ValueError("Aprobacion invalida")
        message = by_id.get(raw.get("id"))
        if message is None or message.conversation in seen:
            raise ValueError("Correo desconocido o repetido en la aprobación")
        seen.add(message.conversation)
        if message.state not in {"pending", "blocked"} or not message.items or message.errors:
            raise ValueError(f"«{message.subject[:80]}» no se puede aprobar: tiene errores o ya está en curso")
        folder = raw.get("folder")
        if folder not in folders:
            raise ValueError(f"Elige una carpeta existente para «{message.subject[:80]}»")
        fields = raw.get("invoices")
        if not isinstance(fields, list) or len(fields) != len(message.items):
            raise ValueError(f"Aprueba todas las facturas de «{message.subject[:80]}»")
        invoices = []
        for index, values in enumerate(fields):
            if not isinstance(values, dict):
                raise ValueError("Factura invalida")
            invoice = reviewed_invoice_from_fields({key: _text(values.get(key, ""), key) for key in FIELDS})
            invoices.append(_resolve_legacy(workbook, f"{message.conversation}#{index}", invoice, choices, questions))
        staged.append(replace(
            message, folder=folder, invoices=invoices, approver=approver, approved_at=approved_at,
            state="approved", last_error="",
            context=replace(
                message.context, supplier=invoices[0].legal_name,
                invoices=tuple(
                    {**context, "supplier": invoice.legal_name}
                    for context, invoice in zip(message.context.invoices, invoices, strict=True)
                ),
            ),
        ))
    if questions:
        raise LegacyDecisionRequired(questions)
    # All approvals persist atomically BEFORE any Excel or Outlook side effect.
    if staged:
        journal.save(staged)
    updated = {message.conversation: message for message in staged}
    messages[:] = [updated.get(message.conversation, message) for message in messages]
    return staged


# ------------------------------------------------------------------ classification simulation


def classification_view(report: dict, store_path: Path | None, approver: str) -> dict:
    """What the panel shows for a read-only classification run."""
    return {
        "mode": "classification", "approver": approver, "folders": report["folders"],
        "decisions_path": str(store_path) if store_path else "",
        "messages": [
            {
                "id": str(index), "subject": message["subject"] or "Correo no identificable",
                "sender": message["sender"], "errors": list(message["errors"]),
                "eligible": message["eligible"], "folder": message.get("folder") or "",
                "reason": message.get("reason", ""), "source": message.get("source", ""),
                "confirmable": "classification_context" in message,
                "confirmed": message.get("confirmed_folder") or "",
                "invoices": [
                    {"number": invoice["number"], "status": STATUS_TEXT[invoice["status"]]}
                    for invoice in message["invoices"]
                ],
            }
            for index, message in enumerate(report["messages"])
        ],
    }


def save_classification(
    report: dict, store: ClassificationDecisionStore, submission: dict,
) -> int:
    """Keep only the destinations a person confirmed as examples; nothing is moved."""
    if not isinstance(submission, dict) or not isinstance(submission.get("confirmed"), list):
        raise ValueError("Decisiones invalidas")
    approver = _text(submission.get("approver", ""), "Confirmado por", 100)
    examples, confirmed = [], []
    for raw in submission["confirmed"]:
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not raw["id"].isdigit():
            raise ValueError("Decisiones invalidas")
        index = int(raw["id"])
        if not 0 <= index < len(report["messages"]):
            raise ValueError("Correo desconocido")
        message = report["messages"][index]
        if "classification_context" not in message:
            raise ValueError("Ese correo no tiene contexto para guardarse como ejemplo")
        folder = raw.get("folder")
        context = message["classification_context"]
        examples.append(ConfirmedExample(
            IncomingMessage(
                context["sender"], context["subject"], context["supplier"], context["body"],
                tuple(context["invoices"]),
            ),
            folder if isinstance(folder, str) else "",
        ))
        confirmed.append((message, folder))
    try:
        count = store.save(examples, approver, report["folders"])
    except sqlite3.Error as error:
        raise ValueError(f"No se guardaron las decisiones: {error}") from error
    for message, folder in confirmed:
        message["confirmed_folder"] = folder
    return count


def record_reviewed_folders(report: dict, folders: dict) -> None:
    """Remember the folder shown for each message when the simulation closes."""
    for index, message in enumerate(report["messages"]):
        folder = folders.get(str(index)) if isinstance(folders, dict) else None
        message["reviewed_folder"] = folder if isinstance(folder, str) and folder in report["folders"] else None
