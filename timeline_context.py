"""Context-selection rules for xiaoke's shared timeline.

This module has no network access and does not read Dylan data.  It decides
what is eligible for the shared timeline, finds overlap with client history,
and selects a bounded handoff package from supplied test records.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

EXCLUDED_MARKERS = ("<memories>", "## Memories", "记忆库使用策略")
TEXT_PLACEHOLDERS = {"image": "[图片]", "file": "[文件]"}
ELIGIBLE_ROLES = {"user", "assistant", "event"}

@dataclass(frozen=True)
class TimelineRecord:
    id: str
    sequence: int
    role: str
    content: str
    source: str = "unknown"
    eligible: bool = True
    created_at: str | None = None


def text_from_content(content: Any) -> str:
    """Keep text; replace attachments with safe placeholders."""
    if isinstance(content, str):
        return content.strip()
    if content is None:
        return ""
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                if part.strip(): parts.append(part.strip())
            elif isinstance(part, dict):
                kind = str(part.get("type", "")).lower()
                if kind in ("text", "input_text"):
                    value = str(part.get("text", part.get("content", ""))).strip()
                    if value: parts.append(value)
                elif "image" in kind or part.get("image_url"):
                    parts.append(TEXT_PLACEHOLDERS["image"])
                elif "file" in kind or part.get("file"):
                    parts.append(TEXT_PLACEHOLDERS["file"])
        return "\n".join(parts)
    if isinstance(content, dict):
        kind = str(content.get("type", "")).lower()
        if "image" in kind or content.get("image_url"):
            return TEXT_PLACEHOLDERS["image"]
        if "file" in kind or content.get("file"):
            return TEXT_PLACEHOLDERS["file"]
    return ""


def is_timeline_eligible(message: dict[str, Any]) -> bool:
    """System/prompt wrappers and incomplete tool chains never enter handoff."""
    role = str(message.get("role", ""))
    if role not in ELIGIBLE_ROLES or message.get("tool_calls") or role == "tool":
        return False
    text = text_from_content(message.get("content"))
    if not text or any(marker in text for marker in EXCLUDED_MARKERS):
        return False
    return True


def fingerprint(role: str, content: Any) -> tuple[str, str]:
    """Conservative fallback matcher for clients that do not provide xiaoke IDs."""
    return str(role), text_from_content(content)


def client_eligible_messages(messages: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [m for m in messages if is_timeline_eligible(m)]


def find_latest_anchor(client_messages: Iterable[dict[str, Any]], records: Iterable[TimelineRecord]) -> int | None:
    """Return newest shared sequence represented in client history, if any.

    A future supported client can send `xiaoke_record_id`; otherwise matching is
    deliberately conservative: role + normalized text, newest occurrence wins.
    """
    record_list = list(records)
    by_id = {r.id: r.sequence for r in record_list}
    by_fp: dict[tuple[str, str], list[int]] = {}
    for r in record_list:
        by_fp.setdefault(fingerprint(r.role, r.content), []).append(r.sequence)

    anchors: list[int] = []
    for msg in client_eligible_messages(client_messages):
        supplied_id = msg.get("xiaoke_record_id")
        if supplied_id in by_id:
            anchors.append(by_id[supplied_id])
            continue
        choices = by_fp.get(fingerprint(str(msg.get("role", "")), msg.get("content")), [])
        if choices:
            anchors.append(max(choices))
    return max(anchors) if anchors else None


def needs_handoff(client_messages: Iterable[dict[str, Any]], records: Iterable[TimelineRecord]) -> tuple[bool, int | None]:
    """Automatic rule for stage 2.

    No shared anchor while a shared timeline exists => new-window handoff.
    Anchor behind latest shared record => missing-history handoff.
    Anchor at latest shared record => same-window continuation.
    """
    usable = [r for r in records if r.eligible]
    if not usable:
        return False, None
    anchor = find_latest_anchor(client_messages, usable)
    latest = max(r.sequence for r in usable)
    return anchor is None or anchor < latest, anchor


def handoff_records(client_messages: Iterable[dict[str, Any]], records: Iterable[TimelineRecord], max_records: int = 999, max_chars: int = 160000) -> list[TimelineRecord]:
    """Select only history missing from this request, newest-first by budget.

    Returned records are chronological. `max_chars` is a safe temporary stand-in
    for tokenizer-aware budgeting; the gateway stage will reserve actual SP/input/
    output budget before calling this selector.
    """
    usable = sorted((r for r in records if r.eligible), key=lambda r: r.sequence)
    required, anchor = needs_handoff(client_messages, usable)
    if not required:
        return []

    client_fps = {fingerprint(str(m.get("role", "")), m.get("content")) for m in client_eligible_messages(client_messages)}
    candidates = [r for r in usable if (anchor is None or r.sequence > anchor) and fingerprint(r.role, r.content) not in client_fps]
    selected: list[TimelineRecord] = []
    used_chars = 0
    for record in reversed(candidates):
        size = len(record.content)
        if len(selected) >= max_records or used_chars + size > max_chars:
            break
        selected.append(record)
        used_chars += size
    return list(reversed(selected))

def rolling_records(client_messages: Iterable[dict[str, Any]], records: Iterable[TimelineRecord], max_records: int = 999, max_chars: int = 160000) -> list[TimelineRecord]:
    """Rebuild a bounded, newest-first context window without mutating storage."""
    usable = sorted((record for record in records if record.eligible), key=lambda record: record.sequence)
    client_fps = {fingerprint(str(message.get("role", "")), message.get("content")) for message in client_eligible_messages(client_messages)}
    candidates = [record for record in usable if fingerprint(record.role, record.content) not in client_fps]
    selected: list[TimelineRecord] = []
    used_chars = 0
    for record in reversed(candidates):
        size = len(record.content)
        if len(selected) >= max_records or used_chars + size > max_chars:
            break
        selected.append(record)
        used_chars += size
    return list(reversed(selected))
