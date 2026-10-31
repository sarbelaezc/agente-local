from __future__ import annotations

import argparse
import time
from pathlib import Path

from mail_invoices.assist import complete_candidate, describe, to_json
from mail_invoices.documents import MAX_FILE_BYTES, DocumentError, parse_document
from mail_invoices.local_model import LocalModelError, OllamaJsonModel
from mail_invoices.ocr import TesseractOcr
from mail_invoices.settings import MailSettings, load_settings


def _login(settings: MailSettings) -> int:
    from playwright.sync_api import sync_playwright

    if not settings.executable.is_file():
        raise FileNotFoundError("Configured Opera executable does not exist")
    settings.profile_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(settings.profile_dir),
            executable_path=str(settings.executable),
            headless=False,
            accept_downloads=True,
        )
        try:
            # Restored tabs from earlier runs are closed so only one Outlook tab remains.
            pages = [page for page in context.pages if page.url != "about:blank"]
            page = pages[0] if pages else context.new_page()
            for extra in context.pages:
                if extra is not page:
                    extra.close()
            page.goto("https://outlook.live.com/mail/0/", wait_until="domcontentloaded")
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


def _analyze_file(settings: MailSettings, path: Path, use_model: bool) -> int:
    """Read-only analysis of a local attachment: never writes Excel or touches mail."""
    if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
        raise SystemExit("The attachment must be an existing file of at most 10 MB")
    ocr = TesseractOcr(
        settings.tesseract_executable, settings.tessdata_dir, settings.ocr_languages
    )
    try:
        candidates = parse_document(path.read_bytes(), path.name, ocr)
    except DocumentError as exc:
        print(to_json({"source": path.name, "error": str(exc), "requires_review": True}))
        return 1
    model = OllamaJsonModel(settings.ollama_url, settings.ollama_model) if use_model else None
    results = []
    for candidate in candidates:
        rejected: tuple[str, ...] = ()
        model_error = None
        if model is not None:
            try:
                assisted = complete_candidate(candidate, model)
                candidate, rejected = assisted.candidate, assisted.rejected
            except LocalModelError as exc:
                model_error = str(exc)
        summary = describe(candidate)
        summary["rejected_model_fields"] = list(rejected)
        if model_error:
            summary["model_error"] = model_error
        results.append(summary)
    print(to_json(results))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Human-reviewed Hotmail invoice organizer")
    parser.add_argument("--config", type=Path, default=Path("config") / "mail-invoices.yaml")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--login", action="store_true", help="Open Outlook for a manual sign-in")
    mode.add_argument(
        "--analyze-file", type=Path, metavar="PATH",
        help="Extract invoice data from a local XML, PDF or ZIP (read-only)",
    )
    parser.add_argument(
        "--no-model", action="store_true", help="Skip the local Ollama field completion"
    )
    args = parser.parse_args()
    settings = load_settings(args.config)
    if args.login:
        return _login(settings)
    return _analyze_file(settings, args.analyze_file, not args.no_model)


if __name__ == "__main__":
    raise SystemExit(main())
