from __future__ import annotations

import time
from pathlib import Path
from tempfile import TemporaryDirectory

from mail_invoices.assist import complete_candidate, describe, to_json
from mail_invoices.browser import outlook_page, reopen_outlook
from mail_invoices.documents import (
    MAX_FILE_BYTES,
    DocumentError,
    InvoiceCandidate,
    parse_document,
)
from mail_invoices.local_model import LocalModelError, OllamaJsonModel
from mail_invoices.inbox import MESSAGE_SELECTOR
from mail_invoices.ocr import TesseractOcr
from mail_invoices.review import ReviewItem
from mail_invoices.review_ui import run_invoice_review
from mail_invoices.settings import MailSettings
from mail_invoices.workbook import InvoiceWorkbook

OPERA_EXECUTABLE_ERROR = "Configured Opera executable does not exist"


def process_one_invoice(settings: MailSettings, days: int, use_model: bool) -> int:
    from dataclasses import replace
    from playwright.sync_api import sync_playwright, Error as PlaywrightError
    from mail_invoices.classification import simulate_classification
    from mail_invoices.classification_ui import review_classification
    from mail_invoices.classification_decisions import ClassificationDecisionStore, decisions_path
    from mail_invoices.movement import approve_movement, move_registered_message

    report = simulate_classification(
        replace(settings, batch_limit=1), days, [], use_model, register=True,
    )
    review_classification(report, ClassificationDecisionStore(decisions_path()), settings.approver)
    for message in report["messages"]:
        if not message["eligible"]:
            continue
        folder = approve_movement(message, report["folders"])
        if folder is None:
            continue
        try:
            store = InvoiceWorkbook(settings.workbook, settings.backup_dir, settings.sheet)
            registered = message.get("registered_invoices", [])
            if not registered or any(
                store.registration_status(
                    invoice.number, invoice.legal_name, invoice.issued, invoice.amount, invoice.currency,
                    company=invoice.company,
                ) != "registered" for invoice in registered
            ):
                raise ValueError("Excel registration changed after approval; movement blocked")
            with sync_playwright() as playwright:
                context = playwright.chromium.launch_persistent_context(
                    str(settings.profile_dir), executable_path=str(settings.executable),
                    headless=False, accept_downloads=True,
                )
                try:
                    page = outlook_page(context)
                    page.wait_for_selector(MESSAGE_SELECTOR, timeout=60000)
                    move_registered_message(page, message, folder, approved=True)
                    report["moved"] = 1
                    report["simulation"] = False
                    message["moved_folder"] = folder
                finally:
                    context.close()
        except (LookupError, PlaywrightError, OSError, ValueError) as error:
            message["errors"].append(
                f"Movement not verified; inspect Outlook before retrying: {error}"
            )
    for message in report["messages"]:
        message.pop("classification_context", None)
        message.pop("registered_invoices", None)
    if any(message.get("movement_attempted") for message in report["messages"]):
        report["simulation"] = False
    print(to_json(report))
    return 0 if report["moved"] == 1 else 1


def classify_invoices(
    settings: MailSettings, days: int, authorized_history: list[str], use_model: bool,
    candidate_text: str | None = None,
) -> int:
    """Simulate classification, review destinations, and print the read-only report."""
    from mail_invoices.classification import simulate_classification
    from mail_invoices.classification_ui import review_classification
    from mail_invoices.classification_decisions import ClassificationDecisionStore, decisions_path

    report = simulate_classification(
        settings, days, authorized_history, use_model, candidate_text=candidate_text,
    )
    review_classification(report, ClassificationDecisionStore(decisions_path()), settings.approver)
    for message in report["messages"]:
        message.pop("classification_context", None)
    print(to_json(report))
    return 1 if any(item["errors"] for item in report["messages"]) else 0


def login(settings: MailSettings) -> int:
    from playwright.sync_api import sync_playwright

    if not settings.executable.is_file():
        raise FileNotFoundError(OPERA_EXECUTABLE_ERROR)
    settings.profile_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(settings.profile_dir),
            executable_path=str(settings.executable),
            headless=False,
            accept_downloads=True,
        )
        try:
            outlook_page(context)
            print(
                "Sign in to your personal Hotmail account manually. "
                "Close this Opera window when the inbox is available. "
                "No messages are opened or modified in login mode.",
                flush=True,
            )
            while context.pages:
                time.sleep(1)
        finally:
            context.close()
    return 0


def _extract_file(
    path: Path, ocr: TesseractOcr, model: OllamaJsonModel | None
) -> tuple[list[tuple[InvoiceCandidate, tuple[str, ...], str | None]], list[dict]]:
    """Extracts candidates and model-review metadata from one local file."""
    try:
        candidates = parse_document(path.read_bytes(), path.name, ocr)
    except DocumentError as exc:
        return [], [{"source": path.name, "error": str(exc), "requires_review": True}]
    results: list[tuple[InvoiceCandidate, tuple[str, ...], str | None]] = []
    for candidate in candidates:
        rejected: tuple[str, ...] = ()
        model_error = None
        if model is not None:
            try:
                assisted = complete_candidate(candidate, model)
                candidate, rejected = assisted.candidate, assisted.rejected
            except LocalModelError as exc:
                model_error = str(exc)
        results.append((candidate, rejected, model_error))
    return results, []


def _summarize_file(path: Path, ocr: TesseractOcr, model: OllamaJsonModel | None) -> list[dict]:
    """Extracts candidates from one local file; errors are returned as review entries."""
    extracted, results = _extract_file(path, ocr, model)
    for candidate, rejected, model_error in extracted:
        summary = describe(candidate)
        summary["rejected_model_fields"] = list(rejected)
        if model_error:
            summary["model_error"] = model_error
        results.append(summary)
    return results


def _build_ocr(settings: MailSettings) -> TesseractOcr:
    return TesseractOcr(settings.tesseract_executable, settings.tessdata_dir, settings.ocr_languages)


def analyze_file(settings: MailSettings, path: Path, use_model: bool) -> int:
    """Read-only analysis of a local attachment: never writes Excel or touches mail."""
    if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
        raise SystemExit("The attachment must be an existing file of at most 10 MB")
    model = OllamaJsonModel(settings.ollama_url, settings.ollama_model) if use_model else None
    results = _summarize_file(path, _build_ocr(settings), model)
    print(to_json(results))
    return 1 if any("error" in item for item in results) else 0


def download_and_analyze(settings: MailSettings, days: int, use_model: bool) -> int:
    """Downloads invoice attachments from candidate messages, then analyzes them read-only.

    Opening a candidate marks it as read in Outlook. Downloaded files are temporary.
    """
    settings.attachments_dir.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="download-run-", dir=settings.attachments_dir) as run_dir:
        downloads, skipped = _download_batch(settings, days, Path(run_dir))
        model = OllamaJsonModel(settings.ollama_url, settings.ollama_model) if use_model else None
        ocr = _build_ocr(settings)
        results = list(skipped)
        for label, path in downloads:
            for item in _summarize_file(path, ocr, model):
                item["message"] = label
                results.append(item)
        print(to_json(results))
    return 0


def _download_batch(
    settings: MailSettings, days: int, target_dir: Path,
) -> tuple[list[tuple[str, Path]], list[dict]]:
    """Downloads supported attachments while collecting per-message failures."""
    from datetime import date

    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    from mail_invoices.inbox import MESSAGE_SELECTOR, download_attachments, scan_inbox

    target_dir.mkdir(parents=True, exist_ok=True)
    if not settings.executable.is_file():
        raise FileNotFoundError(OPERA_EXECUTABLE_ERROR)
    settings.profile_dir.mkdir(parents=True, exist_ok=True)
    downloads: list[tuple[str, Path]] = []
    skipped: list[dict] = []
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(settings.profile_dir),
            executable_path=str(settings.executable),
            headless=False,
            accept_downloads=True,
        )
        try:
            page = outlook_page(context)
            page.wait_for_selector(MESSAGE_SELECTOR, timeout=60000)
            candidates = scan_inbox(page, days, date.today())[: settings.batch_limit]
            for message in candidates:
                reopen_outlook(page, MESSAGE_SELECTOR)
                try:
                    paths = download_attachments(
                        page, message.label, target_dir
                    )
                except (LookupError, PlaywrightError) as error:
                    skipped.append(
                        {
                            "source": message.label[:80],
                            "error": str(error),
                            "requires_review": True,
                        }
                    )
                    continue
                for path in paths:
                    if path.stat().st_size > MAX_FILE_BYTES:
                        skipped.append(
                            {
                                "source": path.name,
                                "error": "larger than 10 MB",
                                "requires_review": True,
                            }
                        )
                        path.unlink()
                        continue
                    downloads.append((message.label[:200], path))
        finally:
            context.close()
    return downloads, skipped


def _prefer_xml_candidates(items: list[ReviewItem]) -> list[ReviewItem]:
    """Keeps the XML representation when a ZIP contains its matching PDF."""
    selected = []
    zip_candidates: dict[tuple[str, str], int] = {}
    for item in items:
        archive, separator, _entry = item.candidate.source.rpartition(":")
        number = item.candidate.number
        if not separator or not number:
            selected.append(item)
            continue
        key = (archive, number.casefold())
        position = zip_candidates.get(key)
        if position is None:
            zip_candidates[key] = len(selected)
            selected.append(item)
        elif (
            item.candidate.method == "xml"
            and selected[position].candidate.method != "xml"
        ):
            selected[position] = item
    return selected


def review_invoices(settings: MailSettings, days: int, use_model: bool) -> int:
    """Downloads, analyzes, and requests explicit approval before recording a batch."""
    settings.attachments_dir.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="review-run-", dir=settings.attachments_dir) as run_dir:
        downloads, issues = _download_batch(settings, days, Path(run_dir))
        model = OllamaJsonModel(settings.ollama_url, settings.ollama_model) if use_model else None
        ocr = _build_ocr(settings)
        review_items = []
        for message, path in downloads:
            extracted, file_issues = _extract_file(path, ocr, model)
            for issue in file_issues:
                issue["message"] = message
            issues.extend(file_issues)
            review_items.extend(
                ReviewItem(candidate, message, rejected, model_error)
                for candidate, rejected, model_error in extracted
            )
        review_items = _prefer_xml_candidates(review_items)
        if not review_items:
            print(to_json({"saved": False, "issues": issues}))
            return 1
        saved = run_invoice_review(review_items, issues, settings)
        if not saved:
            print(to_json({"saved": False, "message": "El lote se cerró sin modificar el Excel."}))
        return 0 if saved else 1


def scan_inbox(settings: MailSettings, days: int) -> int:
    """Read-only: lists invoice candidates from the inbox list without opening messages."""
    from datetime import date

    from playwright.sync_api import sync_playwright

    from mail_invoices.inbox import MESSAGE_SELECTOR, scan_inbox

    if not settings.executable.is_file():
        raise FileNotFoundError(OPERA_EXECUTABLE_ERROR)
    settings.profile_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(settings.profile_dir),
            executable_path=str(settings.executable),
            headless=False,
        )
        try:
            page = outlook_page(context)
            page.wait_for_selector(MESSAGE_SELECTOR, timeout=60000)
            candidates = scan_inbox(page, days, date.today())
        finally:
            context.close()
    print(
        to_json(
            [
                {
                    "unread": message.unread,
                    "received": message.received.isoformat() if message.received else None,
                    "preview": message.label[:300],
                }
                for message in candidates
            ]
        )
    )
    return 0
