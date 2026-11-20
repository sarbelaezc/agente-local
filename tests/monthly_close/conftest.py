from __future__ import annotations

import threading
from collections.abc import Callable, Iterator

import pytest

from builders import SENDER, settings
from monthly_close.domain import Preferences
from monthly_close.extraction import Mail
from monthly_close.server import create_server
from monthly_close.store import Store


@pytest.fixture
def categories() -> list[str]:
    return ["Compras", "Salud y farmacia", "Transporte", "Vacaciones", "Otros gastos"]


@pytest.fixture
def start_panel() -> Iterator[Callable[..., str]]:
    """Start the local panel for a store and return its URL; every panel stops after the test.

    The server checks for shutdown every 0.05 s instead of the default 0.5 s, so stopping
    it does not make each test wait.
    """
    started: list[tuple] = []

    def start(store: Store, model=None) -> str:
        server = create_server(store, 0, model)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
        thread.start()
        started.append((server, thread))
        return f"http://127.0.0.1:{server.server_port}"

    yield start
    for server, thread in started:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.fixture
def make_preferences(categories) -> Callable[..., Preferences]:
    """Step-zero answers with any field changed: make_preferences(excluded_accounts=[...])."""
    return lambda **overrides: Preferences.parse(settings(overrides.pop("categories", categories), **overrides))


@pytest.fixture
def make_store(tmp_path, categories) -> Callable[..., Store]:
    """A configured store in the test's folder: make_store(name, **changed_settings)."""
    def make(name: str = "close.sqlite3", **overrides) -> Store:
        store = Store(tmp_path / name)
        store.configure(settings(overrides.pop("categories", categories), **overrides))
        return store

    return make


@pytest.fixture
def preferences(make_preferences) -> Preferences:
    """The usual answers, with one excluded account; a module can override this fixture."""
    return make_preferences(excluded_accounts=["Cuenta B"])


@pytest.fixture
def mail() -> Mail:
    """A purchase notice from the approved sender."""
    return Mail(
        "message-1", SENDER, "Compra Tienda",
        "2026-03-01T02:15:00Z",
        "Compraste en Tienda con Tarjeta A $ 1.234,56.",
    )
