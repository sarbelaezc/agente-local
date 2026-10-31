from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from config.settings import ensure_feature_ollama_isolation
from mail_invoices.local_model import validate_local_url

DEFAULT_TESSERACT = r"C:\Program Files\Tesseract-OCR\tesseract.exe"


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
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen2.5:7b"
    tesseract_executable: Path = Path(DEFAULT_TESSERACT)
    tessdata_dir: Path | None = None
    ocr_languages: str = "spa+eng"
    folder_rules: dict[str, str] = field(default_factory=dict)
    attachments_dir: Path = Path("tmp")
    approver: str = ""


def _ollama_settings(
    root: dict, invoices: dict, unified: bool
) -> tuple[dict, str]:
    ollama = invoices.get("ollama")
    if ollama is None and unified:
        ollama = root.get("ollama")
    ollama = ollama or {}
    if not isinstance(ollama, dict):
        raise ValueError("ollama, ocr and folder_rules must be mappings")
    url = validate_local_url(str(ollama.get("url", "http://127.0.0.1:11434")))
    if unified:
        ensure_feature_ollama_isolation(root, "invoices", url)
    return ollama, url


def load_settings(path: Path) -> MailSettings:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Mail configuration must be a mapping")
    unified = "teams" in raw or "invoices" in raw
    root = raw
    if unified:
        raw = root.get("invoices")
        if not isinstance(raw, dict):
            raise ValueError("invoices configuration must be a mapping")
    ollama, ollama_url = _ollama_settings(root, raw, unified)
    limit = int(raw.get("batch_limit", 20))
    if not 1 <= limit <= 50:
        raise ValueError("Pilot batch limit must be between 1 and 50")
    ocr = raw.get("ocr") or {}
    rules = raw.get("folder_rules") or {}
    if not isinstance(ocr, dict) or not isinstance(rules, dict):
        raise ValueError("ollama, ocr and folder_rules must be mappings")
    tessdata = ocr.get("tessdata_dir")
    return MailSettings(
        _path(raw["opera_executable"]), _path(raw["profile_dir"]),
        _path(raw["workbook"]), _path(raw["backup_dir"]),
        str(raw.get("sheet", "Hoja1")), limit,
        ollama_url=ollama_url,
        ollama_model=str(ollama.get("model", "qwen2.5:7b")),
        tesseract_executable=_path(str(ocr.get("tesseract_executable", DEFAULT_TESSERACT))),
        tessdata_dir=_path(str(tessdata)) if tessdata else None,
        ocr_languages=str(ocr.get("languages", "spa+eng")),
        folder_rules={str(key): str(value) for key, value in rules.items()},
        attachments_dir=_path(str(raw.get("attachments_dir", "tmp"))),
        approver=" ".join(str(raw.get("approver") or "").split())[:100],
    )
