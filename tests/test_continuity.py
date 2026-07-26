"""Verify later turns retain facts that existed only before handoff."""
import tempfile
from pathlib import Path

from context_session import ContinuityRegistry
from mock_gateway import assemble
from storage import TimelineStore

with tempfile.TemporaryDirectory() as directory:
    store = TimelineStore(Path(directory) / "continuity.sqlite")

    # This fact exists only in the pre-handoff timeline.
    store.completed_turn(
        "雪儿喜欢蓝色雨伞",
        "记得，雪儿喜欢蓝色雨伞。",
        "kelivo",
    )

    first_request = [
        {"role": "system", "content": "CURRENT SP"},
        {"role": "user", "content": "哥哥，我下午有安排吗？"},
    ]
    first_assembled, baseline = assemble(first_request, store)
    assert any("蓝色雨伞" in str(message["content"]) for message in first_assembled)

    # First new-window turn completes successfully, then starts continuity state.
    first_reply = "你下午有安排。"
    store.completed_turn("哥哥，我下午有安排吗？", first_reply, "polaris")
    registry = ContinuityRegistry()
    registry.create(baseline, "哥哥，我下午有安排吗？", first_reply)

    # The frontend's second request contains only new-window bubbles; it does
    # not itself contain the old fact about 雪儿.
    second_request = [
        {"role": "system", "content": "CURRENT SP"},
        {"role": "user", "content": "哥哥，我下午有安排吗？"},
        {"role": "assistant", "content": first_reply},
        {"role": "user", "content": "雪儿喜欢什么颜色？"},
    ]

    ordinary_assembled, ordinary_injected = assemble(second_request, store)
    assert ordinary_injected == []
    assert not any("蓝色雨伞" in str(message["content"]) for message in ordinary_assembled)

    # The session's saved baseline restores the pre-handoff fact on turn two.
    sustained = registry.baseline_for(second_request)
    assert sustained is not None
    final_second = [second_request[0]] + [
        {"role": record.role if record.role in ("user", "assistant") else "assistant", "content": record.content}
        for record in sustained
    ] + second_request[1:]
    assert any("蓝色雨伞" in str(message["content"]) for message in final_second)
    assert final_second[-1]["content"] == "雪儿喜欢什么颜色？"

print("continuity-context-retained-ok")
