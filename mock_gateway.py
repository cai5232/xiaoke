"""Local mock flow only. No network calls."""
from storage import TimelineStore
from timeline_context import handoff_records, is_timeline_eligible, rolling_records, text_from_content

LABEL = "xiaoke shared timeline"

def current_user(messages):
    for message in reversed(messages):
        if message.get("role") == "user" and is_timeline_eligible(message):
            return message
    return None

def assemble(messages, store, max_records=999, max_chars=160000, rolling=False):
    selector = rolling_records if rolling else handoff_records
    injected = selector(messages, store.records(), max_records, max_chars)
    systems = [m for m in messages if m.get("role") == "system"]
    other = [m for m in messages if m.get("role") != "system"]
    history = []
    for record in injected:
        role = record.role if record.role in ("user", "assistant") else "assistant"
        content = record.content
        if record.role == "event":
            content = "[{}; source={}] {}".format(LABEL, record.source, content)
        history.append({"role": role, "content": content, "xiaoke_record_id": record.id})
    return systems + history + other, injected

def complete(messages, store, source="unknown"):
    assembled, injected = assemble(messages, store)
    user = current_user(messages)
    if user is None:
        raise ValueError("eligible user message required")
    user_text = text_from_content(user.get("content"))
    reply = "[mock reply] received: " + user_text
    request_id, user_id, assistant_id = store.completed_turn(user_text, reply, source)
    return {"request_id": request_id, "stored": [user_id, assistant_id], "injected": [r.sequence for r in injected], "assembled": assembled, "reply": reply}
