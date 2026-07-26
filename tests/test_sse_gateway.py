import tempfile
from pathlib import Path
from xiaoke_gateway_api import create_app
from storage import TimelineStore

payload = {'model':'test', 'stream':True, 'messages':[{'role':'user','content':'流式测试'}]}
with tempfile.TemporaryDirectory() as directory:
    db = Path(directory) / 'sse.sqlite'
    app = create_app(db)
    client = app.test_client()

    normal = client.post('/v1/chat/completions', json=payload)
    normal_body = normal.get_data(as_text=True)
    assert normal.status_code == 200
    assert normal.content_type.startswith('text/event-stream')
    assert 'data: [DONE]' in normal_body
    assert TimelineStore(db).records()[-2].content == '流式测试'
    assert TimelineStore(db).records()[-1].content == '[xiaoke local mock] received: 流式测试'
    before = len(TimelineStore(db).records())

    broken = client.post('/v1/chat/completions', headers={'X-Xiaoke-Test-Disconnect':'1'}, json=payload)
    broken_body = broken.get_data(as_text=True)
    assert broken.status_code == 200
    assert 'data: [DONE]' not in broken_body
    assert len(TimelineStore(db).records()) == before
print('sse-complete-commits-broken-stream-does-not-ok')
