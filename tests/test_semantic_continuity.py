import tempfile
from pathlib import Path
from context_session import ContinuityRegistry
from mock_gateway import assemble
from storage import TimelineStore

fact = "雪儿喜欢蓝色雨伞"
question = "雪儿喜欢什么颜色？"

def answer(messages):
    text = "\n".join(str(m.get("content", "")) for m in messages)
    if question in text and fact in text:
        return "蓝色"
    if question in text:
        return "我不知道"
    return "无关问题"

with tempfile.TemporaryDirectory() as directory:
    store = TimelineStore(Path(directory) / "semantic.sqlite")
    store.completed_turn(fact, "记得。", "kelivo")
    first = [
        {"role": "system", "content": "CURRENT SP"},
        {"role": "user", "content": "哥哥，我下午有安排吗？"},
    ]
    _, baseline = assemble(first, store)
    first_reply = "你下午有安排。"
    store.completed_turn("哥哥，我下午有安排吗？", first_reply, "polaris")
    second = [
        {"role": "system", "content": "CURRENT SP"},
        {"role": "user", "content": "哥哥，我下午有安排吗？"},
        {"role": "assistant", "content": first_reply},
        {"role": "user", "content": question},
    ]
    one_shot, injected = assemble(second, store)
    assert injected == []
    assert answer(one_shot) == "我不知道"
    registry = ContinuityRegistry()
    registry.create(baseline, "哥哥，我下午有安排吗？", first_reply)
    sustained = registry.baseline_for(second)
    assert sustained is not None
    continuous = [second[0]] + [
        {"role": r.role if r.role in ("user", "assistant") else "assistant", "content": r.content}
        for r in sustained
    ] + second[1:]
    assert answer(continuous) == "蓝色"
print("one_shot_second_reply=我不知道")
print("continuous_second_reply=蓝色")
print("semantic-continuity-proof-ok")
