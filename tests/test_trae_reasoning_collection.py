"""Offline raw SSE regressions for non-stream reasoning separation."""
import json

import pytest

from buddy_proxy.protocols.anthropic_adapter import chat_completion_to_anthropic_message
from buddy_proxy.trae import provider as facade


def sse(outputs):
    events = [('output', output) for output in outputs]
    events += [('token_usage', {'prompt_tokens': 10, 'completion_tokens': 20,
                                'total_tokens': 30}), ('done', {})]
    return ''.join(f'event: {event}\ndata: {json.dumps(data)}\n\n'
                   for event, data in events)


@pytest.fixture
def collector(monkeypatch):
    provider = facade.TraeProvider()
    monkeypatch.setattr(facade, '_debug_dump', lambda *args, **kwargs: None)
    monkeypatch.setattr(facade, '_pace_after_reject', lambda model: None)
    monkeypatch.setattr(facade, '_note_native_ok', lambda model: None)

    def collect(outputs, native=False):
        raw = sse(outputs)
        monkeypatch.setattr(facade, 'send_trae_chat', lambda *args, **kwargs: raw)
        monkeypatch.setattr(provider, '_send_native_request', lambda *args, **kwargs: raw)
        return provider._collect([], 'deepseek-v4.1-flash',
                                 native={'messages': [], 'tools': []} if native else None)
    return collect


@pytest.mark.parametrize('native', [False, True])
@pytest.mark.parametrize('content', ['', 'OK'])
def test_nonempty_reasoning_is_separate_and_verbatim(collector, native, content):
    result = collector([{'reasoning_content': 'first\n'}, {'reasoning_content': ''},
                        {'reasoning_content': 'second', 'response': content}], native)
    message = result['choices'][0]['message']
    assert message['reasoning_content'] == 'first\nsecond'
    assert message['content'] == content
    assert '<think>' not in message['content']
    assert result['usage'] == {'prompt_tokens': 10, 'completion_tokens': 20,
                               'total_tokens': 30}
    # The JSON wire field is the extension consumed by OpenAI-compatible clients.
    parsed = json.loads(json.dumps(result))['choices'][0]['message']
    assert parsed['reasoning_content'] == 'first\nsecond'
    assert parsed['content'] == content
    anthropic = chat_completion_to_anthropic_message(result)
    thinking = [block for block in anthropic['content'] if block['type'] == 'thinking']
    assert len(thinking) == 1
    assert thinking[0]['thinking'] == 'first\nsecond'


def test_reasoning_and_fragmented_native_tool_call_survive(collector):
    result = collector([
        {'reasoning_content': 'first'},
        {'reasoning_content': '\nsecond', 'tool_calls': [
            {'index': 0, 'id': 'call_probe', 'type': 'function',
             'function_call': {'name': 'probe', 'arguments': '{"step":'}}]},
        {'tool_calls': [{'index': 0, 'function_call': {'arguments': '1}'}}]},
    ], native=True)
    choice = result['choices'][0]
    assert choice['finish_reason'] == 'tool_calls'
    assert choice['message']['content'] == ''
    assert choice['message']['reasoning_content'] == 'first\nsecond'
    calls = choice['message']['tool_calls']
    assert len(calls) == 1
    assert calls[0]['id'] == 'call_probe'
    assert calls[0]['function']['name'] == 'probe'
    assert json.loads(calls[0]['function']['arguments']) == {'step': 1}
    assert result['usage']['total_tokens'] == 30


def test_empty_reasoning_does_not_invent_protocol_evidence(collector):
    result = collector([{'reasoning_content': '', 'response': 'OK'}])
    assert result['choices'][0]['message'] == {'role': 'assistant', 'content': 'OK'}


def test_no_content_reasoning_or_tools_keeps_existing_empty_response_fallback(collector):
    result = collector([{}])
    assert '空响应' in result['choices'][0]['message']['content']
    assert 'reasoning_content' not in result['choices'][0]['message']
