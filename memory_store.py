"""
本地记忆存储，存在持久盘 /app/data/memories.json
每条记忆是一个字符串，最多保留 200 条。

环境变量：
  XIAOKE_MEMORY_PATH  覆盖默认存储路径（可选）
"""
from __future__ import annotations
import json
from pathlib import Path
from typing import Optional

DEFAULT_MEMORY_PATH = Path('/app/data/memories.json')


def _path() -> Path:
    import os
    p = os.environ.get('XIAOKE_MEMORY_PATH', '')
    return Path(p) if p else DEFAULT_MEMORY_PATH


def load_memories() -> list[str]:
    p = _path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text('utf-8'))
        if isinstance(data, list):
            return [str(m) for m in data if m]
        return []
    except Exception:
        return []


def load_memories_text(max_count: int = 30) -> Optional[str]:
    """返回格式化的记忆文本，没有记忆时返回 None。"""
    memories = load_memories()
    if not memories:
        return None
    recent = memories[-max_count:]
    return '\n'.join(f'- {m}' for m in recent)


def save_memory(content: str) -> bool:
    """追加一条记忆，超过 200 条时自动丢弃最旧的，返回是否成功。"""
    content = content.strip()
    if not content:
        return False
    p = _path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        memories = load_memories()
        memories.append(content)
        if len(memories) > 200:
            memories = memories[-200:]
        p.write_text(json.dumps(memories, ensure_ascii=False, indent=2), 'utf-8')
        return True
    except Exception:
        return False


def delete_memory_by_index(index: int) -> bool:
    """按 0-based 索引删除一条记忆。"""
    memories = load_memories()
    if 0 <= index < len(memories):
        memories.pop(index)
        p = _path()
        try:
            p.write_text(json.dumps(memories, ensure_ascii=False, indent=2), 'utf-8')
            return True
        except Exception:
            return False
    return False
