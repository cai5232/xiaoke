"""Temporary continuity state for locally tested xiaoke handoff sessions."""
from dataclasses import dataclass
from timeline_context import TimelineRecord, fingerprint

@dataclass
class ContinuitySession:
    seed_user: tuple[str, str]
    seed_assistant: tuple[str, str]
    baseline: list[TimelineRecord]

class ContinuityRegistry:
    def __init__(self):
        self._sessions: list[ContinuitySession] = []

    def create(self, baseline: list[TimelineRecord], user_content: str, assistant_content: str) -> None:
        self._sessions.append(ContinuitySession(
            seed_user=fingerprint("user", user_content),
            seed_assistant=fingerprint("assistant", assistant_content),
            baseline=list(baseline),
        ))

    def baseline_for(self, client_messages: list[dict]) -> list[TimelineRecord] | None:
        fingerprints = {
            fingerprint(str(message.get("role", "")), message.get("content"))
            for message in client_messages
        }
        for session in reversed(self._sessions):
            if session.seed_user in fingerprints and session.seed_assistant in fingerprints:
                return list(session.baseline)
        return None
