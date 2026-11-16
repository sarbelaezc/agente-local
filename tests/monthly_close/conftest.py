from __future__ import annotations

import pytest


@pytest.fixture
def categories() -> list[str]:
    return ["Compras", "Salud y farmacia", "Transporte", "Vacaciones", "Otros gastos"]
