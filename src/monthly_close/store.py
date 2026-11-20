from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Iterator

from monthly_close.domain import (
    COUNTERPART_KINDS, DECISIONS, HOUSEHOLD, INCOME_CATEGORIES, INCOME_KINDS, LOAN_DECISIONS, NON_EXPENSES,
    OWNERS, Preferences, clean_text, owner_id,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS transactions (
    id TEXT PRIMARY KEY, message_id TEXT NOT NULL, day TEXT NOT NULL,
    merchant TEXT NOT NULL, account TEXT NOT NULL, amount_minor INTEGER NOT NULL,
    currency TEXT NOT NULL, category TEXT NOT NULL, kind TEXT NOT NULL,
    decision TEXT, reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS processed (message_id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS extraction_failures (
    message_id TEXT PRIMARY KEY, sender TEXT NOT NULL, subject TEXT NOT NULL,
    received TEXT NOT NULL, day TEXT NOT NULL, folder TEXT NOT NULL,
    field TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scan_progress (
    scope TEXT NOT NULL, item TEXT NOT NULL, status TEXT NOT NULL,
    PRIMARY KEY (scope, item)
);
CREATE TABLE IF NOT EXISTS mailbox_sync (
    mailbox TEXT NOT NULL, folder TEXT NOT NULL, token TEXT NOT NULL,
    PRIMARY KEY (mailbox, folder)
);
CREATE TABLE IF NOT EXISTS closes (month TEXT PRIMARY KEY, closed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS coverage (
    month TEXT PRIMARY KEY, status TEXT NOT NULL, note TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY, at TEXT NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS merchant_rules (
    id INTEGER PRIMARY KEY, pattern TEXT NOT NULL UNIQUE, category TEXT NOT NULL,
    decision TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS group_rules (
    owner TEXT NOT NULL, group_key TEXT NOT NULL, decision TEXT,
    between_owners INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (owner, group_key)
);
CREATE TABLE IF NOT EXISTS owner_accounts (suffix TEXT PRIMARY KEY, owner TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS category_suggestions (
    event_id TEXT PRIMARY KEY, category TEXT NOT NULL, source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS payroll_deductions (
    owner TEXT NOT NULL, suffix TEXT NOT NULL, PRIMARY KEY (owner, suffix)
);
CREATE TABLE IF NOT EXISTS statement_checks (
    identity TEXT PRIMARY KEY, product TEXT NOT NULL, suffix TEXT NOT NULL,
    period_start TEXT NOT NULL, period_end TEXT NOT NULL, movements INTEGER NOT NULL,
    matched INTEGER NOT NULL, missing INTEGER NOT NULL, balance_minor INTEGER,
    checked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS card_series (
    owner TEXT NOT NULL, product TEXT NOT NULL, suffix TEXT NOT NULL,
    replaces TEXT, decided_at TEXT, PRIMARY KEY (owner, product, suffix)
);
"""
# card_series.replaces: NULL while a person has not answered, '' for a card of its own,
# 'PRODUCT:SUFFIX' when it is that earlier card with a new number.

MONTH = re.compile(r"\d{4}-(0[1-9]|1[0-2])")
# Statements that list every movement of a product. Credits only bring balances (their
# payments leave the savings account) and Protección only figures, so they do not count.
CARD_PRODUCTS = {"TARJETA_MASTERCARD", "TARJETA_AMEX", "TARJETA_VISA"}
COVERAGE_PRODUCTS = {"CTA_AHORROS", "FIDUCUENTA", *CARD_PRODUCTS}

# Household columns added to databases created before owners existed. The defaults keep
# every existing movement as Santiago's, with its amount and decision untouched.
HOUSEHOLD_COLUMNS = {
    "owner": "TEXT NOT NULL DEFAULT 'santiago'",
    "group_key": "TEXT",
    "time": "TEXT",
    "note": "TEXT",
    "account_label": "TEXT",
    "category_uncertain": "INTEGER NOT NULL DEFAULT 0",
    "between_owners": "INTEGER NOT NULL DEFAULT 0",
}
STATE_COLUMNS = (
    "id,day,merchant,account,amount_minor,currency,category,kind,decision,reason,"
    "exchange_rate,cop_amount_minor,owner,group_key,time,note,account_label,"
    "category_uncertain,between_owners"
)
INVESTMENT_FIGURES = ("opening_minor", "contributions_minor", "withdrawals_minor", "returns_minor")
SPECIAL_CATEGORIES = {
    "investment": "Inversiones",
    "investment_withdrawal": "Retiros de inversión",
    "cash_withdrawal": "Retiro en efectivo",
}


# A mailbox movement that may repeat one the same member already brought by file import.
# It stays pending, and no rule decides it: a person compares both.
DUPLICATE_NOTE = "Posible duplicado"
NOT_DUPLICATE = f"(note IS NULL OR note NOT LIKE '{DUPLICATE_NOTE}%')"

# Movements of a notice that a reprocessing run must leave alone: everything except
# card payments the model guessed before they were read from the bank's fixed sentence.
KEPT = (
    "SELECT 1 FROM transactions WHERE message_id=? "
    "AND NOT (kind='card_payment' AND category<>'Pago de tarjeta')"
)


@dataclass(frozen=True)
class FixedStatementLine:
    """A statement wording that both owners confirmed always means the same decision."""

    wording: str  # regular expression over the line's words (see merchant_words)
    account: str  # SQL LIKE pattern for the account it appears in
    kind: str
    decision: str
    category: str | None
    reason: str


FIXED_STATEMENT_LINES = (
    FixedStatementLine(
        r"ABONO SUCURSAL VIRTUAL", "Tarjeta de cr%", "card_payment", "exclude", None,
        "en el extracto de la tarjeta es un pago propio desde la cuenta de ahorros: no cuenta.",
    ),
    FixedStatementLine(
        r"PAGO SUC VIRT TC (?:MASTER|AMEX|VISA)(?: DOLAR)?", "Cuenta de ahorros", "card_payment",
        "exclude", None,
        "es la salida de ahorros con la que pagaste tu propia tarjeta: no cuenta, el gasto son las compras.",
    ),
    FixedStatementLine(
        r"ABONO INTERESES AHORROS", "Cuenta de ahorros", "income", "income", "Otros ingresos",
        "son intereses de la cuenta de ahorros: ingreso.",
    ),
)


def merchant_words(text: str) -> str:
    """Uppercase words padded with spaces, so rules match whole words only."""
    return " " + " ".join(re.findall(r"[^\W_]+", text.upper())) + " "


def group_of(row: sqlite3.Row | dict) -> str:
    """Destination a movement belongs to: its imported key, else its merchant's words."""
    return row["group_key"] or "com:" + merchant_words(row["merchant"]).strip()


def counterpart_owner(row: sqlite3.Row | dict, accounts: dict[str, str]) -> str | None:
    """Other household member whose account (by its last digits) the movement names."""
    if row["kind"] not in COUNTERPART_KINDS:
        return None
    numbers = re.findall(r"\*\s*(\d{4,})", row["merchant"])
    key = row["group_key"] or ""
    if key.startswith("tr:") and key[3:].isdigit():
        numbers.append(key[3:])
    for number in numbers:
        for suffix, owner in accounts.items():
            if owner != row["owner"] and number.endswith(suffix):
                return owner
    return None


class Store:
    """Persist minimal extracted facts and human decisions, never message bodies."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            columns = {row["name"] for row in db.execute("PRAGMA table_info(transactions)")}
            backup = None
            if columns and not set(HOUSEHOLD_COLUMNS) <= columns:
                backup = self._backup(db)
            db.executescript(SCHEMA)
            if not columns:
                columns = {row["name"] for row in db.execute("PRAGMA table_info(transactions)")}
            if "exchange_rate" not in columns:
                db.execute("ALTER TABLE transactions ADD COLUMN exchange_rate TEXT")
            if "cop_amount_minor" not in columns:
                db.execute("ALTER TABLE transactions ADD COLUMN cop_amount_minor INTEGER")
            for name, definition in HOUSEHOLD_COLUMNS.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE transactions ADD COLUMN {name} {definition}")
            checks = {row["name"] for row in db.execute("PRAGMA table_info(statement_checks)")}
            if "due_minor" not in checks:
                db.execute("ALTER TABLE statement_checks ADD COLUMN due_minor INTEGER")
            if "paid_minor" not in checks:
                db.execute("ALTER TABLE statement_checks ADD COLUMN paid_minor INTEGER")
            # Statements read before every mailbox was searched came from Santiago's Outlook.
            if "owner" not in checks:
                db.execute("ALTER TABLE statement_checks ADD COLUMN owner TEXT NOT NULL DEFAULT 'santiago'")
            # Investment figures of a period (Protección); NULL for other statements.
            for column in INVESTMENT_FIGURES:
                if column not in checks:
                    db.execute(f"ALTER TABLE statement_checks ADD COLUMN {column} INTEGER")
            # A destination rule may also remember the category chosen with it.
            if "category" not in {row["name"] for row in db.execute("PRAGMA table_info(group_rules)")}:
                db.execute("ALTER TABLE group_rules ADD COLUMN category TEXT")
            if backup is not None:
                self._audit(db, "migrate-household", self.path.name, backup.name)

    def _backup(self, db: sqlite3.Connection) -> Path:
        """Copy the database next to it before a schema change; never overwrite a copy."""
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        target = self.path.with_name(f"{self.path.name}.backup-{stamp}")
        if target.exists():
            raise ValueError("Ya existe una copia con ese nombre; no se migra.")
        copy = sqlite3.connect(target)
        try:
            db.backup(copy)
        finally:
            copy.close()
        return target

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

    def record_extraction_failure(
        self, message_id: str, sender: str, subject: str, received: str,
        day: str, folder: str, field: str,
    ) -> None:
        """Retain only metadata needed to locate and retry an unimported notice."""
        with self.connect() as db:
            db.execute(
                """INSERT INTO extraction_failures VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(message_id) DO UPDATE SET sender=excluded.sender,
                subject=excluded.subject,received=excluded.received,day=excluded.day,
                folder=excluded.folder,field=excluded.field""",
                (message_id, sender, subject, received, day, folder, field),
            )
            db.execute(
                """UPDATE coverage SET status='partial',
                note='Hay avisos pendientes de extraccion; faltan movimientos por revisar.'
                WHERE month=?""", (day[:7],),
            )
            self._audit(db, "extraction-pending", message_id, field)

    def queued_failures(self) -> list[dict]:
        """Notices waiting in the review queue, with the metadata needed to find them again."""
        with self.connect() as db:
            return [
                dict(row) for row in db.execute(
                    "SELECT message_id,sender,subject,received,folder FROM extraction_failures ORDER BY received",
                )
            ]

    def dismiss_failure(self, message_id: str) -> None:
        """A person reviewed a queued notice and decided it has nothing to import.

        It leaves the queue and is not retried by normal scans; scan --reprocess-empty
        reads it again.
        """
        with self.connect() as db:
            if db.execute(
                "DELETE FROM extraction_failures WHERE message_id=?", (message_id,),
            ).rowcount != 1:
                raise ValueError("Aviso inexistente en la cola de revision.")
            db.execute("INSERT OR IGNORE INTO processed VALUES (?)", (message_id,))
            self._audit(db, "extraction-dismissed-local-user", message_id, "no-movement")

    def processed_without_events(self, message_id: str) -> bool:
        """True for a notice read before that left nothing worth keeping.

        That is no movement at all (e.g. income once discarded), or only card payments
        guessed by the model, which the fixed-sentence reader now replaces.
        """
        with self.connect() as db:
            return db.execute(
                "SELECT 1 FROM processed WHERE message_id=? AND NOT EXISTS (" + KEPT + ")",
                (message_id, message_id),
            ).fetchone() is not None

    def last_close(self) -> str | None:
        """Most recent month the scheduled close completed (YYYY-MM), if any."""
        with self.connect() as db:
            return db.execute("SELECT max(month) AS month FROM closes").fetchone()["month"]

    def record_close(self, month: str) -> None:
        if not MONTH.fullmatch(month):
            raise ValueError("Mes de cierre invalido.")
        with self.connect() as db:
            db.execute(
                "INSERT INTO closes VALUES (?,?) ON CONFLICT(month) DO UPDATE SET closed_at=excluded.closed_at",
                (month, datetime.now(timezone.utc).isoformat()),
            )
            self._audit(db, "scheduled-close", month, "completed")

    def sync_state(self, mailbox: str) -> dict[str, str]:
        """Saved incremental position of a mailbox, per folder (or Gmail history)."""
        with self.connect() as db:
            return {
                row["folder"]: row["token"]
                for row in db.execute("SELECT folder,token FROM mailbox_sync WHERE mailbox=?", (mailbox,))
            }

    def save_sync(self, mailbox: str, folder: str, token: str) -> None:
        with self.connect() as db:
            db.execute(
                """INSERT INTO mailbox_sync VALUES (?,?,?)
                ON CONFLICT(mailbox,folder) DO UPDATE SET token=excluded.token""",
                (mailbox, folder, token),
            )

    def prune_sync(self, mailbox: str, keep: set[str]) -> None:
        """Forget the position of folders that no longer exist."""
        with self.connect() as db:
            for row in db.execute("SELECT folder FROM mailbox_sync WHERE mailbox=?", (mailbox,)).fetchall():
                if row["folder"] not in keep:
                    db.execute("DELETE FROM mailbox_sync WHERE mailbox=? AND folder=?", (mailbox, row["folder"]))

    def last_import_day(self, owner: str) -> str | None:
        """Last day covered by the member's file import (import-manuela), if any."""
        owner = owner_id(owner)
        with self.connect() as db:
            row = db.execute(
                "SELECT max(day) AS day FROM transactions WHERE owner=? AND message_id LIKE ?",
                (owner, f"{owner}-import:%"),
            ).fetchone()
        return row["day"]

    @staticmethod
    def _mark_possible_duplicates(db: sqlite3.Connection, owner: str, events: list[dict]) -> list[dict]:
        """Leave pending, with a note, movements matching a file-imported one of that member."""
        marked = []
        for event in events:
            twin = db.execute(
                """SELECT id FROM transactions WHERE owner=? AND day=? AND amount_minor=?
                AND currency=? AND message_id LIKE ? LIMIT 1""",
                (owner, event["day"], event["amount_minor"], event["currency"], f"{owner}-import:%"),
            ).fetchone()
            if twin is None:
                marked.append(event)
                continue
            marked.append({
                **event, "decision": None,
                "note": f"{DUPLICATE_NOTE} del movimiento importado del {event['day']} con el mismo valor: "
                        "si es el mismo dinero, elige No cuenta",
            })
            Store._audit(db, "mailbox-possible-duplicate", event["id"], twin["id"])
        return marked

    def import_message(
        self, message_id: str, events: list[dict], reprocess: bool = False, owner: str = "santiago",
    ) -> None:
        """Import once per notice; reprocess only adds to a notice that left no movement."""
        owner = owner_id(owner)
        with self.connect() as db:
            if db.execute("SELECT 1 FROM processed WHERE message_id=?", (message_id,)).fetchone() and (
                not reprocess or db.execute(KEPT, (message_id,)).fetchone()
            ):
                return
            replaced = db.execute(
                "DELETE FROM transactions WHERE message_id=?", (message_id,),
            ).rowcount
            if replaced:
                self._audit(db, "reprocess-replace-card-payments", message_id, str(replaced))
            for event in self._mark_possible_duplicates(db, owner, events):
                db.execute(
                    """INSERT INTO transactions (
                    id,message_id,day,merchant,account,amount_minor,currency,
                    category,kind,decision,reason,group_key,time,note,owner) VALUES (
                    :id,:message_id,:day,:merchant,:account,:amount_minor,:currency,
                    :category,:kind,:decision,:reason,:group_key,:time,:note,:owner)""",
                    {"group_key": None, "time": None, "note": None, **event, "owner": owner},
                )
                db.execute(
                    """UPDATE coverage SET status='partial',
                    note='Hay movimientos nuevos despues de la ultima revision o conciliacion.'
                    WHERE month=? AND status IN ('reviewed','reconciled')""", (event["day"][:7],),
                )
            db.execute("INSERT OR IGNORE INTO processed VALUES (?)", (message_id,))
            db.execute("DELETE FROM extraction_failures WHERE message_id=?", (message_id,))
            self._flag_between_owners(db, [event["id"] for event in events])
            self._audit(db, "import", message_id, str(len(events)))
        self.apply_rules()

    def rules(self) -> list[dict]:
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM merchant_rules ORDER BY pattern")]

    def add_rule(self, pattern: str, category: str, decision: str) -> int:
        """Save a user-written merchant rule and apply it to pending purchases."""
        pattern = merchant_words(clean_text(pattern, 60)).strip()
        if len(pattern) < 3:
            raise ValueError("La regla necesita al menos tres letras o numeros del comercio.")
        if decision not in {"personal", "family", "income"}:
            raise ValueError("Una regla solo puede decidir Personal, Familiar o Ingreso.")
        allowed = INCOME_CATEGORIES if decision == "income" else self.preferences().categories
        if category not in allowed:
            raise ValueError("Categoria no autorizada.")
        with self.connect() as db:
            db.execute(
                """INSERT INTO merchant_rules(pattern,category,decision) VALUES (?,?,?)
                ON CONFLICT(pattern) DO UPDATE SET category=excluded.category,
                decision=excluded.decision""", (pattern, category, decision),
            )
            self._audit(db, "rule-local-user", pattern, f"{category}/{decision}")
        return self.apply_rules()

    def delete_rule(self, rule_id: int) -> None:
        """Remove a rule; decisions it already made stay and can be changed by hand."""
        with self.connect() as db:
            row = db.execute("SELECT pattern FROM merchant_rules WHERE id=?", (rule_id,)).fetchone()
            if row is None:
                raise ValueError("Regla inexistente.")
            db.execute("DELETE FROM merchant_rules WHERE id=?", (rule_id,))
            self._audit(db, "rule-delete-local-user", row["pattern"], "deleted")

    def apply_rules(self) -> int:
        """Decide pending purchases that match a rule; anything doubtful stays pending."""
        decided = self._apply_group_rules()
        with self.connect() as db:
            self._sync_payroll_income(db)
        rules = self.rules()
        if not rules:
            return decided
        with self.connect() as db:
            pending = db.execute(
                """SELECT id,merchant,kind FROM transactions
                WHERE decision IS NULL AND kind IN ('purchase','income') AND """ + NOT_DUPLICATE,
            ).fetchall()
        decided = 0
        for row in pending:
            words = merchant_words(row["merchant"])
            # The longest matching pattern is the most specific one.
            # Income rules only decide money received; expense rules only decide purchases.
            matches = [
                rule for rule in rules if f" {rule['pattern']} " in words
                and (rule["decision"] == "income") == (row["kind"] == "income")
            ]
            if not matches:
                continue
            rule = max(matches, key=lambda item: len(item["pattern"]))
            try:
                self.decide(row["id"], rule["decision"], rule["category"], rule=rule["pattern"])
            except ValueError:
                continue  # e.g. USD without a rate or an excluded account: a person decides.
            decided += 1
        return decided

    def save_category_suggestions(self, suggestions: dict[str, dict], *, replace: bool) -> int:
        """Keep proposed categories so the panel shows them after a reload; nothing is decided.

        replace drops every earlier proposal first (a full recalculation); otherwise new
        proposals are added or updated. Only sources and categories that a proposal can
        carry are stored, and only for movements that exist.
        """
        allowed = set(self.preferences().categories) | {"Retiro en efectivo"}
        with self.connect() as db:
            if replace:
                db.execute("DELETE FROM category_suggestions")
            saved = 0
            for event_id, item in suggestions.items():
                if item.get("source") not in {"rule", "history", "ollama"} or item.get("category") not in allowed:
                    raise ValueError("Sugerencia de categoria invalida.")
                if db.execute("SELECT 1 FROM transactions WHERE id=?", (event_id,)).fetchone() is None:
                    continue
                db.execute(
                    """INSERT INTO category_suggestions VALUES (?,?,?) ON CONFLICT(event_id)
                    DO UPDATE SET category=excluded.category, source=excluded.source""",
                    (event_id, item["category"], item["source"]),
                )
                saved += 1
            self._audit(db, "category-suggestions", "pending", f"{saved} {'recalculadas' if replace else 'nuevas'}")
        return saved

    def pending_purchases(self) -> list[dict]:
        """Id and merchant of every pending purchase, from notices and statements."""
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT id,merchant FROM transactions WHERE decision IS NULL AND kind='purchase'",
            )]

    def confirmed_purchase_categories(self) -> list[dict]:
        """Merchant and category of purchases a person (or their rule) decided as expense."""
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                """SELECT merchant,category FROM transactions
                WHERE kind='purchase' AND decision IN ('personal','family') AND category IS NOT NULL""",
            )]

    def unruled_merchants(self, limit: int = 25) -> list[dict]:
        """Merchants of pending purchases that no rule covers, most frequent first."""
        with self.connect() as db:
            pending = db.execute(
                """SELECT merchant FROM transactions
                WHERE decision IS NULL AND kind='purchase' AND """ + NOT_DUPLICATE,
            ).fetchall()
        patterns = [rule["pattern"] for rule in self.rules() if rule["decision"] != "income"]
        counts: dict[str, int] = {}
        for row in pending:
            words = merchant_words(row["merchant"])
            if len(words.strip()) >= 3 and not any(f" {pattern} " in words for pattern in patterns):
                counts[words.strip()] = counts.get(words.strip(), 0) + 1
        ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]
        return [{"merchant": merchant, "count": count} for merchant, count in ranked]

    def _apply_group_rules(self) -> int:
        """Decide pending movements, from notices or statements, whose destination has a remembered decision.

        Possible duplicates are never decided, and a movement that fails validation (for
        example USD without a rate) stays pending for a person.
        """
        with self.connect() as db:
            rules = {
                (row["owner"], row["group_key"]): (row["decision"], row["category"])
                for row in db.execute("SELECT * FROM group_rules WHERE decision IS NOT NULL")
            }
            if not rules:
                return 0
            pending = db.execute(
                "SELECT * FROM transactions WHERE decision IS NULL AND " + NOT_DUPLICATE,
            ).fetchall()
        decided = 0
        for row in pending:
            group = group_of(row)
            rule = rules.get((row["owner"], group))
            if rule is None:
                continue
            decision, category = rule
            try:
                with self.connect() as db:
                    self._decide(
                        db, row["id"], decision, category,
                        audit=("decision-group-rule", f"{decision}: {group}"),
                    )
            except ValueError:
                continue  # e.g. USD without a rate: a person decides.
            decided += 1
        return decided

    def decide(
        self, event_id: str, decision: str, category: str | None = None, *,
        currency: str | None = None, exchange_rate: str | None = None, rule: str | None = None,
        audit: tuple[str, str] | None = None,
    ) -> None:
        """Record a decision (by the user, or by one of their rules) and an optional USD/COP rate.

        A person deciding one movement also confirms its category; audit names a
        non-interactive origin such as an approved import, which confirms nothing.
        """
        with self.connect() as db:
            self._decide(
                db, event_id, decision, category, currency=currency,
                exchange_rate=exchange_rate, rule=rule, audit=audit,
            )
            if rule is None and audit is None:
                db.execute("UPDATE transactions SET category_uncertain=0 WHERE id=?", (event_id,))
            self._sync_payroll_income(db)

    def _decide(
        self, db: sqlite3.Connection, event_id: str, decision: str, category: str | None = None, *,
        currency: str | None = None, exchange_rate: str | None = None, rule: str | None = None,
        audit: tuple[str, str] | None = None,
    ) -> None:
        if decision not in DECISIONS:
            raise ValueError("Decision no reconocida.")
        row = db.execute("SELECT * FROM transactions WHERE id=?", (event_id,)).fetchone()
        if row is None:
            raise ValueError("Movimiento inexistente.")
        if decision == "third_party_payment" and row["kind"] != "card_payment":
            raise ValueError("Solo un abono recibido en una tarjeta puede ser abono de tercero.")
        if row["kind"] in NON_EXPENSES and decision not in {"exclude", "third_party_payment"}:
            raise ValueError("Prestamos y pagos de tarjeta no cuentan como gasto ni ingreso.")
        if row["kind"] == "loan" and decision not in LOAN_DECISIONS:
            raise ValueError("Una cuota de credito es gasto personal, familiar o no cuenta.")
        if row["kind"] == "income" and decision not in {"income", "exclude"}:
            raise ValueError("El dinero recibido no es un gasto: Ingreso o No cuenta.")
        if decision == "income":
            if row["kind"] not in INCOME_KINDS:
                raise ValueError("Este movimiento no puede contarse como ingreso.")
            if category not in INCOME_CATEGORIES:
                category = (
                    row["category"] if row["category"] in INCOME_CATEGORIES
                    else INCOME_CATEGORIES[-1]
                )
        settings = db.execute("SELECT value FROM settings WHERE id=1").fetchone()
        preferences = Preferences.parse(json.loads(settings["value"])) if settings else None
        if row["kind"] == "investment" and decision not in {
            "investment", "investment_withdrawal", "cash_withdrawal", "exclude",
        }:
            raise ValueError("Un aporte de inversion no cuenta como gasto personal ni familiar.")
        special_categories = SPECIAL_CATEGORIES
        if decision in special_categories:
            if preferences and row["account"] in preferences.excluded_accounts:
                raise ValueError("Una cuenta excluida no se suma a inversiones propias.")
            category = special_categories[decision]
        if decision in {"personal", "cash_withdrawal"} and (
            row["kind"] == "own_transfer"
            or (preferences and row["account"] in preferences.excluded_accounts)
        ):
            raise ValueError("Transferencias propias o a cuentas excluidas: Familiar o No cuenta.")
        if category is not None and category != row["category"] and (
            not preferences or (
                category not in preferences.categories
                and not (
                    decision in {"personal", "family", "exclude"}
                    and category == "Retiro en efectivo"
                )
                and not (decision in {"income", "exclude"} and category in INCOME_CATEGORIES)
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
        if audit is not None:
            self._audit(db, audit[0], event_id, audit[1])
        elif rule is None:
            self._audit(db, "decision-local-user", event_id, decision)
        else:
            db.execute(
                "UPDATE transactions SET reason=? WHERE id=?",
                (f"Decidido por tu regla «{rule}». Puedes cambiarlo aqui.", event_id),
            )
            self._audit(db, "decision-rule", event_id, f"{decision}: {rule}")
        if currency != row["currency"] or rate != row["exchange_rate"]:
            self._audit(db, "exchange-local-user", event_id, json.dumps({
                "previous_currency": row["currency"], "currency": currency,
                "previous_exchange_rate": row["exchange_rate"],
                "exchange_rate": rate, "cop_amount_minor": converted,
            }))

    @staticmethod
    def _group_rows(
        db: sqlite3.Connection, owner: str, group: str, pending_only: bool,
    ) -> list[sqlite3.Row]:
        rows = db.execute(
            "SELECT * FROM transactions WHERE owner=?" + (" AND decision IS NULL" if pending_only else ""),
            (owner,),
        ).fetchall()
        return [row for row in rows if group_of(row) == group]

    def decide_group(
        self, owner: str, group: str, decision: str, *,
        pending_only: bool = True, remember: bool = False, category: str | None = None,
    ) -> int:
        """Apply one person's decision to every movement to a destination, all or nothing.

        Each movement passes the same validation as a single decision and gets its own
        audit row. remember also decides later movements to the same destination.
        category, chosen by the person, applies only to Personal or Familiar decisions.
        """
        if category is not None and decision not in {"personal", "family"}:
            raise ValueError("Solo Gasto personal o Familiar admiten una categoria para todo el destino.")
        owner = owner_id(owner)
        group = clean_text(group, 200)
        with self.connect() as db:
            rows = self._group_rows(db, owner, group, pending_only)
            if not rows:
                raise ValueError("Destino sin movimientos por decidir.")
            for row in rows:
                self._decide(
                    db, row["id"], decision, category,
                    audit=("decision-group-local-user", f"{decision}: {group}"),
                )
            if remember:
                db.execute(
                    """INSERT INTO group_rules(owner,group_key,decision,category) VALUES (?,?,?,?)
                    ON CONFLICT(owner,group_key) DO UPDATE SET decision=excluded.decision,
                    category=excluded.category""",
                    (owner, group, decision, category),
                )
                self._audit(db, "group-rule-local-user", f"{owner}/{group}", f"{decision}/{category}" if category else decision)
            self._sync_payroll_income(db)
        return len(rows)

    def mark_between_owners(
        self, owner: str, group: str, value: bool, *, remember: bool = False,
    ) -> int:
        """Mark a destination as money moving between household members.

        Such movements keep their decision in each member's own view and are left out of
        the household view, so the same money is not counted twice.
        """
        owner = owner_id(owner)
        group = clean_text(group, 200)
        if type(value) is not bool:
            raise ValueError("Marca entre titulares invalida.")
        with self.connect() as db:
            rows = self._group_rows(db, owner, group, pending_only=False)
            if not rows:
                raise ValueError("Destino sin movimientos.")
            for row in rows:
                db.execute(
                    "UPDATE transactions SET between_owners=? WHERE id=?", (int(value), row["id"]),
                )
                self._audit(db, "between-owners-local-user", row["id"], str(int(value)))
            if remember:
                db.execute(
                    """INSERT INTO group_rules(owner,group_key,between_owners) VALUES (?,?,?)
                    ON CONFLICT(owner,group_key) DO UPDATE SET between_owners=excluded.between_owners""",
                    (owner, group, int(value)),
                )
                db.execute(
                    """DELETE FROM group_rules WHERE owner=? AND group_key=?
                    AND decision IS NULL AND between_owners=0""", (owner, group),
                )
                self._audit(db, "group-rule-between-local-user", f"{owner}/{group}", str(int(value)))
        return len(rows)

    def delete_group_rule(self, owner: str, group: str) -> None:
        """Forget a destination rule; movements it already decided keep their decision."""
        with self.connect() as db:
            if db.execute(
                "DELETE FROM group_rules WHERE owner=? AND group_key=?", (owner_id(owner), group),
            ).rowcount != 1:
                raise ValueError("Regla de destino inexistente.")
            self._audit(db, "group-rule-delete-local-user", f"{owner}/{group}", "deleted")

    def merge_category(self, source: str, target: str) -> dict[str, int]:
        """Fold one spending category into another, in one audited transaction.

        Movements and merchant rules of the source move to the target, and the source
        leaves the configured categories. Decisions and amounts do not change.
        """
        preferences = self.preferences()
        if source == target or source not in preferences.categories or target not in preferences.categories:
            raise ValueError("Indica dos categorias configuradas y distintas.")
        settings = preferences.to_dict()
        settings["categories"] = [category for category in preferences.categories if category != source]
        with self.connect() as db:
            moved = [row["id"] for row in db.execute("SELECT id FROM transactions WHERE category=?", (source,))]
            db.execute("UPDATE transactions SET category=? WHERE category=?", (target, source))
            for event_id in moved:
                self._audit(db, "category-merge-local-user", event_id, f"{source} -> {target}")
            rules = db.execute("UPDATE merchant_rules SET category=? WHERE category=?", (target, source)).rowcount
            rules += db.execute("UPDATE group_rules SET category=? WHERE category=?", (target, source)).rowcount
            db.execute("UPDATE settings SET value=? WHERE id=1", (json.dumps(settings, ensure_ascii=False),))
            self._audit(db, "category-merge-local-user", source, f"-> {target}: {len(moved)} movimientos, {rules} reglas")
        return {"movements": len(moved), "rules": rules}

    def set_payroll_deduction(self, owner: str, suffix: str) -> int:
        """Register a credit whose instalments are deducted from the member's payroll.

        The payroll notice then shows the net salary, so each counted instalment also
        gets a matching payroll income; otherwise the deduction would count twice.
        """
        owner = owner_id(owner)
        if not isinstance(suffix, str) or not re.fullmatch(r"\d{4}", suffix):
            raise ValueError("Indica solo los ultimos cuatro digitos del credito.")
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO payroll_deductions VALUES (?,?)", (owner, suffix))
            self._audit(db, "payroll-deduction-local-user", "*" + suffix, owner)
            return self._sync_payroll_income(db)

    @staticmethod
    def _ordinary_instalment(db: sqlite3.Connection, suffix: str, period_end: str, paid: int) -> int:
        """The instalment due for a payment: the previous statement's "cuota a pagar".

        Without a previous statement it falls back to this statement's own due amount,
        and without any it is the payment itself.
        """
        row = db.execute(
            """SELECT due_minor FROM statement_checks WHERE product='CREDITO' AND suffix=?
            AND due_minor IS NOT NULL AND period_end < ? ORDER BY period_end DESC LIMIT 1""",
            (suffix, period_end),
        ).fetchone() or db.execute(
            """SELECT due_minor FROM statement_checks WHERE product='CREDITO' AND suffix=?
            AND due_minor IS NOT NULL AND period_end = ?""", (suffix, period_end),
        ).fetchone()
        return row["due_minor"] if row else paid

    def import_credit_ledger(
        self, owner: str, suffix: str, events: list[dict], product: str = "CREDITO",
    ) -> dict[str, int]:
        """Import a credit's payments from the person's register, in one transaction.

        The register replaces the instalments read from the credit statement, which only
        show the last payment of each period. Payments enter pending; account movements
        with the same amount within three days get a note, so a person can mark the
        duplicate as «No cuenta». Repeating the import changes nothing.
        """
        owner = owner_id(owner)
        with self.connect() as db:
            inserted = []
            for event in events:
                if event["decision"] is not None or not event["id"].startswith("ledger:"):
                    raise ValueError("Un pago del registro solo entra pendiente.")
                if db.execute(
                    """INSERT OR IGNORE INTO transactions (id,message_id,day,merchant,account,
                    amount_minor,currency,category,kind,decision,reason,group_key,note,owner)
                    VALUES (:id,:message_id,:day,:merchant,:account,:amount_minor,:currency,
                    :category,:kind,NULL,:reason,:group_key,:note,:owner)""", {**event, "owner": owner},
                ).rowcount:
                    inserted.append(event)
                    self._audit(db, "ledger-import", event["id"], event["message_id"])
            replaced = 0
            for loan in db.execute(
                "SELECT id FROM transactions WHERE owner=? AND message_id LIKE ?",
                (owner, f"statement:{product}:{suffix}:%"),
            ).fetchall():
                for twin in ("payroll:" + loan["id"], "payroll-extra:" + loan["id"], loan["id"]):
                    if db.execute("DELETE FROM transactions WHERE id=?", (twin,)).rowcount:
                        self._audit(db, "ledger-supersedes-statement", twin, suffix)
                replaced += 1
            linked = 0
            for event in inserted:
                for other in db.execute(
                    """SELECT id FROM transactions WHERE owner=? AND decision IS NULL AND amount_minor=?
                    AND id NOT LIKE 'ledger:%' AND id NOT LIKE 'payroll%'
                    AND abs(julianday(day) - julianday(?)) <= 3""",
                    (owner, event["amount_minor"], event["day"]),
                ).fetchall():
                    db.execute(
                        "UPDATE transactions SET note=? WHERE id=?",
                        (f"Mismo valor que el pago del {event['day']} a tu crédito *{suffix} (registro de pagos): "
                         "si es el mismo dinero, elige No cuenta", other["id"]),
                    )
                    self._audit(db, "ledger-possible-duplicate", other["id"], event["id"])
                    linked += 1
            self._sync_payroll_income(db)
        self.apply_rules()
        return {"imported": len(inserted), "replaced": replaced, "possible_duplicates": linked}

    def _sync_ledger_payroll(self, db: sqlite3.Connection, owner: str, suffix: str) -> int:
        """Payroll income for payments read from a register: one per fortnight, pending for bonus extras."""
        changes = 0
        for loan in db.execute(
            "SELECT * FROM transactions WHERE owner=? AND kind='loan' AND message_id LIKE ?",
            (owner, f"ledger:CREDITO:{suffix}:%"),
        ).fetchall():
            source = loan["message_id"].rsplit(":", 1)[1]
            counted = loan["decision"] in {"personal", "family"}
            twins = {
                "payroll:" + loan["id"]: (source == "payroll", "income", "Descuento de nómina: cuota quincenal",
                                          f"Tu nómina llega neta: se descontó esta cuota del crédito *{suffix}."),
                "payroll-extra:" + loan["id"]: (source == "bonus", None, f"Abono de prima al crédito *{suffix}: ¿descontado de la prima?",
                                                f"Tu registro muestra un abono a capital en mes de prima. Si se descontó de la prima, "
                                                "elige Ingreso; si lo pagaste desde tu cuenta, No cuenta."),
            }
            for twin, (applies, decision, merchant, reason) in twins.items():
                exists = db.execute("SELECT 1 FROM transactions WHERE id=?", (twin,)).fetchone()
                if counted and applies and not exists:
                    db.execute(
                        """INSERT INTO transactions (id,message_id,day,merchant,account,amount_minor,
                        currency,category,kind,decision,reason,owner) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (twin, "payroll:" + loan["message_id"], loan["day"], merchant, "Nómina",
                         loan["amount_minor"], loan["currency"], INCOME_CATEGORIES[0], "income", decision,
                         reason, owner),
                    )
                    self._audit(db, "payroll-deduction-income", twin, "added")
                    changes += 1
                elif not (counted and applies) and exists:
                    db.execute("DELETE FROM transactions WHERE id=?", (twin,))
                    self._audit(db, "payroll-deduction-income", twin, "removed")
                    changes += 1
        return changes

    def _sync_payroll_income(self, db: sqlite3.Connection) -> int:
        """Keep the payroll income that mirrors each counted instalment of a deducted credit.

        Only the ordinary instalment is mirrored as decided payroll income. Anything paid
        above it (a bonus-month payment, or a capital payment made from an account) gets
        a pending income for a person to confirm. Both disappear when the instalment
        stops counting. Returns the number of changes.
        """
        changes = 0
        for credit in db.execute("SELECT * FROM payroll_deductions").fetchall():
            suffix = credit["suffix"]
            if db.execute(
                "SELECT 1 FROM transactions WHERE owner=? AND message_id LIKE ? LIMIT 1",
                (credit["owner"], f"ledger:CREDITO:{suffix}:%"),
            ).fetchone():
                # A payment register replaces the statement's partial view of this credit.
                changes += self._sync_ledger_payroll(db, credit["owner"], suffix)
                continue
            loans = db.execute(
                "SELECT * FROM transactions WHERE owner=? AND kind='loan' AND message_id LIKE ?",
                (credit["owner"], f"statement:CREDITO:{suffix}:%"),
            ).fetchall()
            for loan in loans:
                counted = loan["decision"] in {"personal", "family"}
                period_end = loan["message_id"].split(":")[3]
                ordinary = min(
                    loan["amount_minor"],
                    self._ordinary_instalment(db, suffix, period_end, loan["amount_minor"]),
                )
                parts = {
                    "payroll:" + loan["id"]: (ordinary, "income", (
                        f"Tu nómina llega neta: se descontó la cuota ordinaria del crédito *{suffix}. "
                        "Se suma al ingreso para que la cuota no se cuente dos veces."
                    ), f"Descuento de nómina: cuota del crédito *{suffix}"),
                    "payroll-extra:" + loan["id"]: (loan["amount_minor"] - ordinary, None, (
                        f"Pagaste al crédito *{suffix} más que la cuota ordinaria. Si el extra se descontó "
                        "de la nómina o de la prima, elige Ingreso; si lo pagaste desde tu cuenta, No cuenta."
                    ), f"Abono extra al crédito *{suffix}: ¿descontado de la nómina?"),
                }
                for twin, (amount, decision, reason, merchant) in parts.items():
                    existing = db.execute("SELECT * FROM transactions WHERE id=?", (twin,)).fetchone()
                    wanted = counted and amount > 0
                    if wanted and existing is None:
                        db.execute(
                            """INSERT INTO transactions (id,message_id,day,merchant,account,amount_minor,
                            currency,category,kind,decision,reason,owner) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (twin, "payroll:" + loan["message_id"], loan["day"], merchant, "Nómina", amount,
                             loan["currency"], INCOME_CATEGORIES[0], "income", decision, reason, loan["owner"]),
                        )
                        self._audit(db, "payroll-deduction-income", twin, "added")
                        changes += 1
                    elif wanted and existing["amount_minor"] != amount and (
                        twin.startswith("payroll:") or existing["decision"] is None
                    ):
                        db.execute(
                            "UPDATE transactions SET amount_minor=?, merchant=?, reason=? WHERE id=?",
                            (amount, merchant, reason, twin),
                        )
                        self._audit(db, "payroll-deduction-income", twin, f"amount {existing['amount_minor']} -> {amount}")
                        changes += 1
                    elif not wanted and existing is not None:
                        db.execute("DELETE FROM transactions WHERE id=?", (twin,))
                        self._audit(db, "payroll-deduction-income", twin, "removed")
                        changes += 1
        return changes

    def card_statement_suffixes(self, owner: str) -> list[tuple[str, str]]:
        """(product, last four digits) of every credit card a member's statements showed."""
        with self.connect() as db:
            return [(row["product"], row["suffix"]) for row in db.execute(
                """SELECT DISTINCT product, suffix FROM statement_checks
                WHERE owner=? AND product LIKE 'TARJETA%' AND suffix IS NOT NULL AND suffix <> ''""",
                (owner_id(owner),),
            )]

    def own_account_suffixes(self, owner: str) -> list[str]:
        """Last four digits of the accounts registered as a member's own."""
        with self.connect() as db:
            return [row["suffix"] for row in db.execute(
                "SELECT suffix FROM owner_accounts WHERE owner=? ORDER BY suffix", (owner_id(owner),),
            )]

    def set_owner_account(self, owner: str, suffix: str) -> int:
        """Register the last four digits of a member's account to recognise transfers between members."""
        owner = owner_id(owner)
        if not isinstance(suffix, str) or not re.fullmatch(r"\d{4}", suffix):
            raise ValueError("Indica solo los ultimos cuatro digitos de la cuenta.")
        with self.connect() as db:
            db.execute(
                """INSERT INTO owner_accounts VALUES (?,?)
                ON CONFLICT(suffix) DO UPDATE SET owner=excluded.owner""", (suffix, owner),
            )
            self._audit(db, "owner-account-local-user", "*" + suffix, owner)
            return self._flag_between_owners(db, None)

    def record_statements(self, checks: list[dict], events: list[dict]) -> int:
        """Store each statement's reconciliation and import the movements it alone shows.

        Imported movements stay pending unless a fixed wording the owners confirmed or one
        of the person's own rules decides them (never possible duplicates). Repeating the
        reconciliation imports nothing twice, because every imported id is derived from the
        statement line and existing ids are skipped.
        """
        now = datetime.now(timezone.utc).isoformat()
        inserted: list[str] = []
        with self.connect() as db:
            for check in checks:
                check = {"owner": "santiago", **check}
                known = db.execute(
                    "SELECT owner FROM statement_checks WHERE identity=?", (check["identity"],),
                ).fetchone()
                if known is not None and known["owner"] != check["owner"]:
                    raise ValueError("Un extracto ya registrado de otro titular no cambia de titular.")
                db.execute(
                    """INSERT INTO statement_checks (identity,owner,product,suffix,period_start,period_end,
                    movements,matched,missing,balance_minor,checked_at,due_minor,paid_minor,
                    opening_minor,contributions_minor,withdrawals_minor,returns_minor) VALUES (
                    :identity,:owner,:product,:suffix,:period_start,:period_end,:movements,:matched,
                    :missing,:balance_minor,:checked_at,:due_minor,:paid_minor,
                    :opening_minor,:contributions_minor,:withdrawals_minor,:returns_minor) ON CONFLICT(identity) DO UPDATE SET
                    movements=excluded.movements,matched=excluded.matched,missing=excluded.missing,
                    balance_minor=excluded.balance_minor,checked_at=excluded.checked_at,
                    due_minor=excluded.due_minor,
                    paid_minor=COALESCE(excluded.paid_minor, statement_checks.paid_minor),
                    opening_minor=excluded.opening_minor,contributions_minor=excluded.contributions_minor,
                    withdrawals_minor=excluded.withdrawals_minor,returns_minor=excluded.returns_minor""",
                    {
                        "due_minor": None, "paid_minor": None, **dict.fromkeys(INVESTMENT_FIGURES),
                        **check, "checked_at": now,
                    },
                )
                self._audit(db, "statement-check", check["identity"], f"{check['matched']}/{check['movements']}")
            for event in events:
                if event["decision"] is not None or not event["id"].startswith("stmt:"):
                    raise ValueError("Un movimiento de extracto solo entra pendiente.")
                if db.execute(
                    """INSERT OR IGNORE INTO transactions (
                    id,message_id,day,merchant,account,amount_minor,currency,category,kind,
                    decision,reason,owner) VALUES (:id,:message_id,:day,:merchant,:account,
                    :amount_minor,:currency,:category,:kind,NULL,:reason,:owner)""",
                    {"owner": "santiago", **event},
                ).rowcount:
                    inserted.append(event["id"])
                    self._audit(db, "statement-import", event["id"], event["message_id"])
            self._flag_between_owners(db, inserted)
            # Fixed wordings the owners confirmed first; then the person's own rules, with the
            # same safeguards as for notices. Ollama never decides.
            self._decide_fixed_statement_lines(db)
        self.apply_rules()
        return len(inserted)

    def _decide_fixed_statement_lines(self, db: sqlite3.Connection) -> int:
        """Decide pending statement lines whose exact wording has one meaning for both owners.

        Only the wordings in FIXED_STATEMENT_LINES, in their account and kind, are decided;
        a decision already taken is never changed. Each one is audited and can be corrected.
        """
        decided = 0
        for line in FIXED_STATEMENT_LINES:
            rows = db.execute(
                """SELECT id,merchant FROM transactions WHERE decision IS NULL AND id LIKE 'stmt:%'
                AND kind=? AND account LIKE ?""", (line.kind, line.account),
            ).fetchall()
            for row in rows:
                words = merchant_words(row["merchant"]).strip()
                if not re.fullmatch(line.wording, words):
                    continue
                self._decide(
                    db, row["id"], line.decision, line.category,
                    audit=("decision-statement-phrase", f"{line.decision}: {words}"),
                )
                db.execute(
                    "UPDATE transactions SET reason=? WHERE id=?",
                    (f"«{words}» {line.reason} Puedes cambiarlo aqui.", row["id"]),
                )
                decided += 1
        return decided

    def reclassify_statement_movements(self, kinds: dict[str, str]) -> int:
        """Correct the kind of statement movements still pending, after the reader improved.

        Decided movements are never touched: a person's decision stands. Each change is
        audited with its previous kind.
        """
        changed = 0
        with self.connect() as db:
            for event_id, kind in kinds.items():
                row = db.execute(
                    "SELECT kind FROM transactions WHERE id=? AND id LIKE 'stmt:%' AND decision IS NULL",
                    (event_id,),
                ).fetchone()
                if row is None or row["kind"] == kind:
                    continue
                db.execute("UPDATE transactions SET kind=? WHERE id=?", (kind, event_id))
                self._audit(db, "statement-reclassify", event_id, f"{row['kind']} -> {kind}")
                changed += 1
        return changed

    def set_destinations(self, destinations: dict[str, str]) -> int:
        """Record destinations read again from the original notices.

        Only movements without one change; amounts, decisions and categories stay as
        they are. Each change is audited, and transfers to a registered account of the
        other member are then marked as money between members.
        """
        changed: list[str] = []
        with self.connect() as db:
            for event_id, key in destinations.items():
                if not isinstance(key, str) or not re.fullmatch(r"(tr|bb|qr|fd):[\w@.\-]{1,60}", key):
                    raise ValueError("Destino invalido.")
                if db.execute(
                    "UPDATE transactions SET group_key=? WHERE id=? AND group_key IS NULL",
                    (key, event_id),
                ).rowcount:
                    self._audit(db, "destination-backfill", event_id, key)
                    changed.append(event_id)
            self._flag_between_owners(db, changed)
        return len(changed)

    def _flag_between_owners(self, db: sqlite3.Connection, ids: list[str] | None) -> int:
        """Mark movements naming another member's account, or a remembered destination.

        Runs on newly imported movements (or all of them when an account is added); it
        never clears a mark, so a person's choice is only changed by that person.
        """
        accounts = {row["suffix"]: row["owner"] for row in db.execute("SELECT * FROM owner_accounts")}
        remembered = {
            (row["owner"], row["group_key"])
            for row in db.execute("SELECT owner,group_key FROM group_rules WHERE between_owners=1")
        }
        if not accounts and not remembered:
            return 0
        rows = db.execute("SELECT * FROM transactions WHERE between_owners=0").fetchall()
        if ids is not None:
            wanted = set(ids)
            rows = [row for row in rows if row["id"] in wanted]
        flagged = 0
        for row in rows:
            if (row["owner"], group_of(row)) in remembered:
                reason = "group-rule"
            elif counterpart_owner(row, accounts):
                reason = "account"
            else:
                continue
            db.execute("UPDATE transactions SET between_owners=1 WHERE id=?", (row["id"],))
            self._audit(db, "between-owners-" + reason, row["id"], "1")
            flagged += 1
        return flagged

    def import_external(
        self, owner: str, source: str, events: list[dict], group_rules: dict[str, str],
    ) -> dict[str, int]:
        """Import movements another member approved elsewhere, in one transaction.

        Existing ids are skipped, so repeating the import changes nothing. Each movement
        is decided through the same validation as the panel and audited one by one;
        destination decisions become remembered rules unless a person already set one.
        No mailbox notice is involved, so processed and coverage stay untouched.
        """
        owner = owner_id(owner)
        if not re.fullmatch(r"[a-z][a-z-]{2,30}", source):
            raise ValueError("Origen de importacion invalido.")
        with self.connect() as db:
            existing = {row["id"] for row in db.execute("SELECT id FROM transactions WHERE owner=?", (owner,))}
            new = [event for event in events if event["id"] not in existing]
            for event in new:
                if event["owner"] != owner:
                    raise ValueError("Movimiento de otro titular en la importacion.")
                db.execute(
                    """INSERT INTO transactions (
                    id,message_id,day,merchant,account,amount_minor,currency,category,kind,
                    decision,reason,owner,group_key,time,note,account_label,category_uncertain)
                    VALUES (:id,:message_id,:day,:merchant,:account,:amount_minor,:currency,
                    :category,:kind,NULL,:reason,:owner,:group_key,:time,:note,:account_label,
                    :category_uncertain)""", event,
                )
            for group, decision in group_rules.items():
                if decision not in DECISIONS:
                    raise ValueError("Decision de destino no reconocida.")
                if db.execute(
                    "INSERT OR IGNORE INTO group_rules(owner,group_key,decision) VALUES (?,?,?)",
                    (owner, group, decision),
                ).rowcount:
                    self._audit(db, f"group-rule-{source}", f"{owner}/{group}", decision)
            rules = {
                row["group_key"]: row["decision"] for row in db.execute(
                    "SELECT * FROM group_rules WHERE owner=? AND decision IS NOT NULL", (owner,),
                )
            }
            decided = by_group = 0
            for event in new:
                if event["group_key"]:
                    decision = rules.get(event["group_key"])
                    if decision is None:
                        continue
                    self._decide(
                        db, event["id"], decision, None,
                        audit=("decision-group-rule", f"{decision}: {event['group_key']}"),
                    )
                    by_group += 1
                else:
                    self._decide(
                        db, event["id"], event["decision"], None,
                        audit=(f"decision-{source}", f"{event['decision']}: aprobado en su panel"),
                    )
                    decided += 1
            flagged = self._flag_between_owners(db, [event["id"] for event in new])
            self._audit(db, f"import-{source}", owner, str(len(new)))
        return {
            "imported": len(new), "skipped": len(events) - len(new), "approved": decided,
            "by_destination": by_group, "pending": len(new) - decided - by_group,
            "between_owners": flagged,
        }

    def monthly_totals(self, owner: str) -> dict[str, dict[str, int]]:
        """Decided totals per month in the reporting currency (COP for converted USD).

        personal includes cash withdrawals; net is personal + family minus third-party
        payments to a card. The household view leaves out money between members.
        """
        owner = owner_id(owner, allow_household=True)
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM transactions"
                + ("" if owner == HOUSEHOLD else " WHERE owner=?"),
                () if owner == HOUSEHOLD else (owner,),
            ).fetchall()
        totals: dict[str, dict[str, int]] = {}
        for row in rows:
            if owner == HOUSEHOLD and row["between_owners"]:
                continue
            amount = row["amount_minor"]
            if row["currency"] == "USD" and row["cop_amount_minor"] is not None:
                amount = row["cop_amount_minor"]
            elif row["currency"] != "COP":
                continue
            month = totals.setdefault(row["day"][:7], dict.fromkeys(
                ("personal", "family", "cash", "income", "third_party", "net", "pending"), 0,
            ))
            decision = row["decision"]
            if decision in {"personal", "cash_withdrawal"}:
                month["personal"] += amount
            if decision == "cash_withdrawal":
                month["cash"] += amount
            if decision == "family":
                month["family"] += amount
            if decision == "income":
                month["income"] += amount
            if decision == "third_party_payment":
                month["third_party"] += amount
            if decision is None:
                month["pending"] += amount
        for month in totals.values():
            month["net"] = month["personal"] + month["family"] - month["third_party"]
        return totals

    def update_reconciled_coverage(self, today: date) -> dict[str, list[str]]:
        """Mark each closed month that the statements fully reconcile; undo it when that stops.

        A month is reconciled when every product with movements (savings, cards and
        Fiducuenta) of any owner that had started by then has statements covering each of its days,
        none of them left a movement unmatched, and the month has neither pending
        decisions nor notices in the review queue, for any owner. A person's review is
        never replaced. Only closed months (before today's) are considered.
        """
        current = today.strftime("%Y-%m")
        with self.connect() as db:
            self._register_card_series(db)
            periods: dict[tuple[str, str, str], list[tuple[date, date, int]]] = {}
            for row in db.execute(
                "SELECT owner,product,suffix,period_start,period_end,missing FROM statement_checks",
            ):
                if row["product"] in COVERAGE_PRODUCTS:
                    periods.setdefault((row["owner"], row["product"], row["suffix"]), []).append((
                        date.fromisoformat(row["period_start"]), date.fromisoformat(row["period_end"]),
                        row["missing"],
                    ))
            periods, names, unanswered = self._join_card_series(db, periods)
            status = {row["month"]: row["status"] for row in db.execute("SELECT month,status FROM coverage")}
            months = set(status) | {
                row[0] for row in db.execute("SELECT DISTINCT substr(day,1,7) FROM transactions")
            }
            # A month the statements cover may have no movement at all and still be complete.
            for rows in periods.values():
                for start, end, _ in rows:
                    day = start.replace(day=1)
                    while day <= end:
                        months.add(day.strftime("%Y-%m"))
                        day = (day + timedelta(days=32)).replace(day=1)
            changed: dict[str, list[str]] = {"reconciled": [], "revoked": []}
            for month in sorted(m for m in months if MONTH.fullmatch(m) and m < current):
                reason, used, owners = self._reconcile_month(db, month, periods, names, unanswered)
                if reason is None and status.get(month) not in {"reviewed", "reconciled"}:
                    note = (
                        f"Conciliado automaticamente con {used} extractos de {owners}: todos sus "
                        "movimientos coinciden, sin pendientes ni avisos en cola de ningun titular."
                    )
                    db.execute(
                        """INSERT INTO coverage VALUES (?,'reconciled',?) ON CONFLICT(month)
                        DO UPDATE SET status='reconciled',note=excluded.note""", (month, note),
                    )
                    self._audit(db, "coverage-reconciled", month, f"{used} extractos")
                    changed["reconciled"].append(month)
                elif reason is not None and status.get(month) in {"partial", "unverified"}:
                    # The note tells why the month is not reconciled yet; the status stays.
                    db.execute(
                        "UPDATE coverage SET note=? WHERE month=?",
                        (f"Sin conciliar con los extractos: {reason}.", month),
                    )
                elif reason is not None and status.get(month) == "reconciled":
                    db.execute(
                        "UPDATE coverage SET status='partial',note=? WHERE month=?",
                        (f"La conciliacion automatica dejo de cumplirse: {reason}.", month),
                    )
                    self._audit(db, "coverage-reconcile-revoked", month, reason)
                    changed["revoked"].append(month)
        return changed

    @staticmethod
    def _card_firsts(db: sqlite3.Connection) -> dict[tuple[str, str, str], str]:
        """First statement start of each card number, per owner."""
        return {
            (row["owner"], row["product"], row["suffix"]): row["first"] for row in db.execute(
                f"""SELECT owner,product,suffix,MIN(period_start) AS first FROM statement_checks
                WHERE product IN ({",".join("?" * len(CARD_PRODUCTS))}) GROUP BY owner,product,suffix""",
                sorted(CARD_PRODUCTS),
            )
        }

    def _register_card_series(self, db: sqlite3.Connection) -> None:
        """Ask about a card number whose statements start after another card of the same owner.

        It may be that card reissued with a new number, or a new card: only a person
        knows. Cards seen from the first statements are recorded as cards of their own.
        """
        known = {
            (row["owner"], row["product"], row["suffix"])
            for row in db.execute("SELECT owner,product,suffix FROM card_series")
        }
        firsts = self._card_firsts(db)
        for key, first in sorted(firsts.items(), key=lambda item: item[1]):
            if key in known:
                continue
            later = any(other[0] == key[0] and other != key and start < first for other, start in firsts.items())
            db.execute(
                "INSERT INTO card_series VALUES (?,?,?,?,?)",
                (*key, None, None) if later else (*key, "", datetime.now(timezone.utc).isoformat()),
            )
            if later:
                self._audit(db, "card-series-question", f"{key[0]}:{key[1]}:*{key[2]}", first)

    def _card_options(self, db: sqlite3.Connection, owner: str, product: str, suffix: str) -> list[dict]:
        """Earlier cards of the owner that a new card number may continue."""
        firsts = self._card_firsts(db)
        first = firsts.get((owner, product, suffix))
        taken = {
            row["replaces"] for row in db.execute(
                "SELECT replaces FROM card_series WHERE owner=? AND replaces<>''", (owner,),
            )
        }
        options = [
            {"product": key[1], "suffix": key[2], "first": start, "last": db.execute(
                "SELECT MAX(period_start) FROM statement_checks WHERE owner=? AND product=? AND suffix=?", key,
            ).fetchone()[0]}
            for key, start in firsts.items()
            if key[0] == owner and first is not None and start < first and f"{key[1]}:{key[2]}" not in taken
        ]
        # Most likely first: the same brand, then the card whose statements stopped last.
        options.sort(key=lambda option: option["last"], reverse=True)
        options.sort(key=lambda option: option["product"] != product)
        return options

    def card_questions(self) -> list[dict]:
        """Card numbers waiting for a person to say whether they replace an earlier card."""
        with self.connect() as db:
            firsts = self._card_firsts(db)
            return [
                {
                    "owner": row["owner"], "product": row["product"], "suffix": row["suffix"],
                    "first": firsts.get((row["owner"], row["product"], row["suffix"])),
                    "options": self._card_options(db, row["owner"], row["product"], row["suffix"]),
                }
                for row in db.execute(
                    "SELECT * FROM card_series WHERE decided_at IS NULL ORDER BY owner,product,suffix",
                )
            ]

    def decide_card_series(self, owner: str, product: str, suffix: str, replaces: str | None) -> None:
        """Record a person's answer: the card replaces an earlier one ('PRODUCT:SUFFIX') or is new (None)."""
        owner = owner_id(owner)
        with self.connect() as db:
            row = db.execute(
                "SELECT decided_at FROM card_series WHERE owner=? AND product=? AND suffix=?",
                (owner, product, suffix),
            ).fetchone()
            if row is None or row["decided_at"] is not None:
                raise ValueError("No hay una pregunta pendiente para esa tarjeta.")
            options = {f"{option['product']}:{option['suffix']}" for option in self._card_options(db, owner, product, suffix)}
            if replaces is not None and replaces not in options:
                raise ValueError("La tarjeta elegida no es una tarjeta anterior de ese titular.")
            db.execute(
                "UPDATE card_series SET replaces=?,decided_at=? WHERE owner=? AND product=? AND suffix=?",
                (replaces or "", datetime.now(timezone.utc).isoformat(), owner, product, suffix),
            )
            self._audit(
                db, "card-series-local-user", f"{owner}:{product}:*{suffix}",
                f"reemplaza {replaces}" if replaces else "tarjeta nueva",
            )

    @staticmethod
    def _join_card_series(
        db: sqlite3.Connection, periods: dict[tuple[str, str, str], list[tuple[date, date, int]]],
    ) -> tuple[dict, dict[tuple[str, str, str], str], set[tuple[str, str, str]]]:
        """One series per card across its number changes, its display name, and unanswered numbers."""
        links, unanswered = {}, set()
        for row in db.execute("SELECT * FROM card_series"):
            key = (row["owner"], row["product"], row["suffix"])
            if row["decided_at"] is None:
                unanswered.add(key)
            elif row["replaces"]:
                links[key] = (row["owner"], *row["replaces"].split(":", 1))

        def root(key: tuple[str, str, str]) -> tuple[str, str, str]:
            seen = {key}
            while key in links and links[key] not in seen:
                key = links[key]
                seen.add(key)
            return key

        joined: dict[tuple[str, str, str], list[tuple[date, date, int]]] = {}
        members: dict[tuple[str, str, str], list[tuple[str, str, str]]] = {}
        for key in sorted(periods, key=lambda k: min(start for start, _, _ in periods[k])):
            joined.setdefault(root(key), []).extend(periods[key])
            members.setdefault(root(key), []).append(key)
        names = {
            key: " / ".join(f"{product} *{suffix}" for _, product, suffix in parts)
            + f" de {OWNERS.get(key[0], key[0])}"
            for key, parts in members.items()
        }
        return joined, names, unanswered

    @staticmethod
    def _reconcile_month(
        db: sqlite3.Connection, month: str, periods: dict[tuple[str, str, str], list[tuple[date, date, int]]],
        names: dict[tuple[str, str, str], str] | None = None, unanswered: set[tuple[str, str, str]] = frozenset(),
    ) -> tuple[str | None, int, str]:
        """Why the month is not reconciled (None when it is), how many statements back it and whose."""
        first = date.fromisoformat(month + "-01")
        last = date(first.year + first.month // 12, first.month % 12 + 1, 1) - timedelta(days=1)
        required = {key: rows for key, rows in periods.items() if min(start for start, _, _ in rows) <= last}
        if not required:
            return "no hay extractos para el mes", 0, ""
        used = 0
        next_first = last + timedelta(days=1)
        asked = sorted(unanswered & set(required))
        if asked:
            owner, product, suffix = asked[0]
            return (
                f"falta decidir en Decisiones si la tarjeta {product} *{suffix} de "
                f"{OWNERS.get(owner, owner)} es nueva o reemplaza a otra"
            ), 0, ""
        for (owner, product, suffix), rows in required.items():
            name = (names or {}).get((owner, product, suffix)) or f"{product} *{suffix} de {OWNERS.get(owner, owner)}"
            if product in CARD_PRODUCTS:
                # A card statement carries the month of its cut-off date but lists the
                # movements since the previous cut-off, in the middle of the month before.
                # The statements of this month and the next one hold every day of it.
                overlapping = [row for row in rows if row[0] in (first, next_first)]
                for needed in (first, next_first):
                    if not any(row[0] == needed for row in overlapping):
                        return f"falta el extracto {needed:%Y-%m} de {name}", 0, ""
            else:
                overlapping = [row for row in rows if row[0] <= last and row[1] >= first]
                day = first
                while day <= last:
                    if not any(start <= day <= end for start, end, _ in overlapping):
                        return f"falta el extracto de {name} que cubre el {day.isoformat()}", 0, ""
                    day += timedelta(days=1)
            if any(missing for _, _, missing in overlapping):
                return f"el extracto de {name} tiene movimientos sin coincidir", 0, ""
            used += len(overlapping)
        if db.execute(
            "SELECT 1 FROM transactions WHERE day LIKE ? AND decision IS NULL LIMIT 1", (month + "-%",),
        ).fetchone():
            return "hay movimientos pendientes de decision", 0, ""
        if db.execute(
            "SELECT 1 FROM extraction_failures WHERE day LIKE ? LIMIT 1", (month + "-%",),
        ).fetchone():
            return "hay avisos en la cola de revision", 0, ""
        owners = sorted({OWNERS.get(owner, owner) for owner, _, _ in required})
        return None, used, " y ".join(owners)

    def coverage(self, month: str, status: str, note: str) -> None:
        if not MONTH.fullmatch(month):
            raise ValueError("Mes invalido.")
        if status not in {"partial", "unverified", "reviewed"}:
            raise ValueError("Estado de cobertura invalido.")
        note = clean_text(note, 1000)
        with self.connect() as db:
            if status == "reviewed" and db.execute(
                "SELECT 1 FROM extraction_failures WHERE day LIKE ? LIMIT 1", (month + "-%",),
            ).fetchone():
                raise ValueError("Hay avisos pendientes de extraccion en este mes; reintenta su importacion.")
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
                "categories": config["categories"] if config else [],
                "income_categories": INCOME_CATEGORIES,
                "transactions": [
                    {**dict(row), "group": group_of(row)} for row in db.execute(
                        f"SELECT {STATE_COLUMNS} FROM transactions ORDER BY day,id"
                    )
                ],
                "owners": [{"id": key, "label": label} for key, label in OWNERS.items()],
                "owner_accounts": [
                    dict(row) for row in db.execute("SELECT * FROM owner_accounts ORDER BY owner,suffix")
                ],
                "group_rules": [
                    dict(row) for row in db.execute("SELECT * FROM group_rules ORDER BY owner,group_key")
                ],
                "statement_checks": [
                    dict(row) for row in db.execute(
                        "SELECT * FROM statement_checks ORDER BY period_end,product,suffix",
                    )
                ],
                "coverage": [dict(row) for row in db.execute("SELECT * FROM coverage ORDER BY month")],
                # Proposals only for movements still pending; a decision makes them irrelevant.
                "category_suggestions": {
                    row["event_id"]: {"category": row["category"], "source": row["source"]}
                    for row in db.execute(
                        """SELECT s.* FROM category_suggestions s JOIN transactions t
                        ON t.id=s.event_id WHERE t.decision IS NULL""",
                    )
                },
                "rules": [
                    dict(row) for row in db.execute("SELECT * FROM merchant_rules ORDER BY pattern")
                ],
                "extraction_failures": [
                    dict(row) for row in db.execute(
                        "SELECT * FROM extraction_failures ORDER BY day,received,message_id",
                    )
                ],
                "card_questions": self.card_questions(),
            }

    def summary(self, month: str, owner: str = "santiago") -> str:
        """Plain-text close of one member, or of the household without money between members."""
        owner = owner_id(owner, allow_household=True)
        rows = [
            row for row in self.state()["transactions"] if row["day"].startswith(month)
            and (row["owner"] == owner or (owner == HOUSEHOLD and not row["between_owners"]))
        ]
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
            income = sum(row["amount_minor"] for row in selected if row["decision"] == "income")
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
            third = sum(
                row["amount_minor"] for row in selected if row["decision"] == "third_party_payment"
            )
            # Third-party payments to a card reduce spending; without them the line is unchanged.
            paid_back = (
                f"abonos de terceros {Decimal(third) / 100:.2f} (restan del gasto); "
                f"gasto neto {Decimal(personal + family - third) / 100:.2f}; " if third else ""
            )
            parts.append(
                f"{currency}: personal {Decimal(personal) / 100:.2f}; "
                f"familiar {Decimal(family) / 100:.2f}; " + paid_back +
                f"inversiones {Decimal(investment - withdrawals) / 100:.2f} (fuera de gastos); "
                f"retiros de inversion {Decimal(withdrawals) / 100:.2f}; "
                f"retiro en efectivo {Decimal(cash) / 100:.2f} (incluido en gastos); "
                f"ingresos {Decimal(income) / 100:.2f}; "
                f"balance {Decimal(income - personal - family + third) / 100:.2f} (ingresos - gastos)"
            )
        pending = sum(row["decision"] is None for row in rows)
        unresolved = sum(
            failure["day"].startswith(month) for failure in self.state()["extraction_failures"]
        )
        return (
            f"Cierre {month}: " + (" | ".join(parts) or "sin gastos confirmados") +
            f"\nDudas abiertas: {pending}. Avisos sin extraer: {unresolved}. "
            "Cobertura no garantizada; revisa el panel."
        )
