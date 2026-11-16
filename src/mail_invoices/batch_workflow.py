from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from playwright.sync_api import Error as PlaywrightError, sync_playwright

from mail_invoices.assist import to_json
from mail_invoices.batch_review_ui import BatchReviewWindow
from mail_invoices.batch_state import (
    BatchMessage, BatchStateStore, batch_lock, batch_scope, batch_state_path,
)
from mail_invoices.browser import outlook_page
from mail_invoices.classification import simulate_classification
from mail_invoices.classification_decisions import ClassificationDecisionStore, decisions_path
from mail_invoices.folders import ConfirmedExample
from mail_invoices.inbox import MESSAGE_SELECTOR
from mail_invoices.movement import move_registered_message, verify_destination
from mail_invoices.outlook_folders import discover_folders
from mail_invoices.settings import MailSettings
from mail_invoices.workbook import InvoiceWorkbook, WorkbookError


def _active_messages(messages: list[BatchMessage], limit: int) -> list[BatchMessage]:
    def priority(message: BatchMessage) -> int:
        if message.state in {"moving", "registered", "approved"}:
            return 0
        if message.state == "pending" and message.items and not message.errors:
            return 1
        return 2

    return sorted(
        (message for message in messages if message.state != "moved"), key=priority,
    )[:limit]


def _persist_execution_error(
    message: BatchMessage, journal: BatchStateStore, error: Exception,
) -> None:
    if (
        message.state == "registered"
        and "body changed after review" in str(error).casefold()
    ):
        message.state = "blocked"
    message.last_error = str(error)
    journal.save([message])


def execute_message(
    message: BatchMessage, journal: BatchStateStore, workbook: InvoiceWorkbook,
    decisions: ClassificationDecisionStore, page,
) -> None:
    """Resume a durable approval; uncertain moves are verification-only."""
    if message.state not in {"approved", "registered", "moving"}:
        return
    data = {
        "conversation": message.conversation, "sender": message.sender,
        "subject": message.subject, "eligible": True,
        "body_digest": message.body_digest,
    }
    try:
        if message.state == "approved":
            try:
                workbook.record(message.invoices)
            except WorkbookError:
                message.state = "blocked"
                raise
            message.state = "registered"
            journal.save([message])
        statuses = [
            workbook.registration_status(
                invoice.number, invoice.legal_name, invoice.issued, invoice.amount, invoice.currency,
                company=invoice.company,
            ) for invoice in message.invoices
        ]
        if not statuses or any(status != "registered" for status in statuses):
            if message.state != "moving":
                message.state = "blocked"
            raise ValueError("El registro en Excel no coincide; no se movera el correo")
        if message.folder is None:
            raise ValueError("Falta el destino aprobado")
        folders = [folder.path for folder in discover_folders(page)]
        if message.folder not in folders:
            raise LookupError("La carpeta aprobada ya no existe; no se movera el correo")
        decisions.save(
            [ConfirmedExample(message.context, message.folder)], message.approver, folders,
        )
        if message.state == "moving":
            verify_destination(page, data, message.folder)
        else:
            def mark_moving() -> None:
                message.state = "moving"
                journal.save([message])

            move_registered_message(
                page, data, message.folder, approved=True, before_move=mark_moving,
            )
        message.state = "moved"
        message.last_error = ""
        journal.save([message])
    except (OSError, ValueError, LookupError, sqlite3.Error, PlaywrightError) as error:
        _persist_execution_error(message, journal, error)
        raise


def process_invoice_batch(
    settings: MailSettings, days: int, use_model: bool, batch_size: int, *, resume: bool = False,
) -> int:
    if not 1 <= batch_size <= 5:
        raise ValueError("Los lotes controlados admiten entre 1 y 5 correos")
    settings.attachments_dir.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="invoice-batch-", dir=settings.attachments_dir) as run_dir:
        return _process_invoice_batch(
            settings, days, use_model, batch_size, resume=resume,
            attachment_dir=Path(run_dir),
        )


def _process_invoice_batch(
    settings: MailSettings, days: int, use_model: bool, batch_size: int,
    *, resume: bool, attachment_dir: Path,
) -> int:
    path = batch_state_path()
    with batch_lock(path.with_suffix(".lock")):
        journal = BatchStateStore(path, batch_scope(settings))
        stored = {message.conversation: message for message in journal.load()}
        active = _active_messages(list(stored.values()), batch_size)
        missing_identity = []
        if resume and not active:
            print(to_json({"messages": [], "message": "No hay correos pendientes"}))
            return 0
        if not settings.executable.is_file():
            raise FileNotFoundError("Configured Opera executable does not exist")
        # One browser session per run, always closed, including cancellation.
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(settings.profile_dir), executable_path=str(settings.executable),
                headless=False, accept_downloads=True,
            )
            try:
                page = outlook_page(context)
                page.wait_for_selector(MESSAGE_SELECTOR, timeout=60000)
                if not resume:
                    report = simulate_classification(
                        replace(settings, batch_limit=batch_size), days, [], use_model,
                        include_review_items=True, attachment_dir=attachment_dir, page=page,
                    )
                    for raw in report["messages"]:
                        if not raw.get("conversation"):
                            missing_identity.append(raw["errors"])
                            continue
                        prepared = BatchMessage.from_report(raw)
                        existing = stored.get(prepared.conversation)
                        if existing is None or existing.state == "pending":
                            journal.save([prepared])
                            stored[prepared.conversation] = prepared
                    active = _active_messages(list(stored.values()), batch_size)
                    if missing_identity:
                        print(to_json({"untracked_errors": missing_identity}))
                if not active:
                    print(to_json({"messages": [], "message": "No hay correos pendientes"}))
                    return 1 if missing_identity else 0
                folders = [folder.path for folder in discover_folders(page)]
                window = BatchReviewWindow(active, folders, settings, journal, attachment_dir)
                if not window.run():
                    print(to_json({"executed": False, "message": "Lote cerrado sin ejecutar"}))
                    return 1
                decisions = ClassificationDecisionStore(decisions_path())
                workbook = InvoiceWorkbook(settings.workbook, settings.backup_dir, settings.sheet)
                failed = False
                for message in active:
                    try:
                        execute_message(message, journal, workbook, decisions, page)
                    except (OSError, ValueError, LookupError, sqlite3.Error, PlaywrightError):
                        failed = True
                        # Stop the batch on failures; remaining durable approvals can be resumed.
                        break
            finally:
                context.close()
        print(to_json({
            "messages": [
                {"conversation": message.conversation, "state": message.state,
                 "folder": message.folder, "error": message.last_error, "errors": message.errors}
                for message in active
            ],
        }))
        return 1 if failed or missing_identity or any(message.state != "moved" for message in active) else 0
