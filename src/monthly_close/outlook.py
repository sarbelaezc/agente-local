from __future__ import annotations

from dataclasses import dataclass


class OutlookError(RuntimeError):
    """The mailbox did not answer as expected; the run stops instead of guessing."""


@dataclass(frozen=True)
class Candidate:
    """A listed notice from an approved sender, before its source is read."""

    item_id: str
    sender: str
    subject: str
