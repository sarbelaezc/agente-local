from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml


def _path(value: str) -> Path:
    expanded = os.path.expandvars(value)
    if re.search(r"%[^%]+%", expanded):
        raise ValueError("A required environment variable for a local path is unset")
    return Path(expanded).expanduser()


@dataclass(frozen=True)
class MailSettings:
    executable: Path
    profile_dir: Path
    workbook: Path
    backup_dir: Path
    sheet: str
    batch_limit: int


def load_settings(path: Path) -> MailSettings:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Mail configuration must be a mapping")
    limit = int(raw.get("batch_limit", 20))
    if not 1 <= limit <= 50:
        raise ValueError("Pilot batch limit must be between 1 and 50")
    return MailSettings(
        _path(raw["opera_executable"]), _path(raw["profile_dir"]),
        _path(raw["workbook"]), _path(raw["backup_dir"]),
        str(raw.get("sheet", "Hoja1")), limit,
    )
