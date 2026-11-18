"""Manual cleanup of the invoice batch states: forget what no longer needs to be kept.

Three groups are deleted, and nothing else:

- messages of the current configuration already archived (``moved``) more than
  ``older_than_days`` ago: Excel and the mailbox already hold the result;
- pending messages from an ``excluded_senders`` entry, which the panel never shows;
- every record of another configuration (the retired Opera reader, or another
  workbook or sheet), which the agent never acts on.

Approved, registered, moving and blocked messages of the current configuration are
never deleted. Without ``apply`` it only reports counts; with it, the database is
backed up first and compacted afterwards so the deleted rows do not linger in it.
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mail_invoices.batch_state import batch_lock, batch_scope, batch_state_path
from mail_invoices.settings import MailSettings, is_excluded_sender

MAX_RETENTION_DAYS = 3650


@dataclass
class CleanupPlan:
    archived: list[tuple[str, str]] = field(default_factory=list)
    excluded_pending: list[tuple[str, str]] = field(default_factory=list)
    other_configuration: list[tuple[str, str]] = field(default_factory=list)
    other_states: Counter = field(default_factory=Counter)
    kept: int = 0

    @property
    def keys(self) -> list[tuple[str, str]]:
        return self.archived + self.excluded_pending + self.other_configuration


def plan_cleanup(
    path: Path, scope: str, older_than_days: int, excluded: tuple[str, ...], now: datetime,
) -> CleanupPlan:
    """Choose the records to delete; reads only state, sender and update time."""
    if not 0 <= older_than_days <= MAX_RETENTION_DAYS:
        raise ValueError(f"Los dias de retencion deben estar entre 0 y {MAX_RETENTION_DAYS}")
    plan = CleanupPlan()
    if not path.exists():
        return plan
    cutoff = now - timedelta(days=older_than_days)
    with closing(sqlite3.connect(path)) as connection:
        rows = connection.execute("SELECT scope, conversation, payload, updated_at FROM messages").fetchall()
    for row_scope, conversation, payload, updated_at in rows:
        key = (row_scope, conversation)
        raw = json.loads(payload)
        state = raw.get("state") if isinstance(raw, dict) else None
        if row_scope != scope:
            plan.other_configuration.append(key)
            plan.other_states[str(state)] += 1
        elif state == "moved" and datetime.fromisoformat(updated_at) <= cutoff:
            plan.archived.append(key)
        elif state == "pending" and is_excluded_sender(str(raw.get("sender", "")), excluded):
            plan.excluded_pending.append(key)
        else:
            plan.kept += 1
    return plan


def apply_cleanup(path: Path, plan: CleanupPlan, now: datetime) -> Path:
    """Back up the database, delete the planned records and compact the file."""
    backup = path.with_name(f"{path.name}.backup-{now.astimezone():%Y%m%d-%H%M%S}")
    with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(backup)) as target:
        source.backup(target)
    with closing(sqlite3.connect(path)) as connection:
        with connection:
            before = connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            connection.executemany("DELETE FROM messages WHERE scope=? AND conversation=?", plan.keys)
            after = connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            if before - after != len(plan.keys):
                raise ValueError("La limpieza no borro exactamente lo previsto; no se aplico ningun cambio")
        connection.execute("VACUUM")
    return backup


def cleanup_batches(
    settings: MailSettings, older_than_days: int, apply: bool, now: datetime | None = None,
) -> int:
    """Report, and with ``apply`` perform, the cleanup of the invoice batch states."""
    moment = now or datetime.now(timezone.utc)
    path = batch_state_path()
    with batch_lock(path.with_suffix(".lock")):
        plan = plan_cleanup(path, batch_scope(settings), older_than_days, settings.excluded_senders, moment)
        others = ", ".join(f"{state} {count}" for state, count in sorted(plan.other_states.items()))
        print(f"Limpieza de lotes de facturas ({'se aplica' if apply else 'vista previa, sin cambios'}):")
        print(f"- Archivados hace mas de {older_than_days} dias: {len(plan.archived)}")
        print(f"- Pendientes de remitentes excluidos: {len(plan.excluded_pending)}")
        print(
            f"- De otra configuracion (lector retirado de Opera u otro libro u hoja): "
            f"{len(plan.other_configuration)}{f' ({others})' if others else ''}"
        )
        print(f"- Se conservan: {plan.kept}")
        if not plan.keys:
            print("Nada que borrar.")
            return 0
        if not apply:
            print("Vista previa: no se borro nada. Para borrar, repite con -Apply.")
            return 0
        backup = apply_cleanup(path, plan, moment)
        approver = settings.approver or "quien ejecuto el comando"
        print(f"Copia de seguridad previa: {backup.name}")
        print(
            f"Borrados {len(plan.keys)} registros; aprobado por {approver} "
            f"el {moment.astimezone().isoformat(timespec='seconds')}."
        )
        return 0
