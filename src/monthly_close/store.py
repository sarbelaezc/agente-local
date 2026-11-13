from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Iterator

from monthly_close.domain import CATEGORIES, NON_EXPENSES, Preferences, clean_text

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS transactions (
    id TEXT PRIMARY KEY, message_id TEXT NOT NULL, day TEXT NOT NULL,
    merchant TEXT NOT NULL, account TEXT NOT NULL, amount_minor INTEGER NOT NULL,
    currency TEXT NOT NULL, category TEXT NOT NULL, kind TEXT NOT NULL,
    decision TEXT, reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS processed (message_id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS scan_progress (
    scope TEXT NOT NULL, item TEXT NOT NULL, status TEXT NOT NULL,
    PRIMARY KEY (scope, item)
);
CREATE TABLE IF NOT EXISTS coverage (
    month TEXT PRIMARY KEY, status TEXT NOT NULL, note TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY, at TEXT NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL,
    value TEXT NOT NULL
);
"""


class Store:
    """Persist minimal extracted facts and human decisions, never message bodies."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(transactions)")}
            if "exchange_rate" not in columns:
                db.execute("ALTER TABLE transactions ADD COLUMN exchange_rate TEXT")
            if "cop_amount_minor" not in columns:
                db.execute("ALTER TABLE transactions ADD COLUMN cop_amount_minor INTEGER")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def preferences(self) -> Preferences:
        with self.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE id=1").fetchone()
        if row is None:
            raise ValueError("Completa primero las seis respuestas del paso 0 en el panel.")
        return Preferences.parse(json.loads(row["value"]))

    def configure(self, raw: dict) -> None:
        preferences = Preferences.parse(raw)
        with self.connect() as db:
            db.execute(
                "INSERT INTO settings VALUES (1,?) ON CONFLICT(id) DO UPDATE SET value=excluded.value",
                (json.dumps(preferences.to_dict(), ensure_ascii=False),),
            )
            self._audit(db, "configure", "step0", "updated")

    @staticmethod
    def _audit(db: sqlite3.Connection, action: str, target: str, value: str) -> None:
        db.execute(
            "INSERT INTO audit(at,action,target,value) VALUES (?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(), action, target, value),
        )

    def seen(self, message_id: str) -> bool:
        with self.connect() as db:
            return db.execute(
                "SELECT 1 FROM processed WHERE message_id=?", (message_id,),
            ).fetchone() is not None

    def scan_status(self, scope: str, item: str) -> str | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT status FROM scan_progress WHERE scope=? AND item=?", (scope, item),
            ).fetchone()
        return row["status"] if row else None

    def scan_checkpoint(self, scope: str, item: str, status: str) -> None:
        if status not in {"pending", "complete"}:
            raise ValueError("Estado de reanudacion invalido.")
        with self.connect() as db:
            db.execute(
                """INSERT INTO scan_progress VALUES (?,?,?)
                ON CONFLICT(scope,item) DO UPDATE SET status=excluded.status""",
                (scope, item, status),
            )

    def clear_scan_progress(self, scope: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM scan_progress WHERE scope=?", (scope,))

    def import_message(self, message_id: str, events: list[dict]) -> None:
        with self.connect() as db:
            if db.execute("SELECT 1 FROM processed WHERE message_id=?", (message_id,)).fetchone():
                return
            for event in events:
                db.execute(
                    """INSERT INTO transactions (
                    id,message_id,day,merchant,account,amount_minor,currency,
                    category,kind,decision,reason) VALUES (
                    :id,:message_id,:day,:merchant,:account,:amount_minor,:currency,
                    :category,:kind,:decision,:reason)""", event,
                )
                db.execute(
                    """UPDATE coverage SET status='partial',
                    note='Hay movimientos nuevos despues de la ultima revision manual.'
                    WHERE month=? AND status='reviewed'""", (event["day"][:7],),
                )
            db.execute("INSERT INTO processed VALUES (?)", (message_id,))
            self._audit(db, "import", message_id, str(len(events)))

    def decide(
        self, event_id: str, decision: str, category: str | None = None, *,
        currency: str | None = None, exchange_rate: str | None = None,
    ) -> None:
        """Record a human decision and an optional estimated USD/COP conversion."""
        if decision not in {
            "personal", "family", "exclude", "investment", "investment_withdrawal", "cash_withdrawal",
        }:
            raise ValueError("Decision no reconocida.")
        with self.connect() as db:
            row = db.execute("SELECT * FROM transactions WHERE id=?", (event_id,)).fetchone()
            if row is None:
                raise ValueError("Movimiento inexistente.")
            if row["kind"] in NON_EXPENSES and decision != "exclude":
                raise ValueError("Ingresos, prestamos y pagos de tarjeta no cuentan como gasto.")
            settings = db.execute("SELECT value FROM settings WHERE id=1").fetchone()
            preferences = Preferences.parse(json.loads(settings["value"])) if settings else None
            if row["kind"] == "investment" and decision not in {
                "investment", "investment_withdrawal", "cash_withdrawal", "exclude",
            }:
                raise ValueError("Un aporte de inversion no cuenta como gasto personal ni familiar.")
            special_categories = {
                "investment": "Inversiones",
                "investment_withdrawal": "Retiros de inversión",
                "cash_withdrawal": "Retiro en efectivo",
            }
            if decision in special_categories:
                if preferences and row["account"] in preferences.excluded_accounts:
                    raise ValueError("Una cuenta excluida no se suma a inversiones propias.")
                category = special_categories[decision]
            if decision in {"personal", "cash_withdrawal"} and (
                row["kind"] == "own_transfer"
                or (preferences and row["account"] in preferences.excluded_accounts)
            ):
                raise ValueError("Transferencias propias o a cuentas excluidas: Familiar o No cuenta.")
            if category is not None and (
                not preferences or (
                    category not in preferences.categories
                    and not (
                        decision in {"personal", "family", "exclude"}
                        and category == "Retiro en efectivo"
                    )
                    and not (
                        decision in {*special_categories, "exclude"}
                        and category in special_categories.values()
                    )
                )
            ):
                raise ValueError("Categoria no autorizada.")
            if (
                decision in {"personal", "family"}
                and (category or row["category"]) in {"Inversiones", "Retiros de inversión"}
            ):
                raise ValueError("Selecciona una categoria de gasto o la decision Inversion.")
            currency = row["currency"] if currency is None else currency
            if currency != row["currency"] and (
                not preferences or preferences.currency != "COP"
                or currency not in {"COP", "USD"} or row["currency"] not in {"COP", "USD"}
            ):
                raise ValueError("Solo se permite corregir COP/USD en un cierre configurado en COP.")
            rate, converted = None, None
            if currency == "USD" and preferences and preferences.currency == "COP":
                if exchange_rate is None:
                    exchange_rate = row["exchange_rate"]
                if exchange_rate not in (None, ""):
                    if not isinstance(exchange_rate, str) or not re.fullmatch(
                        r"\d{1,8}(?:[.,]\d{1,6})?", exchange_rate,
                    ):
                        raise ValueError("Tasa invalida: ingresa COP por USD, con hasta seis decimales.")
                    value = Decimal(exchange_rate.replace(",", "."))
                    if value <= 0:
                        raise ValueError("La tasa COP por USD debe ser positiva.")
                    converted = int(
                        (Decimal(row["amount_minor"]) * value).quantize(
                            Decimal("1"), rounding=ROUND_HALF_UP,
                        ),
                    )
                    if not 0 < converted <= 9007199254740991:
                        raise ValueError("Importe convertido fuera del rango permitido.")
                    rate = format(value, "f")
                elif decision != "exclude":
                    raise ValueError("Ingresa la tasa estimada COP por USD para la fecha del movimiento.")
            elif exchange_rate not in (None, ""):
                raise ValueError("La tasa solo aplica a movimientos USD en un cierre en COP.")
            db.execute(
                """UPDATE transactions SET decision=?,category=?,currency=?,
                exchange_rate=?,cop_amount_minor=? WHERE id=?""",
                (decision, category or row["category"], currency, rate, converted, event_id),
            )
            self._audit(db, "decision-local-user", event_id, decision)
            if currency != row["currency"] or rate != row["exchange_rate"]:
                self._audit(db, "exchange-local-user", event_id, json.dumps({
                    "previous_currency": row["currency"], "currency": currency,
                    "previous_exchange_rate": row["exchange_rate"],
                    "exchange_rate": rate, "cop_amount_minor": converted,
                }))

    def coverage(self, month: str, status: str, note: str) -> None:
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
            raise ValueError("Mes invalido.")
        if status not in {"partial", "unverified", "reviewed"}:
            raise ValueError("Estado de cobertura invalido.")
        note = clean_text(note, 1000)
        with self.connect() as db:
            # Repeated scans must not erase a person's reconciliation or warning.
            if status != "reviewed":
                db.execute("INSERT OR IGNORE INTO coverage VALUES (?,?,?)", (month, status, note))
            else:
                db.execute(
                    """INSERT INTO coverage VALUES (?,?,?) ON CONFLICT(month)
                    DO UPDATE SET status=excluded.status,note=excluded.note""", (month, status, note),
                )
            self._audit(db, "coverage", month, status)

    def state(self) -> dict:
        with self.connect() as db:
            raw = db.execute("SELECT value FROM settings WHERE id=1").fetchone()
            config = json.loads(raw["value"]) if raw else None
            return {
                "configured": config is not None, "config": config,
                "categories": config["categories"] if config else CATEGORIES,
                "transactions": [
                    dict(row) for row in db.execute(
                        """SELECT id,day,merchant,account,amount_minor,currency,
                        category,kind,decision,reason,exchange_rate,cop_amount_minor
                        FROM transactions ORDER BY day,id"""
                    )
                ],
                "coverage": [dict(row) for row in db.execute("SELECT * FROM coverage ORDER BY month")],
            }

    def summary(self, month: str) -> str:
        rows = [row for row in self.state()["transactions"] if row["day"].startswith(month)]
        rows = [
            {**row, "currency": "COP", "amount_minor": row["cop_amount_minor"]}
            if row["currency"] == "USD" and row["cop_amount_minor"] is not None else row
            for row in rows
        ]
        currencies = sorted({row["currency"] for row in rows})
        parts = []
        for currency in currencies:
            selected = [row for row in rows if row["currency"] == currency]
            personal = sum(
                row["amount_minor"] for row in selected
                if row["decision"] in {"personal", "cash_withdrawal"}
            )
            family = sum(row["amount_minor"] for row in selected if row["decision"] == "family")
            investment = sum(row["amount_minor"] for row in selected if row["decision"] == "investment")
            withdrawals = sum(
                row["amount_minor"] for row in selected
                if row["decision"] == "investment_withdrawal"
            )
            cash = sum(
                row["amount_minor"] for row in selected
                if row["decision"] in {"personal", "family", "cash_withdrawal"}
                and row["category"] == "Retiro en efectivo"
            )
            parts.append(
                f"{currency}: personal {Decimal(personal) / 100:.2f}; "
                f"familiar {Decimal(family) / 100:.2f}; "
                f"inversiones {Decimal(investment - withdrawals) / 100:.2f} (fuera de gastos); "
                f"retiros de inversion {Decimal(withdrawals) / 100:.2f}; "
                f"retiro en efectivo {Decimal(cash) / 100:.2f} (incluido en gastos)"
            )
        pending = sum(row["decision"] is None for row in rows)
        return (
            f"Cierre {month}: " + (" | ".join(parts) or "sin gastos confirmados") +
            f"\nDudas abiertas: {pending}. Cobertura no garantizada; revisa el panel."
        )
