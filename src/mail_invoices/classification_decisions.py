from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Sequence
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

from mail_invoices.folders import ConfirmedExample, IncomingMessage, bounded_context


def decisions_path() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        raise ValueError("LOCALAPPDATA is required for local classification decisions")
    return Path(local) / "MailInvoiceAgent" / "classification-decisions.sqlite3"


class ClassificationDecisionStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> list[ConfirmedExample]:
        if not self.path.exists():
            return []
        with sqlite3.connect(self.path) as connection:
            rows = connection.execute(
                "SELECT context, folder FROM decisions ORDER BY confirmed_at DESC"
            ).fetchall()
        examples = []
        for context, folder in rows:
            raw = json.loads(context)
            message = IncomingMessage(
                raw["sender"], raw["subject"], raw["supplier"], raw["body"],
                tuple(raw["invoices"]),
            )
            examples.append(ConfirmedExample(bounded_context(message), folder))
        return examples

    def save(
        self, decisions: Sequence[ConfirmedExample], approver: str,
        available_folders: Sequence[str],
    ) -> int:
        approver = " ".join(approver.split())[:100]
        if not approver:
            raise ValueError("Indique quien confirma las decisiones")
        if not decisions:
            raise ValueError("Seleccione al menos una decision para confirmar")
        if any(example.folder not in available_folders for example in decisions):
            raise ValueError("Cada decision seleccionada requiere una carpeta existente")
        rows = []
        for example in decisions:
            context = json.dumps(
                asdict(bounded_context(example.message)), ensure_ascii=False, sort_keys=True,
            )
            rows.append((
                sha256(context.encode("utf-8")).hexdigest(), context, example.folder,
                approver, datetime.now(timezone.utc).isoformat(),
            ))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS decisions ("
                "identity TEXT PRIMARY KEY, context TEXT NOT NULL, folder TEXT NOT NULL, "
                "approver TEXT NOT NULL, confirmed_at TEXT NOT NULL)"
            )
            connection.executemany(
                "INSERT INTO decisions VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(identity) DO UPDATE SET folder=excluded.folder, "
                "approver=excluded.approver, confirmed_at=excluded.confirmed_at",
                rows,
            )
        return len(rows)
