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


def _call_mcp_tool(tool_name: str, arguments: dict, timeout: int = 6) -> Optional[list]:
    """通用 MCP 工具调用，返回 content 列表，失败返回 None。"""
    if not OMBRE_URL:
        return None
    try:
        payload = json.dumps({
            'jsonrpc': '2.0',
            'id': uuid.uuid4().hex,
            'method': 'tools/call',
            'params': {'name': tool_name, 'arguments': arguments}
        }).encode()
        req = urllib.request.Request(
            f'{OMBRE_URL}/mcp',
            data=payload,
            headers=_headers(),
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode('utf-8', errors='replace')
            for line in text.splitlines():
                line = line.strip()
                if line.startswith('data:'):
                    line = line[5:].strip()
                if not line or line == '[DONE]':
                    continue
                try:
                    j = json.loads(line)
                    content = (j.get('result') or {}).get('content') or []
                    if content:
                        return content
                except Exception:
                    continue
    except Exception:
        pass
    return None


_CLEAN_PATTERNS = [
    # 元数据行
    r'\[bucket_id:[^\]]*\]',
    r'\[content_role:[^\]]*\]',
    r'\[instructions:[^\]]*\]',
    r'\[may_call_tools:[^\]]*\]',
    r'\[boundary_id:[^\]]*\]',
    r'\[payload_[^\]]*\]',
    r'📌\s*\[核心准则\][^\n]*',
    r'👣[^\n]*',           # Footprint 行
    r'💭\s*meaning:[^\n]*',
    r'^meaning:[^\n]*',
]


def _clean_memory_text(raw: str) -> str:
    """清洗 OB 返回文本，去掉元数据行，保留正文。"""
    import re
    text = raw
    for pat in _CLEAN_PATTERNS:
        text = re.sub(pat, '', text, flags=re.IGNORECASE | re.MULTILINE)
    lines = [l.strip() for l in text.splitlines()]
    lines = [l for l in lines if l and l != '---' and not l.startswith('[')]
    return '\n'.join(lines).strip()


def hold_memory(content: str, importance: int = 5, tags: str = '') -> bool:
    """调用 OB hold 工具存一条记忆，成功返回 True。"""
    if not OMBRE_URL or not (content or '').strip():
        return False
    args: dict = {'content': content.strip(), 'importance': importance}
    if tags:
        args['tags'] = tags
    result = _call_mcp_tool('hold', args, timeout=12)
    return result is not None


def search_memories(query: str, max_results: int = 4) -> Optional[str]:
    """用 breath_search 搜索相关记忆，返回清洗后的文本，无结果返回 None。"""
    if not OMBRE_URL or not (query or '').strip():
        return None
    content = _call_mcp_tool('breath_search', {
        'query': query[:150],
        'max_results': max_results,
    })
    if not content:
        return None
    raw = '\n---\n'.join(block.get('text') or '' for block in content if block.get('type') == 'text')
    cleaned = _clean_memory_text(raw)
    # 如果清洗后剩下的内容太短（<15字），认为没有有用信息
    return cleaned if len(cleaned) >= 15 else None


def get_breath_memories(max_results: int = 15) -> Optional[str]:
    """调用 OB breath 工具，返回格式化的记忆文本，失败返回 None。"""
    # MCP协议调试中，暂时禁用避免阻塞
    return None
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
        with urllib.request.urlopen(req, timeout=3) as resp:
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
