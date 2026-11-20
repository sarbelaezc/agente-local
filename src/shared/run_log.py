"""Per-execution log of a feature: what each run printed, kept for the last runs.

Every command writes one file in its feature's local data folder (outside Git). Only
what the program already prints is kept: progress-bar redraws stay on screen, and
message bodies are never printed, so they never reach a log. A command whose screen
output lists personal data can keep only its errors and its result.
"""
from __future__ import annotations

import re
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator, TextIO

LOG_DIR = "logs"
KEEP = 10
# 20261130-220001-scheduled.log: sortable by name, one per execution.
LOG_NAME = re.compile(r"\d{8}-\d{6}-[a-z][a-z-]{0,40}\.log")


class _Tee:
    """Write to the console stream and to the log; console-only redraws are skipped."""

    def __init__(self, stream: TextIO, log: TextIO) -> None:
        self._stream = stream
        self._log = log

    def write(self, text: str) -> int:
        written = self._stream.write(text)
        if not text.startswith("\r"):  # the progress bar redraws its own line
            self._log.write(text)
        return written

    def flush(self) -> None:
        self._stream.flush()
        self._log.flush()

    def isatty(self) -> bool:
        return bool(getattr(self._stream, "isatty", lambda: False)())

    def __getattr__(self, name: str):
        return getattr(self._stream, name)


def new_log_path(data_dir: Path, command: str, now: datetime | None = None) -> Path:
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    return data_dir / LOG_DIR / f"{stamp}-{command}.log"


def checked_log_path(data_dir: Path, path: Path) -> Path:
    """A log path given by the launcher must be a log file inside the logs folder."""
    folder = (data_dir / LOG_DIR).resolve()
    resolved = path.expanduser().resolve()
    if resolved.parent != folder or not LOG_NAME.fullmatch(resolved.name):
        raise ValueError("El registro debe ser un archivo .log dentro de la carpeta logs de los datos locales.")
    return resolved


def prune(folder: Path, keep: int = KEEP) -> list[Path]:
    """Delete all but the newest execution logs; other files are never touched."""
    logs = sorted(path for path in folder.glob("*.log") if LOG_NAME.fullmatch(path.name))
    removed = []
    for path in logs[:-keep] if keep > 0 else logs:
        try:
            path.unlink()
        except PermissionError:
            continue  # still open, e.g. by a running panel; the next run removes it
        removed.append(path)
    return removed


@contextmanager
def run_log(
    data_dir: Path, command: str, path: Path | None = None, *,
    feature: str = "cierre mensual", capture_output: bool = True,
) -> Iterator[Path]:
    """Copy what the run prints to its log, then keep only the last runs.

    With ``capture_output=False`` only standard error (progress and stops) is kept,
    not the standard output report.
    """
    target = checked_log_path(data_dir, path) if path else new_log_path(data_dir, command)
    target.parent.mkdir(parents=True, exist_ok=True)
    out, err = sys.stdout, sys.stderr
    status = "error"
    try:
        with target.open("a", encoding="utf-8") as log:
            log.write(f"== {datetime.now().isoformat(timespec='seconds')} {feature}: {command}\n")
            if not capture_output:
                log.write("(el informe de pantalla no se guarda: puede listar datos personales)\n")
            sys.stdout = _Tee(out, log) if capture_output else out
            sys.stderr = _Tee(err, log)
            try:
                yield target
                status = "terminado"
            except SystemExit as exc:
                status = "terminado" if exc.code in (0, None) else f"detenido (codigo {exc.code})"
                raise
            except KeyboardInterrupt:
                status = "detenido por el usuario"
                raise
            finally:
                sys.stdout, sys.stderr = out, err
                log.write(f"== {datetime.now().isoformat(timespec='seconds')} fin: {status}\n")
    finally:
        prune(target.parent)
