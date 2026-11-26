"""Raw SSE cache evidence survives both Trae response transports."""
import json

import pytest

from buddy_proxy.trae import provider as facade
from buddy_proxy.trae.usage import normalize_token_usage


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('extra,cached', [
    ({}, None),
    ({'prompt_tokens_details': {'cached_tokens': 0, 'other': 3}}, 0),
    ({'prompt_tokens_details': {'cached_tokens': 6}}, 6),
    ({'prompt_cache_hit_tokens': 5, 'prompt_cache_miss_tokens': 5}, 5),
    ({'cache_read_input_tokens': 0}, 0),
    ({'cache_read_input_tokens': 4, 'cache_creation_input_tokens': 2}, 4),
    ({'cached_tokens': 3}, 3),
    ({'prompt_tokens_details': {'cached_tokens': 0}, 'prompt_cache_hit_tokens': 5}, 0),
])
def test_raw_usage_presence_preserved(monkeypatch, stream, extra, cached):
    provider = facade.TraeProvider()
    base = {'prompt_tokens': 10, 'completion_tokens': 20, 'total_tokens': 30}
    upstream = {**base, **extra}
    raw = ('event: output\ndata: {"response":"OK","reasoning_content":"fixture"}\n\n'
           f'event: token_usage\ndata: {json.dumps(upstream)}\n\n'
           'event: done\ndata: {}\n\n')
    monkeypatch.setattr(facade, 'send_trae_chat', lambda *args, **kwargs: raw)
    monkeypatch.setattr(facade, '_debug_dump', lambda *args, **kwargs: None)
    if stream:
        chunks = [json.loads(item[6:]) for item in provider._stream(
            [], 'deepseek-v4.1-flash', include_usage=True) if item.startswith('data: {')]
        usage = next(chunk['usage'] for chunk in chunks if 'usage' in chunk)
        assert any(chunk['choices'] and chunk['choices'][0]['delta'].get(
            'reasoning_content') == 'fixture' for chunk in chunks)
    else:
        usage = provider._collect([], 'deepseek-v4.1-flash')['usage']
    for key, value in upstream.items():
        assert usage[key] == value
    assert {key: usage[key] for key in base} == base
    if cached is None:
        assert 'prompt_tokens_details' not in usage
        assert 'cached_tokens' not in usage
    else:
        assert usage['prompt_tokens_details']['cached_tokens'] == cached
    # Do not mutate the upstream details dictionary while adding normalized evidence.
    assert normalize_token_usage(upstream) is not upstream
    assert upstream == {**base, **extra}


@pytest.mark.parametrize('cached', [True, False, -1, float('inf'), float('nan'), '5'])
def test_invalid_cache_alias_is_not_promoted_to_standard_evidence(cached):
    usage = normalize_token_usage({'prompt_tokens': 10, 'cache_read_input_tokens': cached})
    assert usage['prompt_tokens'] == 10
    assert 'prompt_tokens_details' not in usage


def test_invalid_base_usage_keeps_existing_none_behavior():
    assert normalize_token_usage({'prompt_tokens': 'invalid'}) is None
