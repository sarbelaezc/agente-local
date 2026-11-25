from __future__ import annotations

from datetime import date
from dataclasses import asdict, replace
from pathlib import Path
from tempfile import TemporaryDirectory

from mail_invoices.documents import MAX_FILE_BYTES, InvoiceCandidate, rendered_xml
from mail_invoices.folders import IncomingMessage, bounded_context, suggest_folder
from mail_invoices.classification_decisions import ClassificationDecisionStore, decisions_path
from mail_invoices.graph_mailbox import (
    discover_folders, filter_candidates, open_mailbox, read_history, scan_candidates,
)
from shared.local_model import LocalModelError, OllamaJsonModel
from mail_invoices.settings import MailSettings, is_excluded_sender
from mail_invoices.workbook import InvoiceWorkbook


def classification_ready(statuses: list[str], errors: list[str]) -> bool:
    return bool(statuses) and not errors and all(status == "registered" for status in statuses)


def simulate_classification(
    settings: MailSettings, days: int, authorized_history: list[str], use_model: bool,
    *, include_review_items: bool = False,
    candidate_text: str | None = None,
    attachment_dir: Path | None = None,
    mailbox=None,
) -> dict:
    """Run classification with attachments isolated to a cleaned temporary directory.

    A supplied ``mailbox`` reuses an already open Graph session.
    """
    if attachment_dir is not None:
        attachment_dir.mkdir(parents=True, exist_ok=True)
        return _simulate_classification(
            replace(settings, attachments_dir=attachment_dir), days, authorized_history, use_model,
            include_review_items=include_review_items, candidate_text=candidate_text, mailbox=mailbox,
        )
    settings.attachments_dir.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="classification-run-", dir=settings.attachments_dir) as run_dir:
        return _simulate_classification(
            replace(settings, attachments_dir=Path(run_dir)), days, authorized_history, use_model,
            include_review_items=include_review_items, candidate_text=candidate_text, mailbox=mailbox,
        )


def _simulate_classification(
    settings: MailSettings, days: int, authorized_history: list[str], use_model: bool,
    *, include_review_items: bool = False,
    candidate_text: str | None = None,
    mailbox=None,
) -> dict:
    """Suggest destinations and verify Excel read-only. No move command is invoked."""
    if mailbox is not None:
        return _classify(
            mailbox, settings, days, authorized_history, use_model,
            include_review_items=include_review_items, candidate_text=candidate_text,
        )
    with open_mailbox(settings) as mailbox:
        return _classify(
            mailbox, settings, days, authorized_history, use_model,
            include_review_items=include_review_items, candidate_text=candidate_text,
        )


def _classify(
    mailbox, settings: MailSettings, days: int, authorized_history: list[str], use_model: bool,
    *, include_review_items: bool, candidate_text: str | None,
) -> dict:
    from mail_invoices.workflow import _build_ocr

    store = InvoiceWorkbook(settings.workbook, settings.backup_dir, settings.sheet)
    model = OllamaJsonModel(settings.ollama_url, settings.ollama_model) if use_model else None
    ocr = _build_ocr(settings)
    confirmed = ClassificationDecisionStore(decisions_path()).load()
    results = []
    folders = [folder.path for folder in discover_folders(mailbox)]
    history = read_history(mailbox, authorized_history)
    messages = scan_candidates(mailbox, days, date.today())
    if candidate_text is not None:
        messages = filter_candidates(messages, candidate_text)
    messages = [message for message in messages if not is_excluded_sender(message.sender, settings.excluded_senders)]
    messages = messages[:settings.batch_limit]
    for index, message in enumerate(messages, 1):
        result = {
            "sender": "", "subject": "", "invoices": [],
            "errors": [], "eligible": False, "folder": None,
        }
        results.append(result)
        try:
            detail = mailbox.message(message.id)
            sender, subject, body = detail.sender, detail.subject, detail.body
            result.update(sender=sender, subject=subject, conversation=detail.id)
            # Separate message directories prevent same-name attachment overwrites.
            paths, download_errors = mailbox.download_attachments(
                detail.id, settings.attachments_dir / f"classification-{index}",
            )
            result["errors"].extend(download_errors)
            result["body_digest"] = detail.body_digest
            if not paths:
                result["errors"].append("No supported attachments downloaded")
            items = _review_items(paths, subject, ocr, model, result["errors"])
            if include_review_items:
                result["review_items"] = items
            statuses = []
            for item in items:
                c = item.candidate
                status = store.registration_status(
                    c.number, c.legal_name, c.issued, c.amount, c.currency,
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
                    "source": c.source, "number": c.number,
                    "supplier": c.legal_name, "status": status,
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
        # A MailboxError (session, Graph) is not caught: it stops the whole run.
        except (LookupError, OSError, ValueError, LocalModelError) as error:
            result["errors"].append(str(error))
            result["eligible"] = False
    return {
        "simulation": True, "moved": 0, "folders": folders,
        "authorized_history": authorized_history, "history_examples": len(history),
        "confirmed_examples": len(confirmed),
        "messages": results,
    }


def _review_items(paths: list[Path], subject: str, ocr, model, errors: list[str]) -> list:
    """The invoices found in a message's attachments; problems are added to errors.

    XML first: a loose PDF paired one-to-one with an XML is its graphic rendering,
    exactly as inside a ZIP, so it is not read, OCR'd or sent to Ollama.
    """
    from mail_invoices.workflow import _extract_file, _prefer_xml_candidates
    from mail_invoices.review import ReviewItem

    items = []
    xmls: list[tuple[str, set[str]]] = []
    claimed: set[int] = set()
    for path in sorted(paths, key=lambda item: item.suffix.lower() != ".xml"):
        if path.suffix.lower() == ".pdf":
            pair = rendered_xml(path.name, xmls)
            if pair is not None and pair not in claimed:
                claimed.add(pair)
                continue
        if path.stat().st_size > MAX_FILE_BYTES:
            errors.append(f"{path.name}: larger than 10 MB")
            path.unlink()
            continue
        extracted, issues = _extract_file(path, ocr, model)
        # Only an XML that yielded an invoice can make its PDF a mere rendering.
        if path.suffix.lower() == ".xml" and extracted:
            xmls.append((path.stem.lower(), {c.number.lower() for c, _, _ in extracted if c.number}))
        errors.extend(f"{i['source']}: {i['error']}" for i in issues)
        for candidate, rejected, error in extracted:
            # Include archive name in identity, without merging different ZIPs.
            items.append(ReviewItem(_with_archive_path(candidate, path), subject, rejected, error))
    return _prefer_xml_candidates(items)


def _with_archive_path(candidate: InvoiceCandidate, path: Path) -> InvoiceCandidate:
    source = f"{path.parent.name}/{path.name}"
    if ":" in candidate.source:
        source = f"{source}:{candidate.source.split(':', 1)[1]}"
    return replace(candidate, source=source)
