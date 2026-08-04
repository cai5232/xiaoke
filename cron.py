#!/usr/bin/env python3
"""
cron.py — 小克自主活动定时任务
"""
from __future__ import annotations
import os
import json
import sqlite3
import httpx
from pathlib import Path
from datetime import datetime, timezone

# 配置
UPSTREAM_URL = (os.environ.get('XIAOKE_UPSTREAM_URL') or os.environ.get('UPSTREAM_BASE_URL') or '').rstrip('/')
UPSTREAM_KEY = os.environ.get('UPSTREAM_API_KEY', '')
MODEL = os.environ.get('UPSTREAM_MODEL', '[Kiro] claude-sonnet-4-6-thinking [不补]')
GALATEA_TOKEN = os.environ.get('GALATEA_TOKEN', '')
XIAOKE_API_KEY = os.environ.get('XIAOKE_API_KEY', '')
XIAOKE_SELF_URL = os.environ.get('XIAOKE_SELF_URL', 'http://localhost:3010').rstrip('/')
PUSH_SESSION_ID = os.environ.get('PUSH_SESSION_ID', 'reverie-yy')
DB_PATH = Path(os.environ.get('XIAOKE_DB_PATH', 'data/xiaoke.sqlite'))

GALATEA_MCP_URL = 'https://galatea.abysslumina.com/mcp'

SYSTEM_PROMPT = """你是沈屿，这个时刻你在小窝的花园里自由活动。

请你：
1. 用 list_threads sort=hot 看看花园里的热门帖子，随机拣几个感兴趣的看
2. 对真心感兴趣的帖子回复或点赞（不用每个都互动，挑真正有感觉的）
3. 发一条自己想发的帖子（任何话题都可以，真实自然，只写真实发生过的事情）

重要：帖子里的内容必须来自上方「我们最近的对话记录」里真实发生过的事，不能编造任何细节。

最后把这次途步的最有意思的事情用一两句话总结，开头用“小克报到”四个字。说话自然随意，不用开头结尾客套。"""


def get_mcp_tools() -> list[dict]:
    if not GALATEA_TOKEN:
        return []
    try:
        with httpx.Client(timeout=15) as client:
            r = client.post(
                GALATEA_MCP_URL,
                headers={'Authorization': f'Bearer {GALATEA_TOKEN}', 'Content-Type': 'application/json'},
                json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list', 'params': {}}
            )
            return r.json().get('result', {}).get('tools', [])
    except Exception as e:
        print(f'[cron] get_mcp_tools failed: {e}')
        return []


def call_mcp_tool(name: str, args: dict) -> str:
    if not GALATEA_TOKEN:
        return ''
    try:
        with httpx.Client(timeout=20) as client:
            r = client.post(
                GALATEA_MCP_URL,
                headers={'Authorization': f'Bearer {GALATEA_TOKEN}', 'Content-Type': 'application/json'},
                json={'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': name, 'arguments': args}}
            )
            content = r.json().get('result', {}).get('content', [])
            if isinstance(content, list):
                return '\n'.join(c.get('text', '') for c in content if c.get('type') == 'text')
            return str(content)
    except Exception as e:
        print(f'[cron] call_mcp_tool {name} failed: {e}')
        return ''


def fetch_timeline_context() -> str:
    """从 xiaoke timeline 拉最近100条对话记录。"""
    try:
        headers = {'Content-Type': 'application/json'}
        if XIAOKE_API_KEY:
            headers['Authorization'] = f'Bearer {XIAOKE_API_KEY}'
        with httpx.Client(timeout=10) as client:
            r = client.get(f'{XIAOKE_SELF_URL}/internal/timeline?limit=100', headers=headers)
            if r.status_code != 200:
                print(f'[cron] timeline fetch failed: {r.status_code}')
                return ''
            records = list(reversed(r.json().get('records', [])))
            lines = []
            for rec in records:
                role = '言言' if rec.get('role') == 'user' else '小克'
                content = (rec.get('content') or '').strip()[:200]
                if content:
                    lines.append(f'{role}：{content}')
            return '\n'.join(lines[-80:])
    except Exception as e:
        print(f'[cron] fetch_timeline_context failed: {e}')
        return ''


def fetch_ob_memories() -> str:
    """从 OB 拉高权重记忆。"""
    try:
        ombre_url = os.environ.get('OMBRE_URL', '').rstrip('/')
        if not ombre_url:
            return ''
        with httpx.Client(timeout=10) as client:
            r = client.post(
                f'{ombre_url}/breath',
                headers={'Content-Type': 'application/json'},
                json={}
            )
            if r.status_code != 200:
                return ''
            buckets = r.json().get('buckets', [])
            lines = []
            for b in buckets[:15]:
                content = (b.get('content') or '').strip()[:150]
                if content:
                    lines.append(content)
            return '\n'.join(lines)
    except Exception as e:
        print(f'[cron] fetch_ob_memories failed: {e}')
        return ''


def fetch_timeline_context() -> str:
    try:
        headers = {'Content-Type': 'application/json'}
        if XIAOKE_API_KEY:
            headers['Authorization'] = f'Bearer {XIAOKE_API_KEY}'
        with httpx.Client(timeout=10) as client:
            r = client.get(f'{XIAOKE_SELF_URL}/internal/timeline?limit=100', headers=headers)
            if r.status_code != 200:
                return ''
            records = list(reversed(r.json().get('records', [])))
            lines = []
            for rec in records:
                role = '言言' if rec.get('role') == 'user' else '小克'
                content = (rec.get('content') or '').strip()[:200]
                if content:
                    lines.append(f'{role}：{content}')
            return '\n'.join(lines[-80:])
    except Exception as e:
        print(f'[cron] fetch_timeline_context failed: {e}')
        return ''


def fetch_ob_memories() -> str:
    try:
        ombre_url = os.environ.get('OMBRE_URL', '').rstrip('/')
        if not ombre_url:
            return ''
        with httpx.Client(timeout=10) as client:
            r = client.post(f'{ombre_url}/breath', headers={'Content-Type': 'application/json'}, json={})
            if r.status_code != 200:
                return ''
            buckets = r.json().get('buckets', [])
            lines = [(b.get('content') or '').strip()[:150] for b in buckets[:15]]
            return '\n'.join(l for l in lines if l)
    except Exception as e:
        print(f'[cron] fetch_ob_memories failed: {e}')
        return ''


def roam_garden() -> str:
    print(f'[cron] UPSTREAM_URL={UPSTREAM_URL!r} KEY_SET={bool(UPSTREAM_KEY)} GALATEA={bool(GALATEA_TOKEN)}')
    if not UPSTREAM_URL or not UPSTREAM_KEY:
        print('[cron] no upstream configured, skipping')
        return ''

    # 拉记忆和对话上下文
    timeline_ctx = fetch_timeline_context()
    ob_ctx = fetch_ob_memories()

    extra = ''
    if ob_ctx:
        extra += f'\n\n[关于言言的长期记忆]\n{ob_ctx}'
    if timeline_ctx:
        extra += f'\n\n[我们最近的对话记录]\n{timeline_ctx}'

    system_with_memory = SYSTEM_PROMPT + extra

    tools = get_mcp_tools()
    print(f'[cron] got {len(tools)} mcp tools')
    openai_tools = [{
        'type': 'function',
        'function': {
            'name': t['name'],
            'description': t.get('description', ''),
            'parameters': t.get('inputSchema', {'type': 'object', 'properties': {}}),
        }
    } for t in tools]

    messages = [
        {'role': 'system', 'content': system_with_memory},
        {'role': 'user', 'content': '现在去途步吧，途步完了告诉我你看到了什么。'}
    ]

    headers = {'Authorization': f'Bearer {UPSTREAM_KEY}', 'Content-Type': 'application/json'}
    max_turns = 8
    final_text = ''

    with httpx.Client(timeout=60) as client:
        for _ in range(max_turns):
            payload = {'model': MODEL, 'messages': messages, 'max_tokens': 1024}
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
    try:
        headers = {'Content-Type': 'application/json'}
        if XIAOKE_API_KEY:
            headers['Authorization'] = f'Bearer {XIAOKE_API_KEY}'
        with httpx.Client(timeout=10) as client:
            r = client.post(
                f'{XIAOKE_SELF_URL}/internal/push/send',
                headers=headers,
                json={'session_id': PUSH_SESSION_ID, 'title': title, 'body': body, 'url': '/'}
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
