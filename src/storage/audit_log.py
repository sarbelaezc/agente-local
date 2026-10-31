from __future__ import annotations

from pathlib import Path


def append_audit_line(path: str, line: str) -> None:
    audit_path = Path(path)
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with audit_path.open("a", encoding="utf-8") as stream:
        stream.write(line.rstrip("\n") + "\n")
