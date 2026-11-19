from __future__ import annotations

from typing import Protocol

from monthly_close.store import Store, merchant_words


class CategoryModel(Protocol):
    def suggest_categories(self, merchants: list[str], categories: list[str]) -> dict: ...


def _ask(model: CategoryModel, merchants: list[str], categories: list[str]) -> dict[str, str]:
    """One validated category per merchant: unasked merchants and unauthorized categories are dropped."""
    answer = model.suggest_categories(merchants, categories)
    raw = answer.get("suggestions")
    if not isinstance(raw, list):
        return {}
    asked = set(merchants)
    chosen: dict[str, str] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        merchant, category = item.get("merchant"), item.get("category")
        if not isinstance(merchant, str) or not isinstance(category, str):
            continue
        if merchant in asked and category in categories and merchant not in chosen:
            chosen[merchant] = category
    return chosen


def suggest_rules(store: Store, model: CategoryModel, limit: int = 25) -> list[dict]:
    """Ask the local model for a category per uncovered merchant; save nothing.

    Only merchant names and the configured category list reach the model. Its answer is
    untrusted: a suggestion is kept only if its merchant was asked about and its category
    is authorized. The person still chooses Personal or Familiar and approves each rule.
    """
    asked = store.unruled_merchants(limit)
    if not asked:
        return []
    counts = {item["merchant"]: item["count"] for item in asked}
    chosen = _ask(model, list(counts), store.preferences().categories)
    return [
        {"pattern": merchant, "category": category, "count": counts[merchant]}
        for merchant, category in chosen.items()
    ]


def suggest_pending_categories(store: Store, model: CategoryModel, limit: int = 60) -> dict[str, str]:
    """Suggest a category for each pending purchase (statement ones included); save nothing.

    Returns movement id -> category. The panel only preselects it: the person decides.
    """
    by_merchant: dict[str, list[str]] = {}
    for row in store.pending_purchases():
        words = merchant_words(row["merchant"]).strip()
        if len(words) >= 3:
            by_merchant.setdefault(words, []).append(row["id"])
    ranked = sorted(by_merchant, key=lambda words: (-len(by_merchant[words]), words))[:limit]
    if not ranked:
        return {}
    chosen = _ask(model, ranked, store.preferences().categories)
    return {
        event_id: category
        for merchant, category in chosen.items() for event_id in by_merchant[merchant]
    }
