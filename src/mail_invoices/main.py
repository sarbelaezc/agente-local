from __future__ import annotations

import argparse
import sqlite3
import sys
import traceback
from pathlib import Path

from mail_invoices import workflow
from mail_invoices.graph_mailbox import MailboxError
from mail_invoices.settings import load_settings
from shared.local_model import LocalModelError
from shared.run_log import run_log
from shared.storage import data_root

# Read-only diagnostics print invoice fields, senders or subjects: their log keeps
# only progress, stops and the result, never that report.
REPORT_ONLY_ON_SCREEN = {"classify", "scan-inbox", "analyze-file"}


def invoice_data_dir() -> Path:
    return data_root() / "MailInvoiceAgent"


def _command_name(args: argparse.Namespace) -> str:
    if args.process_invoice_batch:
        return "batch"
    if args.resume_invoice_batch:
        return "resume"
    if args.login:
        return "login"
    if args.classify_invoices:
        return "classify"
    if args.scan_inbox:
        return "scan-inbox"
    if args.cleanup_batches:
        return "cleanup"
    return "analyze-file"


def main() -> int:
    parser = argparse.ArgumentParser(description="Human-reviewed Hotmail invoice organizer")
    parser.add_argument("--config", type=Path, default=Path("config") / "settings.yaml")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--process-invoice-batch", action="store_true", help="Review and approve a small E2E batch.")
    mode.add_argument("--resume-invoice-batch", action="store_true", help="Resume local pending batches without rescanning.")
    mode.add_argument("--login", action="store_true", help="Sign in to the invoice mailbox (Microsoft Graph)")
    mode.add_argument(
        "--classify-invoices", action="store_true",
        help="Simulate folder classification and verify original Excel; never moves mail.",
    )
    mode.add_argument(
        "--analyze-file", type=Path, metavar="PATH",
        help="Extract invoice data from a local XML, PDF or ZIP (read-only)",
    )
    mode.add_argument(
        "--scan-inbox", action="store_true",
        help="List invoice candidates in the inbox (read-only, opens no message)",
    )
    mode.add_argument(
        "--cleanup-batches", action="store_true",
        help="Report (or with --apply delete) archived, excluded and retired batch records",
    )
    parser.add_argument(
        "--older-than-days", type=int,
        help="With --cleanup-batches, archived messages older than this are deleted (0-3650, default 30)",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="With --cleanup-batches, back up and delete; without it only a preview",
    )
    parser.add_argument(
        "--batch-size", type=int, default=10, help="Controlled E2E batch size (1-20, default 10)",
    )
    parser.add_argument(
        "--history-folder", action="append", default=[],
        help="Authorize metadata history from an exact folder path (classification only).",
    )
    parser.add_argument(
        "--candidate-text",
        help="Only classify inbox candidates whose list preview contains this text.",
    )
    parser.add_argument(
        "--days", type=int, default=90,
        help="How many days back inbox modes look (1-365, default 90)",
    )
    parser.add_argument(
        "--no-model", action="store_true", help="Skip the local Ollama field completion"
    )
    parser.add_argument(
        "--lan", action="store_true",
        help="With a batch, also open the review panel to paired devices of the home network (HTTPS)",
    )
    parser.add_argument(
        "--log-file", type=Path,
        help="This run's log inside the logs folder of the invoice data (the launcher sets it)",
    )
    args = parser.parse_args()
    if not 1 <= args.days <= 365:
        parser.error("--days must be between 1 and 365")
    if not 1 <= args.batch_size <= 20:
        parser.error("--batch-size must be between 1 and 20")
    if args.lan and not (args.process_invoice_batch or args.resume_invoice_batch):
        parser.error("--lan only applies to --process-invoice-batch and --resume-invoice-batch")
    if args.history_folder and not args.classify_invoices:
        parser.error("--history-folder requires --classify-invoices")
    if args.candidate_text is not None and not args.classify_invoices:
        parser.error("--candidate-text requires --classify-invoices")
    if (args.apply or args.older_than_days is not None) and not args.cleanup_batches:
        parser.error("--apply and --older-than-days require --cleanup-batches")
    if args.older_than_days is None:
        args.older_than_days = 30
    if not 0 <= args.older_than_days <= 3650:
        parser.error("--older-than-days must be between 0 and 3650")
    command = _command_name(args)
    try:
        with run_log(
            invoice_data_dir(), command, args.log_file,
            feature="facturas", capture_output=command not in REPORT_ONLY_ON_SCREEN,
        ):
            return _run_reporting(args)
    except ValueError as exc:  # an invalid --log-file, before anything ran
        print(f"Detenido: {exc}", file=sys.stderr)
        return 1


def _run_reporting(args: argparse.Namespace) -> int:
    """Run a command and report failures without message bodies; the run log keeps them."""
    try:
        code = _run(args)
    except KeyboardInterrupt:
        print("Detenido por el usuario.", file=sys.stderr)
        return 130
    except (ValueError, OSError, LookupError, sqlite3.Error, MailboxError, LocalModelError) as exc:
        print(f"Detenido: {exc}", file=sys.stderr)
        frames = [
            frame for frame in traceback.extract_tb(exc.__traceback__)
            if Path(frame.filename).parent.name in {"mail_invoices", "shared"}
        ]
        for frame in frames[-2:]:
            print(f"Punto de parada: {Path(frame.filename).name}:{frame.lineno}.", file=sys.stderr)
        return 1
    print(f"Resultado: {'sin errores' if code == 0 else f'con pendientes o errores (codigo {code})'}.", file=sys.stderr)
    return code


def _run(args: argparse.Namespace) -> int:
    settings = load_settings(args.config)
    if args.process_invoice_batch or args.resume_invoice_batch:
        from mail_invoices.batch_workflow import process_invoice_batch

        return process_invoice_batch(
            settings, args.days, not args.no_model, args.batch_size, resume=args.resume_invoice_batch,
            lan=args.lan,
        )
    if args.classify_invoices:
        return workflow.classify_invoices(
            settings, args.days, args.history_folder, not args.no_model, args.candidate_text,
        )
    if args.login:
        return workflow.login(settings)
    if args.scan_inbox:
        return workflow.scan_inbox(settings, args.days)
    if args.cleanup_batches:
        from mail_invoices.batch_cleanup import cleanup_batches

        return cleanup_batches(settings, args.older_than_days, args.apply)
    return workflow.analyze_file(settings, args.analyze_file, not args.no_model)


if __name__ == "__main__":
    raise SystemExit(main())
