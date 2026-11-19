from __future__ import annotations

from collections import defaultdict
from typing import Protocol

from monthly_close.store import Store, merchant_words


class CategoryModel(Protocol):
    def suggest_categories(
        self, merchants: list[str], categories: list[str], examples: list[dict] | None = None,
    ) -> dict: ...


def _tokens(words: str) -> frozenset[str]:
    """Words that identify a merchant; bare numbers (branch or city codes) do not."""
    return frozenset(word for word in words.split() if not word.isdigit() and len(word) >= 2)


class CategoryHistory:
    """What the person already decided, as merchant words -> category.

    Sources are the person's own rules and the categories of purchases they decided as
    Personal or Familiar. A merchant decided with different categories is ambiguous and
    gives no deterministic answer, but still serves as an example.
    """

    def __init__(self, store: Store, categories: list[str]) -> None:
        seen: dict[str, set[str]] = defaultdict(set)
        for row in store.confirmed_purchase_categories():
            words = merchant_words(row["merchant"]).strip()
            if row["category"] in categories and words:
                seen[words].add(row["category"])
        self.exact = {words: next(iter(found)) for words, found in seen.items() if len(found) == 1}
        self.rules = sorted(
            ((rule["pattern"], rule["category"]) for rule in store.rules()
             if rule["decision"] != "income" and rule["category"] in categories),
            key=lambda item: -len(item[0]),
        )
        self._examples = [(words, category) for words, found in seen.items() for category in found]
        self._examples += self.rules

    def known(self, words: str) -> tuple[str, str] | None:
        """(category, source) without the model: the longest matching rule, else a consistent past decision."""
        padded = f" {words} "
        for pattern, category in self.rules:
            if f" {pattern} " in padded:
                return category, "rule"
        if words in self.exact:
            return self.exact[words], "history"
        return None

    # 5 examples per merchant (up to 150) measured better than 3 (up to 60) on real decisions.
    def examples(self, merchants: list[str], per_merchant: int = 5, limit: int = 150) -> list[dict]:
        """The confirmed merchants that share the most words with the ones being asked about."""
        chosen: dict[tuple[str, str], None] = {}
        for merchant in merchants:
            asked = _tokens(merchant)
            scored = []
            for words, category in self._examples:
                shared = asked & _tokens(words)
                if shared:
                    scored.append((len(shared) / len(asked | _tokens(words)), words, category))
            for _, words, category in sorted(scored, reverse=True)[:per_merchant]:
                chosen[(words, category)] = None
        return [{"merchant": words, "category": category} for words, category in list(chosen)[:limit]]


def _ask(
    model: CategoryModel, merchants: list[str], categories: list[str], examples: list[dict],
) -> dict[str, str]:
    """One validated category per merchant: unasked merchants and unauthorized categories are dropped."""
    answer = model.suggest_categories(merchants, categories, examples)
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

    Only merchant names, the configured category list and confirmed examples reach the
    model. Its answer is untrusted: a suggestion is kept only if its merchant was asked
    about and its category is authorized. The person still chooses Personal or Familiar
    and approves each rule.
    """
    asked = store.unruled_merchants(limit)
    if not asked:
        return []
    counts = {item["merchant"]: item["count"] for item in asked}
    categories = store.preferences().categories
    history = CategoryHistory(store, categories)
    chosen = {}
    for merchant in counts:
        known = history.known(merchant)
        if known:
            chosen[merchant] = known[0]
    unknown = [merchant for merchant in counts if merchant not in chosen]
    if unknown:
        chosen |= _ask(model, unknown, categories, history.examples(unknown))
    return [
        {"pattern": merchant, "category": category, "count": counts[merchant]}
        for merchant, category in chosen.items()
    ]


def suggest_pending_categories(store: Store, model: CategoryModel, limit: int = 60) -> dict[str, dict]:
    """Suggest a category for each pending purchase (statement ones included); save nothing.

    Returns movement id -> {category, source}. The source is "rule" or "history" when the
    person's own rules or past decisions answer it, and "ollama" otherwise. The panel only
    preselects it: the person decides.
    """
    by_merchant: dict[str, list[str]] = {}
    for row in store.pending_purchases():
        words = merchant_words(row["merchant"]).strip()
        if len(words) >= 3:
            by_merchant.setdefault(words, []).append(row["id"])
    if not by_merchant:
        return {}
    categories = store.preferences().categories
    history = CategoryHistory(store, categories)
    chosen: dict[str, tuple[str, str]] = {}
    for words in by_merchant:
        known = history.known(words)
        if known:
            chosen[words] = known
    unknown = sorted(
        (words for words in by_merchant if words not in chosen),
        key=lambda words: (-len(by_merchant[words]), words),
    )[:limit]
    if unknown:
        for words, category in _ask(model, unknown, categories, history.examples(unknown)).items():
            chosen[words] = (category, "ollama")
    return {
        event_id: {"category": category, "source": source}
        for words, (category, source) in chosen.items() for event_id in by_merchant[words]
    }
