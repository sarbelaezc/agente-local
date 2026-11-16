from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_local_data(tmp_path, monkeypatch):
    """Tests never read or write the real data kept in the project's .local folder."""
    monkeypatch.setenv("AGENTE_LOCAL_DATA", str(tmp_path / "local-data"))
