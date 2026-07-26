"""OpenAI-compatible gateway for xiaoke.
Supports both local mock (stage 3A) and real upstream forwarding (stage 3B+)."""
from __future__ import annotations
import json
import uuid
import os
import hmac
from pathlib import Path
from typing import Any
from flask import Flask, Response, jsonify, request
from mock_gateway import assemble
from storage import TimelineStore, utcnow
from coreading import stage as coreading_stage, deliver as coreading_deliver
from timeline_context import TimelineRecord, rolling_records, text_from_content
from window_identity import identify_window, new_session_id

DEFAULT_DB = Path(__file__).resolve().parent / 'data' / 'xiaoke.sqlite'
DEFAULT_HANDOFF_RECORDS = 999
DEFAULT_HANDOFF_CHARS = 800000



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
    """Render shared history as ordinary prior dialogue.

    Timestamps remain in SQLite for ordering, wake-up logic, and future user
    views, but are intentionally not sent upstream. A time/header marker is
    not needed for continuity and can leak into a model's visible reply.
    """
    result = []
    for record in records:
        role = record.role if record.role in ('user', 'assistant') else 'assistant'
        result.append({'role': role, 'content': record.content, 'xiaoke_record_id': record.id})
    return result

def build_messages(messages: list[dict[str, Any]], store: TimelineStore, session_id: str | None, max_records: int, max_chars: int) -> tuple[list[dict[str, Any]], list[TimelineRecord], bool]:
    """Build the full message list to send to model.
    Returns (assembled_messages, baseline_records, is_continuing_session)."""
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
    store = TimelineStore(db_path)
    max_records = max_handoff_records if max_handoff_records is not None else int(os.environ.get("MAX_HANDOFF_RECORDS", DEFAULT_HANDOFF_RECORDS))
    max_chars = max_handoff_chars if max_handoff_chars is not None else int(os.environ.get("MAX_HANDOFF_CHARS", DEFAULT_HANDOFF_CHARS))
    required_api_key = api_key if api_key is not None else os.environ.get("XIAOKE_API_KEY", "")

    @app.get('/healthz')
    def healthz():
        mode = 'upstream' if has_upstream() else 'local-mock'
        return jsonify({
            'service': 'xiaoke',
            'mode': mode,
            'database': 'ready',
            'timeline_records': len(store.records()),
        })

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

    @app.post('/v1/chat/completions')
    def chat_completions():
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

        # Window identification is frontend-specific and intentionally conservative.
        identity = identify_window(request.headers, messages, store)
        session_id = identity.session_id
        source = identity.source


        # Kelivo keeps the search tool schema, but its duplicate usage guide is not forwarded.

        # Build assembled messages with timeline injection
        assembled, baseline, continuing = build_messages(messages, store, session_id, max_records, max_chars)
        model = str(body.get('model') or 'claude-opus-4-6-thinking')
        is_stream = body.get('stream', False)
        # xiaoke owns rebuilt messages/model/stream. Preserve all other
        # OpenAI-compatible options, including tools and tool_choice.
        request_options = {key: value for key, value in body.items()
                           if key not in ('messages', 'model', 'stream')}

        if has_upstream():
            # Real upstream forwarding
            from upstream import forward_non_stream, forward_stream, extract_stream_content, request_payload

            # Clean assembled messages: remove xiaoke_record_id before sending upstream
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
                        # Upstream error/disconnect: don't commit
                        return

                    if completed:
                        reply = extract_stream_content(chunks_collected)
                        if reply.strip():
                            store.completed_turn(user_text, reply, source=source, user_created_at=received_at)
                            if baseline and not continuing:
                                store.save_continuity(session_id or new_session_id(source), [r.__dict__ for r in baseline], user_text, reply)

                return Response(generate_stream(), content_type='text/event-stream',
                              headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})
            else:
                # Non-streaming
                try:
                    upstream_response = forward_non_stream(clean_messages, model, request_options)
                except Exception as e:
                    return jsonify({'error': {'message': f'upstream error: {str(e)}', 'type': 'upstream_error'}}), 502

                # Extract assistant reply
                choices = upstream_response.get('choices', [])
                reply = ''
                if choices:
                    reply = choices[0].get('message', {}).get('content', '')

                if reply.strip():
                    store.completed_turn(user_text, reply, source=source, user_created_at=received_at)
                    if baseline and not continuing:
                        store.save_continuity(session_id or new_session_id(source), [r.__dict__ for r in baseline], user_text, reply)

                return jsonify(upstream_response)

        else:
            # Local mock mode (stage 3A)
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


# Mock SSE helpers (retained for local testing)
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
