"""只走回环验证门控，不向供应商发包。"""
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from qa_model_gate import BUDGET_ATTEMPTS, bounded_relay, dedicated_config, reserve_attempt


def test_dedicated_config_never_falls_back(monkeypatch, tmp_path):
    monkeypatch.setenv('CLARA_QA_MODEL', 'openai/qa')
    monkeypatch.setenv('CLARA_QA_MODEL_URL', 'https://example.invalid/v1')
    monkeypatch.setenv('CLARA_QA_MODEL_BUDGET_DIR', str(tmp_path))
    monkeypatch.setenv('OPENAI_API_KEY', '非专用')
    monkeypatch.delenv('CLARA_QA_MODEL_KEY', raising=False)
    assert dedicated_config() is None
    monkeypatch.setenv('CLARA_QA_MODEL_KEY', '专用')
    assert dedicated_config() is not None
    monkeypatch.setenv('CLARA_QA_MODEL_URL', 'http://example.invalid/v1')
    assert dedicated_config() is None


def test_relay_retry_cannot_cross_shared_budget(tmp_path, monkeypatch):
    # 前轮预占 BUDGET_ATTEMPTS-1 次；第二轮一次失败供应商应答也消耗额度，
    # SDK 重试不能透传。
    for _ in range(BUDGET_ATTEMPTS - 1):
        assert reserve_attempt(tmp_path)
    upstream = []
    real_client = httpx.Client
    def upstream_client(**kwargs):
        def reply(request):
            upstream.append((str(request.url), request.headers['Authorization']))
            return httpx.Response(503)
        return real_client(transport=httpx.MockTransport(reply))
    monkeypatch.setattr(httpx, 'Client', upstream_client)
    with real_client(trust_env=False) as local:
        for attempt in range(2):
            with bounded_relay('qa-only', 'https://example.invalid/v1', tmp_path) as (url, key, gate):
                response = local.post(url + '/chat/completions', json={}, headers={'Authorization': 'Bearer ' + key})
                assert response.status_code == (502 if attempt == 0 else 403)
                assert gate.blocked == (attempt == 1)
    assert upstream == [('https://example.invalid/v1/chat/completions', 'Bearer qa-only')]


def test_concurrent_budget_stops_at_cap(tmp_path):
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(lambda _: reserve_attempt(tmp_path),
                            range(BUDGET_ATTEMPTS + 10))) == BUDGET_ATTEMPTS
    assert not reserve_attempt(tmp_path)


@pytest.mark.parametrize('events', [[], [{'type': 'done'}],
    [{'type': 'reply', 'content': '  '}, {'type': 'done'}],
    [{'type': 'error'}, {'type': 'reply', 'content': '哪个会？'}, {'type': 'done'}]])
def test_empty_or_failed_real_turn_cannot_pass(events):
    from test_qa_real_llm import check_chat_reply

    with pytest.raises(AssertionError):
        check_chat_reply(events)


@pytest.mark.parametrize('start,duration,ok', [('2026-11-27T15:30', 30, True),
    ('2026-11-27T15:30', 60, False), ('2026-11-27T16:00', 30, False),
    ('2026-11-28T16:00', 60, True)])
def test_real_cutoff_scores_full_interval(start, duration, ok):
    from datetime import datetime
    from test_qa_real_llm import friday_cutoff_ok

    assert friday_cutoff_ok(datetime.fromisoformat(start), duration) is ok
