from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml


@dataclass(frozen=True)
class AllowedChat:
    id: str
    name: str


@dataclass(frozen=True)
class Settings:
    edge_executable: str
    edge_profile_dir: str
    teams_url: str
    database_path: str
    allowed_chats: tuple[AllowedChat, ...]
    ollama_url: str
    ollama_model: str
    context_messages: int = 20
    allowed_ollama_hosts: tuple[str, ...] = ("127.0.0.1", "localhost", "::1")


def _expand_local_path(value: str, label: str) -> str:
    expanded = os.path.expandvars(value)
    if re.search(r"%[^%]+%", expanded):
        raise ValueError(f"{label} contains an unset environment variable: {value}")
    return str(Path(expanded).expanduser())


def load_settings(path: str | Path) -> Settings:
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as stream:
        raw: dict[str, Any] = yaml.safe_load(stream) or {}

    chats = tuple(
        AllowedChat(id=str(chat["id"]), name=str(chat["name"]))
        for chat in raw.get("allowed_chats", [])
    )
    if not chats:
        raise ValueError("At least one allowed chat is required")

    ollama = raw.get("ollama", {})
    edge = raw.get("edge", {})
    storage = raw.get("storage", {})
    ollama_url = str(ollama.get("url", "http://127.0.0.1:11434")).rstrip("/")
    parsed_ollama_url = urlparse(ollama_url)
    if parsed_ollama_url.scheme != "http" or parsed_ollama_url.hostname not in (
        "127.0.0.1",
        "localhost",
        "::1",
    ):
        raise ValueError("Ollama URL must be local (127.0.0.1, localhost, or ::1)")

    return Settings(
        edge_executable=str(edge["executable"]),
        edge_profile_dir=_expand_local_path(str(edge["profile_dir"]), "edge.profile_dir"),
        teams_url=str(raw.get("teams_url", "https://teams.microsoft.com/")),
        database_path=_expand_local_path(
            str(storage["database_path"]), "storage.database_path"
        ),
        allowed_chats=chats,
        ollama_url=ollama_url,
        ollama_model=str(ollama.get("model", "qwen2.5:7b")),
        context_messages=int(raw.get("context_messages", 20)),
    )
