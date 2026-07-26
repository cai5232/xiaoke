import importlib.util
import sqlite3
from pathlib import Path

root = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("xiaoke_app", root / "xiaoke_app.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)  # initializes the empty local schema

p = root / "data" / "xiaoke.sqlite"
with sqlite3.connect(p) as c:
    names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
assert {"requests", "timeline_records", "timeline_favorites", "continuity_sessions"} <= names
assert c.execute("SELECT COUNT(*) FROM timeline_records").fetchone()[0] == 0
print("schema-ok; timeline-empty")
