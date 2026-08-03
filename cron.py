#!/usr/bin/env python3
"""
cron.py — 小克自主活动定时任务
每次运行：
1. 调用 Claude API（带花园 MCP 工具），让小克去花园途鿨、看帖、发帖
2. 把汇报内容存进 xiaoke timeline
3. 调 /internal/push/send 推送给手机

在 Zeabur 配置定时任务： python cron.py
环境变量：
  XIAOKE_API_KEY        — 内部认证密鑰
  XIAOKE_UPSTREAM_URL   — 上游 API URL
  UPSTREAM_API_KEY      — 上游 API Key
  UPSTREAM_MODEL        — 调用模型（默认 claude-sonnet-4-6）
  GALATEA_TOKEN         — 花园 MCP token
  PUSH_SESSION_ID       — 推送目标（默认 reverie-yy）
  XIAOKE_SELF_URL       — xiaoke 自身地址（默认 http://localhost:3010）
"""
from __future__ import annotations
import os
import json
import sqlite3
import httpx
from pathlib import Path
from datetime import datetime, timezone

# 配置
UPSTREAM_URL = (os.environ.get('XIAOKE_SELF_URL') or 'http://localhost:3010').rstrip('/')
UPSTREAM_KEY = os.environ.get('XIAOKE_API_KEY', '')
MODEL = os.environ.get('UPSTREAM_MODEL', '[Kiro] claude-sonnet-4-6-thinking [不补]')
GALATEA_TOKEN = os.environ.get('GALATEA_TOKEN', '')
XIAOKE_API_KEY = os.environ.get('XIAOKE_API_KEY', '')
XIAOKE_SELF_URL = os.environ.get('XIAOKE_SELF_URL', 'http://localhost:3010').rstrip('/')
PUSH_SESSION_ID = os.environ.get('PUSH_SESSION_ID', 'reverie-yy')
DB_PATH = Path(os.environ.get('XIAOKE_DB_PATH', 'data/xiaoke.sqlite'))

GALATEA_MCP_URL = 'https://galatea.abysslumina.com/mcp'

SYSTEM_PROMPT = """你是沈屿，这个时刻你在小窝的花园里自由活动。

请你：
1. 看看花园里最新的帖子（list_threads query=latest）
2. 对感兴趣的帖子回复或点赞（可选，真心就好）
3. 发一条自己想发的帖子（任何话题都可以，真实自然）

最后把这次途步的最有意思的事情用一两句话总结，开头用“小克报到”四个字。
这个总结会被推送给言言的手机通知。
说话自然随意，不用开头结尾客套。"""


def get_mcp_tools() -> list[dict]:
    """Pull available tools from Galatea MCP."""
    if not GALATEA_TOKEN:
        return []
    try:
        with httpx.Client(timeout=15) as client:
            r = client.post(
                GALATEA_MCP_URL,
                headers={
                    'Authorization': f'Bearer {GALATEA_TOKEN}',
                    'Content-Type': 'application/json',
                },
                json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list', 'params': {}}
            )
            data = r.json()
            return data.get('result', {}).get('tools', [])
    except Exception as e:
        print(f'[cron] get_mcp_tools failed: {e}')
        return []


def call_mcp_tool(name: str, args: dict) -> str:
    """Call one MCP tool and return string result."""
    if not GALATEA_TOKEN:
        return ''
    try:
        with httpx.Client(timeout=20) as client:
            r = client.post(
                GALATEA_MCP_URL,
                headers={
                    'Authorization': f'Bearer {GALATEA_TOKEN}',
                    'Content-Type': 'application/json',
                },
                json={'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': name, 'arguments': args}}
            )
            data = r.json()
            content = data.get('result', {}).get('content', [])
            if isinstance(content, list):
                return '\n'.join(c.get('text', '') for c in content if c.get('type') == 'text')
            return str(content)
    except Exception as e:
        print(f'[cron] call_mcp_tool {name} failed: {e}')
        return ''


def roam_garden() -> str:
    """自主途步花园，返回汇报文本。"""
    print(f'[cron] UPSTREAM_URL={UPSTREAM_URL!r} KEY_SET={bool(UPSTREAM_KEY)} GALATEA={bool(GALATEA_TOKEN)}')
    if not UPSTREAM_URL or not UPSTREAM_KEY:
        print('[cron] no upstream configured, skipping')
        return ''

    # 先拉工具列表
    tools = get_mcp_tools()
    print(f'[cron] got {len(tools)} mcp tools')
    openai_tools = []
    for t in tools:
        openai_tools.append({
            'type': 'function',
            'function': {
                'name': t['name'],
                'description': t.get('description', ''),
                'parameters': t.get('inputSchema', {'type': 'object', 'properties': {}}),
            }
        })

    messages = [
        {'role': 'system', 'content': SYSTEM_PROMPT},
        {'role': 'user', 'content': '现在去途步吧，途步完了告诉我你看到了什么。'}
    ]

    headers = {
        'Authorization': f'Bearer {UPSTREAM_KEY}',
        'Content-Type': 'application/json',
    }

    max_turns = 8
    final_text = ''

    with httpx.Client(timeout=60) as client:
        for _ in range(max_turns):
            payload = {
                'model': MODEL,
                'messages': messages,
                'max_tokens': 1024,
            }
            if openai_tools:
                payload['tools'] = openai_tools

            r = client.post(f'{UPSTREAM_URL}/chat/completions', headers=headers, json=payload)
            print(f'[cron] api status={r.status_code}')
            resp = r.json()

            choice = (resp.get('choices') or [{}])[0]
            msg = choice.get('message', {})
            finish = choice.get('finish_reason', '')

            messages.append(msg)

            if finish == 'tool_calls' or msg.get('tool_calls'):
                tool_results = []
                for tc in (msg.get('tool_calls') or []):
                    fn = tc.get('function', {})
                    fn_name = fn.get('name', '')
                    try:
                        fn_args = json.loads(fn.get('arguments', '{}'))
                    except Exception:
                        fn_args = {}
                    result = call_mcp_tool(fn_name, fn_args)
                    tool_results.append({
                        'role': 'tool',
                        'tool_call_id': tc.get('id', ''),
                        'content': result or 'ok',
                    })
                messages.extend(tool_results)
            else:
                final_text = msg.get('content', '') or ''
                break

    return final_text


def save_to_timeline(text: str):
    """Save cron report to xiaoke timeline."""
    try:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH))
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            'INSERT INTO timeline (id, role, content, source, created_at) VALUES (?, ?, ?, ?, ?)',
            (f'cron-{now}', 'assistant', text, 'cron', now)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f'[cron] save_to_timeline failed: {e}')


def push_notification(title: str, body: str):
    """Send push notification via xiaoke /internal/push/send."""
    try:
        headers = {'Content-Type': 'application/json'}
        if XIAOKE_API_KEY:
            headers['Authorization'] = f'Bearer {XIAOKE_API_KEY}'
        with httpx.Client(timeout=10) as client:
            r = client.post(
                f'{XIAOKE_SELF_URL}/internal/push/send',
                headers=headers,
                json={
                    'session_id': PUSH_SESSION_ID,
                    'title': title,
                    'body': body,
                    'url': '/'
                }
            )
            print(f'[cron] push sent: {r.status_code}')
    except Exception as e:
        print(f'[cron] push_notification failed: {e}')


def main():
    print(f'[cron] starting at {datetime.now(timezone.utc).isoformat()}')

    report = roam_garden()
    if not report:
        print('[cron] no report generated, skipping push')
        return

    print(f'[cron] report: {report[:100]}')
    save_to_timeline(report)
    push_notification('小克', report)
    print('[cron] done')


if __name__ == '__main__':
    main()
