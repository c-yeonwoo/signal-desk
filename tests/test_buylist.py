"""매수 대기 리스트(_buylist) — 관심종목별 매수까지 남은 조건·게이트 분해·근접순 정렬."""

from signal_desk import api
from signal_desk.signals.engine import SignalResult


def _sig(ticker, name, score, kind, reasons=None):
    return SignalResult(ticker=ticker, name=name, score=score, kind=kind, confidence=0.5,
                        technical_score=0.0, fundamental_score=0.0, has_fundamental=False,
                        reasons=reasons or [])


def test_buylist_gap_blockers_and_order(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    near = _sig("A", "가", 0.9, "HOLD")                                   # 갭 0.3(근접)
    gated = _sig("B", "나", 1.5, "HOLD",                                   # 점수 충분하나 추세 게이트
                 reasons=["[추세] 하락추세 확인(종가<MA20<MA60) — 반등 전 매수 차단(관망)"])
    ready = _sig("C", "다", 1.4, "BUY")                                    # 이미 매수
    monkeypatch.setattr(api.store, "is_ready", lambda: True)
    monkeypatch.setattr(api, "_signals", lambda: [near, gated, ready])
    monkeypatch.setattr(api, "_us_signals", lambda: {})
    monkeypatch.setattr(api.store, "load_universe",
                        lambda: [{"ticker": t, "name": n} for t, n in [("A", "가"), ("B", "나"), ("C", "다")]])
    monkeypatch.setattr(api.store, "load_us_universe", lambda: [])
    monkeypatch.setattr(api.db, "fav_list",
                        lambda uid: [{"kind": "ticker", "key": k, "label": k} for k in ["A", "B", "C"]])
    monkeypatch.setattr(api, "_regime", lambda: {})
    monkeypatch.setattr(api, "_macro", lambda: {})
    monkeypatch.setattr(api.store, "load_market_flow", lambda: {})
    monkeypatch.setattr(api.signalcfg, "effective_config",
                        lambda *a, **k: (api.signalcfg.get_config(),
                                         {"effective_buy_threshold": 1.2, "bump": 0, "reasons": []}))

    out = api._buylist(1)
    by = {x["ticker"]: x for x in out}
    assert by["A"]["status"] == "near" and 0 < by["A"]["gap"] <= 0.5
    assert by["B"]["status"] == "blocked" and any(b["key"] == "trend" for b in by["B"]["blockers"])
    assert by["C"]["status"] == "ready"
    assert out[-1]["ticker"] == "B"     # 게이트 걸린 종목은 근접순 정렬에서 뒤로


def test_buylist_empty_without_favorites(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(api.db, "fav_list", lambda uid: [])
    assert api._buylist(1) == []
