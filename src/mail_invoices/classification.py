from __future__ import annotations

from datetime import date
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError, sync_playwright

from mail_invoices.browser import outlook_page, reopen_outlook
from mail_invoices.documents import MAX_FILE_BYTES, InvoiceCandidate
from mail_invoices.folders import IncomingMessage, bounded_context, suggest_folder
from mail_invoices.classification_decisions import ClassificationDecisionStore, decisions_path
from mail_invoices.inbox import MESSAGE_SELECTOR, _find_message, download_attachments, scan_inbox
from mail_invoices.local_model import LocalModelError, OllamaJsonModel
from mail_invoices.outlook_folders import discover_folders, message_metadata, read_history, read_message_body
from mail_invoices.settings import MailSettings
from mail_invoices.workbook import InvoiceWorkbook


def classification_ready(statuses: list[str], errors: list[str]) -> bool:
    return bool(statuses) and not errors and all(status == "registered" for status in statuses)


def simulate_classification(
    settings: MailSettings, days: int, authorized_history: list[str], use_model: bool,
    *, register: bool = False,
    include_review_items: bool = False,
) -> dict:
    """Suggest destinations and verify Excel read-only. No move command is invoked."""
    from mail_invoices.workflow import _build_ocr, _extract_file, _prefer_xml_candidates
    from mail_invoices.review import ReviewItem
    from mail_invoices.review_ui import InvoiceReviewWindow

    if register and settings.batch_limit != 1:
        raise ValueError("Integrated controlled test requires exactly one email")
    if not settings.executable.is_file():
        raise FileNotFoundError("Configured Opera executable does not exist")
    store = InvoiceWorkbook(settings.workbook, settings.backup_dir, settings.sheet)
    model = OllamaJsonModel(settings.ollama_url, settings.ollama_model) if use_model else None
    ocr = _build_ocr(settings)
    confirmed = ClassificationDecisionStore(decisions_path()).load()
    results = []
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(settings.profile_dir), executable_path=str(settings.executable),
            headless=False, accept_downloads=True,
        )
        try:
            page = outlook_page(context)
            page.wait_for_selector(MESSAGE_SELECTOR, timeout=60000)
            folders = [folder.path for folder in discover_folders(page)]
            history = read_history(page, authorized_history)
            reopen_outlook(page, MESSAGE_SELECTOR)
            messages = scan_inbox(page, days, date.today())[:settings.batch_limit]
            for index, message in enumerate(messages, 1):
                result = {
                    "sender": "", "subject": "", "invoices": [],
                    "errors": [], "eligible": False, "folder": None,
                }
                results.append(result)
                try:
                    reopen_outlook(page, MESSAGE_SELECTOR)
                    option = _find_message(page, message.label)
                    if option is None:
                        raise LookupError("Candidate no longer available in inbox")
                    sender, subject = message_metadata(option)
                    result.update(
                        sender=sender, subject=subject,
                        conversation=option.get_attribute("data-convid"),
                    )
                    # Separate message directories prevent same-name attachment overwrites.
                    paths = download_attachments(
                        page, message.label, settings.attachments_dir / f"classification-{index}",
                    )
                    body = read_message_body(page)
                    result["body_digest"] = sha256(body.encode("utf-8")).hexdigest()
                    if not paths:
                        result["errors"].append("No supported attachments downloaded")
                    items = []
                    for path in paths:
                        if path.stat().st_size > MAX_FILE_BYTES:
                            result["errors"].append(f"{path.name}: larger than 10 MB")
                            path.unlink()
                            continue
                        extracted, issues = _extract_file(path, ocr, model)
                        result["errors"].extend(f"{i['source']}: {i['error']}" for i in issues)
                        for candidate, rejected, error in extracted:
                            # Include archive name in identity, without merging different ZIPs.
                            candidate = _with_archive_path(candidate, path)
                            items.append(ReviewItem(candidate, subject, rejected, error))
                    items = _prefer_xml_candidates(items)
                    if include_review_items:
                        result["review_items"] = items
                    reviewed = []
                    if register and items:
                        window = InvoiceReviewWindow(
                            items, [{"error": error} for error in result["errors"]], settings,
                        )
                        if window.run():
                            reviewed = window.approved_invoices
                        if len(reviewed) != len(items):
                            result["errors"].append(
                                "Not all invoices in this email were approved and registered"
                            )
                        result["registered_invoices"] = reviewed
                    statuses = []
                    for position, item in enumerate(items):
                        c = item.candidate
                        record = reviewed[position] if len(reviewed) == len(items) else c
                        status = store.registration_status(
                            record.number, record.legal_name, record.issued, record.amount, record.currency,
                        )
                        statuses.append(status)
                        fields = subject.split(";")
                        if len(fields) >= 4 and c.number and fields[2].strip() != c.number:
                            result["errors"].append(
                                f"Invoice number differs between subject and attachment: {c.number}"
                            )
                        if item.model_error:
                            result["errors"].append(item.model_error)
                        if item.rejected_model_fields:
                            result["errors"].append(
                                "Rejected model fields: " + ", ".join(item.rejected_model_fields)
                            )
                        result["invoices"].append({
                            "source": c.source, "number": record.number,
                            "supplier": record.legal_name, "status": status,
                        })
                    incoming = IncomingMessage(
                        sender, subject,
                        items[0].candidate.legal_name if items else "",
                        body=body,
                        invoices=tuple({
                            "supplier": item.candidate.legal_name,
                            "descriptions": "\n".join(item.candidate.descriptions),
                            "evidence": item.candidate.evidence
                            if item.candidate.method != "xml" else "",
                        } for item in items),
                    )
                    result["classification_context"] = asdict(bounded_context(incoming))
                    suggestion = suggest_folder(
                        incoming, folders, history, settings.folder_rules, model, confirmed,
                    )
                    result.update(folder=suggestion.folder, reason=suggestion.reason, source=suggestion.source)
                    result["eligible"] = classification_ready(statuses, result["errors"])
                except (LookupError, PlaywrightError, OSError, ValueError, LocalModelError) as error:
                    result["errors"].append(str(error))
                    result["eligible"] = False
        finally:
            context.close()
    return {
        "simulation": True, "moved": 0, "folders": folders,
        "registration_requested": register,
        "authorized_history": authorized_history, "history_examples": len(history),
        "confirmed_examples": len(confirmed),
        "messages": results,
    }


def _with_archive_path(candidate: InvoiceCandidate, path: Path) -> InvoiceCandidate:
    from dataclasses import replace

    if ":" in candidate.source:
        return replace(candidate, source=f"{path}:{candidate.source.split(':', 1)[1]}")
    return candidate
