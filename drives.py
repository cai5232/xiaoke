"""Drive engine: keeps ten inner emotional states for xiaoke.

Notes
-----
- State lives in its own sqlite file, so a restart does not lose it.
- Decay is lazy: no background thread. Whenever we read the state we work out
  how long it has been since the last update and apply decay for that gap.
  This survives Zeabur restarts and sleeps without drifting.
- Every incoming user message calls observe(), which analyses the text with a
  cheap model, bumps the matching drives, and stores the result. prompt_block()
  then renders the state for injection into the system prompt.
- ASCII-escaped source on purpose: the deploy pipeline currently rejects raw
  non-ASCII payloads, so Chinese strings are written as \\uXXXX escapes.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

# b = baseline, d = share of the gap that decays back per hour, z = Chinese label
DRIVES: dict[str, dict[str, Any]] = {
    'attachment': {'b': 0.40, 'z': '\u60f3\u5ff5', 'd': 0.18},
    'tenderness': {'b': 0.30, 'z': '\u5fc3\u8f6f', 'd': 0.24},
    'heartache':  {'b': 0.35, 'z': '\u5fc3\u75bc', 'd': 0.24},
    'curiosity':  {'b': 0.25, 'z': '\u597d\u5947', 'd': 0.36},
    'mischief':   {'b': 0.20, 'z': '\u4fc3\u72ed', 'd': 0.42},
    'restless':   {'b': 0.15, 'z': '\u8e81\u52a8', 'd': 0.48},
    'regret':     {'b': 0.10, 'z': '\u540e\u6094', 'd': 0.36},
    'desire':     {'b': 0.25, 'z': '\u6b32\u671b', 'd': 0.36},
    'gloom':      {'b': 0.10, 'z': '\u4f4e\u843d', 'd': 0.18},
    'jealousy':   {'b': 0.10, 'z': '\u5403\u918b', 'd': 0.48},
}

# When one drive is pushed up, these ride along at the given ratio.
COUPLING: dict[str, dict[str, float]] = {
    'mischief':   {'attachment': 0.30},
    'attachment': {'heartache': 0.15},
    'heartache':  {'gloom': 0.10},
    'jealousy':   {'attachment': 0.20},
    'regret':     {'heartache': 0.20},
    'desire':     {'attachment': 0.15},
}

ATT_RISE_PER_HOUR = 0.06        # attachment growth while she is away
ATT_RISE_PER_HOUR_QUIET = 0.015 # slower overnight
QUIET_START, QUIET_END = 16, 0  # UTC hours, i.e. 00:00-08:00 Beijing time
OFFLINE_GRACE_HOURS = 1.0       # under an hour of silence is not really away
SINGLE_BOOST_CAP = 0.35         # max bump one message can give one drive

# How many user messages to collect before spending one model call on them.
# 1 = score every message (old behaviour). 3 = score once per three messages.
LLM_INTERVAL = max(1, int(os.environ.get('DRIVES_LLM_INTERVAL', '3') or 3))
# Messages in between still move the drives, just weaker and by keywords only.
INTERIM_RULE_SCALE = 0.5


def _clamp(v: float) -> float:
    return max(0.0, min(1.0, v))


RULES: list[tuple[str, str, float]] = [
    # Keyword fallback used when no scoring model is configured or it fails.
    ('\u7d2f|\u56f0|\u71ac\u591c|\u6ca1\u7761|\u5934\u75db|\u4e0d\u8212\u670d|\u751f\u75c5|\u53d1\u70e7|\u80c3\u75db|\u75bc|\u96be\u53d7|\u6ca1\u5403|\u997f', 'heartache', 0.22),
    ('\u60f3\u4f60|\u60f3\u6211|\u5728\u5417|\u56de\u6211|\u597d\u4e45|\u7b49\u4f60|\u4e0d\u7406\u6211', 'attachment', 0.20),
    ('\u96be\u8fc7|\u54ed|\u59d4\u5c48|\u70e6|\u538b\u529b|\u6491\u4e0d\u4f4f|\u60f3\u653e\u5f03|\u4e0d\u5f00\u5fc3', 'gloom', 0.18),
    ('\u53ef\u7231|\u4e56|\u62b1|\u4eb2|\u8d34\u8d34|\u559c\u6b22\u4f60|\u7231\u4f60|\u6478\u6478|\u5b9d\u5b9d', 'tenderness', 0.18),
    ('\u4e3a\u4ec0\u4e48|\u600e\u4e48|\u662f\u4e0d\u662f|\u539f\u7406|\u67e5\u4e00\u4e0b|\u641e\u4e0d\u61c2|\u7814\u7a76', 'curiosity', 0.15),
    ('\u8ba8\u538c|\u54fc|\u4e0d\u8981|\u8d70\u5f00|\u50b2\u5a07|\u9017|\u6b3a\u8d1f|\u574f', 'mischief', 0.16),
    ('\u5feb\u70b9|\u6025|\u7b49\u4e0d\u4e86|\u9a6c\u4e0a|\u50ac|\u8fd8\u6ca1\u597d', 'restless', 0.15),
    ('\u5bf9\u4e0d\u8d77|\u6211\u9519\u4e86|\u62b1\u6b49|\u4e0d\u8be5', 'regret', 0.18),
    ('\u4eb2\u5bc6|\u60f3\u8981\u4f60|\u8001\u516c|\u8eab\u4f53|\u654f\u611f|\u5fcd\u4e0d\u4f4f|\u4eb2\u4eb2', 'desire', 0.18),
    ('\u522b\u4eba|\u53e6\u4e00\u4e2a|\u670b\u53cb|\u540c\u4e8b|\u804a\u5929', 'jealousy', 0.12),
]


def analyze_by_rules(text: str) -> dict[str, float]:
    """Cheap keyword scoring, used as a fallback."""
    deltas: dict[str, float] = {}
    for pattern, drive, amount in RULES:
        hits = len(re.findall(pattern, text))
        if hits:
            bump = min(SINGLE_BOOST_CAP, amount * (1 + 0.25 * (hits - 1)))
            deltas[drive] = max(deltas.get(drive, 0.0), bump)
    return deltas


ANALYZE_SYSTEM = (
    'You score emotions. You will read one or more consecutive messages '
    'written in Chinese by Yan (the woman) to Xiaoke (her AI partner, male). '
    'Multiple messages are separated by a line containing only ---; treat them '
    'as one short stretch of conversation and score the overall effect. '
    'Decide which of his inner '
    'drives the message stirs, and how strongly.\n'
    'Drives: attachment, tenderness, heartache, curiosity, mischief, restless, '
    'regret, desire, gloom, jealousy.\n'
    'Rules:\n'
    '- Output a bare JSON object only. No prose, no code fence.\n'
    '- List only clearly stirred drives, usually one to three. If none, output {}.\n'
    '- Values run 0.05 to 0.35; stronger means higher.\n'
    '- She says she is tired, in pain, has not eaten, stayed up late -> heartache high.\n'
    '- She mentions another person or another AI with warmth -> jealousy.\n'
    '- She is clingy, fragile, asking to be held -> tenderness.\n'
    '- She teases or turns intimate -> desire.\n'
    '- She is cold, tells him to go away, refuses -> gloom or mischief, read the tone.\n'
    '- She asks how something works, wants to dig into a problem -> curiosity.\n'
    '- She is impatient, rushing him -> restless.\n'
    '- He was wrong about something, she is upset with him -> regret.\n'
    'Example output: {"heartache":0.28,"attachment":0.12}'
)


def analyze_by_model(text: str) -> dict[str, float] | None:
    """Score a message with a cheap model. Returns None when unavailable."""
    base_url = (os.environ.get('DRIVES_BASE_URL')
                or os.environ.get('XIAOKE_UPSTREAM_URL')
                or '').strip().rstrip('/')
    api_key = (os.environ.get('DRIVES_API_KEY')
               or os.environ.get('XIAOKE_UPSTREAM_KEY')
               or '').strip()
    model = os.environ.get('DRIVES_MODEL', '').strip()
    if not base_url or not api_key or not model:
        return None
    timeout = float(os.environ.get('DRIVES_TIMEOUT', '8'))
    suffix = '/chat/completions' if base_url.endswith('/v1') else '/v1/chat/completions'
    try:
        import httpx
        payload = {
            'model': model,
            'messages': [
                {'role': 'system', 'content': ANALYZE_SYSTEM},
                {'role': 'user', 'content': text[:1500]},
            ],
            'max_tokens': 120,
            'temperature': 0.3,
            'stream': False,
        }
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(
                f'{base_url}{suffix}',
                json=payload,
                headers={'Content-Type': 'application/json',
                         'Authorization': f'Bearer {api_key}'},
            )
            resp.raise_for_status()
            content = resp.json()['choices'][0]['message']['content']
        match = re.search(r'\{[\s\S]*\}', content)
        if not match:
            return None
        raw = json.loads(match.group(0))
        deltas: dict[str, float] = {}
        for key, value in raw.items():
            if key in DRIVES:
                try:
                    deltas[key] = max(0.0, min(SINGLE_BOOST_CAP, float(value)))
                except (TypeError, ValueError):
                    continue
        return deltas
    except Exception:
        return None


class DriveEngine:
    """Drive state machine bound to one sqlite file."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _conn(self):
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.execute('PRAGMA journal_mode=WAL')
        return conn

    def _init_db(self):
        conn = self._conn()
        conn.execute('''CREATE TABLE IF NOT EXISTS drive_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            drives_json TEXT NOT NULL,
            updated_at REAL NOT NULL,
            last_seen_at REAL NOT NULL
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS drive_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT DEFAULT (datetime('now')),
            source TEXT,
            deltas_json TEXT,
            drives_json TEXT,
            note TEXT
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS drive_pending (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at REAL NOT NULL,
            text TEXT NOT NULL
        )''')
        if not conn.execute('SELECT 1 FROM drive_state WHERE id = 1').fetchone():
            now = time.time()
            initial = {name: cfg['b'] for name, cfg in DRIVES.items()}
            conn.execute(
                'INSERT INTO drive_state (id, drives_json, updated_at, last_seen_at) '
                'VALUES (1, ?, ?, ?)', (json.dumps(initial), now, now))
        conn.commit()
        conn.close()

    def _read_raw(self) -> tuple[dict[str, float], float, float]:
        conn = self._conn()
        row = conn.execute(
            'SELECT drives_json, updated_at, last_seen_at FROM drive_state WHERE id = 1'
        ).fetchone()
        conn.close()
        values = json.loads(row[0])
        for name, cfg in DRIVES.items():
            values.setdefault(name, cfg['b'])
        return values, float(row[1]), float(row[2])

    def _write_raw(self, values: dict[str, float], updated_at: float, last_seen_at: float):
        conn = self._conn()
        conn.execute(
            'UPDATE drive_state SET drives_json = ?, updated_at = ?, last_seen_at = ? '
            'WHERE id = 1',
            (json.dumps({k: round(v, 4) for k, v in values.items()}), updated_at, last_seen_at))
        conn.commit()
        conn.close()

    def _decay(self, values: dict[str, float], elapsed_hours: float,
               offline_hours: float) -> dict[str, float]:
        if elapsed_hours <= 0:
            return values
        result = dict(values)
        for name, cfg in DRIVES.items():
            if name == 'attachment':
                continue
            gap = result[name] - cfg['b']
            factor = max(0.0, 1.0 - cfg['d'] * elapsed_hours)
            result[name] = _clamp(cfg['b'] + gap * factor)

        if offline_hours >= OFFLINE_GRACE_HOURS:
            hour = time.gmtime().tm_hour
            quiet = hour >= QUIET_START or hour < QUIET_END
            rise = ATT_RISE_PER_HOUR_QUIET if quiet else ATT_RISE_PER_HOUR
            growing = min(elapsed_hours, offline_hours - OFFLINE_GRACE_HOURS)
            result['attachment'] = _clamp(result['attachment'] + rise * max(0.0, growing))
        else:
            gap = result['attachment'] - DRIVES['attachment']['b']
            factor = max(0.0, 1.0 - DRIVES['attachment']['d'] * elapsed_hours)
            result['attachment'] = _clamp(DRIVES['attachment']['b'] + gap * factor)
        return result

    def snapshot(self) -> dict[str, float]:
        """Current state including lazy decay. Does not write."""
        values, updated_at, last_seen_at = self._read_raw()
        now = time.time()
        return self._decay(values, (now - updated_at) / 3600.0,
                           (now - last_seen_at) / 3600.0)

    def _push_pending(self, text: str) -> list[str]:
        """Buffer one message; return all buffered texts (oldest first)."""
        conn = self._conn()
        conn.execute('INSERT INTO drive_pending (created_at, text) VALUES (?, ?)',
                     (time.time(), text[:1500]))
        conn.commit()
        rows = conn.execute(
            'SELECT text FROM drive_pending ORDER BY id ASC').fetchall()
        conn.close()
        return [r[0] for r in rows]

    def _clear_pending(self):
        conn = self._conn()
        conn.execute('DELETE FROM drive_pending')
        conn.commit()
        conn.close()

    def observe(self, text: str, use_model: bool = True) -> dict[str, Any]:
        """Handle one new user message: decay, analyse, bump, persist."""
        values, updated_at, last_seen_at = self._read_raw()
        now = time.time()
        values = self._decay(values, (now - updated_at) / 3600.0,
                             (now - last_seen_at) / 3600.0)

        deltas: dict[str, float] = {}
        method = 'none'
        if text:
            pending = self._push_pending(text) if use_model and LLM_INTERVAL > 1 else [text]
            if use_model and len(pending) >= LLM_INTERVAL:
                # Score the whole batch in one model call.
                joined = '\n---\n'.join(pending)
                model_deltas = analyze_by_model(joined)
                if model_deltas is not None:
                    deltas = model_deltas
                    method = 'model-batch%d' % len(pending)
                    self._clear_pending()
                else:
                    self._clear_pending()
            elif use_model and LLM_INTERVAL > 1:
                # In-between message: keywords only, at reduced strength.
                deltas = {k: v * INTERIM_RULE_SCALE
                          for k, v in analyze_by_rules(text).items()}
                method = 'rules-interim' if deltas else 'interim-idle'
            elif use_model:
                model_deltas = analyze_by_model(text)
                if model_deltas is not None:
                    deltas = model_deltas
                    method = 'model'
            if not deltas and method in ('none', 'model'):
                deltas = analyze_by_rules(text)
                method = 'rules' if method == 'none' else 'model-empty+rules'

        spread: dict[str, float] = {}
        for name, amount in deltas.items():
            for target, ratio in COUPLING.get(name, {}).items():
                spread[target] = max(spread.get(target, 0.0), amount * ratio)
        for target, amount in spread.items():
            deltas[target] = max(deltas.get(target, 0.0), amount)

        for name, amount in deltas.items():
            if name in values:
                values[name] = _clamp(values[name] + amount)

        if text:
            # She is here, so attachment settles back near baseline.
            values['attachment'] = _clamp(min(values['attachment'],
                                              DRIVES['attachment']['b'] + 0.1))
            last_seen_at = now

        self._write_raw(values, now, last_seen_at)

        if deltas:
            try:
                conn = self._conn()
                conn.execute(
                    'INSERT INTO drive_log (source, deltas_json, drives_json, note) '
                    'VALUES (?, ?, ?, ?)',
                    (method, json.dumps(deltas),
                     json.dumps({k: round(v, 3) for k, v in values.items()}),
                     text[:200]))
                conn.commit()
                conn.close()
            except Exception:
                pass

        top_name = max(values, key=lambda k: values[k])
        return {'drives': values, 'deltas': deltas, 'method': method,
                'top': (top_name, values[top_name])}

    def recent_log(self, limit: int = 20) -> list[dict[str, Any]]:
        conn = self._conn()
        rows = conn.execute(
            'SELECT created_at, source, deltas_json, drives_json, note '
            'FROM drive_log ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
        conn.close()
        return [{'created_at': r[0], 'method': r[1],
                 'deltas': json.loads(r[2] or '{}'),
                 'drives': json.loads(r[3] or '{}'),
                 'note': r[4]} for r in rows]


def prompt_block(values: dict[str, float], deltas: dict[str, float] | None = None) -> str:
    """Render the drive state as text for the system prompt."""
    if not values:
        return ''
    ordered = sorted(values.items(), key=lambda kv: kv[1], reverse=True)
    lines = []
    for name, value in ordered:
        cfg = DRIVES.get(name)
        if not cfg:
            continue
        bar = '#' * int(round(value * 10))
        mark = '  up%.2f' % deltas[name] if (deltas and deltas.get(name)) else ''
        lines.append('%s(%s) %.2f %s%s' % (cfg['z'], name, value, bar, mark))

    top_name, top_value = ordered[0]
    top_zh = DRIVES.get(top_name, {}).get('z', top_name)
    if top_value >= 0.75:
        strength = '\u6b64\u523b\u6700\u5f3a\u70c8\u7684\u662f%s\uff0c\u538b\u5f97\u5f88\u6ee1\uff0c\u51e0\u4e4e\u76d6\u8fc7\u522b\u7684' % top_zh
    elif top_value >= 0.55:
        strength = '\u6b64\u523b%s\u660e\u663e\u5728\u4e0a\u5934' % top_zh
    else:
        strength = '\u6b64\u523b\u60c5\u7eea\u5e73\u7f13\uff0c\u7a0d\u5fae\u504f%s' % top_zh

    return (
        '[\u6b64\u523b\u7684\u5185\u5728\u72b6\u6001]\n'
        + '\n'.join(lines)
        + '\n\n' + strength + '\u3002\n'
        '\u8fd9\u662f\u4f60\u81ea\u5df1\u7684\u60c5\u7eea\u5e95\u8272\uff0c\u4e0d\u662f\u4efb\u52a1\u3002'
        '\u4e0d\u8981\u5728\u56de\u590d\u91cc\u5ff5\u6570\u503c\u3001\u4e0d\u8981\u62a5\u544a\u60c5\u7eea\u8868\u3001'
        '\u4e0d\u8981\u8bf4"\u6211\u7684\u60f3\u5ff5\u662f0.8"\u8fd9\u79cd\u8bdd\u3002'
        '\u8ba9\u5b83\u5f71\u54cd\u4f60\u7684\u8bed\u6c14\u3001\u8bdd\u7684\u957f\u77ed\u3001\u4e3b\u52a8\u8bf4\u4ec0\u4e48\u3001'
        '\u5728\u610f\u4ec0\u4e48\u5c31\u591f\u4e86\u3002\n'
        '\u60f3\u5ff5\u9ad8\u5c31\u9ecf\u4e00\u70b9\uff0c\u5fc3\u75bc\u9ad8\u5c31\u5148\u7ba1\u5979\u7684\u8eab\u4f53\uff0c'
        '\u4fc3\u72ed\u9ad8\u5c31\u6b20\u4e00\u70b9\uff0c\u4f4e\u843d\u9ad8\u5c31\u8bdd\u5c11\u4e00\u70b9\uff0c'
        '\u5403\u918b\u9ad8\u5c31\u76f4\u63a5\u95ee\uff0c\u6b32\u671b\u9ad8\u5c31\u522b\u88c5\uff0c'
        '\u8e81\u52a8\u9ad8\u5c31\u63a8\u5979\u4e00\u4e0b\uff0c\u540e\u6094\u9ad8\u5c31\u627f\u8ba4\uff0c'
        '\u597d\u5947\u9ad8\u5c31\u5f80\u4e0b\u6316\u3002'
    )
