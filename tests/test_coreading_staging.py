import sqlite3, tempfile
from pathlib import Path
from storage import TimelineStore
from coreading import stage, pending, deliver

with tempfile.TemporaryDirectory() as d:
    path = Path(d) / 'isolated-coreading.sqlite'
    store = TimelineStore(path)
    # Existing timeline is deliberately separate from undelivered reading.
    store.completed_turn('旧聊天', '旧回复', source='kelivo',
                         user_created_at='2026-07-25T08:00:00+00:00',
                         assistant_created_at='2026-07-25T08:00:01+00:00')
    first = stage(store, reader_id='pengpeng', book_id='book-1', kind='read',
                  content='【共读已确认】第一段', occurred_at='2026-07-25T08:03:00+00:00',
                  dedupe_key='book-1:read:0-1')
    assert stage(store, reader_id='pengpeng', book_id='book-1', kind='read',
                 content='重复不应新建', occurred_at='2026-07-25T08:03:01+00:00',
                 dedupe_key='book-1:read:0-1') == first
    second = stage(store, reader_id='pengpeng', book_id='book-1', kind='annotation',
                   content='【共读批注｜用户】一句话', occurred_at='2026-07-25T08:04:00+00:00',
                   dedupe_key='book-1:annotation:a1')
    assert len(store.records()) == 2, 'staging must not enter normal timeline early'
    assert [r['id'] for r in pending(store, reader_id='pengpeng', book_id='book-1')] == [first, second]
    delivered = deliver(store, reader_id='pengpeng', book_id='book-1',
                        delivered_at='2026-07-25T08:05:00+00:00')
    assert [x['sequence'] for x in delivered] == [3, 4]
    assert [r.content for r in store.records()][-2:] == ['【共读已确认】第一段', '【共读批注｜用户】一句话']
    assert [r.created_at for r in store.records()][-2:] == ['2026-07-25T08:03:00+00:00', '2026-07-25T08:04:00+00:00']
    assert not pending(store, reader_id='pengpeng', book_id='book-1')
    assert deliver(store, reader_id='pengpeng', book_id='book-1') == []
    with sqlite3.connect(path) as c:
        rows = c.execute('SELECT occurred_at, delivered_at, timeline_record_id FROM coreading_staging ORDER BY occurred_at').fetchall()
    assert [r[0] for r in rows] == ['2026-07-25T08:03:00+00:00', '2026-07-25T08:04:00+00:00']
    assert all(r[1] == '2026-07-25T08:05:00+00:00' and r[2] for r in rows)
print('coreading-staging-delivery-ok')
