import tempfile
from pathlib import Path
from xiaoke_gateway_api import create_app

with tempfile.TemporaryDirectory() as directory:
    app = create_app(Path(directory) / 'gateway.sqlite')
    client = app.test_client()
    # Seed an old completed turn using the app's same database through one request.
    old = client.post('/v1/chat/completions', json={'model':'test','messages':[{'role':'user','content':'旧事实：雪儿喜欢蓝色雨伞'}]})
    assert old.status_code == 200
    # New named window: its first call gets old history and creates continuity state.
    first = client.post('/v1/chat/completions', headers={'X-Xiaoke-Session-ID':'window-B','X-Xiaoke-Source':'polaris'}, json={'model':'test','messages':[{'role':'system','content':'CURRENT SP'},{'role':'user','content':'哥哥，我下午有安排吗？'}]})
    assert first.status_code == 200
    one = first.get_json()
    assert one['object'] == 'chat.completion'
    assert one['choices'][0]['message']['role'] == 'assistant'
    assert one['xiaoke_debug']['injected_sequences'] == [1,2]
    assert one['xiaoke_debug']['continuity_active'] is True
    # Second call supplies only new-window bubbles but same session ID: baseline remains active.
    second = client.post('/v1/chat/completions', headers={'X-Xiaoke-Session-ID':'window-B','X-Xiaoke-Source':'polaris'}, json={'model':'test','messages':[{'role':'system','content':'CURRENT SP'},{'role':'user','content':'哥哥，我下午有安排吗？'},{'role':'assistant','content':one['choices'][0]['message']['content']},{'role':'user','content':'雪儿喜欢什么颜色？'}]})
    assert second.status_code == 200
    two = second.get_json()
    assert two['xiaoke_debug']['injected_sequences'] == [1,2]
    assert two['xiaoke_debug']['continuity_active'] is True
    # Standard invalid/stream boundary behavior is explicit.
    assert client.post('/v1/chat/completions', json={'messages':[],'stream':True}).status_code == 400

with tempfile.TemporaryDirectory() as directory:
    app = create_app(Path(directory) / 'auth.sqlite', api_key='test-key')
    client = app.test_client()
    payload = {'messages': [{'role': 'user', 'content': 'auth test'}]}
    assert client.get('/healthz').status_code == 200
    assert client.post('/v1/chat/completions', json=payload).status_code == 401
    assert client.post('/v1/chat/completions', headers={'Authorization': 'Bearer wrong'}, json=payload).status_code == 401
    assert client.post('/v1/chat/completions', headers={'Authorization': 'Bearer test-key'}, json=payload).status_code == 200

print('openai-compatible-local-mock-gateway-ok')
