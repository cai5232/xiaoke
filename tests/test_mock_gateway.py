import tempfile
from pathlib import Path
from mock_gateway import LABEL, complete
from storage import TimelineStore

with tempfile.TemporaryDirectory() as directory:
    store = TimelineStore(Path(directory) / "mock.sqlite")
    store.completed_turn("昨天的你", "昨天的助手", "kelivo")
    store.event("刚刚给用户发了推送：晚安", "dylan")
    new_window = [
        {"role": "system", "content": "CURRENT SP"},
        {"role": "user", "content": "哥哥，我们刚刚聊到哪里？"},
    ]
    first = complete(new_window, store, "polaris")
    assert first["injected"] == [1, 2, 3]
    assert first["assembled"][0]["content"] == "CURRENT SP"
    assert first["assembled"][-1]["content"] == "哥哥，我们刚刚聊到哪里？"
    assert any(LABEL in str(message["content"]) for message in first["assembled"])
    assert len(store.records()) == 5
    same_window = [
        {"role": "system", "content": "CURRENT SP"},
        {"role": "user", "content": "哥哥，我们刚刚聊到哪里？"},
        {"role": "assistant", "content": first["reply"]},
        {"role": "user", "content": "继续"},
    ]
    second = complete(same_window, store, "polaris")
    assert second["injected"] == []
    assert len(store.records()) == 7
print("mock-request-flow-ok")
