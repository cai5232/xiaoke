"""Ombre Brain 记忆客户端

在 system prompt 里注入 breath 浮现的高权重记忆。
失败时静默返回 None，不阻塞主流程。

环境变量：
  OMBRE_URL      Ombre Brain MCP 服务地址，如 https://caiovo.zeabur.app
  OMBRE_TOKEN    可选，MCP 鉴权 Token（关闭鉴权时留空）
"""
from __future__ import annotations
import os
import json
import uuid
import urllib.request
from typing import Optional

OMBRE_URL = (os.environ.get('OMBRE_URL') or '').rstrip('/')
OMBRE_TOKEN = os.environ.get('OMBRE_TOKEN') or ''


def _headers() -> dict:
    h = {'Content-Type': 'application/json'}
    if OMBRE_TOKEN:
        h['Authorization'] = f'Bearer {OMBRE_TOKEN}'
    return h


def get_breath_memories(max_results: int = 15) -> Optional[str]:
    """调用 OB breath 工具，返回格式化的记忆文本，失败返回 None。"""
    if not OMBRE_URL:
        return None
    try:
        # MCP Streamable HTTP: POST /mcp，JSON-RPC 2.0 格式
        payload = json.dumps({
            'jsonrpc': '2.0',
            'id': uuid.uuid4().hex,
            'method': 'tools/call',
            'params': {
                'name': 'breath',
                'arguments': {'max_results': max_results}
            }
        }).encode()
        req = urllib.request.Request(
            f'{OMBRE_URL}/mcp',
            data=payload,
            headers=_headers(),
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read()
            # Streamable HTTP 可能返回多行 SSE 或单个 JSON，兼容两种
            text = raw.decode('utf-8', errors='replace')
            # 尝试逐行找到 JSON-RPC result
            for line in text.splitlines():
                line = line.strip()
                if line.startswith('data:'):
                    line = line[5:].strip()
                if not line or line == '[DONE]':
                    continue
                try:
                    j = json.loads(line)
                    result = j.get('result') or {}
                    content = result.get('content') or []
                    for block in content:
                        if block.get('type') == 'text':
                            return block.get('text') or None
                except Exception:
                    continue
    except Exception:
        pass
    return None
