from __future__ import annotations

from typing import Protocol

from monthly_close.store import Store


class CategoryModel(Protocol):
    def suggest_categories(self, merchants: list[str], categories: list[str]) -> dict: ...


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
    categories = store.preferences().categories
    answer = model.suggest_categories(list(counts), categories)
    raw = answer.get("suggestions")
    if not isinstance(raw, list):
        return []
    seen: set[str] = set()
    suggestions = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        merchant, category = item.get("merchant"), item.get("category")
        if not isinstance(merchant, str) or not isinstance(category, str):
            continue
        if merchant in counts and category in categories and merchant not in seen:
            seen.add(merchant)
            suggestions.append({"pattern": merchant, "category": category, "count": counts[merchant]})
    return suggestions
