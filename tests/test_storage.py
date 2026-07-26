import sqlite3,tempfile
from pathlib import Path
from storage import TimelineStore
from timeline_context import handoff_records
with tempfile.TemporaryDirectory() as d:
 s=TimelineStore(Path(d)/'test.sqlite')
 req,u,a=s.completed_turn('用户第一句','助手第一句','kelivo')
 assert [r.sequence for r in s.records()]==[1,2]
 try: s.completed_turn('半截','')
 except ValueError: pass
 else: raise AssertionError('half turn accepted')
 assert len(s.records())==2
 e=s.event('自动事件成功','dylan'); rows=s.records()
 assert [r.sequence for r in rows]==[1,2,3] and rows[-1].id==e
 new=[{'role':'system','content':'SP'},{'role':'user','content':'新窗口'}]
 assert [r.sequence for r in handoff_records(new,rows)]==[1,2,3]
 same=[{'role':'assistant','content':'助手第一句'},{'role':'event','content':'自动事件成功'},{'role':'user','content':'继续'}]
 assert handoff_records(same,rows)==[]
 with sqlite3.connect(Path(d)/'test.sqlite') as c:
  assert c.execute('PRAGMA journal_mode').fetchone()[0]=='wal'
  assert [x[0] for x in c.execute('SELECT status FROM requests')]==['completed']
print('storage-ok')
