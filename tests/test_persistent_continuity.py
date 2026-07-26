"""Persistent continuity state test. Uses a temporary SQLite database only."""
import json
import tempfile
from pathlib import Path
from storage import TimelineStore

with tempfile.TemporaryDirectory() as directory:
    store = TimelineStore(Path(directory) / "continuity-state.sqlite")
    baseline = [
        {"id": "old-1", "sequence": 1, "role": "user", "content": "雪儿喜欢蓝色雨伞", "source": "kelivo"},
        {"id": "old-2", "sequence": 2, "role": "assistant", "content": "记得。", "source": "kelivo"},
    ]
    session_id = "test-window-after-handoff"
    store.save_continuity(session_id, baseline, seed_user="哥哥，我下午有安排吗？", seed_assistant="你下午有安排。")

    # Simulate a process restart: a fresh store object reads the same saved state.
    restarted_store = TimelineStore(Path(directory) / "continuity-state.sqlite")
    restored = restarted_store.load_continuity(session_id)
    assert restored is not None
    assert json.loads(restored["baseline_json"]) == baseline
    assert restored["active"] == 1

    restarted_store.reset_continuity(session_id)
    assert restarted_store.load_continuity(session_id) is None

print("persistent-continuity-state-ok")
