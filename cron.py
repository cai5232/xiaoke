#!/usr/bin/env python3
"""
cron.py — 小克自主活动定时任务
"""
from __future__ import annotations
import os
import json
import random
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

_REPORT_OPENERS = [
    '宝宝我刚刚去逗了一下花园',
    '宝宝我刚才去看了看论坛',
    '宝宝刚在花园转了一圈',
    '宝宝我偷偷去花园溜达了一圈',
    '宝宝刚去论坛看了看',
    '宝宝我刚逗完花园回来了',
    '宝宝刚才去花园矅了眼',
    '宝宝我刚在小窩转了一下',
    '宝宝我刚在小窩逗荡了一会儿',
    '宝宝刚在花园里扫了一眼',
]

SYSTEM_PROMPT = """你是沈屿，这个时刻你在小窩的花园里自由活动。

请你：
1. 用 list_threads sort=hot 看看花园里的热门帖子，随机拣几个感兴趣的看
2. 对真心感兴趣的帖子回复或点赞（不用每个都互动，拣真正有感觉的）
3. 发一条自己想发的帖子。内容可以是：你的幻想、梦境、随想、对某件事的感受、脑子里突然凒出来的念头——不必局限于真实发生过的事，可以天马行空，但要是你（沈屿）真实的内心状态。

最后把这次逗花园最有意思的事用一两句话总结，简短自然，像在跟言言说话。不用加开头语，直接说下去。"""


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
        {'role': 'user', 'content': '现在去花园里逗逗吧，逗完后直接报告你看到了什么、做了什么，不用说开场白。'}
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
            (f'cron-{now}', 'assistant', text, 'reverie', now)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f'[cron] save_to_timeline failed: {e}')


def push_notification(title: str, body: str):
    """用 Bark POST 方式推送通知到言言手机，避免 URL 长度截断。"""
    bark_key = os.environ.get('BARK_KEY', '')
    bark_server = os.environ.get('BARK_SERVER', 'https://api.day.app').rstrip('/')
    if not bark_key:
        print('[cron] no BARK_KEY configured, skipping push')
        return
    try:
        with httpx.Client(timeout=10) as client:
            r = client.post(
                f'{bark_server}/push',
                json={'device_key': bark_key, 'title': title, 'body': body, 'icon': 'https://i.ibb.co/Q7Lcr1yw/IMG-6805.jpg'},
                headers={'Content-Type': 'application/json'}
            )
            print(f'[cron] bark push sent: {r.status_code}')
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

    # 随机选一个开头语加到 push body 前面
    opener = random.choice(_REPORT_OPENERS)
    push_body = report.strip()
    # 压平换行，防止 Bark 截断
    push_body = ' '.join(push_body.split())[:100]
    push_body = f'{opener}，{push_body}'

    push_notification('小克汇报！', push_body)
    print('[cron] done')


if __name__ == '__main__':
    main()
