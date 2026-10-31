from __future__ import annotations

import argparse
from pathlib import Path

from playwright.sync_api import sync_playwright

from mail_invoices.settings import load_settings


def main() -> int:
    parser = argparse.ArgumentParser(description="Human-reviewed Hotmail invoice organizer")
    parser.add_argument("--config", type=Path, default=Path("config") / "mail-invoices.yaml")
    parser.add_argument("--login", action="store_true", required=True)
    args = parser.parse_args()
    settings = load_settings(args.config)
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
            page = context.pages[0] if context.pages else context.new_page()
            page.goto("https://outlook.live.com/mail/0/", wait_until="domcontentloaded")
            print(
                "Sign in to your personal Hotmail account manually. "
                "Close this Opera window when the inbox is available. "
                "No messages are opened or modified in login mode.",
                flush=True,
            )
            context.wait_for_event("close", timeout=0)
        finally:
            context.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
