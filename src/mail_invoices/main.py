from __future__ import annotations

import argparse
import time
from pathlib import Path

from mail_invoices.assist import complete_candidate, describe, to_json
from mail_invoices.browser import outlook_page, reopen_outlook
from mail_invoices.documents import (
    MAX_FILE_BYTES,
    DocumentError,
    InvoiceCandidate,
    parse_document,
)
from mail_invoices.local_model import LocalModelError, OllamaJsonModel
from mail_invoices.ocr import TesseractOcr
from mail_invoices.review_ui import ReviewItem, run_invoice_review
from mail_invoices.settings import MailSettings, load_settings

OPERA_EXECUTABLE_ERROR = "Configured Opera executable does not exist"


def _login(settings: MailSettings) -> int:
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


def _analyze_file(settings: MailSettings, path: Path, use_model: bool) -> int:
    """Read-only analysis of a local attachment: never writes Excel or touches mail."""
    if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
        raise SystemExit("The attachment must be an existing file of at most 10 MB")
    model = OllamaJsonModel(settings.ollama_url, settings.ollama_model) if use_model else None
    results = _summarize_file(path, _build_ocr(settings), model)
    print(to_json(results))
    return 1 if any("error" in item for item in results) else 0


def _download_attachments(settings: MailSettings, days: int, use_model: bool) -> int:
    """Downloads invoice attachments from candidate messages, then analyzes them read-only.

    Opening a candidate marks it as read in Outlook. Files are kept in attachments_dir.
    """
    downloads, skipped = _download_batch(settings, days)
    model = OllamaJsonModel(settings.ollama_url, settings.ollama_model) if use_model else None
    ocr = _build_ocr(settings)
    results = list(skipped)
    for label, path in downloads:
        for item in _summarize_file(path, ocr, model):
            item["message"] = label
            results.append(item)
    print(to_json(results))
    return 0


def _download_batch(settings: MailSettings, days: int) -> tuple[list[tuple[str, Path]], list[dict]]:
    """Downloads supported attachments while collecting per-message failures."""
    from datetime import date

    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    from mail_invoices.inbox import MESSAGE_SELECTOR, download_attachments, scan_inbox

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
                        page, message.label, settings.attachments_dir
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


def _review_invoices(settings: MailSettings, days: int, use_model: bool) -> int:
    """Downloads, analyzes, and requests explicit approval before recording a batch."""
    downloads, issues = _download_batch(settings, days)
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


def _scan_inbox(settings: MailSettings, days: int) -> int:
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Human-reviewed Hotmail invoice organizer")
    parser.add_argument("--config", type=Path, default=Path("config") / "settings.yaml")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--login", action="store_true", help="Open Outlook for a manual sign-in")
    mode.add_argument(
        "--analyze-file", type=Path, metavar="PATH",
        help="Extract invoice data from a local XML, PDF or ZIP (read-only)",
    )
    mode.add_argument(
        "--scan-inbox", action="store_true",
        help="List invoice candidates in the inbox list (read-only, opens no message)",
    )
    mode.add_argument(
        "--download-attachments", action="store_true",
        help="Download candidate invoice attachments to attachments_dir and analyze them. "
        "Opening a message marks it as read.",
    )
    mode.add_argument(
        "--review-invoices", action="store_true",
        help="Download and analyze candidates, then require explicit human approval before saving to Excel.",
    )
    parser.add_argument(
        "--days", type=int, default=30,
        help="How many days back inbox modes look (1-365, default 30)",
    )
    parser.add_argument(
        "--no-model", action="store_true", help="Skip the local Ollama field completion"
    )
    args = parser.parse_args()
    if not 1 <= args.days <= 365:
        parser.error("--days must be between 1 and 365")
    settings = load_settings(args.config)
    if args.login:
        return _login(settings)
    if args.scan_inbox:
        return _scan_inbox(settings, args.days)
    if args.download_attachments:
        return _download_attachments(settings, args.days, not args.no_model)
    if args.review_invoices:
        return _review_invoices(settings, args.days, not args.no_model)
    return _analyze_file(settings, args.analyze_file, not args.no_model)


if __name__ == "__main__":
    raise SystemExit(main())
