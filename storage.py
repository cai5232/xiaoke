"""Local SQLite storage rules for xiaoke; no network or Dylan access."""
import sqlite3, uuid, json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from timeline_context import TimelineRecord

SCHEMA = '''
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS requests (
 request_id TEXT PRIMARY KEY, received_at TEXT NOT NULL, source TEXT NOT NULL,
 status TEXT NOT NULL, timeline_version_before INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS timeline_records (
 id TEXT PRIMARY KEY, sequence INTEGER NOT NULL UNIQUE,
 request_id TEXT REFERENCES requests(request_id), created_at TEXT NOT NULL,
 source TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
 eligible_for_handoff INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_handoff ON timeline_records(eligible_for_handoff, sequence DESC);
CREATE TABLE IF NOT EXISTS timeline_favorites (
 record_id TEXT PRIMARY KEY REFERENCES timeline_records(id) ON DELETE CASCADE,
 created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_timeline_favorites_created ON timeline_favorites(created_at DESC);
CREATE TABLE IF NOT EXISTS coreading_staging (
 id TEXT PRIMARY KEY,
 reader_id TEXT NOT NULL,
 book_id TEXT NOT NULL,
 kind TEXT NOT NULL,
 content TEXT NOT NULL,
 occurred_at TEXT NOT NULL,
 dedupe_key TEXT NOT NULL UNIQUE,
 delivered_at TEXT,
 timeline_record_id TEXT UNIQUE REFERENCES timeline_records(id)
);
CREATE INDEX IF NOT EXISTS idx_coreading_pending
 ON coreading_staging(reader_id, book_id, delivered_at, occurred_at);

CREATE TABLE IF NOT EXISTS mailbox_records (
 id TEXT PRIMARY KEY,
 kind TEXT NOT NULL,
 subject TEXT NOT NULL,
 content TEXT NOT NULL,
 created_at TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'active'
);
CREATE INDEX IF NOT EXISTS idx_mailbox_kind_created
 ON mailbox_records(kind, created_at DESC);

CREATE TABLE IF NOT EXISTS continuity_sessions (
 session_id TEXT PRIMARY KEY,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
 seed_user TEXT NOT NULL,
 seed_assistant TEXT NOT NULL,
 baseline_json TEXT NOT NULL
);
'''

def utcnow(): return datetime.now(timezone.utc).isoformat()

class TimelineStore:
 def __init__(self, path):
  self.path=str(path); Path(self.path).parent.mkdir(parents=True, exist_ok=True); self.init()
 @contextmanager
 def db(self):
  c=sqlite3.connect(self.path, timeout=5, isolation_level=None); c.row_factory=sqlite3.Row
  try:
   c.execute('PRAGMA foreign_keys=ON'); c.execute('PRAGMA busy_timeout=5000'); yield c
  finally: c.close()
 def init(self):
  with self.db() as c:
   c.executescript(SCHEMA); c.execute('PRAGMA journal_mode=WAL')
 def records(self):
  with self.db() as c: rows=c.execute('SELECT id,sequence,role,content,source,eligible_for_handoff,created_at FROM timeline_records ORDER BY sequence').fetchall()
  return [TimelineRecord(r['id'],r['sequence'],r['role'],r['content'],r['source'],bool(r['eligible_for_handoff']),r['created_at']) for r in rows]
 def completed_turn(self, user, assistant, source='unknown', user_created_at=None, assistant_created_at=None):
  if not str(user).strip() or not str(assistant).strip(): raise ValueError('complete user/assistant turn required')
  request_id,user_id,assistant_id=str(uuid.uuid4()),str(uuid.uuid4()),str(uuid.uuid4())
  received=user_created_at or utcnow(); completed=assistant_created_at or utcnow()
  with self.db() as c:
   c.execute('BEGIN IMMEDIATE'); last=c.execute('SELECT COALESCE(MAX(sequence),0) FROM timeline_records').fetchone()[0]
   c.execute('INSERT INTO requests VALUES (?,?,?,?,?)',(request_id,received,source,'pending',last))
   c.execute("INSERT INTO timeline_records(id,sequence,request_id,created_at,source,role,content) VALUES (?,?,?,?,?,'user',?)",(user_id,last+1,request_id,received,source,str(user).strip()))
   c.execute("INSERT INTO timeline_records(id,sequence,request_id,created_at,source,role,content) VALUES (?,?,?,?,?,'assistant',?)",(assistant_id,last+2,request_id,completed,source,str(assistant).strip()))
   c.execute("UPDATE requests SET status='completed' WHERE request_id=?",(request_id,)); c.execute('COMMIT')
  return request_id,user_id,assistant_id
 def event(self, content, source='dylan'):
  if not str(content).strip(): raise ValueError('event required')
  eid=str(uuid.uuid4())
  with self.db() as c:
   c.execute('BEGIN IMMEDIATE'); seq=c.execute('SELECT COALESCE(MAX(sequence),0)+1 FROM timeline_records').fetchone()[0]
   c.execute("INSERT INTO timeline_records(id,sequence,created_at,source,role,content) VALUES (?,?,?,?, 'event',?)",(eid,seq,utcnow(),source,str(content).strip())); c.execute('COMMIT')
  return eid

 def mailbox_add(self, kind, subject, content):
  if kind not in ('mail','regret','trash'): raise ValueError('invalid mailbox kind')
  item_id=str(uuid.uuid4())
  with self.db() as c:
   c.execute('INSERT INTO mailbox_records(id,kind,subject,content,created_at,status) VALUES (?,?,?,?,?,?)',
             (item_id,kind,str(subject).strip() or '未命名',str(content).strip(),utcnow(),'active'))
  return item_id
 def mailbox_list(self, kind=None, limit=100):
  query='SELECT id,kind,subject,content,created_at,status FROM mailbox_records'
  args=[]
  if kind:
   query+=' WHERE kind=?'; args.append(kind)
  query+=' ORDER BY created_at DESC LIMIT ?'; args.append(min(max(int(limit),1),200))
  with self.db() as c: rows=c.execute(query,args).fetchall()
  return [dict(row) for row in rows]

 def save_continuity(self, session_id, baseline, seed_user, seed_assistant):
  if not str(session_id).strip(): raise ValueError('session id required')
  timestamp=utcnow(); payload=json.dumps(baseline, ensure_ascii=False, separators=(',', ':'))
  with self.db() as c:
   c.execute("""INSERT INTO continuity_sessions(session_id,created_at,updated_at,active,seed_user,seed_assistant,baseline_json)
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(session_id) DO UPDATE SET updated_at=excluded.updated_at,active=1,
                seed_user=excluded.seed_user,seed_assistant=excluded.seed_assistant,baseline_json=excluded.baseline_json""",
             (str(session_id),timestamp,timestamp,1,str(seed_user),str(seed_assistant),payload))
 def load_continuity(self, session_id):
  with self.db() as c:
   return c.execute('SELECT * FROM continuity_sessions WHERE session_id=? AND active=1',(str(session_id),)).fetchone()
 def active_continuities(self):
  with self.db() as c:
   return c.execute('SELECT * FROM continuity_sessions WHERE active=1 ORDER BY updated_at DESC').fetchall()
 def reset_continuity(self, session_id):
  with self.db() as c:
   c.execute('UPDATE continuity_sessions SET active=0,updated_at=? WHERE session_id=?',(utcnow(),str(session_id)))

 def timeline_export(self, limit=500, before=None):
  limit=max(1,min(int(limit),500))
  try: before=int(before) if before not in (None, "") else None
  except (TypeError, ValueError): before=None
  with self.db() as c:
   if before is None:
    rows=c.execute("SELECT id,sequence,created_at,source,role,content,EXISTS(SELECT 1 FROM timeline_favorites f WHERE f.record_id=timeline_records.id) AS favorite FROM timeline_records ORDER BY sequence DESC LIMIT ?",(limit,)).fetchall()
   else:
    rows=c.execute("SELECT id,sequence,created_at,source,role,content,EXISTS(SELECT 1 FROM timeline_favorites f WHERE f.record_id=timeline_records.id) AS favorite FROM timeline_records WHERE sequence < ? ORDER BY sequence DESC LIMIT ?",(before,limit)).fetchall()
  return [dict(row) for row in reversed(rows)]

 def timeline_search(self, query, limit=100, favorites_only=False):
  query=str(query or '').strip()
  if not query: return []
  limit=max(1,min(int(limit),100))
  where="content LIKE ?"; args=[f'%{query}%']
  if favorites_only: where += " AND EXISTS(SELECT 1 FROM timeline_favorites f WHERE f.record_id=timeline_records.id)"
  with self.db() as c:
   rows=c.execute(f"SELECT id,sequence,created_at,source,role,content,EXISTS(SELECT 1 FROM timeline_favorites f WHERE f.record_id=timeline_records.id) AS favorite FROM timeline_records WHERE {where} ORDER BY sequence DESC LIMIT ?",(*args,limit)).fetchall()
  return [dict(row) for row in reversed(rows)]
 def timeline_favorites(self, limit=500):
  limit=max(1,min(int(limit),500))
  with self.db() as c:
   rows=c.execute("SELECT id,sequence,created_at,source,role,content,1 AS favorite FROM timeline_records WHERE EXISTS(SELECT 1 FROM timeline_favorites f WHERE f.record_id=timeline_records.id) ORDER BY sequence DESC LIMIT ?",(limit,)).fetchall()
  return [dict(row) for row in reversed(rows)]
 def set_timeline_favorite(self, record_id, favorite):
  record_id=str(record_id or '')
  with self.db() as c:
   if not c.execute('SELECT 1 FROM timeline_records WHERE id=?',(record_id,)).fetchone(): return None
   if favorite: c.execute('INSERT OR IGNORE INTO timeline_favorites(record_id,created_at) VALUES (?,?)',(record_id,utcnow()))
   else: c.execute('DELETE FROM timeline_favorites WHERE record_id=?',(record_id,))
  return bool(favorite)
 def delete_timeline_record(self, record_id):
  record_id=str(record_id or '')
  with self.db() as c:
   c.execute('BEGIN IMMEDIATE')
   if not c.execute('SELECT 1 FROM timeline_records WHERE id=?',(record_id,)).fetchone(): c.execute('ROLLBACK'); return False
   c.execute('UPDATE coreading_staging SET timeline_record_id=NULL WHERE timeline_record_id=?',(record_id,))
   c.execute('DELETE FROM timeline_records WHERE id=?',(record_id,))
   c.execute('COMMIT')
  return True

 def timeline_dates(self):
  """Read-only UTC date index for the private timeline viewer."""
  with self.db() as c:
   rows=c.execute("SELECT substr(created_at,1,10) AS day, COUNT(*) AS count FROM timeline_records GROUP BY substr(created_at,1,10) ORDER BY day DESC").fetchall()
  return [dict(row) for row in rows if row['day']]
 def timeline_search_source(self, source, limit=10):
  source=str(source or '')
  limit=max(1,min(int(limit),100))
  with self.db() as c:
   rows=c.execute("SELECT id,sequence,created_at,source,role,content,EXISTS(SELECT 1 FROM timeline_favorites f WHERE f.record_id=timeline_records.id) AS favorite FROM timeline_records WHERE source=? ORDER BY sequence DESC LIMIT ?",(source,limit)).fetchall()
  return [dict(row) for row in reversed(rows)]

 def timeline_day(self, day, limit=500):
  day=str(day or '')
  if len(day)!=10 or day[4:5]!='-' or day[7:8]!='-': return []
  limit=max(1,min(int(limit),500))
  with self.db() as c:
   rows=c.execute("SELECT id,sequence,created_at,source,role,content,EXISTS(SELECT 1 FROM timeline_favorites f WHERE f.record_id=timeline_records.id) AS favorite FROM timeline_records WHERE substr(created_at,1,10)=? ORDER BY sequence LIMIT ?",(day,limit)).fetchall()
  return [dict(row) for row in rows]
