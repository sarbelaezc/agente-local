"""Checks on the rendered panel that several tests share."""

from __future__ import annotations

from collections.abc import Iterator

# For each element: its text contrast against the first opaque background behind it
# (WCAG relative luminance) and its height, to check readability and touch size.
_READABILITY = r"""elements => elements.map(element => {
    const luminance = color => color.match(/[\d.]+/g).slice(0, 3).map(Number)
        .map(value => value / 255)
        .map(value => value <= .04045 ? value / 12.92 : ((value + .055) / 1.055) ** 2.4)
        .reduce((total, value, index) => total + value * [.2126, .7152, .0722][index], 0);
    let host = element, background = "rgba(0, 0, 0, 0)";
    while (host && /rgba\(.*, 0\)$/.test(background)) {
        background = getComputedStyle(host).backgroundColor;
        host = host.parentElement;
    }
    const values = [luminance(getComputedStyle(element).color), luminance(background)];
    return {
        name: (element.textContent.trim() || element.value || "").slice(0, 40),
        ratio: (Math.max(...values) + .05) / (Math.min(...values) + .05),
        height: element.getBoundingClientRect().height,
    };
})"""


def readability(locator) -> list[dict]:
    """Contrast ratio and height of every element the locator matches."""
    return locator.evaluate_all(_READABILITY)


def in_both_themes(page) -> Iterator[str]:
    """Yield "light", then switch the panel to dark and yield "dark"."""
    yield "light"
    page.locator("#theme-toggle").click()
    yield "dark"


def assert_readable(locator, theme: str, *, at_least: int = 1, min_height: float = 0) -> None:
    """Every matched element has at least 4.5:1 contrast (WCAG AA) and the given height."""
    found = readability(locator)
    assert len(found) >= at_least, (theme, found)
    bad = [item for item in found if item["ratio"] < 4.5 or item["height"] < min_height]
    assert not bad, (theme, bad)
