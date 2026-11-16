from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def data_root() -> Path:
    """Folder for local data and configuration of every feature.

    It is ``.local`` inside the project, which Git ignores. Set ``AGENTE_LOCAL_DATA``
    to keep it elsewhere, for example outside a synchronized folder.
    """
    override = os.environ.get("AGENTE_LOCAL_DATA", "").strip()
    return Path(override).expanduser() if override else REPO_ROOT / ".local"
