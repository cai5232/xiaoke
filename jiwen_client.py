"""jiwen 情绪状态客户端

供 xiaoke_gateway_api.py 调用，功能：
- get_guidance()       拉取当前语调指引，注入 system prompt
- notify_user_message() 通知 jiwen 用户发了消息（异步，不阻塞）
- notify_bot_replied()  通知 jiwen bot 回复完成（异步，不阻塞）

环境变量：
  JIWEN_URL     jiwen-server 地址，如 https://caiwi.zeabur.app
  JIWEN_API_KEY jiwen-server 的鉴权 key
"""
from __future__ import annotations
import os
import json
import threading
import urllib.request
import urllib.error
from typing import Optional

JIWEN_URL = (os.environ.get('JIWEN_URL') or '').rstrip('/')
JIWEN_API_KEY = os.environ.get('JIWEN_API_KEY') or ''


def _headers() -> dict:
    h = {'Content-Type': 'application/json'}
    if JIWEN_API_KEY:
        h['Authorization'] = f'Bearer {JIWEN_API_KEY}'
    return h


def get_guidance(mode: str = 'reactive') -> Optional[str]:
    """拉取语调指引文本，失败返回 None。"""
    if not JIWEN_URL:
        return None
    try:
        req = urllib.request.Request(
            f'{JIWEN_URL}/guidance?mode={mode}',
            headers=_headers(),
            method='GET',
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())
            return data.get('guidance') or None
    except Exception:
        return None


def _post_async(path: str, body: dict) -> None:
    """后台线程异步 POST，不阻塞主流程。"""
    if not JIWEN_URL:
        return

    def _do():
        try:
            payload = json.dumps(body).encode()
            req = urllib.request.Request(
                f'{JIWEN_URL}{path}',
                data=payload,
                headers=_headers(),
                method='POST',
            )
            with urllib.request.urlopen(req, timeout=5):
                pass
        except Exception:
            pass

    threading.Thread(target=_do, daemon=True).start()


def notify_user_message(content: str, message_id: Optional[str] = None) -> None:
    """通知 jiwen 用户发了消息，触发 resetConnection + 情绪分析。"""
    body: dict = {'content': content}
    if message_id:
        body['message_id'] = message_id
    _post_async('/event/user-message', body)


def notify_bot_replied(message_id: Optional[str] = None) -> None:
    """通知 jiwen bot 回复完成。"""
    body: dict = {}
    if message_id:
        body['message_id'] = message_id
    _post_async('/event/bot-replied', body)
