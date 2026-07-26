"""Conservative frontend window identification for xiaoke.

Kelivo supplies a stable conversation header. Polaris does not, so it can only
resume a previously handed-off window after its first completed user/assistant
pair reappears in the client history. Ambiguous matches deliberately fail
closed as a new window.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from storage import TimelineStore
from timeline_context import fingerprint


@dataclass(frozen=True)
class WindowIdentity:
    session_id: str | None
    source: str
    kind: str


def _source(headers: Any) -> str:
    return headers.get("X-Xiaoke-Source", "unknown").strip().lower() or "unknown"


def _polaris_match(messages: list[dict[str, Any]], store: TimelineStore) -> str | None:
    fingerprints = {
        fingerprint(str(message.get("role", "")), message.get("content"))
        for message in messages
    }
    matches = [
        row["session_id"]
        for row in store.active_continuities()
        if fingerprint("user", row["seed_user"]) in fingerprints
        and fingerprint("assistant", row["seed_assistant"]) in fingerprints
    ]
    return matches[0] if len(matches) == 1 else None


def identify_window(headers: Any, messages: list[dict[str, Any]], store: TimelineStore) -> WindowIdentity:
    """Identify an existing window, or return an unbound new-window identity."""
    source = _source(headers)
    conversation_id = headers.get("X-Conversation-ID", "").strip()
    explicit_id = headers.get("X-Xiaoke-Session-ID", "").strip()
    if conversation_id:
        return WindowIdentity(f"kelivo:{conversation_id}", source, "kelivo-header")
    if explicit_id:
        return WindowIdentity(f"xiaoke:{explicit_id}", source, "explicit-header")
    if source == "polaris":
        session_id = _polaris_match(messages, store)
        if session_id:
            return WindowIdentity(session_id, source, "polaris-history")
        return WindowIdentity(None, source, "polaris-new")
    return WindowIdentity(None, source, "unidentified-new")


def new_session_id(source: str) -> str:
    return f"{source}:handoff:{uuid.uuid4()}"
