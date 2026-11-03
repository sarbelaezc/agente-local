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


def ensure_feature_ollama_isolation(
    config: dict[str, Any], feature: str, feature_url: str
) -> None:
    other_feature = "invoices" if feature == "teams" else "teams"
    other_config = config.get(other_feature)
    if not isinstance(other_config, dict):
        return
    fallback = config.get("ollama") if feature == "teams" else None
    other_ollama = other_config.get("ollama", fallback) or {}
    if not isinstance(other_ollama, dict):
        raise ValueError(f"{other_feature}.ollama must be a mapping")
    default_port = 11434 if feature == "teams" else 11435
    other_url = str(other_ollama.get("url", f"http://127.0.0.1:{default_port}"))
    feature_endpoint = urlparse(feature_url.rstrip("/"))
    other_endpoint = urlparse(other_url.rstrip("/"))
    loopback_hosts = ("127.0.0.1", "localhost", "::1")
    feature_host = "loopback" if feature_endpoint.hostname in loopback_hosts else feature_endpoint.hostname
    other_host = "loopback" if other_endpoint.hostname in loopback_hosts else other_endpoint.hostname
    if (feature_host, feature_endpoint.port or 80) == (
        other_host,
        other_endpoint.port or 80,
    ):
        raise ValueError("Teams and invoices must use separate Ollama instances")


def load_settings(path: str | Path) -> Settings:
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as stream:
        raw: dict[str, Any] = yaml.safe_load(stream) or {}

    if not isinstance(raw, dict):
        raise ValueError("Configuration must be a mapping")
    unified = "teams" in raw or "invoices" in raw
    root = raw
    if unified:
        raw = root.get("teams")
        if not isinstance(raw, dict):
            raise ValueError("teams configuration must be a mapping")
    ollama = raw.get("ollama") or {}
    if not isinstance(ollama, dict):
        raise ValueError("teams.ollama must be a mapping")

    chats = tuple(
        AllowedChat(id=str(chat["id"]), name=str(chat["name"]))
        for chat in raw.get("allowed_chats", [])
    )
    if not chats:
        raise ValueError("At least one allowed chat is required")

    edge = raw.get("edge", {})
    storage = raw.get("storage", {})
    ollama_url = str(ollama.get("url", "http://127.0.0.1:11435")).rstrip("/")
    parsed_ollama_url = urlparse(ollama_url)
    if parsed_ollama_url.scheme != "http" or parsed_ollama_url.hostname not in (
        "127.0.0.1",
        "localhost",
        "::1",
    ):
        raise ValueError("Ollama URL must be local (127.0.0.1, localhost, or ::1)")
    if unified:
        ensure_feature_ollama_isolation(root, "teams", ollama_url)

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
