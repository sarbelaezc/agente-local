from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ChatMessage:
    sender: str
    text: str
    timestamp: str


def build_context(messages: list[ChatMessage], limit: int = 20) -> str:
    if limit < 1:
        raise ValueError("limit must be positive")
    lines = []
    for message in messages[-limit:]:
        sender = message.sender.replace("\n", " ").strip()
        text = message.text.replace("\n", " ").strip()
        timestamp = message.timestamp.replace("\n", " ").strip()
        lines.append(f"[{timestamp}] {sender}: {text}")
    return "\n".join(lines)
