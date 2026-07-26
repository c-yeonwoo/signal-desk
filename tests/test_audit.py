"""감사 가설 큐 — LLM에게 판정권이 없다는 것을 테스트로 못박는다.

이 레이어의 가치는 "무엇을 할 수 있나"가 아니라 **"무엇을 할 수 없나"**에 있다.
설정을 바꾸지 못하고, 반증 불가능한 지적은 저장되지 못하며, 키가 없으면 조용히 꺼진다.
"""

from __future__ import annotations

import pytest

from signal_desk import audit, db


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")


def _item(**over) -> dict:
    base = {"target": "harness", "title": "위상 평균이 대조군에 적용 안 됐을 수 있다",
            "claim": "전략만 평균내면 분산이 줄어 이겨 보인다",
            "falsifier": "대조군 IQR이 단일 위상보다 좁으면 거짓",
            "check_hint": "tests/test_harness.py::test_random_baseline_is_phase_averaged",
            "severity": "high"}
    return {**base, **over}


def test_unfalsifiable_hypotheses_are_dropped():
    """'표본이 더 필요합니다'는 영원히 참이라 아무것도 막지 못한다."""
    saved = audit.save([
        _item(),
        _item(title="지속적인 모니터링이 필요합니다", falsifier=""),
        _item(title="데이터 품질에 유의", falsifier="   "),
    ])
    assert saved == 1
    assert len(db.audit_hypothesis_list()) == 1


def test_same_finding_is_not_stacked_every_run():
    """같은 지적이 매주 새 항목으로 쌓이면 큐가 소음이 되고 아무도 안 본다."""
    audit.save([_item()])
    audit.save([_item(claim="문구만 살짝 다른 같은 지적")])
    assert len(db.audit_hypothesis_list()) == 1


def test_items_without_a_title_are_dropped():
    assert audit.save([_item(title="")]) == 0


def test_status_flow_is_human_only_and_validated():
    audit.save([_item()])
    hid = db.audit_hypothesis_list()[0]["id"]
    assert db.audit_pending_count() == 1
    assert db.audit_hypothesis_set_status(hid, "promoted", "테스트로 옮김")
    assert db.audit_pending_count() == 0
    assert db.audit_hypothesis_list()[0]["status"] == "promoted"
    with pytest.raises(ValueError):
        db.audit_hypothesis_set_status(hid, "approved")     # 승인 같은 상태는 없다
    assert not db.audit_hypothesis_set_status("nope", "dismissed")


def test_generate_is_disabled_without_a_key(monkeypatch):
    """키가 없으면 조용히 꺼진다 — 다른 shadow들과 같은 규약."""
    monkeypatch.setattr(audit.llm, "available", lambda: False)
    out = audit.generate(context={})
    assert out["ready"] is False and out["saved"] == 0
    assert db.audit_hypothesis_list() == []


def _fake_llm(monkeypatch, raw: str):
    monkeypatch.setattr(audit.llm, "available", lambda: True)
    monkeypatch.setattr(audit.llm, "complete", lambda *a, **k: raw)


def test_generate_saves_only_falsifiable_items(monkeypatch):
    import json
    _fake_llm(monkeypatch, json.dumps(
        {"hypotheses": [_item(), _item(title="막연한 우려", falsifier=None)]},
        ensure_ascii=False))
    out = audit.generate(context={"engine_config": {}})
    assert out["saved"] == 1 and out["dropped"] == 1


def test_malformed_llm_output_does_not_crash(monkeypatch):
    _fake_llm(monkeypatch, '{"hypotheses": ["문자열", null, 42]}')
    assert audit.generate(context={})["saved"] == 0


def test_truncated_response_keeps_the_complete_items(monkeypatch):
    """max_tokens에서 잘리는 건 예외가 아니라 기본값에 가깝다.
    통째로 버리면 멀쩡한 앞쪽 가설까지 사라지고, 화면에는 '응답 없음'만 남는다."""
    import json
    good = json.dumps(_item(), ensure_ascii=False)
    truncated = ('{"hypotheses": [' + good
                 + ', {"target": "x", "title": "잘린 항목", "claim": "여기서 끊')
    _fake_llm(monkeypatch, truncated)
    out = audit.generate(context={})
    assert out["saved"] == 1, out
    assert db.audit_hypothesis_list()[0]["title"] == _item()["title"]


def test_parser_handles_code_fences_and_braces_in_strings():
    raw = ('```json\n{"hypotheses": [{"target": "t", "title": "중괄호 { 포함",'
           ' "claim": "문자열 안 } 도 안전", "falsifier": "f"}]}\n```')
    items = audit.parse_hypotheses(raw)
    assert len(items) == 1 and items[0]["title"] == "중괄호 { 포함"


def test_parser_returns_empty_for_prose():
    assert audit.parse_hypotheses("죄송하지만 확인할 수 없습니다.") == []


def test_audit_cannot_change_engine_config():
    """가설 생성기가 설정을 만질 수 있으면 그건 관측이 아니라 조종이다."""
    import inspect
    src = inspect.getsource(audit)
    for forbidden in ("set_dict", "signalcfg.set", "save_config", "bot.run"):
        assert forbidden not in src, f"감사 모듈이 {forbidden}을 호출한다"


def test_summary_states_it_has_no_authority():
    audit.save([_item()])
    out = audit.summary()
    assert out["pending"] == 1
    assert "영향" in out["disclaimer"]


def test_prompt_forbids_recommendations():
    """프롬프트가 '개선안'을 요구하면 결국 파라미터를 바꾸라는 말이 돌아온다."""
    assert "개선안을 제안하지 마라" in audit._SYSTEM
    assert "반증" in audit._SYSTEM
    assert "판정권이 없다" in audit._SYSTEM


def test_endpoints_are_admin_only(tmp_path, monkeypatch):
    """감사 큐는 관리자 전용 — 일반 유저에게 노출할 내용이 아니다."""
    import importlib

    from fastapi.testclient import TestClient
    monkeypatch.chdir(tmp_path)
    from signal_desk import db as db_module
    importlib.reload(db_module)
    from signal_desk import api as api_module
    importlib.reload(api_module)
    client = TestClient(api_module.app)

    assert client.get("/api/audit/hypotheses").status_code == 401
    assert client.post("/api/audit/run").status_code == 401
    client.post("/api/auth/signup", json={"email": "u@e.com", "pw": "abcdef12"})
    assert client.get("/api/audit/hypotheses").status_code == 403
    assert client.post("/api/audit/run").status_code == 403
    assert client.post("/api/audit/hypotheses/x", json={"status": "promoted"}).status_code == 403
