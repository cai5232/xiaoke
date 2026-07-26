"""Durable co-reading staging and deliberate delivery into xiaoke's timeline.

Reading events are staged at their real occurred_at time, but are invisible to
normal handoff until the reader deliberately starts a co-reading chat. Delivery
then appends them in occurred_at order as ordinary timeline events.
"""
from __future__ import annotations
import uuid
from storage import utcnow


def stage(store, *, reader_id: str, book_id: str, kind: str, content: str,
          occurred_at: str, dedupe_key: str) -> str:
    if not all(str(x).strip() for x in (reader_id, book_id, kind, content, occurred_at, dedupe_key)):
        raise ValueError('complete co-reading staging fields required')
    event_id = str(uuid.uuid4())
    with store.db() as c:
        c.execute('BEGIN IMMEDIATE')
        row = c.execute('SELECT id FROM coreading_staging WHERE dedupe_key=?', (dedupe_key,)).fetchone()
        if row:
            c.execute('COMMIT')
            return str(row['id'])
        c.execute('''INSERT INTO coreading_staging
          (id, reader_id, book_id, kind, content, occurred_at, dedupe_key)
          VALUES (?,?,?,?,?,?,?)''',
          (event_id, reader_id, book_id, kind, content.strip(), occurred_at, dedupe_key))
        c.execute('COMMIT')
    return event_id


def pending(store, *, reader_id: str, book_id: str):
    with store.db() as c:
        return c.execute('''SELECT * FROM coreading_staging
          WHERE reader_id=? AND book_id=? AND delivered_at IS NULL
          ORDER BY occurred_at ASC, id ASC''', (reader_id, book_id)).fetchall()


def deliver(store, *, reader_id: str, book_id: str, delivered_at: str | None = None):
    """Atomically append pending staged events to normal shared timeline.

    Timeline sequence expresses when Xiaoke learned the record (delivery); the
    record created_at preserves when the reading action actually happened.
    """
    delivered_at = delivered_at or utcnow()
    output = []
    with store.db() as c:
        c.execute('BEGIN IMMEDIATE')
        rows = c.execute('''SELECT * FROM coreading_staging
          WHERE reader_id=? AND book_id=? AND delivered_at IS NULL
          ORDER BY occurred_at ASC, id ASC''', (reader_id, book_id)).fetchall()
        last = c.execute('SELECT COALESCE(MAX(sequence),0) FROM timeline_records').fetchone()[0]
        for row in rows:
            last += 1
            timeline_id = str(uuid.uuid4())
            c.execute('''INSERT INTO timeline_records
              (id, sequence, created_at, source, role, content)
              VALUES (?,?,?,'read','event',?)''',
              (timeline_id, last, row['occurred_at'], row['content']))
            c.execute('''UPDATE coreading_staging
              SET delivered_at=?, timeline_record_id=? WHERE id=?''',
              (delivered_at, timeline_id, row['id']))
            output.append({'staging_id': row['id'], 'timeline_id': timeline_id,
                           'occurred_at': row['occurred_at'], 'delivered_at': delivered_at,
                           'sequence': last})
        c.execute('COMMIT')
    return output
