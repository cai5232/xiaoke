"""Real upstream forwarding for xiaoke Gateway.
Handles both streaming and non-streaming OpenAI-compatible API calls."""
from __future__ import annotations
import json
import os
import httpx
from typing import Generator

UPSTREAM_URL = os.environ.get('XIAOKE_UPSTREAM_URL', '').strip().rstrip('/')
UPSTREAM_KEY = os.environ.get('XIAOKE_UPSTREAM_KEY', '').strip()
UPSTREAM_TIMEOUT = int(os.environ.get('XIAOKE_UPSTREAM_TIMEOUT', '120'))


def upstream_config() -> tuple[str, str, int]:
    """Read xiaoke-specific settings first, then the existing gateway config."""
    base_url = (os.environ.get("XIAOKE_UPSTREAM_URL") or os.environ.get("UPSTREAM_BASE_URL") or "").strip().rstrip("/")
    api_key = (os.environ.get("XIAOKE_UPSTREAM_KEY") or os.environ.get("UPSTREAM_API_KEY") or "").strip()
    timeout = int(os.environ.get("XIAOKE_UPSTREAM_TIMEOUT", "120"))
    return base_url, api_key, timeout


def chat_completions_url() -> str:
    base_url, _, _ = upstream_config()
    suffix = "/chat/completions" if base_url.endswith("/v1") else "/v1/chat/completions"
    return f"{base_url}{suffix}"

def _headers() -> dict[str, str]:
    _, api_key, _ = upstream_config()
    h = {
        'Content-Type': 'application/json',
        'Accept': 'application/json',
    }
    if api_key:
        h['Authorization'] = f'Bearer {api_key}'
    return h


def request_payload(messages: list[dict], model: str, stream: bool, request_options: dict | None = None) -> dict:
    """Rebuild a request after xiaoke has replaced its messages."""
    payload = dict(request_options or {})
    payload.update({'model': model, 'messages': messages, 'stream': stream})
    return payload


def forward_non_stream(messages: list[dict], model: str, request_options: dict | None = None) -> dict:
    """Forward a non-streaming request to upstream and return the full response."""
    url = chat_completions_url()
    payload = request_payload(messages, model, False, request_options)
    with httpx.Client(timeout=upstream_config()[2]) as client:
        resp = client.post(url, json=payload, headers=_headers())
        resp.raise_for_status()
        return resp.json()


def forward_stream(messages: list[dict], model: str, request_options: dict | None = None) -> Generator[tuple[str, bool], None, None]:
    """Forward a streaming request. Yields (sse_event_string, is_done) tuples.
    
    Each sse_event_string is ready to send directly to the client.
    is_done=True on the final [DONE] event.
    """
    url = chat_completions_url()
    payload = request_payload(messages, model, True, request_options)
    with httpx.Client(timeout=upstream_config()[2]) as client:
        with client.stream('POST', url, json=payload, headers=_headers()) as resp:
            resp.raise_for_status()
            buffer = ''
            for raw_chunk in resp.iter_bytes():
                buffer += raw_chunk.decode('utf-8', errors='replace')
                lines = buffer.split('\n')
                buffer = lines.pop()  # 最后一个不完整的行留到下一轮拼接
                for line in lines:
                    line = line.strip()
                    if not line:
                        continue
                    if line.startswith('data:'):
                        data_part = line[5:].strip()
                        is_done = (data_part == '[DONE]')
                        yield line + '\n\n', is_done
                        if is_done:
                            return
            # 处理剩余 buffer
            if buffer.strip():
                line = buffer.strip()
                if line.startswith('data:'):
                    data_part = line[5:].strip()
                    is_done = (data_part == '[DONE]')
                    yield line + '\n\n', is_done


def extract_stream_content(sse_events: list[str]) -> str:
    """Extract the full assistant reply from collected SSE events."""
    content_parts = []
    for event_str in sse_events:
        if not event_str.startswith('data:'):
            continue
        data_part = event_str[5:].strip().rstrip('\n')
        if data_part == '[DONE]':
            continue
        try:
            obj = json.loads(data_part)
            choices = obj.get('choices', [])
            if choices:
                delta = choices[0].get('delta', {})
                c = delta.get('content', '')
                if c:
                    content_parts.append(c)
        except (json.JSONDecodeError, IndexError, KeyError):
            continue
    return ''.join(content_parts)
