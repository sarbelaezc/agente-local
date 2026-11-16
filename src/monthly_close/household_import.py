"""Import another household member's movements from local files they already approved.

The files live only in the local data folder. Nothing here reads a mailbox, a browser or
Ollama: every field is validated and every movement is decided through Store rules.
"""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

from monthly_close.domain import INCOME_CATEGORIES, KINDS, clean_text, owner_id
from monthly_close.store import Store

MOVEMENTS_FILE = "movimientos_manuela.normalizado.json"
DECISIONS_FILE = "decisiones_manuela.json"
PARITY_FILE = "paridad_totales_manuela.json"
SOURCE = "manuela"
# Destination decisions as named in Manuela's dashboard.
DESTINATION_DECISIONS = {
    "personal": "personal", "familiar": "family", "inversion": "investment_withdrawal",
    "cajero": "cash_withdrawal", "propia": "exclude", "nocuenta": "exclude",
}
PROPOSALS = {
    "personal", "family", "income", "exclude", "investment_withdrawal", "cash_withdrawal",
}
GROUP_KEY = re.compile(r"(qr|tr|bb|fd|cj|pago):\S.{0,118}")
MAX_RECORDS = 20000


def _read_json(path: Path) -> object:
    if not path.is_file():
        raise ValueError(f"Falta el archivo {path.name} en la carpeta de importacion.")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path.name} no es JSON valido.") from exc


def _optional_text(value: object, limit: int) -> str | None:
    return None if value is None else clean_text(value, limit)


def _record(raw: object, owner: str, rules: dict[str, str]) -> dict:
    """Validate one exported movement and map it to a transaction row."""
    if not isinstance(raw, dict):
        raise ValueError("Movimiento sin formato de objeto.")
    legacy = raw.get("legacy_id")
    if not isinstance(legacy, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", legacy):
        raise ValueError("Identificador de origen invalido.")
    if raw.get("owner") != owner:
        raise ValueError("Titular distinto al de la importacion.")
    day = raw.get("day")
    if not isinstance(day, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise ValueError("Fecha invalida.")
    date.fromisoformat(day)
    time = raw.get("time")
    if time is not None and (not isinstance(time, str) or not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", time)):
        raise ValueError("Hora invalida.")
    amount = raw.get("amount_minor")
    if type(amount) is not int or not 0 < amount <= 10**14:
        raise ValueError("Importe invalido.")
    currency = raw.get("currency")
    if currency != "COP":
        raise ValueError("Solo se importan movimientos en COP.")
    kind = raw.get("kind")
    if kind not in KINDS:
        raise ValueError("Tipo de movimiento no reconocido.")
    proposal = raw.get("decision_proposal")
    if proposal not in PROPOSALS:
        raise ValueError("Decision propuesta no reconocida.")
    if type(raw.get("category_uncertain")) is not bool:
        raise ValueError("Marca de categoria dudosa invalida.")
    group = raw.get("group_key")
    if group is not None:
        if not isinstance(group, str) or not GROUP_KEY.fullmatch(group):
            raise ValueError("Destino invalido.")
        if group not in rules:
            raise ValueError("Destino sin decision en el archivo de decisiones.")
        if rules[group] != proposal:
            raise ValueError("La decision del destino no coincide con la del movimiento.")
        decision = None  # decided by its destination rule
    elif kind == "card_payment":
        # A third party paying the member's card: it reduces her spending (decision 3).
        if proposal != "exclude":
            raise ValueError("Abono a tarjeta con decision inesperada.")
        decision = "third_party_payment"
    else:
        decision = proposal
    category = _optional_text(raw.get("category"), 80)
    if kind == "income":
        income_category = raw.get("income_category")
        if income_category is not None and income_category not in INCOME_CATEGORIES:
            raise ValueError("Tipo de ingreso no reconocido.")
        category = income_category or INCOME_CATEGORIES[-1]
    elif category is None:
        # Destination payments carry no category: Manuela's dashboard counts them as
        # «Otros gastos»; special decisions replace it with their own category.
        category = "Otros gastos"
    reason = (
        "Importado de Manuela: decidido por destino en su panel."
        if group else "Importado de Manuela: aprobado en su panel."
    )
    return {
        "id": f"{owner}:{legacy}", "message_id": f"{owner}-import:{legacy}", "owner": owner,
        "day": day, "time": time, "merchant": clean_text(raw.get("merchant"), 200),
        "account": clean_text(raw.get("account"), 60),
        "account_label": _optional_text(raw.get("account_label"), 80),
        "amount_minor": amount, "currency": currency, "category": category, "kind": kind,
        "decision": decision, "reason": reason, "group_key": group,
        "note": _optional_text(raw.get("note"), 300),
        "category_uncertain": int(raw["category_uncertain"]),
    }


def load_destination_rules(path: Path) -> dict[str, str]:
    raw = _read_json(path)
    if not isinstance(raw, list) or len(raw) > MAX_RECORDS:
        raise ValueError(f"{path.name} debe ser una lista de decisiones.")
    rules: dict[str, str] = {}
    for index, item in enumerate(raw):
        group = item.get("group_key") if isinstance(item, dict) else None
        decision = item.get("decision") if isinstance(item, dict) else None
        if not isinstance(group, str) or not GROUP_KEY.fullmatch(group) or decision not in DESTINATION_DECISIONS:
            raise ValueError(f"Decision de destino {index + 1} invalida.")
        mapped = DESTINATION_DECISIONS[decision]
        if rules.setdefault(group, mapped) != mapped:
            raise ValueError(f"Destino con decisiones contradictorias (registro {index + 1}).")
    return rules


def load_movements(path: Path, owner: str, rules: dict[str, str]) -> list[dict]:
    raw = _read_json(path)
    if not isinstance(raw, list) or not raw or len(raw) > MAX_RECORDS:
        raise ValueError(f"{path.name} debe ser una lista no vacia de movimientos.")
    events = []
    for index, item in enumerate(raw):
        try:
            events.append(_record(item, owner, rules))
        except ValueError as exc:
            # The record number locates the problem without printing its content.
            raise ValueError(f"Movimiento {index + 1}: {exc}") from exc
    if len({event["id"] for event in events}) != len(events):
        raise ValueError("Hay identificadores de origen repetidos.")
    return events


def import_member(store: Store, folder: Path, owner: str = SOURCE) -> dict[str, int]:
    """Validate both files completely, then import them in a single transaction."""
    owner = owner_id(owner)
    store.preferences()  # categories and accounts are validated against the step-zero answers
    rules = load_destination_rules(folder / DECISIONS_FILE)
    events = load_movements(folder / MOVEMENTS_FILE, owner, rules)
    used = {event["group_key"] for event in events if event["group_key"]}
    return store.import_external(owner, SOURCE, events, {key: rules[key] for key in sorted(used)})


def parity_report(store: Store, path: Path, owner: str = SOURCE) -> tuple[bool, list[str]]:
    """Compare imported monthly totals with the member's own dashboard totals.

    The only accepted difference is cash withdrawals: here they count as personal
    spending, in her dashboard they did not.
    """
    raw = _read_json(path)
    if not isinstance(raw, dict):
        raise ValueError(f"{path.name} debe ser un objeto por mes.")
    totals = store.monthly_totals(owner)
    lines, matched = [], True
    for month in sorted(raw):
        expected = raw[month]
        if not re.fullmatch(r"\d{4}-\d{2}", month) or not isinstance(expected, dict):
            raise ValueError(f"{path.name}: mes invalido.")
        got = totals.get(month, {})
        cash = got.get("cash", 0) // 100
        checks = {
            "gasto_personal": got.get("personal", 0) // 100 - cash,
            "gasto_familiar": got.get("family", 0) // 100,
            "ingresos": got.get("income", 0) // 100,
            "terceros": got.get("third_party", 0) // 100,
            "gasto_neto_dashboard": got.get("net", 0) // 100 - cash,
        }
        wrong = [name for name, value in checks.items() if value != expected.get(name)]
        if wrong:
            matched = False
            lines.append(f"{month}: NO coincide en {', '.join(wrong)}.")
        else:
            pesos = f"{cash:,}".replace(",", ".")
            extra = f" (+{pesos} COP de retiro en cajero, aprobado)" if cash else ""
            lines.append(f"{month}: coincide{extra}.")
    return matched, lines
