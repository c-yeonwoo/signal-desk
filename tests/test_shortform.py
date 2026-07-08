"""숏폼 생성 → 검수 큐 → 승인/반려 라이프사이클 (LLM 없이 규칙 기반 스크립트)."""

from signal_desk import db, shortform
from signal_desk.signals.engine import SignalResult


def _sig(t, n, kind, score, reasons=None):
    return SignalResult(ticker=t, name=n, score=score, kind=kind, confidence=0.6,
                        technical_score=0.0, fundamental_score=0.0, has_fundamental=False,
                        reasons=reasons or ["[기술] 골든크로스(상승 전환)", "[저평가] PER 업종 하위"])


def _setup(monkeypatch, tmp_path, sigs):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(shortform.store, "load_universe", lambda: [{"ticker": s.ticker, "name": s.name} for s in sigs])
    monkeypatch.setattr(shortform.store, "load_price_series", lambda: {s.ticker: [100.0, 110.0] for s in sigs})
    monkeypatch.setattr(shortform.store, "load_fundamentals", lambda: {})
    monkeypatch.setattr(shortform.store, "load_warned_tickers", lambda: set())
    monkeypatch.setattr(shortform.engine, "evaluate", lambda *a, **k: sigs)
    monkeypatch.setattr(shortform.kb, "sentiment_map", lambda: {})
    monkeypatch.setattr(shortform.sectors, "sector_of", lambda t: "반도체")


def test_generate_creates_drafts(tmp_path, monkeypatch):
    sigs = [_sig("005930", "삼성전자", "STRONG_BUY", 2.3), _sig("000660", "SK하이닉스", "BUY", 1.6),
            _sig("035720", "카카오", "HOLD", 0.4)]  # HOLD는 제외돼야
    _setup(monkeypatch, tmp_path, sigs)
    out = shortform.generate(limit=5)
    assert out["ok"] and out["count"] == 2                     # 매수 2건만
    tickers = {m["ticker"] for m in out["created"]}
    assert tickers == {"005930", "000660"}
    q = db.shortform_list(status="draft")
    assert len(q) == 2
    d = db.shortform_get(out["created"][0]["id"])
    assert d["card_svg"].startswith("<svg") and d["script"]      # 카드 + 스크립트 저장
    assert "투자 권유" in d["caption"]                           # 면책 포함


def test_event_risk_and_warned_excluded(tmp_path, monkeypatch):
    sigs = [_sig("005930", "삼성전자", "BUY", 2.0), _sig("000660", "SK하이닉스", "BUY", 1.9)]
    sigs[0].event_risk = True                                    # 악재 → 제외
    _setup(monkeypatch, tmp_path, sigs)
    monkeypatch.setattr(shortform.store, "load_warned_tickers", lambda: {"000660"})  # 경고 → 제외
    out = shortform.generate(limit=5)
    assert out["count"] == 0 and not out["ok"]


def test_review_lifecycle(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path, [_sig("005930", "삼성전자", "BUY", 1.8)])
    out = shortform.generate(limit=1)
    sid = out["created"][0]["id"]
    db.shortform_set_status(sid, "approved", "좋음")
    assert db.shortform_get(sid)["status"] == "approved"
    assert db.shortform_list(status="draft") == []
    assert len(db.shortform_list(status="approved")) == 1
    db.shortform_delete(sid)
    assert db.shortform_get(sid) is None


def test_skips_recent_duplicates(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path, [_sig("005930", "삼성전자", "BUY", 1.8)])
    assert shortform.generate(limit=1)["count"] == 1
    # 방금 생성 → 중복 제외로 두 번째는 0 (자동 모드)
    assert shortform.generate(limit=1)["count"] == 0


def test_candidates_signal_order(tmp_path, monkeypatch):
    sigs = [_sig("005930", "삼성전자", "STRONG_BUY", 2.3), _sig("000660", "SK하이닉스", "BUY", 1.6),
            _sig("035720", "카카오", "HOLD", 0.4)]
    _setup(monkeypatch, tmp_path, sigs)
    cs = shortform.candidates()
    assert [c["ticker"] for c in cs] == ["005930", "000660"]   # 매수만, 점수 순
    assert cs[0]["reasons"] and "recent" in cs[0]              # 근거·중복표시 포함, 생성은 안 함
    assert shortform.db.shortform_list() == []


def test_fixed_six_scene_template(tmp_path, monkeypatch):
    # 고정 템플릿: 0 인트로·1 기업개요·2 정량·3 정성·4 추천 (봇 성과 없으면 아웃트로 생략 → 5장면).
    sigs = [_sig("005930", "삼성전자", "BUY", 1.8,
                 ["[기술] 골든크로스(상승 전환)", "[저평가] PER 업종 하위", "[수급] 외국인 순매수"])]
    _setup(monkeypatch, tmp_path, sigs)
    d = db.shortform_get(shortform.generate(limit=1)["created"][0]["id"])
    labels = [s["label"] for s in d["scenes"]]
    assert labels == ["0·인트로", "1·기업 개요", "2·정량 근거", "3·정성 근거", "4·주목 포인트"]
    assert "오늘의 시그널" in d["scenes"][0]["svg"] and d["card_svg"] == d["scenes"][0]["svg"]
    assert "기업 개요" in d["scenes"][1]["svg"] and "정량 지표 근거" in d["scenes"][2]["svg"]
    assert all(sc["svg"].startswith("<svg") and sc.get("narration") for sc in d["scenes"])


def test_quant_scene_has_price_chart(tmp_path, monkeypatch):
    # 정량 근거 장면에 최근 주가 차트가 붙어야.
    sigs = [_sig("005930", "삼성전자", "BUY", 1.8, ["[기술] 골든크로스(상승 전환)"])]
    _setup(monkeypatch, tmp_path, sigs)
    monkeypatch.setattr(shortform.store, "load_price_series",
                        lambda: {"005930": [100.0, 105.0, 103.0, 108.0, 112.0]})
    d = db.shortform_get(shortform.generate(limit=1)["created"][0]["id"])
    quant = [s for s in d["scenes"] if s["label"] == "2·정량 근거"][0]
    assert "최근 1개월 주가 흐름" in quant["svg"] and "<polyline" in quant["svg"]


def test_company_scene_uses_dart_profile_and_financials(tmp_path, monkeypatch):
    # 기업개요 장면에 DART 기업개황(설립·대표), 정량 장면에 DART 재무(PER·ROE·매출성장)가 노출돼야.
    sigs = [_sig("005930", "삼성전자", "BUY", 1.8, ["[기술] 골든크로스(상승 전환)"])]
    _setup(monkeypatch, tmp_path, sigs)
    monkeypatch.setattr(shortform.store, "load_price_series", lambda: {"005930": [70000.0, 72000.0]})
    monkeypatch.setattr(shortform.store, "load_fundamentals",
                        lambda: {"005930": {"per": 13.2, "pbr": 1.4, "roe": 12.3,
                                            "revenue_growth": 8.0, "mktcap": 480e12}})
    monkeypatch.setattr(shortform.store, "load_company_profiles",
                        lambda: {"005930": {"est_year": "1969", "ceo": "한종희", "name_eng": "Samsung Electronics"}})
    d = db.shortform_get(shortform.generate(limit=1)["created"][0]["id"])
    company = [s for s in d["scenes"] if s["label"] == "1·기업 개요"][0]["svg"]
    quant = [s for s in d["scenes"] if s["label"] == "2·정량 근거"][0]["svg"]
    assert "1969년" in company and "한종희" in company               # DART 기업개황
    assert "PER 13.2" in quant and "ROE 12%" in quant and "매출 +8%" in quant  # DART 재무


def test_qualitative_scene_uses_kb(tmp_path, monkeypatch):
    # 정성 근거 장면이 KB 다이제스트(뉴스·시황)를 반영해야.
    sigs = [_sig("005930", "삼성전자", "BUY", 1.8, ["[기술] 골든크로스(상승 전환)"])]
    _setup(monkeypatch, tmp_path, sigs)
    db.kb_digest_set("005930", "삼성전자", 0.4, "HBM 수요 호조로 업황 개선 기대", ["HBM 수주 확대"], 3)
    d = db.shortform_get(shortform.generate(limit=1)["created"][0]["id"])
    qual = [s for s in d["scenes"] if s["label"] == "3·정성 근거"][0]
    assert "HBM" in qual["svg"]


def test_outro_scene_appended_with_track_record(tmp_path, monkeypatch):
    # 봇 track record가 있으면 마지막에 수익률 차트 아웃트로가 붙어야.
    sigs = [_sig("005930", "삼성전자", "BUY", 1.8, ["[기술] 골든크로스(상승 전환)"])]
    _setup(monkeypatch, tmp_path, sigs)
    curve = [{"date": f"d{i}", "total_eval": 10_000_000 * (1 + i * 0.003)} for i in range(30)]
    monkeypatch.setattr(shortform, "_reference_outro",
                        lambda *a, **k: {"label": "균형형 봇", "ret_pct": 8.7, "curve": curve})
    d = db.shortform_get(shortform.generate(limit=1)["created"][0]["id"])
    last = d["scenes"][-1]
    assert last["label"] == "5·아웃트로" and "+8.7%" in last["svg"] and "모의투자" in last["svg"]


def test_disclaimer_in_caption_not_on_card(tmp_path, monkeypatch):
    # 투자유의(면책)는 캡션에만, 카드 프레임엔 없어야. 근거 종합 해설은 캡션에 포함.
    sigs = [_sig("005930", "삼성전자", "BUY", 1.8, ["[기술] 골든크로스(상승 전환)", "[저평가] PER 업종 하위"])]
    _setup(monkeypatch, tmp_path, sigs)
    d = db.shortform_get(shortform.generate(limit=1)["created"][0]["id"])
    assert "투자 권유가 아닙니다" in d["caption"]                # 면책은 캡션
    assert "골든크로스" in d["caption"]                          # 근거 종합도 캡션
    assert all("투자 권유가 아닙니다" not in sc["svg"] for sc in d["scenes"])  # 카드엔 면책 없음


def test_high_score_non_buy_is_candidate(tmp_path, monkeypatch):
    # 매수가 아니어도(HOLD) 종합점수 1.5+ 면 숏폼 소재거리로 후보에 포함.
    sigs = [_sig("005930", "삼성전자", "HOLD", 1.7), _sig("000660", "SK하이닉스", "HOLD", 0.9)]
    _setup(monkeypatch, tmp_path, sigs)
    cs = shortform.candidates()
    assert [c["ticker"] for c in cs] == ["005930"]             # 1.7만 포함, 0.9 제외
    assert cs[0]["basis"] == "고점수 +1.7"


def test_qualitative_hozae_non_buy_is_candidate(tmp_path, monkeypatch):
    # 매수도 아니고 점수도 낮지만(0.5) 정성 호재(KB 감성 0.6)가 크면 후보에 포함.
    s = _sig("035720", "카카오", "HOLD", 0.5)
    s.has_qualitative = True
    s.qualitative_score = 0.6
    _setup(monkeypatch, tmp_path, [s])
    cs = shortform.candidates()
    assert len(cs) == 1 and cs[0]["basis"] == "정성 호재"


def test_high_score_but_event_risk_excluded(tmp_path, monkeypatch):
    # 고점수라도 악재(event_risk)면 숏폼 부적합 → 제외.
    s = _sig("005930", "삼성전자", "HOLD", 2.0)
    s.event_risk = True
    _setup(monkeypatch, tmp_path, [s])
    assert shortform.candidates() == []


def test_generate_selected_only(tmp_path, monkeypatch):
    sigs = [_sig("005930", "삼성전자", "STRONG_BUY", 2.3), _sig("000660", "SK하이닉스", "BUY", 1.9),
            _sig("035420", "NAVER", "BUY", 1.5)]
    _setup(monkeypatch, tmp_path, sigs)
    out = shortform.generate(tickers=["000660"])              # 선택한 종목만
    assert out["count"] == 1 and out["created"][0]["ticker"] == "000660"
    assert {i["ticker"] for i in db.shortform_list()} == {"000660"}
