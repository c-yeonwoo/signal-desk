"""회귀: /api/narrative가 narrative 모듈을 실제 호출 — import 누락 시 NameError를 잡는다."""

from signal_desk import api, company, llm
from signal_desk.signals.engine import SignalResult


def test_narrative_endpoint_wires_module(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sig = SignalResult(ticker="005930", name="삼성전자", score=1.5, kind="BUY", confidence=0.6,
                       technical_score=0.0, fundamental_score=0.0, has_fundamental=False, reasons=[],
                       narrative="규칙 해설")
    monkeypatch.setattr(api.store, "is_ready", lambda: True)
    monkeypatch.setattr(api, "_signals", lambda: [sig])
    monkeypatch.setattr(api.store, "load_universe", lambda: [{"ticker": "005930", "name": "삼성전자"}])
    monkeypatch.setattr(api.store, "load_us_universe", lambda: [])
    monkeypatch.setattr(api.db, "kb_digest_get", lambda t: None)
    monkeypatch.setattr(company, "about", lambda *a, **k: "반도체 회사")
    monkeypatch.setattr(llm, "available", lambda: False)  # 실제 LLM 호출 방지(결정론·부작용 없음)
    # explain_llm None → v1 폴백. 핵심은 narrative.explain_llm 참조가 NameError 안 나는 것.
    out = api.narrative_get("005930")
    assert out["ok"] and out.get("narrative") is not None


def test_narrative_hold_skips_llm(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sig = SignalResult(ticker="005930", name="삼성전자", score=0.2, kind="HOLD", confidence=0.1,
                       technical_score=0.0, fundamental_score=0.0, has_fundamental=False, reasons=[],
                       narrative="관망 규칙 해설")
    monkeypatch.setattr(api.store, "is_ready", lambda: True)
    monkeypatch.setattr(api, "_signals", lambda: [sig])
    monkeypatch.setattr(api.store, "load_universe", lambda: [{"ticker": "005930", "name": "삼성전자"}])
    monkeypatch.setattr(api.store, "load_us_universe", lambda: [])
    called = {"n": 0}

    def boom(*a, **k):
        called["n"] += 1
        raise AssertionError("HOLD면 explain_llm 호출하면 안 됨")

    monkeypatch.setattr(api.narrative, "explain_llm", boom)
    out = api.narrative_get("005930")
    assert out["ok"] and out["source"] == "rule" and out["narrative"] == "관망 규칙 해설"
    assert called["n"] == 0
