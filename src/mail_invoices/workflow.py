from __future__ import annotations

from pathlib import Path

from mail_invoices.assist import complete_candidate, describe, to_json
from mail_invoices.documents import (
    MAX_FILE_BYTES,
    DocumentError,
    InvoiceCandidate,
    parse_document,
)
from shared.local_model import LocalModelError, OllamaJsonModel
from mail_invoices.ocr import TesseractOcr
from mail_invoices.review import ReviewItem
from mail_invoices.settings import MailSettings

def classify_invoices(
    settings: MailSettings, days: int, authorized_history: list[str], use_model: bool,
    candidate_text: str | None = None,
) -> int:
    """Simulate classification, review destinations, and print the read-only report."""
    from mail_invoices.classification import simulate_classification
    from mail_invoices.classification_decisions import ClassificationDecisionStore, decisions_path

    report = simulate_classification(
        settings, days, authorized_history, use_model, candidate_text=candidate_text,
    )
    review_classification(report, ClassificationDecisionStore(decisions_path()), settings.approver)
    for message in report["messages"]:
        message.pop("classification_context", None)
    print(to_json(report))
    return 1 if any(item["errors"] for item in report["messages"]) else 0


def review_classification(report: dict, store, approver: str) -> None:
    """Web review of a classification run: saves only confirmed examples, never moves mail."""
    import sqlite3

    from mail_invoices.batch_review import classification_view, record_reviewed_folders, save_classification
    from mail_invoices.review_server import ReviewPanel

    def save(raw: dict) -> dict:
        try:
            return {"saved": save_classification(report, store, raw)}
        except (OSError, sqlite3.Error) as error:
            raise ValueError(f"No se guardaron las decisiones: {error}") from error

    def close(raw: dict) -> dict:
        record_reviewed_folders(report, raw.get("folders", {}))
        return {}

    panel = ReviewPanel(
        lambda: classification_view(report, store.path, approver),
        {"/api/save-decisions": save, "/api/close": close},
    )
    panel.run()
    panel.stop()


def login(settings: MailSettings) -> int:
    """Sign in to the invoice mailbox in the system browser; the agent never sees the password."""
    from mail_invoices.graph_mailbox import interactive_login, token_cache_path

    account = interactive_login(settings.graph_client_id, settings.graph_authority, token_cache_path())
    print(
        f"Sesion de Microsoft para facturas guardada para {account}. Permiso: Mail.ReadWrite; "
        "el agente solo lee correos y mueve los aprobados.",
        flush=True,
    )
    return 0


def _extract_file(
    path: Path, ocr: TesseractOcr, model: OllamaJsonModel | None
) -> tuple[list[tuple[InvoiceCandidate, tuple[str, ...], str | None]], list[dict]]:
    """Extracts candidates and model-review metadata from one local file."""
    member_errors: list[tuple[str, str]] = []
    try:
        candidates = parse_document(path.read_bytes(), path.name, ocr, member_errors)
    except DocumentError as exc:
        return [], [{"source": path.name, "error": str(exc), "requires_review": True}]
    issues = [
        {"source": source, "error": error, "requires_review": True}
        for source, error in member_errors
    ]
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
    return results, issues


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


def scan_inbox(settings: MailSettings, days: int) -> int:
    """Read-only: lists invoice candidates from the inbox without opening messages."""
    from datetime import date

    from mail_invoices.graph_mailbox import open_mailbox, scan_candidates

    with open_mailbox(settings) as mailbox:
        candidates = scan_candidates(mailbox, days, date.today())
    print(
        to_json(
            [
                {
                    "unread": message.unread,
                    "received": message.received.isoformat(),
                    "sender": message.sender,
                    "subject": message.subject[:300],
                }
                for message in candidates
            ]
        )
    )
    return 0
