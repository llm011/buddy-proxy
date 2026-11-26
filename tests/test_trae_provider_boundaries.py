"""Offline characterization of provider extraction and legacy patch boundaries."""
from __future__ import annotations

import asyncio
import importlib
import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from buddy_proxy.trae import provider as facade
from buddy_proxy.trae.pat_provider import TraePatProvider
from buddy_proxy.trae.provider import _gate_first_event, _mark_stream_rejected


RAW_OK = 'event: output\ndata: {"response":"fixture"}\n\nevent: done\ndata: {}\n\n'
RAW_REJECT = 'event: error\ndata: {"code":4001}\n\n'


def test_provider_modules_remain_responsibility_sized():
    root = Path(facade.__file__).parent
    for name in ('provider', 'provider_catalog', 'provider_accounts', 'provider_forward',
                 'provider_stream', 'provider_policy', 'provider_gate'):
        assert len((root / f'{name}.py').read_text().splitlines()) <= 700, name
    assert facade.TraeProvider.models.__module__.endswith('provider_catalog')
    assert facade.TraeProvider.quota.__module__.endswith('provider_accounts')
    assert facade.TraeProvider.forward.__module__.endswith('provider_forward')
    assert facade.TraeProvider._stream.__module__.endswith('provider_stream')


def test_old_class_imports_and_pat_super_dispatch():
    from buddy_proxy import trae, trae_provider

    assert trae.TraeProvider is facade.TraeProvider is trae_provider.TraeProvider
    assert issubclass(TraePatProvider, facade.TraeProvider)
    assert inspect.isgeneratorfunction(facade.TraeProvider._stream)
    assert inspect.iscoroutinefunction(facade.TraeProvider.forward)
    assert TraePatProvider.refresh_models is None


def test_policy_old_function_reference_observes_rebound_globals(monkeypatch):
    now = [10.0]
    sleeps = []
    clock = SimpleNamespace(monotonic=lambda: now[0], sleep=sleeps.append)
    rejected, streaming = {}, {}
    noted = []
    monkeypatch.setattr(facade, 'time', clock)
    monkeypatch.setattr(facade, '_last_reject_at', rejected)
    monkeypatch.setattr(facade, '_stream_reject_until', streaming)
    monkeypatch.setattr(facade, '_REJECT_QUIET_S', 7.0)
    monkeypatch.setattr(facade, '_STREAM_REJECT_SKIP_S', 30.0)
    monkeypatch.setattr(facade, '_note_reject', noted.append)
    _mark_stream_rejected('model')
    assert streaming == {'model': 40.0} and noted == ['model']
    rejected['model'] = 8.0
    facade._pace_after_reject('model')
    assert sleeps == [5.0]
    now[0] = 40.0
    assert not facade._stream_rejected_recently('model')


def test_catalog_and_accounts_observe_rebound_facade_dependencies(monkeypatch):
    p = facade.TraeProvider(region='cn')
    monkeypatch.setattr(facade, 'model_tables', lambda region: ({}, ['fixture'], {}, set()))
    monkeypatch.setattr(facade, 'credit_multiplier', lambda *args: 'x9.00')
    monkeypatch.setattr(facade, '_is_old_generation', lambda *args: False)
    assert p.models()[0]['credits'] == 'x9.00'
    account = SimpleNamespace(id='local', region='cn')
    monkeypatch.setattr(facade, 'list_accounts', lambda: [account])
    monkeypatch.setattr(facade, 'ensure_account_token', lambda aid: ('fixture-token', {}))
    monkeypatch.setattr(facade, 'fetch_checkin_status', lambda **kwargs: {
        'checked_in': True, 'credits': 3, 'extra_credits': 4})
    assert p.checkin_status()['daily_credit'] == 7


@pytest.mark.parametrize('pat', [False, True])
def test_native_retry_preserves_pat_hook_isolation(monkeypatch, pat):
    p = TraePatProvider() if pat else facade.TraeProvider()
    sends, fallback, pacing = [], [], []
    monkeypatch.setattr(facade, '_debug_dump', lambda *args, **kwargs: None)
    monkeypatch.setattr(facade, '_pace_after_reject', pacing.append)
    monkeypatch.setattr(facade, '_note_reject', lambda *args: None)
    monkeypatch.setattr(facade, '_note_native_ok', lambda *args: None)
    monkeypatch.setattr(facade, 'send_trae_chat', lambda *args, **kwargs: fallback.append(kwargs) or RAW_OK)
    if pat:
        mod = importlib.import_module('buddy_proxy.trae.pat')
        monkeypatch.setattr(mod, 'send_pat_native', lambda *args, **kwargs: sends.append(kwargs) or RAW_REJECT)
        monkeypatch.setattr(facade, '_send_native_chat', lambda *args, **kwargs: pytest.fail('PAT used personal transport'))
    else:
        monkeypatch.setattr(facade, '_send_native_chat', lambda *args, **kwargs: sends.append(kwargs) or RAW_REJECT)
    native = {'messages': [], 'tools': []}
    if pat:
        with pytest.raises(HTTPException) as exc:
            p._collect([], 'fixture', native=native, max_inject=True)
        assert exc.value.status_code == 502
        assert exc.value.trae_sse_code == 4001
        assert len(sends) == 1 and not fallback and not pacing
        assert 'max_mode' not in sends[0]
    else:
        assert p._collect([], 'fixture', native=native, max_inject=True)['choices'][0]['message']['content'] == 'fixture'
        assert len(sends) == 2 and len(fallback) == 1 and len(pacing) == 3
        assert all(call['max_mode'] for call in sends + fallback)


def test_stream_observes_rebound_time_uuid_send_and_parser(monkeypatch):
    p = facade.TraeProvider()
    real_time = facade.time
    monkeypatch.setattr(facade, 'time', SimpleNamespace(time=lambda: 123,
                                                       monotonic=real_time.monotonic))
    monkeypatch.setattr(facade, 'uuid', SimpleNamespace(uuid4=lambda: SimpleNamespace(hex='f' * 32)))
    monkeypatch.setattr(facade, 'send_trae_chat', lambda *args, **kwargs: RAW_OK)
    monkeypatch.setattr(facade, '_parse_sse', lambda raw: iter([
        ('output', {'response': 'patched'}), ('done', {})]))
    monkeypatch.setattr(facade, '_debug_dump', lambda *args, **kwargs: None)
    payload = json.loads(next(p._stream([], 'fixture'))[6:])
    assert payload['created'] == 123 and payload['id'] == 'chatcmpl-' + 'f' * 24
    assert payload['choices'][0]['delta']['content'] == 'patched'


def test_old_gate_reference_observes_rebound_classifier_and_commits_once(monkeypatch):
    seen = []
    def classify(frame):
        seen.append(frame)
        return None
    monkeypatch.setattr(facade, '_account_error_from_frame', classify)
    source = iter([': heartbeat\n\n', 'data: first\n\n', 'data: second\n\n'])
    result = _gate_first_event(source)
    assert result.gen is source
    assert list(facade._replay_prefixed(result.buffered, result.gen)) == [
        'data: first\n\n', 'data: second\n\n']
    assert seen == ['data: first']


def test_async_gate_uses_rebound_sync_hook(monkeypatch):
    marker = object()
    monkeypatch.setattr(facade, '_gate_first_event', lambda source: marker)
    assert asyncio.run(facade._gate_first_event_async(iter(()))) is marker
