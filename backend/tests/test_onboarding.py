"""首启入职访谈（2026-11-17 批 1）：完成/跳过落库语义与种子规则替换。"""

from fastapi.testclient import TestClient

from conftest import TestingSessionLocal
from services.ai_data import seed_memory_rules_if_empty


def _seed_rules() -> None:
    with TestingSessionLocal() as db:
        seed_memory_rules_if_empty(db)


def _rule_contents(client: TestClient) -> list[str]:
    return [r["content"] for r in client.get("/api/ai/memory-rules").json()]


def test_complete_writes_flag_settings_and_rules(client: TestClient):
    _seed_rules()
    res = client.post("/api/onboarding/complete", json={
        "settings": {"work_minutes_per_day": 420},
        "rules": [{"category": "精力节律", "content": "上午效率最高",
                   "evidence": "入职访谈"}],
        "skipped": False,
    })
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["settings"]["onboarded_at"] is not None
    assert body["settings"]["work_minutes_per_day"] == 420
    assert body["rules_created"] == 1
    contents = _rule_contents(client)
    assert "上午效率最高" in contents
    # 种子规则被替换（evidence 前缀「从应用设置」不再存在）
    rules = client.get("/api/ai/memory-rules").json()
    assert not any((r.get("evidence") or "").startswith("从应用设置的") for r in rules)


def test_skip_marks_flag_and_keeps_seed_rules(client: TestClient):
    _seed_rules()
    before = _rule_contents(client)
    assert before  # 种子规则存在
    res = client.post("/api/onboarding/complete", json={"skipped": True})
    assert res.status_code == 200
    assert res.json()["settings"]["onboarded_at"] is not None
    assert res.json()["rules_created"] == 0
    assert _rule_contents(client) == before


def test_complete_validates_settings(client: TestClient):
    res = client.post("/api/onboarding/complete", json={
        "settings": {"work_minutes_per_day": 5},  # 越界
        "skipped": False,
    })
    assert res.status_code == 422
