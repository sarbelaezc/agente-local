from __future__ import annotations

import argparse
from pathlib import Path

from mail_invoices import workflow
from mail_invoices.settings import load_settings


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
        return workflow.login(settings)
    if args.scan_inbox:
        return workflow.scan_inbox(settings, args.days)
    if args.download_attachments:
        return workflow.download_and_analyze(settings, args.days, not args.no_model)
    if args.review_invoices:
        return workflow.review_invoices(settings, args.days, not args.no_model)
    return workflow.analyze_file(settings, args.analyze_file, not args.no_model)


if __name__ == "__main__":
    raise SystemExit(main())
