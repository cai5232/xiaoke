import sqlite3
import tempfile
from pathlib import Path
from storage import TimelineStore
from timeline_context import TimelineRecord
from xiaoke_gateway_api import timeline_injection_messages

with tempfile.TemporaryDirectory() as directory:
    db = Path(directory) / "time.sqlite"
    store = TimelineStore(db)
    store.completed_turn("u", "a", "test", user_created_at="2026-07-25T01:00:00+00:00", assistant_created_at="2026-07-25T01:00:07+00:00")
    records = store.records()
    assert records[0].created_at == "2026-07-25T01:00:00+00:00"
    assert records[1].created_at == "2026-07-25T01:00:07+00:00"
    exported = store.timeline_export()
    assert exported[0]["created_at"] == "2026-07-25T01:00:00+00:00"
    assert exported[1]["created_at"] == "2026-07-25T01:00:07+00:00"

injected = timeline_injection_messages([
    TimelineRecord("a", 1, "user", "old user", created_at="2026-07-24T18:44:01+00:00"),
    TimelineRecord("b", 2, "assistant", "old reply", created_at="2026-07-24T18:44:49+00:00"),
    TimelineRecord("c", 3, "user", "unknown time"),
])
assert injected[0]["content"] == "old user"
assert injected[1]["content"] == "old reply"
assert injected[2]["content"] == "unknown time"
assert all("共同时间线" not in m["content"] for m in injected)
assert all("xiaoke_record_id" in m for m in injected)
print("timeline-time-ok")
