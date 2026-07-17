"""시그널 해설 v2(#17) — LLM 해설 + 폴백."""

from signal_desk.signals import narrative


def test_explain_llm_none_without_llm(monkeypatch):
    import signal_desk.llm as llm
    monkeypatch.setattr(llm, "available", lambda: False)
    assert narrative.explain_llm("삼성전자", "005930", "BUY", 1.8,
                                 ["[기술] 골든크로스"], "업황 회복") is None


def test_explain_llm_uses_grounding(monkeypatch):
    import signal_desk.llm as llm
    captured = {}

    def fake_complete(system, user, *, max_tokens=320, **kw):
        captured["system"] = system
        captured["user"] = user
        return "  기술적 골든크로스와 저평가가 관찰됩니다.  "

    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "complete", fake_complete)
    out = narrative.explain_llm("삼성전자", "005930", "BUY", 1.8,
                                ["[기술] 골든크로스", "[저평가] PER 하위"], "반도체 업황 회복 기대")
    assert out == "기술적 골든크로스와 저평가가 관찰됩니다."   # strip 적용
    # 근거·KB가 프롬프트에 실렸는지(그라운딩)
    assert "골든크로스" in captured["user"] and "반도체 업황 회복" in captured["user"]
    assert "지어내지" in captured["system"]                    # 환각 금지 지시


def test_explain_llm_includes_about(monkeypatch):
    import signal_desk.llm as llm
    captured = {}

    def fake_complete(system, user, *, max_tokens=320, **kw):
        captured["user"] = user
        captured["model"] = kw.get("model")
        return "쉽게 말하면, 저평가 구간입니다."

    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "complete", fake_complete)
    out = narrative.explain_llm(
        "삼성전자", "005930", "BUY", 1.8, ["[기술] 골든크로스"], "",
        about="메모리 반도체와 스마트폰을 만드는 회사",
        model=llm.SIGNAL_EXPLAIN_MODEL,
    )
    assert out
    assert "메모리 반도체" in captured["user"]
    assert captured["model"] == llm.SIGNAL_EXPLAIN_MODEL
