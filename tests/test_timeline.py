from timeline_context import TimelineRecord, handoff_records, is_timeline_eligible, needs_handoff, text_from_content

records = [TimelineRecord(id=f"r{i}", sequence=i, role="user" if i % 2 else "assistant", content=f"m{i}") for i in range(1, 501)]

# New window: only system wrapper/current user input. System is never timeline eligible.
new_window = [{"role": "system", "content": "SP <memories>private</memories>"}, {"role": "user", "content": "new question"}]
assert needs_handoff(new_window, records) == (True, None)
bundle = handoff_records(new_window, records, max_records=999)
assert len(bundle) == 500 and bundle[0].sequence == 1 and bundle[-1].sequence == 500

# Same window: client carries the newest shared anchor, so nothing is added.
same_window = [{"role": "user", "content": "m499"}, {"role": "assistant", "content": "m500"}, {"role": "user", "content": "continue"}]
assert needs_handoff(same_window, records) == (False, 500)
assert handoff_records(same_window, records) == []

# Old window: it knows through r450, so only the later missing segment is selected.
old_window = [{"role": "assistant", "content": "m450"}, {"role": "user", "content": "continue elsewhere"}]
assert needs_handoff(old_window, records) == (True, 450)
gap = handoff_records(old_window, records)
assert [r.sequence for r in gap] == list(range(451, 501))

# If client history overlaps selected content, it is not copied a second time.
overlap = [{"role": "assistant", "content": "m450"}, {"role": "user", "content": "m453"}, {"role": "user", "content": "continue"}]
gap = handoff_records(overlap, records)
assert 453 not in [r.sequence for r in gap]

# Rule boundary: prompt wrappers are excluded; attachments become text placeholders.
assert not is_timeline_eligible({"role": "system", "content": "normal SP"})
assert not is_timeline_eligible({"role": "user", "content": "before ## Memories hidden"})
assert text_from_content([{"type": "image_url", "image_url": {"url": "data:image/png;base64,secret"}}]) == "[图片]"

# Character budget keeps the newest records, not the oldest.
small = handoff_records(new_window, records, max_records=999, max_chars=8)
assert [r.sequence for r in small] == [499, 500]
print("timeline-rules-ok")
