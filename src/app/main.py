from __future__ import annotations

import argparse
import signal

from app.approval_ui import ApprovalWindow
from automation.edge_session import EdgeSession
from config.settings import load_settings
from storage.database import AuditDatabase


def main() -> int:
    parser = argparse.ArgumentParser(description="Local Teams drafting agent")
    parser.add_argument("--config", default="config/settings.yaml")
    parser.add_argument(
        "--process", choices=("draft", "review", "login"), default="draft"
    )
    args = parser.parse_args()

    settings = load_settings(args.config)
    if args.process == "login":
        session = EdgeSession(settings.edge_executable, settings.edge_profile_dir)
        try:
            context = session.start()
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(settings.teams_url, wait_until="domcontentloaded")
            print(
                "Complete Teams sign-in manually in Edge, then close the Edge window. "
                "No chats will be opened.",
                flush=True,
            )
            context.wait_for_event("close", timeout=0)
        finally:
            session.close()
        return 0

    database = AuditDatabase(settings.database_path)
    stopping = {"value": False}

    def stop_agent(_signum: object, _frame: object) -> None:
        stopping["value"] = True

    signal.signal(signal.SIGINT, stop_agent)
    window = ApprovalWindow(settings=settings, database=database, stopping=stopping)
    window.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
