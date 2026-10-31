from __future__ import annotations

import json
import msvcrt
import sqlite3
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

from mail_invoices.classification_decisions import decisions_path
from mail_invoices.documents import InvoiceCandidate
from mail_invoices.folders import IncomingMessage
from mail_invoices.review import ReviewItem
from mail_invoices.settings import MailSettings
from mail_invoices.workbook import ReviewedInvoice

STATES = {"pending", "approved", "registered", "moving", "moved", "blocked"}


def batch_state_path() -> Path:
    return decisions_path().with_name("invoice-batches.sqlite3")


def batch_scope(settings: MailSettings) -> str:
    identity = f"{settings.profile_dir.resolve()}\n{settings.workbook.resolve()}\n{settings.sheet}"
    return sha256(identity.casefold().encode("utf-8")).hexdigest()


@contextmanager
def batch_lock(path: Path) -> Generator[None]:
    """OS lock released even if the process crashes; never delete the lock file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            raise ValueError("Ya hay un lote de facturas en ejecucion") from error
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def _json_default(value: object) -> str:
    if isinstance(value, (date, Decimal)):
        return str(value)
    raise TypeError(f"Unsupported persisted value: {type(value).__name__}")


@dataclass
class BatchMessage:
    conversation: str
    sender: str
    subject: str
    items: list[ReviewItem]
    context: IncomingMessage
    errors: list[str] = field(default_factory=list)
    folder: str | None = None
    reason: str = ""
    state: str = "pending"
    invoices: list[ReviewedInvoice] = field(default_factory=list)
    approver: str = ""
    approved_at: str = ""
    last_error: str = ""
    body_digest: str = ""

    @classmethod
    def from_report(cls, message: dict) -> BatchMessage:
        identity = message.get("conversation")
        if not identity:
            raise ValueError("Cannot persist a message without a stable conversation identity")
        context = message.get("classification_context", {})
        errors = list(message["errors"])
        if len(message.get("review_items", [])) > 10:
            errors.append("Mas de 10 facturas: fuera del limite del lote controlado")
        return cls(
            identity, message["sender"], message["subject"], [
                replace(item, candidate=replace(
                    item.candidate, evidence="", source=Path(item.candidate.source).name,
                )) for item in message.get("review_items", [])
            ],
            IncomingMessage(
                message["sender"], message["subject"], context.get("supplier", ""),
                context.get("body", ""), tuple(context.get("invoices", [])),
            ),
            errors=errors, folder=message.get("folder"),
            reason=message.get("reason", ""),
            body_digest=message.get("body_digest", ""),
        )


def _decode(payload: str) -> BatchMessage:
    raw = json.loads(payload)
    items = []
    for item in raw["items"]:
        candidate = item["candidate"]
        candidate["issued"] = date.fromisoformat(candidate["issued"]) if candidate["issued"] else None
        candidate["amount"] = Decimal(candidate["amount"]) if candidate["amount"] is not None else None
        candidate["descriptions"] = tuple(candidate["descriptions"])
        candidate["assisted_fields"] = tuple(candidate["assisted_fields"])
        items.append(ReviewItem(
            InvoiceCandidate(**candidate), item["message"],
            tuple(item["rejected_model_fields"]), item["model_error"],
        ))
    raw["items"] = items
    context = raw["context"]
    context["invoices"] = tuple(context["invoices"])
    raw["context"] = IncomingMessage(**context)
    for invoice in raw["invoices"]:
        invoice["issued"] = date.fromisoformat(invoice["issued"])
        invoice["amount"] = Decimal(invoice["amount"])
    raw["invoices"] = [ReviewedInvoice(**invoice) for invoice in raw["invoices"]]
    return BatchMessage(**raw)


class BatchStateStore:
    def __init__(self, path: Path, scope: str):
        self.path, self.scope = path, scope

    def load(self) -> list[BatchMessage]:
        if not self.path.exists():
            return []
        with sqlite3.connect(self.path) as connection:
            rows = connection.execute(
                "SELECT payload FROM messages WHERE scope=? ORDER BY updated_at, conversation",
                (self.scope,),
            ).fetchall()
        return [_decode(row[0]) for row in rows]

    def save(self, messages: Sequence[BatchMessage]) -> None:
        for message in messages:
            if not message.conversation or message.state not in STATES:
                raise ValueError("Invalid persisted conversation or state")
            if message.state in {"approved", "registered", "moving", "moved"}:
                if not message.approver.strip() or not message.folder or not message.invoices:
                    raise ValueError("Executable state requires approved invoices and destination")
                if message.errors or len(message.invoices) != len(message.items):
                    raise ValueError("Executable state requires all invoices and no extraction errors")
                for invoice in message.invoices:
                    invoice.validate()
        rows = [
            (
                self.scope, message.conversation,
                json.dumps(asdict(message), default=_json_default, ensure_ascii=False),
                datetime.now(timezone.utc).isoformat(),
            )
            for message in messages
        ]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS messages (scope TEXT NOT NULL, "
                "conversation TEXT NOT NULL, payload TEXT NOT NULL, updated_at TEXT NOT NULL, "
                "PRIMARY KEY(scope, conversation))"
            )
            connection.executemany(
                "INSERT INTO messages VALUES (?, ?, ?, ?) ON CONFLICT(scope, conversation) "
                "DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at",
                rows,
            )
