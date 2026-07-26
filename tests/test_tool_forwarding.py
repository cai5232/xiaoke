from upstream import request_payload
messages = [{'role': 'user', 'content': 'check'}]
tools = [{'type': 'function', 'function': {'name': 'lookup', 'parameters': {'type': 'object', 'properties': {}}}}]
options = {'tools': tools, 'tool_choice': 'auto', 'parallel_tool_calls': True, 'temperature': 0.2, 'messages': [{'role': 'user', 'content': 'stale'}], 'model': 'stale', 'stream': True}
payload = request_payload(messages, 'actual-model', False, options)
assert payload['messages'] == messages
assert payload['model'] == 'actual-model'
assert payload['stream'] is False
assert payload['tools'] == tools
assert payload['tool_choice'] == 'auto'
assert payload['parallel_tool_calls'] is True
assert payload['temperature'] == 0.2
assert request_payload(messages, 'actual-model', True, options)['stream'] is True
print('tool-request-options-forwarded-ok')
