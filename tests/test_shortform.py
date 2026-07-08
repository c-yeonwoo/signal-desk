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
