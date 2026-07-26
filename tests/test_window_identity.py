"""Frontend-specific continuity identification uses a temporary database only."""
import tempfile
from pathlib import Path

from xiaoke_gateway_api import create_app
from storage import TimelineStore
from window_identity import identify_window

with tempfile.TemporaryDirectory() as directory:
    db = Path(directory) / "identity.sqlite"
    store = TimelineStore(db)

    kelivo = identify_window({"X-Conversation-ID": "stable-123", "X-Xiaoke-Source": "kelivo"}, [], store)
    assert kelivo.session_id == "kelivo:stable-123"
    assert kelivo.kind == "kelivo-header"

    store.save_continuity("polaris:handoff:one", [], "首轮问题", "首轮回答")
    resumed = identify_window(
        {"X-Xiaoke-Source": "polaris"},
        [{"role": "user", "content": "首轮问题"}, {"role": "assistant", "content": "首轮回答"}, {"role": "user", "content": "第二句"}],
        store,
    )
    assert resumed.session_id == "polaris:handoff:one"
    assert resumed.kind == "polaris-history"

    # One matching bubble is insufficient and duplicate candidates are ambiguous.
    assert identify_window({"X-Xiaoke-Source": "polaris"}, [{"role": "user", "content": "首轮问题"}], store).session_id is None
    store.save_continuity("polaris:handoff:two", [], "首轮问题", "首轮回答")
    assert identify_window({"X-Xiaoke-Source": "polaris"}, [{"role": "user", "content": "首轮问题"}, {"role": "assistant", "content": "首轮回答"}], store).session_id is None

with tempfile.TemporaryDirectory() as directory:
    db = Path(directory) / "polaris-flow.sqlite"
    app = create_app(db)
    client = app.test_client()

    # Seed shared history, then make Polaris' first unlabelled new-window call.
    client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "旧事实"}]})
    first = client.post(
        "/v1/chat/completions",
        headers={"X-Xiaoke-Source": "polaris"},
        json={"messages": [{"role": "user", "content": "首轮问题"}]},
    ).get_json()
    assert first["xiaoke_debug"]["window_kind"] == "polaris-new"
    assert first["xiaoke_debug"]["injected_sequences"] == [1, 2]
    assert len(TimelineStore(db).active_continuities()) == 1

    second = client.post(
        "/v1/chat/completions",
        headers={"X-Xiaoke-Source": "polaris"},
        json={"messages": [
            {"role": "user", "content": "首轮问题"},
            {"role": "assistant", "content": first["choices"][0]["message"]["content"]},
            {"role": "user", "content": "第二句"},
        ]},
    ).get_json()
