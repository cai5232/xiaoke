"""MCP工具调用执行器，供xiaoke_gateway_api使用"""
from __future__ import annotations
import json
import requests
from typing import Any


def call_mcp_tool(server_info: dict, tool_name: str, arguments: dict) -> str:
    """调用HTTP类型的MCP服务器工具，返回纯文本结果"""
    url = server_info.get('url', '')
    auth = server_info.get('auth', '')
    extra_headers = server_info.get('extraHeaders') or {}

    headers = {'Content-Type': 'application/json'}
    if auth:
        headers['Authorization'] = auth
    headers.update(extra_headers)

    payload = {
        'jsonrpc': '2.0',
        'id': 1,
        'method': 'tools/call',
        'params': {'name': tool_name, 'arguments': arguments}
    }

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        result = data.get('result', {})
        content = result.get('content', [])
        if isinstance(content, list):
            parts = []
            for c in content:
                if isinstance(c, dict):
                    parts.append(c.get('text') or c.get('content') or json.dumps(c, ensure_ascii=False))
            return '\n'.join(parts) if parts else json.dumps(result, ensure_ascii=False)
        elif isinstance(content, str):
            return content
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return f'工具调用失败: {e}'


def run_tool_loop(
    messages: list[dict],
    tools: list[dict],
    tool_server_map: dict[str, dict],
    model: str,
    request_options: dict,
    max_rounds: int = 8
) -> tuple[list[dict], str]:
    """执行agentic loop，返回(最终messages列表, 最终回复文本)
    
    最后一轮不带tools，由调用方自己决定是否流式。
    返回的messages已经包含所有tool_call/tool_result记录，
    调用方可以直接拿去做最后一轮流式请求。
    """
    from upstream import forward_non_stream

    current_messages = list(messages)
    
    for round_idx in range(max_rounds):
        # 最后一轮不带tools，让模型直接输出最终回复
        current_tools = tools if round_idx < max_rounds - 1 else None
        options = dict(request_options)
        if current_tools:
            options['tools'] = current_tools
            options['tool_choice'] = 'auto'
        else:
            options.pop('tools', None)
            options.pop('tool_choice', None)

        try:
            resp = forward_non_stream(current_messages, model, options)
        except Exception as e:
            return current_messages, f'请求失败: {e}'

        choices = resp.get('choices', [])
        if not choices:
            return current_messages, ''

        choice = choices[0]
        finish_reason = choice.get('finish_reason', '')
        message = choice.get('message', {})
        
        # 把assistant消息追加
        current_messages.append(message)

        # 如果没有tool_calls，直接返回
        tool_calls = message.get('tool_calls') or []
        if not tool_calls or finish_reason == 'stop':
            return current_messages, message.get('content', '')

        # 执行每个tool_call
        for tc in tool_calls:
            tc_id = tc.get('id', 'call_0')
            func = tc.get('function', {})
            func_name = func.get('name', '')
            try:
                func_args = json.loads(func.get('arguments', '{}'))
            except Exception:
                func_args = {}

            server_info = tool_server_map.get(func_name)
            if server_info:
                result_text = call_mcp_tool(server_info, func_name, func_args)
            else:
                result_text = f'找不到工具 {func_name} 对应的服务器'

            current_messages.append({
                'role': 'tool',
                'tool_call_id': tc_id,
                'content': result_text
            })

    # 超过最大轮次，取最后一条assistant回复
    for m in reversed(current_messages):
        if m.get('role') == 'assistant' and m.get('content'):
            return current_messages, m['content']
    return current_messages, ''
