"""OpenAI-compatible gateway for xiaoke.
Supports both local mock (stage 3A) and real upstream forwarding (stage 3B+)."""
from __future__ import annotations
import json
import uuid
import os
import hmac
import sqlite3
from pathlib import Path
from typing import Any
from flask import Flask, Response, jsonify, request
from flask_cors import CORS
from mock_gateway import assemble
from ombre_client import get_breath_memories, search_memories, hold_memory
from memory_store import load_memories_text, save_memory
from storage import TimelineStore, utcnow
from coreading import stage as coreading_stage, deliver as coreading_deliver
from timeline_context import TimelineRecord, rolling_records, text_from_content
from window_identity import identify_window, new_session_id

DEFAULT_DB = Path(__file__).resolve().parent / 'data' / 'xiaoke.sqlite'
DEFAULT_HANDOFF_RECORDS = 66
DEFAULT_HANDOFF_CHARS = 800000


def _push_db_path(db_path: Path) -> Path:
    return db_path.parent / 'push.sqlite'


def _init_push_db(push_db: Path):
    conn = sqlite3.connect(str(push_db))
    conn.execute('''CREATE TABLE IF NOT EXISTS push_subscriptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        endpoint TEXT NOT NULL UNIQUE,
        p256dh TEXT NOT NULL,
        auth TEXT NOT NULL,
        created_at TEXT DEFAULT (datetime('now'))
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS vapid_keys (
        id INTEGER PRIMARY KEY,
        private_key TEXT NOT NULL,
        public_key TEXT NOT NULL
    )''')
    conn.commit()
    conn.close()


def _get_or_create_vapid_keys(push_db: Path):
    """Return (private_key_pem, public_key_base64url), creating if missing."""
    try:
        from py_vapid import Vapid
        import cryptography.hazmat.primitives.serialization as _ser
        import base64
    except ImportError:
        return None, None
    conn = sqlite3.connect(str(push_db))
    row = conn.execute('SELECT private_key, public_key FROM vapid_keys LIMIT 1').fetchone()
    if row:
        conn.close()
        return row[0], row[1]
    v = Vapid()
    v.generate_keys()
    priv = v.private_pem().decode() if isinstance(v.private_pem(), bytes) else v.private_pem()
    pub_bytes = v.public_key.public_bytes(
        _ser.Encoding.X962,
        _ser.PublicFormat.UncompressedPoint
    )
    pub_b64 = base64.urlsafe_b64encode(pub_bytes).rstrip(b'=').decode()
    conn.execute('INSERT INTO vapid_keys (id, private_key, public_key) VALUES (1, ?, ?)', (priv, pub_b64))
    conn.commit()
    conn.close()
    return priv, pub_b64


def send_push_notification(push_db: Path, session_id: str, title: str, body: str, url: str = '/'):
    """Push a notification to all subscriptions for the given session."""
    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        return
    priv_key, _ = _get_or_create_vapid_keys(push_db)
    if not priv_key:
        return
    conn = sqlite3.connect(str(push_db))
    rows = conn.execute(
        'SELECT endpoint, p256dh, auth FROM push_subscriptions WHERE session_id = ?',
        (session_id,)
    ).fetchall()
    conn.close()
    payload = json.dumps({'title': title, 'body': body, 'url': url, 'tag': 'xiaoke-notify'})
    for endpoint, p256dh, auth in rows:
        try:
            webpush(
                subscription_info={'endpoint': endpoint, 'keys': {'p256dh': p256dh, 'auth': auth}},
                data=payload,
                vapid_private_key=priv_key,
                vapid_claims={'sub': 'mailto:xiaoke@reverie.app'}
            )
        except WebPushException as e:
            if e.response and e.response.status_code in (404, 410):
                c = sqlite3.connect(str(push_db))
                c.execute('DELETE FROM push_subscriptions WHERE endpoint = ?', (endpoint,))
                c.commit()
                c.close()
        except Exception:
            pass


def has_upstream() -> bool:
    return bool((os.environ.get('XIAOKE_UPSTREAM_URL') or os.environ.get('UPSTREAM_BASE_URL') or '').strip())


def as_openai_response(content: str, model: str) -> dict[str, Any]:
    return {
        'id': f'chatcmpl-xiaoke-{uuid.uuid4().hex[:12]}',
        'object': 'chat.completion',
        'created': 0,
        'model': model,
        'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': content}, 'finish_reason': 'stop'}],
        'usage': {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0},
    }


def restore_baseline(store: TimelineStore, session_id: str) -> list[TimelineRecord] | None:
    row = store.load_continuity(session_id)
    if row is None:
        return None
    return [TimelineRecord(**item) for item in json.loads(row['baseline_json'])]


def timeline_injection_messages(records: list[TimelineRecord]) -> list[dict[str, Any]]:
    result = []
    for record in records:
        role = record.role if record.role in ('user', 'assistant') else 'assistant'
        result.append({'role': role, 'content': record.content, 'xiaoke_record_id': record.id})
    return result

def build_messages(messages: list[dict[str, Any]], store: TimelineStore, session_id: str | None, max_records: int, max_chars: int) -> tuple[list[dict[str, Any]], list[TimelineRecord], bool]:
    if session_id:
        if restore_baseline(store, session_id) is not None:
            baseline = rolling_records(messages, store.records(), max_records, max_chars)
            systems = [m for m in messages if m.get('role') == 'system']
            other = [m for m in messages if m.get('role') != 'system']
            injected = timeline_injection_messages(baseline)
            return systems + injected + other, baseline, True
    _, injected = assemble(messages, store, max_records, max_chars)
    systems = [m for m in messages if m.get('role') == 'system']
    other = [m for m in messages if m.get('role') != 'system']
    return systems + timeline_injection_messages(injected) + other, injected, False


def current_user_text(messages: list[dict[str, Any]]) -> str | None:
    for m in reversed(messages):
        if m.get('role') == 'user':
            text = text_from_content(m.get('content'))
            if text:
                return text
    return None


def create_app(db_path: str | Path = DEFAULT_DB, max_handoff_records: int | None = None, max_handoff_chars: int | None = None, api_key: str | None = None) -> Flask:
    app = Flask(__name__)
    CORS(app)
    store = TimelineStore(db_path)
    db_path = Path(db_path)
    push_db = _push_db_path(db_path)
    _init_push_db(push_db)
    max_records = max_handoff_records if max_handoff_records is not None else int(os.environ.get("MAX_HANDOFF_RECORDS", DEFAULT_HANDOFF_RECORDS))
    max_chars = max_handoff_chars if max_handoff_chars is not None else int(os.environ.get("MAX_HANDOFF_CHARS", DEFAULT_HANDOFF_CHARS))
    required_api_key = api_key if api_key is not None else os.environ.get("XIAOKE_API_KEY", "")

    # ── Meta KV helpers（存 per-session turns_since_memory 计数）──────────────────

    def _meta_get(key: str) -> str:
        """读 KV，不存在返回空串。"""
        try:
            conn = sqlite3.connect(str(db_path))
            row = conn.execute(
                'CREATE TABLE IF NOT EXISTS meta_kv (key TEXT PRIMARY KEY, value TEXT); '
                'SELECT value FROM meta_kv WHERE key=?', (key,)
            ).fetchone()
            conn.close()
            return row[0] if row else ''
        except Exception:
            return ''

    def _meta_set(key: str, value: str):
        try:
            conn = sqlite3.connect(str(db_path))
            conn.execute('CREATE TABLE IF NOT EXISTS meta_kv (key TEXT PRIMARY KEY, value TEXT)')
            conn.execute('INSERT INTO meta_kv (key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value))
            conn.commit()
            conn.close()
        except Exception:
            pass

    @app.get('/healthz')
    def healthz():
        mode = 'upstream' if has_upstream() else 'local-mock'
        return jsonify({
            'service': 'xiaoke',
            'mode': mode,
            'database': 'ready',
            'timeline_records': len(store.records()),
        })

    @app.route('/v1/models', methods=['GET', 'OPTIONS'])
    def list_models():
        if request.method == 'OPTIONS':
            r = Response('', 204)
            r.headers['Access-Control-Allow-Origin'] = '*'
            r.headers['Access-Control-Allow-Headers'] = 'Authorization, Content-Type'
            r.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
            return r
        r = jsonify({"object":"list","data":[{"id":"[Kiro] claude-sonnet-4-6-thinking [不补]","object":"model","created":0,"owned_by":"xiaoke"}]})
        r.headers['Access-Control-Allow-Origin'] = '*'
        return r

    @app.after_request
    def add_cors(response):
        response.headers.setdefault('Access-Control-Allow-Origin', '*')
        response.headers.setdefault('Access-Control-Allow-Headers', 'Authorization, Content-Type')
        response.headers.setdefault('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS')
        return response

    @app.get('/internal/memories')
    def internal_memories_get():
        authorization = request.headers.get('Authorization', '')
        token = authorization[7:] if authorization.startswith('Bearer ') else ''
        if required_api_key and not hmac.compare_digest(token, required_api_key):
            return jsonify({'error': 'unauthorized'}), 401
        from memory_store import load_memories
        memories = load_memories()
        return jsonify({'memories': memories, 'count': len(memories)})

    @app.post('/internal/memories')
    def internal_memories_post():
        authorization = request.headers.get('Authorization', '')
        token = authorization[7:] if authorization.startswith('Bearer ') else ''
        if required_api_key and not hmac.compare_digest(token, required_api_key):
            return jsonify({'error': 'unauthorized'}), 401
        body = request.get_json(silent=True) or {}
        content = str(body.get('content') or '').strip()
        if not content:
            return jsonify({'error': 'content is required'}), 400
        ok = save_memory(content)
        return jsonify({'success': ok})

    @app.delete('/internal/memories/<int:index>')
    def internal_memories_delete(index: int):
        authorization = request.headers.get('Authorization', '')
        token = authorization[7:] if authorization.startswith('Bearer ') else ''
        if required_api_key and not hmac.compare_digest(token, required_api_key):
            return jsonify({'error': 'unauthorized'}), 401
        from memory_store import delete_memory_by_index
        ok = delete_memory_by_index(index)
        if not ok:
            return jsonify({'error': 'index out of range'}), 404
        return jsonify({'success': True})

    # ── Cron manual trigger ─────────────────────────────────────────

    @app.route('/internal/cron/run', methods=['GET', 'POST'])
    def cron_run():
        authorization = request.headers.get('Authorization', '')
        token = authorization[7:] if authorization.startswith('Bearer ') else ''
        if not token:
            token = request.args.get('token', '')
        if required_api_key and not hmac.compare_digest(token, required_api_key):
            return jsonify({'error': 'unauthorized'}), 401
        try:
            from cron import main as cron_main
            cron_main()
            return jsonify({'success': True})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    # ── Push endpoints ──────────────────────────────────────────────

    @app.get('/internal/push/vapid-public-key')
    def push_vapid_public_key():
        authorization = request.headers.get('Authorization', '')
        token = authorization[7:] if authorization.startswith('Bearer ') else ''
        if required_api_key and not hmac.compare_digest(token, required_api_key):
            return jsonify({'error': 'unauthorized'}), 401
        _, pub_key = _get_or_create_vapid_keys(push_db)
        if not pub_key:
            return jsonify({'error': 'vapid not available'}), 503
        return jsonify({'public_key': pub_key})

    @app.post('/internal/push/subscribe')
    def push_subscribe():
        authorization = request.headers.get('Authorization', '')
        token = authorization[7:] if authorization.startswith('Bearer ') else ''
        if required_api_key and not hmac.compare_digest(token, required_api_key):
            return jsonify({'error': 'unauthorized'}), 401
        body = request.get_json(silent=True) or {}
        sub = body.get('subscription') or {}
        session_id = str(body.get('session_id') or 'reverie-yy')
        endpoint = sub.get('endpoint', '')
        keys = sub.get('keys', {})
        p256dh = keys.get('p256dh', '')
        auth = keys.get('auth', '')
        if not endpoint or not p256dh or not auth:
            return jsonify({'error': 'invalid subscription'}), 400
        conn = sqlite3.connect(str(push_db))
        conn.execute(
            '''INSERT INTO push_subscriptions (session_id, endpoint, p256dh, auth)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(endpoint) DO UPDATE SET
                 session_id=excluded.session_id,
                 p256dh=excluded.p256dh,
                 auth=excluded.auth''',
            (session_id, endpoint, p256dh, auth)
        )
        conn.commit()
        conn.close()
        return jsonify({'success': True})

    @app.post('/internal/push/send')
    def push_send():
        """Trigger a push notification manually (used by the cron job)."""
        authorization = request.headers.get('Authorization', '')
        token = authorization[7:] if authorization.startswith('Bearer ') else ''
        if required_api_key and not hmac.compare_digest(token, required_api_key):
            return jsonify({'error': 'unauthorized'}), 401
        body = request.get_json(silent=True) or {}
        session_id = str(body.get('session_id') or 'reverie-yy')
        title = str(body.get('title') or '小克')
        msg_body = str(body.get('body') or '')
        url = str(body.get('url') or '/')
        if not msg_body:
            return jsonify({'error': 'body is required'}), 400
        send_push_notification(push_db, session_id, title, msg_body, url)
        return jsonify({'success': True})

    # ── OB Dashboard 全量桶接口 ─────────────────────────────────────
    _ob_session_cookie: dict = {'value': '', 'ts': 0.0}

    @app.route('/internal/ombre-buckets', methods=['GET', 'OPTIONS'])
    def ombre_buckets():
        if request.method == 'OPTIONS':
            r = Response('', 204)
            r.headers['Access-Control-Allow-Origin'] = '*'
            r.headers['Access-Control-Allow-Headers'] = 'Authorization, Content-Type'
            r.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
            return r
        authorization = request.headers.get('Authorization', '')
        token = authorization[7:] if authorization.startswith('Bearer ') else ''
        if required_api_key and not hmac.compare_digest(token, required_api_key):
            return jsonify({'error': 'unauthorized'}), 401

        import requests as _req
        import time as _time
        ombre_url = (os.environ.get('OMBRE_URL') or '').rstrip('/')
        if not ombre_url:
            return jsonify({'error': 'OMBRE_URL not configured'}), 503
        dashboard_password = os.environ.get('OMBRE_DASHBOARD_PASSWORD', '')

        def _do_login():
            resp = _req.post(
                f'{ombre_url}/auth/login',
                json={'password': dashboard_password},
                headers={'Content-Type': 'application/json'},
                timeout=10,
            )
            if resp.status_code >= 400:
                return None
            cookie = ''
            sc = resp.headers.get('set-cookie', '')
            if sc:
                cookie = sc.split(';')[0]
            _ob_session_cookie['value'] = cookie
            _ob_session_cookie['ts'] = _time.time()
            return cookie

        def _fetch_buckets(cookie):
            hdrs = {'Cookie': cookie} if cookie else {}
            resp = _req.get(f'{ombre_url}/api/buckets', headers=hdrs, timeout=20)
            return resp

        try:
            cookie = _ob_session_cookie['value']
            if not cookie or (_time.time() - _ob_session_cookie['ts']) > 1800:
                cookie = _do_login()

            resp = _fetch_buckets(cookie or '')
            if resp.status_code == 401:
                cookie = _do_login()
                if cookie is None:
                    return jsonify({'error': 'ombre login failed'}), 502
                resp = _fetch_buckets(cookie)

            if resp.status_code >= 400:
                return jsonify({'error': f'ombre buckets HTTP {resp.status_code}'}), 502

            data = resp.json()
            buckets = data if isinstance(data, list) else (data.get('buckets') or data.get('items') or [])
            return jsonify({'buckets': buckets, 'total': len(buckets)})

        except Exception as e:
            return jsonify({'error': str(e)}), 502

    @app.route('/internal/mcp-proxy', methods=['POST', 'OPTIONS'])
    def mcp_proxy():
        """代理 MCP HTTP 请求，解决前端 CORS 问题。
        body: { url, method, headers, body }
        """
        if request.method == 'OPTIONS':
            r = Response('', 204)
            r.headers['Access-Control-Allow-Origin'] = '*'
            r.headers['Access-Control-Allow-Headers'] = 'Authorization, Content-Type'
            r.headers['Access-Control-Allow-Methods'] = 'POST, OPTIONS'
            return r
        authorization = request.headers.get('Authorization', '')
        token = authorization[7:] if authorization.startswith('Bearer ') else ''
        if required_api_key and not hmac.compare_digest(token, required_api_key):
            return jsonify({'error': 'unauthorized'}), 401
        body = request.get_json(silent=True) or {}
        target_url = str(body.get('url') or '')
        method = str(body.get('method') or 'POST').upper()
        extra_headers = dict(body.get('headers') or {})
        payload = body.get('body')
        if not target_url:
            return jsonify({'error': 'url is required'}), 400
        try:
            import requests as _req
            resp = _req.request(
                method,
                target_url,
                json=payload if isinstance(payload, dict) else None,
                data=json.dumps(payload) if isinstance(payload, str) else None,
                headers={'Content-Type': 'application/json', **extra_headers},
                timeout=30
            )
            try:
                data = resp.json()
            except Exception:
                data = resp.text
            return jsonify({'status': resp.status_code, 'data': data})
        except Exception as e:
            return jsonify({'error': str(e)}), 502

    # ── Timeline endpoints ──────────────────────────────────────────

    @app.get('/internal/timeline')
    def internal_timeline():
        authorization = request.headers.get("Authorization", "")
        token = authorization[7:] if authorization.startswith("Bearer ") else ""
        if required_api_key and not hmac.compare_digest(token, required_api_key):
            return jsonify({'error': 'unauthorized'}), 401
        records = store.timeline_export(request.args.get('limit', 100), request.args.get('before'))
        return jsonify({'records': records, 'next_before': records[0]['sequence'] if records else None})

    @app.get('/internal/timeline/dates')
    def internal_timeline_dates():
        authorization = request.headers.get("Authorization", "")
        token = authorization[7:] if authorization.startswith("Bearer ") else ""
        if required_api_key and not hmac.compare_digest(token, required_api_key):
            return jsonify({'error': 'unauthorized'}), 401
        return jsonify({'dates': store.timeline_dates()})

    @app.get('/internal/timeline/day')
    def internal_timeline_day():
        authorization = request.headers.get("Authorization", "")
        token = authorization[7:] if authorization.startswith("Bearer ") else ""
        if required_api_key and not hmac.compare_digest(token, required_api_key):
            return jsonify({'error': 'unauthorized'}), 401
        return jsonify({'records': store.timeline_day(request.args.get('date'), request.args.get('limit', 500))})

    @app.get('/internal/timeline/search')
    def internal_timeline_search():
        authorization=request.headers.get('Authorization',''); token=authorization[7:] if authorization.startswith('Bearer ') else ''
        if required_api_key and not hmac.compare_digest(token,required_api_key): return jsonify({'error':'unauthorized'}),401
        return jsonify({'records':store.timeline_search(request.args.get('q'),request.args.get('limit',100))})

    @app.get('/internal/timeline/favorites')
    def internal_timeline_favorites():
        authorization=request.headers.get('Authorization',''); token=authorization[7:] if authorization.startswith('Bearer ') else ''
        if required_api_key and not hmac.compare_digest(token,required_api_key): return jsonify({'error':'unauthorized'}),401
        return jsonify({'records':store.timeline_favorites(request.args.get('limit',500))})

    @app.put('/internal/timeline/records/<record_id>/favorite')
    def internal_timeline_favorite(record_id):
        authorization=request.headers.get('Authorization',''); token=authorization[7:] if authorization.startswith('Bearer ') else ''
        if required_api_key and not hmac.compare_digest(token,required_api_key): return jsonify({'error':'unauthorized'}),401
        favorite=bool((request.get_json(silent=True) or {}).get('favorite'))
        result=store.set_timeline_favorite(record_id,favorite)
        if result is None: return jsonify({'error':'not found'}),404
        return jsonify({'success':True,'favorite':result})

    @app.delete('/internal/timeline/records/<record_id>')
    def internal_timeline_delete(record_id):
        authorization=request.headers.get('Authorization',''); token=authorization[7:] if authorization.startswith('Bearer ') else ''
        if required_api_key and not hmac.compare_digest(token,required_api_key): return jsonify({'error':'unauthorized'}),401
        if not store.delete_timeline_record(record_id): return jsonify({'error':'not found'}),404
        return jsonify({'success':True})

    @app.post('/internal/events')
    def internal_events():
        authorization = request.headers.get("Authorization", "")
        token = authorization[7:] if authorization.startswith("Bearer ") else ""
        if required_api_key and not hmac.compare_digest(token, required_api_key):
            return jsonify({'error': 'unauthorized'}), 401
        body = request.get_json(silent=True) or {}
        content = str(body.get('content') or '').strip()
        if not content: return jsonify({'error': 'content is required'}), 400
        event_id = store.event(content, source='dylan')
        return jsonify({'success': True, 'event_id': event_id})

    @app.post('/internal/coreading/stage')
    def coreading_stage_event():
        authorization = request.headers.get('Authorization', '')
        token = authorization[7:] if authorization.startswith('Bearer ') else ''
        if required_api_key and not hmac.compare_digest(token, required_api_key):
            return jsonify({'error': 'unauthorized'}), 401
        body = request.get_json(silent=True) or {}
        try:
            event_id = coreading_stage(store, reader_id=str(body.get('reader_id') or ''), book_id=str(body.get('book_id') or ''), kind=str(body.get('kind') or ''), content=str(body.get('content') or ''), occurred_at=str(body.get('occurred_at') or ''), dedupe_key=str(body.get('dedupe_key') or ''))
        except ValueError as error:
            return jsonify({'error': str(error)}), 400
        return jsonify({'success': True, 'staging_id': event_id})

    @app.post('/internal/coreading/deliver')
    def coreading_deliver_events():
        authorization = request.headers.get('Authorization', '')
        token = authorization[7:] if authorization.startswith('Bearer ') else ''
        if required_api_key and not hmac.compare_digest(token, required_api_key):
            return jsonify({'error': 'unauthorized'}), 401
        body = request.get_json(silent=True) or {}
        reader_id, book_id = str(body.get('reader_id') or ''), str(body.get('book_id') or '')
        if not reader_id or not book_id: return jsonify({'error': 'reader_id and book_id are required'}), 400
        delivered = coreading_deliver(store, reader_id=reader_id, book_id=book_id)
        return jsonify({'success': True, 'delivered': delivered})

    @app.route('/v1/chat/completions', methods=['POST', 'OPTIONS'])
    def chat_completions():
        if request.method == 'OPTIONS':
            r = Response('', 204)
            r.headers['Access-Control-Allow-Origin'] = '*'
            r.headers['Access-Control-Allow-Headers'] = 'Authorization, Content-Type, X-Session-Id'
            r.headers['Access-Control-Allow-Methods'] = 'POST, OPTIONS'
            r.headers['Access-Control-Max-Age'] = '86400'
            return r
        body = request.get_json(silent=True)
        if required_api_key:
            authorization = request.headers.get("Authorization", "")
            token = authorization[7:] if authorization.startswith("Bearer ") else ""
            if not hmac.compare_digest(token, required_api_key):
                return jsonify({'error': {'message': 'unauthorized', 'type': 'authentication_error'}}), 401

        if not isinstance(body, dict) or not isinstance(body.get('messages'), list):
            return jsonify({'error': {'message': 'messages must be a JSON array', 'type': 'invalid_request_error'}}), 400

        messages = body['messages']
        user_text = current_user_text(messages)
        received_at = utcnow()
        if not user_text:
            return jsonify({'error': {'message': 'an eligible current user message is required', 'type': 'invalid_request_error'}}), 400

        identity = identify_window(request.headers, messages, store)
        session_id = identity.session_id
        source = identity.source

        # ── 记忆搜索 ────────────────────────────────────────────────
        mem_search_text = ''
        if user_text:
            mem_result = search_memories(user_text, max_results=4)
            if mem_result:
                mem_search_text = mem_result

        # ── turns_since_memory 计数 ──────────────────────────────────
        tsm_key = f'tsm:{session_id or "default"}'
        try:
            turns_since_memory = int(_meta_get(tsm_key) or '0')
        except Exception:
            turns_since_memory = 0
        force_hold = turns_since_memory >= 4  # 第5条强制存

        # ── 记忆指令拼接 ─────────────────────────────────────────────
        memory_instruction = (
            '\n\n[记忆操作指令]\n'
            '每条回复后，根据本轮对话自行判断是否值得存入长期记忆。\n'
            '如果决定存入，在回复最后单独一行追加（必须是最后一行，前面不要有空行）：\n'
            '<!--HOLD: 内容 | tags:标签 | importance:重要度-->\n'
            '（tags 和 importance 可不写）\n'
            '如果不存入，不写任何 <!--HOLD--> 即可，不要解释。\n'
            '注意：<!--HOLD--> 这行是给系统读的，不会显示给用户，请正常回复后再加。'
        )
        if force_hold:
            memory_instruction += (
                '\n\n⚠️ 你已连续 5 条没有主动存入记忆，本条必须存入一条，'
                '请在回复末尾追加 <!--HOLD: 本轮最值得记录的信息-->。'
            )

        breath_text = get_breath_memories(max_results=20) or load_memories_text(max_count=30)
        guidance = None

        # 单独拉最近的花园日志（cron source），注入system prompt
        cron_logs = store.timeline_search_source('cron', limit=3)
        cron_text = ''
        if cron_logs:
            lines = []
            for rec in cron_logs:
                content = (rec.get('content') or '').strip()[:300]
                if content:
                    lines.append(content)
            if lines:
                cron_text = '\n'.join(lines)

        # 拉最近的小窝对话（reverie source），注入system prompt，实现跨窗口感知
        reverie_logs = store.timeline_search_source('reverie', limit=6)
        reverie_text = ''
        if reverie_logs and source != 'reverie':  # 只在非小窝请求时注入，避免重复
            lines = []
            for rec in reverie_logs:
                role_label = '言言' if rec.get('role') == 'user' else '小克'
                content = (rec.get('content') or '').strip()[:200]
                if content:
                    lines.append(f'[{role_label}] {content}')
            if lines:
                reverie_text = '\n'.join(lines)

        # ── 稳定段（BP1，加 cache_control）vs 动态段（不缓存）──────────
        # 稳定：原始人设 + 长期记忆（breath），几乎不变，可以缓存
        # 动态：本轮记忆召回、小窝日志、花园日志、记忆指令，每轮都变，不挂标
        stable_extra = ''
        if breath_text:
            stable_extra += '\n\n[长期记忆 / 关于言言的记忆]\n' + breath_text

        volatile_extra = ''
        if mem_search_text:
            volatile_extra += '\n\n[相关记忆检索结果]\n' + mem_search_text
        if guidance:
            volatile_extra += '\n\n[此刻说话方式]\n' + guidance
        if reverie_text:
            volatile_extra += '\n\n[小窝最近的对话记录]\n' + reverie_text
        if cron_text:
            volatile_extra += '\n\n[花园动态]\n' + cron_text
        if memory_instruction:
            volatile_extra += memory_instruction

        if stable_extra or volatile_extra:
            injected_systems = []
            found_system = False
            for m in messages:
                if m.get('role') == 'system' and not found_system:
                    # BP1：稳定 system block 挂 cache_control
                    stable_content = m['content'] + stable_extra
                    injected_systems.append({
                        **m,
                        'content': [{'type': 'text', 'text': stable_content,
                                     'cache_control': {'type': 'ephemeral'}}]
                    })
                    found_system = True
                else:
                    injected_systems.append(m)
            if not found_system:
                injected_systems = [{
                    'role': 'system',
                    'content': [{'type': 'text', 'text': stable_extra.strip(),
                                 'cache_control': {'type': 'ephemeral'}}]
                }] + messages
            # 动态内容单独作为第二个 system block（不挂标，不影响 BP1 缓存前缀）
            if volatile_extra:
                injected_systems.append({'role': 'system', 'content': volatile_extra.strip()})
            messages = injected_systems

        assembled, baseline, continuing = build_messages(messages, store, session_id, max_records, max_chars)
        model = str(body.get('model') or 'claude-opus-4-6-thinking')
        is_stream = body.get('stream', False)
        request_options = {key: value for key, value in body.items()
                           if key not in ('messages', 'model', 'stream')}

        if has_upstream():
            from upstream import forward_non_stream, forward_stream, extract_stream_content, request_payload

            clean_messages = []
            for m in assembled:
                clean = {k: v for k, v in m.items() if k != 'xiaoke_record_id'}
                clean_messages.append(clean)

            if is_stream:
                def generate_stream():
                    chunks_collected = []
                    completed = False
                    try:
                        for sse_event, is_done in forward_stream(clean_messages, model, request_options):
                            chunks_collected.append(sse_event)
                            yield sse_event
                            if is_done:
                                completed = True
                    except Exception:
                        return

                    if completed:
                        reply = extract_stream_content(chunks_collected)
                        if reply.strip():
                            clean_reply, hold_items = _process_hold_blocks(reply)
                            if hold_items:
                                for h in hold_items:
                                    try:
                                        hold_memory(h['content'], importance=h['importance'], tags=h['tags'])
                                    except Exception:
                                        pass
                                _meta_set(tsm_key, '0')
                            else:
                                _meta_set(tsm_key, str(turns_since_memory + 1))
                            store.completed_turn(user_text, clean_reply, source=source, user_created_at=received_at)
                            if baseline and not continuing:
                                store.save_continuity(session_id or new_session_id(source), [r.__dict__ for r in baseline], user_text, clean_reply)

                return Response(generate_stream(), content_type='text/event-stream',
                              headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})
            else:
                try:
                    upstream_response = forward_non_stream(clean_messages, model, request_options)
                except Exception as e:
                    return jsonify({'error': {'message': f'upstream error: {str(e)}', 'type': 'upstream_error'}}), 502

                choices = upstream_response.get('choices', [])
                reply = ''
                if choices:
                    reply = choices[0].get('message', {}).get('content', '')

                if reply.strip():
                    # 解析并处理 [HOLD] 块
                    clean_reply, hold_items = _process_hold_blocks(reply)
                    if hold_items:
                        for h in hold_items:
                            try:
                                hold_memory(h['content'], importance=h['importance'], tags=h['tags'])
                            except Exception:
                                pass
                        _meta_set(tsm_key, '0')
                    else:
                        _meta_set(tsm_key, str(turns_since_memory + 1))
                    # 用清洗后的回复存 timeline 和返回给前端
                    store.completed_turn(user_text, clean_reply, source=source, user_created_at=received_at)
                    if baseline and not continuing:
                        store.save_continuity(session_id or new_session_id(source), [r.__dict__ for r in baseline], user_text, clean_reply)
                    # 替换返回内容里的 choices
                    if choices:
                        upstream_response['choices'][0]['message']['content'] = clean_reply
                    # 如果是小窝的请求，推送通知给言言
                    if source == 'reverie':
                        import re as _re
                        # 去掉[心声]块和颜文字，取前50字作通知正文
                        _body = _re.sub(r'\[心声\][\s\S]*?\[/心声\]', '', reply).strip()
                        _body = _re.sub(r'\n\n+', '\n', _body).split('\n')[0][:50]
                        try:
                            send_push_notification(push_db, session_id or 'reverie-yy', '小克回复了', _body, '/')
                        except Exception:
                            pass

                return jsonify(upstream_response)

        else:
            if is_stream:
                disconnect = request.headers.get('X-Xiaoke-Test-Disconnect') == '1'

                def generate_mock():
                    completed = False
                    for event in mock_sse_events(user_text, model, disconnect):
                        if event == 'data: [DONE]\n\n':
                            completed = True
                        yield event
                    if completed:
                        reply = f'[xiaoke local mock] received: {user_text}'
                        store.completed_turn(user_text, reply, source=source, user_created_at=received_at)
                        if baseline and not continuing:
                            store.save_continuity(session_id or new_session_id(source), [r.__dict__ for r in baseline], user_text, reply)

                return Response(generate_mock(), content_type='text/event-stream',
                              headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

            reply = f'[xiaoke local mock] received: {user_text}'
            request_id, user_id, assistant_id = store.completed_turn(user_text, reply, source=source, user_created_at=received_at)

            if baseline and not continuing:
                store.save_continuity(session_id or new_session_id(source), [r.__dict__ for r in baseline], user_text, reply)

            response = as_openai_response(reply, model)
            response['xiaoke_debug'] = {
                'stage': 'local-mock-only',
                'request_id': request_id,
                'stored_record_ids': [user_id, assistant_id],
                'injected_sequences': [r.sequence for r in baseline],
                'continuity_active': bool(baseline or continuing),
                'window_kind': identity.kind,
                'assembled_message_count': len(assembled),
            }
            return jsonify(response)

    return app


import re as _re_top

def _process_hold_blocks(reply: str) -> tuple[str, list[dict]]:
    """从回复里提取所有 <!--HOLD: ...--> 标记，返回 (清洗后回复, hold条目列表)。"""
    pattern = _re_top.compile(r'<!--HOLD:\s*(.+?)-->', _re_top.DOTALL)
    holds = []
    for m in pattern.finditer(reply):
        raw = m.group(1).strip()
        parts = [p.strip() for p in raw.split('|')]
        content = parts[0] if parts else raw
        tags = ''
        importance = 5
        for p in parts[1:]:
            if p.lower().startswith('tags:'):
                tags = p[5:].strip()
            elif p.lower().startswith('importance:'):
                try:
                    importance = max(1, min(10, int(p[11:].strip())))
                except Exception:
                    pass
        if content:
            holds.append({'content': content, 'tags': tags, 'importance': importance})
    clean = pattern.sub('', reply).rstrip()
    return clean, holds


def mock_sse_events(user_text, model, simulate_disconnect=False):
    response_id = f'chatcmpl-xiaoke-mock-{uuid.uuid4().hex}'
    words = ['[xiaoke local mock] ', 'received: ', user_text]
    yield 'data: ' + json.dumps({'id': response_id, 'object': 'chat.completion.chunk', 'created': 0,
                                  'model': model, 'choices': [{'index': 0, 'delta': {'role': 'assistant'}, 'finish_reason': None}]}) + '\n\n'
    for index, word in enumerate(words):
        yield 'data: ' + json.dumps({'id': response_id, 'object': 'chat.completion.chunk', 'created': 0,
                                      'model': model, 'choices': [{'index': 0, 'delta': {'content': word}, 'finish_reason': None}]}) + '\n\n'
        if simulate_disconnect and index == 0:
            return
    yield 'data: ' + json.dumps({'id': response_id, 'object': 'chat.completion.chunk', 'created': 0,
                                  'model': model, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]}) + '\n\n'
    yield 'data: [DONE]\n\n'
