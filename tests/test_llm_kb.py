from signal_desk import kb
from signal_desk.signals import advisor, engine
from signal_desk.signals import qualitative as qual


def test_qual_component_absent_is_excluded():
    norm, weight, reasons, score, has = qual.component(None, 0.15)
    assert (weight, has) == (0.0, False) and score is None


def test_qual_component_clamps_and_carries_reasons():
    norm, weight, reasons, score, has = qual.component({"score": 1.8, "reasons": ["[정성] 호재"]}, 0.15)
    assert norm == 1.0 and weight == 0.15 and has is True and reasons == ["[정성] 호재"]


def test_evaluate_includes_qualitative_when_provided():
    universe = [{"ticker": "005930", "name": "삼성전자"}]
    closes = [100 - i for i in range(20)]  # RSI 과매도 → 기술 BUY 성분
    sentiment = {"005930": {"score": 0.9, "reasons": ["[정성] 호재 뉴스 다수"]}}
    r = engine.evaluate(universe, {"005930": closes}, sentiment=sentiment)[0]
    assert r.has_qualitative is True
    assert r.qualitative_score == 0.9
    assert any("정성" in x for x in r.reasons)


def test_rule_digest_sentiment_from_keywords():
    items = [{"title": "실적 급등 호재 신고가", "summary": ""}, {"title": "수주 개선 기대", "summary": ""}]
    d = kb._rule_digest("가나전자", items)
    assert d["sentiment"] > 0 and d["points"]
    items2 = [{"title": "급락 악재 적자 우려", "summary": ""}]
    assert kb._rule_digest("나다전자", items2)["sentiment"] < 0


def test_advisor_none_without_llm(monkeypatch):
    monkeypatch.setattr(advisor.llm, "available", lambda: False)
    assert advisor.select_buys([{"ticker": "A", "name": "a", "score": 2.0, "confidence": 0.7, "reasons": []}],
                               {}, {}, [], 2) is None


def test_advisor_filters_to_candidates_and_caps(monkeypatch):
    monkeypatch.setattr(advisor.llm, "available", lambda: True)
    # LLM이 후보 밖 티커(ZZZ)와 중복을 섞어 반환해도 후보 안에서만, max_new로 캡
    monkeypatch.setattr(advisor.llm, "complete_json", lambda *a, **k: {
        "picks": [{"ticker": "ZZZ", "rationale": "밖"}, {"ticker": "B", "rationale": "좋음"},
                  {"ticker": "B", "rationale": "중복"}, {"ticker": "A", "rationale": "또"}]})
    cands = [{"ticker": "A", "name": "a", "score": 2.0, "confidence": 0.7, "reasons": []},
             {"ticker": "B", "name": "b", "score": 1.8, "confidence": 0.6, "reasons": []}]
    picks = advisor.select_buys(cands, {}, {}, [], 1)
    assert picks == [{"ticker": "B", "rationale": "좋음"}]  # 밖(ZZZ) 제외, 중복 제거, 1개 캡
