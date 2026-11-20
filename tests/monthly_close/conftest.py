from __future__ import annotations

import threading
from collections.abc import Callable, Iterator

import pytest

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
