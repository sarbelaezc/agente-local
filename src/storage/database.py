from __future__ import annotations

import sqlite3
from pathlib import Path


class AuditDatabase:
    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self._path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    chat_id TEXT NOT NULL,
                    chat_name TEXT NOT NULL,
                    action TEXT NOT NULL,
                    draft TEXT NOT NULL,
                    final_text TEXT,
                    operator TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS review_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT NOT NULL,
                    chat_name TEXT NOT NULL,
                    incoming_summary TEXT NOT NULL,
                    draft TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                )
                """
            )

    def record(
        self,
        chat_id: str,
        chat_name: str,
        action: str,
        draft: str,
        final_text: str | None = None,
        operator: str | None = None,
    ) -> None:
        with sqlite3.connect(self._path) as connection:
            connection.execute(
                "INSERT INTO audit (chat_id, chat_name, action, draft, final_text, operator) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (chat_id, chat_name, action, draft, final_text, operator),
            )
