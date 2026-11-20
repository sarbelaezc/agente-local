from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest


@pytest.fixture(autouse=True)
def isolated_local_data(tmp_path, monkeypatch):
    """Tests never read or write the real data kept in the project's .local folder."""
    monkeypatch.setenv("AGENTE_LOCAL_DATA", str(tmp_path / "local-data"))


@pytest.fixture(scope="session")
def browser() -> Iterator:
    """One headless Chromium for the whole run: launching it per test cost about a second each."""
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as playwright:
        chromium = playwright.chromium.launch(headless=True)
        try:
            yield chromium
        finally:
            chromium.close()


@pytest.fixture
def new_page(browser) -> Iterator[Callable]:
    """Open pages in a fresh browser context, so tests share no cookies, storage or theme."""
    contexts = []

    def open_page(**options):
        context = browser.new_context(**options)
        contexts.append(context)
        return context.new_page()

    yield open_page
    for context in contexts:
        context.close()
