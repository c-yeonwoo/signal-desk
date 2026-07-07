"""회귀: /api/narrative가 narrative 모듈을 실제 호출 — import 누락 시 NameError를 잡는다."""

from signal_desk import api, llm
from signal_desk.signals.engine import SignalResult


def test_narrative_endpoint_wires_module(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sig = SignalResult(ticker="005930", name="삼성전자", score=1.5, kind="BUY", confidence=0.6,
                       technical_score=0.0, fundamental_score=0.0, has_fundamental=False, reasons=[])
    monkeypatch.setattr(api.store, "is_ready", lambda: True)
    monkeypatch.setattr(api, "_signals", lambda: [sig])
    monkeypatch.setattr(api.store, "load_universe", lambda: [{"ticker": "005930", "name": "삼성전자"}])
    monkeypatch.setattr(api.store, "load_us_universe", lambda: [])
    monkeypatch.setattr(api.db, "kb_digest_get", lambda t: None)
    monkeypatch.setattr(llm, "available", lambda: False)  # 실제 LLM 호출 방지(결정론·부작용 없음)
    # explain_llm None → v1 폴백. 핵심은 narrative.explain_llm 참조가 NameError 안 나는 것.
    out = api.narrative_get("005930")
    assert out["ok"] and out.get("narrative") is not None
