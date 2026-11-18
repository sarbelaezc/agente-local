from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from shared.local_model import validate_local_url
from shared.storage import REPO_ROOT

DEFAULT_TESSERACT = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
CLIENT_ID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
AUTHORITIES = {"consumers", "common"}


def _path(value: str) -> Path:
    expanded = os.path.expandvars(value)
    if re.search(r"%[^%]+%", expanded):
        raise ValueError("A required environment variable for a local path is unset")
    path = Path(expanded).expanduser()
    # Relative paths are anchored at the project, not at whatever folder the command ran from.
    return path if path.is_absolute() else REPO_ROOT / path


@dataclass(frozen=True)
class MailSettings:
    """Invoice settings. graph_client_id is the Azure app shared with the monthly close,
    with Mail.ReadWrite added; each feature keeps its own session and allow-list."""

    graph_client_id: str
    workbook: Path
    backup_dir: Path
    sheet: str
    batch_limit: int
    graph_authority: str = "consumers"
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen2.5:7b"
    tesseract_executable: Path = Path(DEFAULT_TESSERACT)
    tessdata_dir: Path | None = None
    ocr_languages: str = "spa+eng"
    folder_rules: dict[str, str] = field(default_factory=dict)
    # Exact addresses or "@domain" whose mail is never an invoice to register, such as
    # foreign receipts in another currency; they are filed by hand.
    excluded_senders: tuple[str, ...] = ()
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
    return ollama, url


SENDER_RULE = re.compile(r"(?:[^@\s]+)?@[^@\s]+\.[^@\s]+")


def _excluded_senders(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) and SENDER_RULE.fullmatch(item.strip()) for item in value):
        raise ValueError("invoices.excluded_senders must be a list of exact addresses or @domain entries")
    return tuple(dict.fromkeys(item.strip().lower() for item in value))


def is_excluded_sender(sender: str, rules: tuple[str, ...]) -> bool:
    """Exact address, or "@domain" for the sender's domain (not its subdomains)."""
    address = sender.strip().lower()
    return address in rules or ("@" + address.rpartition("@")[2]) in rules


def load_settings(path: Path) -> MailSettings:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Mail configuration must be a mapping")
    unified = "invoices" in raw
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
    client_id = str(raw.get("graph_client_id") or "").strip()
    if not CLIENT_ID.fullmatch(client_id):
        raise ValueError(
            "Configure invoices.graph_client_id with the Application (client) ID of the invoice "
            "app in Azure; the Opera reader was retired and opera_executable/profile_dir are ignored"
        )
    authority = str(raw.get("graph_authority", "consumers"))
    if authority not in AUTHORITIES:
        raise ValueError("invoices.graph_authority must be consumers or common")
    return MailSettings(
        client_id, _path(raw["workbook"]), _path(raw["backup_dir"]),
        str(raw.get("sheet", "Hoja1")), limit,
        graph_authority=authority,
        ollama_url=ollama_url,
        ollama_model=str(ollama.get("model", "qwen2.5:7b")),
        tesseract_executable=_path(str(ocr.get("tesseract_executable", DEFAULT_TESSERACT))),
        tessdata_dir=_path(str(tessdata)) if tessdata else None,
        ocr_languages=str(ocr.get("languages", "spa+eng")),
        folder_rules={str(key): str(value) for key, value in rules.items()},
        attachments_dir=_path(str(raw.get("attachments_dir", "tmp"))),
        approver=" ".join(str(raw.get("approver") or "").split())[:100],
        excluded_senders=_excluded_senders(raw.get("excluded_senders")),
    )
