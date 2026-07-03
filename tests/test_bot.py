import datetime

import pytest

from signal_desk import bot
from signal_desk.signals.engine import SignalResult


def _creds():
    return {"app_key": "k", "app_secret": "s", "account_no": "1", "product_cd": "01", "env": "demo"}


def _sig(ticker, name, kind, score=0.0):
    return SignalResult(
        ticker=ticker, name=name, score=score, kind=kind, confidence=0.5,
        technical_score=0.0, fundamental_score=0.0, has_fundamental=False, reasons=[],
    )


def _setup_common(monkeypatch, universe, prices, signals, balance_sequence):
    monkeypatch.setattr(bot.config, "kis_credentials", lambda: _creds())
    monkeypatch.setattr(bot, "is_market_hours", lambda: True)
    monkeypatch.setattr(bot.store, "load_universe", lambda: universe)
    monkeypatch.setattr(bot.store, "load_price_series", lambda: prices)
    monkeypatch.setattr(bot.store, "load_fundamentals", lambda: {})
    monkeypatch.setattr(bot.engine, "evaluate", lambda *a, **k: signals)

    # 기본: 실시간가 조회는 None → 캐시 종가로 폴백(네트워크 없이 기존 기대치 유지).
    # 갭 게이트를 검증하는 테스트는 개별적으로 override 한다.
    monkeypatch.setattr(bot.kis, "current_price", lambda ticker, creds=None: None)

    calls = iter(balance_sequence)
    monkeypatch.setattr(bot.kis, "balance", lambda creds=None: next(calls))


def _set_cfg(**kw):
    """테스트용 bot_config 값 오버라이드(min_buy_score / max_new_buys_per_run / max_positions 등)."""
    bot.db.bot_config_get()  # 기본 행 시딩
    c = bot.db.conn()
    for k, v in kw.items():
        c.execute(f"UPDATE bot_config SET {k}=? WHERE id=1", (v,))
    c.commit()
    c.close()


def test_no_credentials(monkeypatch):
    monkeypatch.setattr(bot.config, "kis_credentials", lambda: None)
    out = bot.run_once()
    assert out == {"ok": False, "reason": "KIS 인증정보 없음(.env 확인)"}


def test_outside_market_hours_blocks_real_run(monkeypatch):
    monkeypatch.setattr(bot.config, "kis_credentials", lambda: _creds())
    monkeypatch.setattr(bot, "is_market_hours", lambda: False)
    out = bot.run_once()
    assert out["ok"] is False and "장 시간" in out["reason"]


def test_dry_run_ignores_market_hours_and_places_no_orders(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    universe = [{"ticker": "AAA", "name": "가"}]
    prices = {"AAA": [100.0]}
    signals = [_sig("AAA", "가", "BUY", score=2.5)]
    bal = {"cash": 10_000.0, "total_eval": 10_000.0, "holdings": []}
    monkeypatch.setattr(bot.config, "kis_credentials", lambda: _creds())
    monkeypatch.setattr(bot, "is_market_hours", lambda: False)  # 장 밖이어도
    monkeypatch.setattr(bot.store, "load_universe", lambda: universe)
    monkeypatch.setattr(bot.store, "load_price_series", lambda: prices)
    monkeypatch.setattr(bot.store, "load_fundamentals", lambda: {})
    monkeypatch.setattr(bot.engine, "evaluate", lambda *a, **k: signals)
    monkeypatch.setattr(bot.kis, "balance", lambda creds=None: bal)
    ordered = []
    monkeypatch.setattr(bot.kis, "place_order", lambda *a, **k: ordered.append(a) or {"order_no": "1"})

    out = bot.run_once(dry_run=True)
    assert out["ok"] is True and out["dry_run"] is True
    assert [b["ticker"] for b in out["buys"]] == ["AAA"]  # 계획엔 잡히지만
    assert ordered == []                                   # 실주문은 없음
    assert bot.db.bot_position_get("AAA") is None           # DB에도 안 씀
    assert bot.db.bot_trades_recent() == []


def test_balance_failure(monkeypatch):
    monkeypatch.setattr(bot.config, "kis_credentials", lambda: _creds())
    monkeypatch.setattr(bot, "is_market_hours", lambda: True)
    monkeypatch.setattr(bot.kis, "balance", lambda creds=None: None)
    out = bot.run_once()
    assert out == {"ok": False, "reason": "KIS 잔고조회 실패"}


def test_no_price_data(monkeypatch):
    monkeypatch.setattr(bot.config, "kis_credentials", lambda: _creds())
    monkeypatch.setattr(bot, "is_market_hours", lambda: True)
    monkeypatch.setattr(bot.kis, "balance", lambda creds=None: {"cash": 100.0, "total_eval": 100.0, "holdings": []})
    monkeypatch.setattr(bot.store, "load_universe", lambda: [])
    monkeypatch.setattr(bot.store, "load_price_series", lambda: {})
    out = bot.run_once()
    assert out["ok"] is False and "시세 데이터" in out["reason"]


def test_sells_on_stop_loss(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    universe = [{"ticker": "005930", "name": "삼성전자"}]
    prices = {"005930": [100.0, 100.0, 90.0]}  # -10% -> 손절(-7%) 발동
    signals = [_sig("005930", "삼성전자", "HOLD")]
    bal_holding = {"cash": 0.0, "total_eval": 900.0,
                   "holdings": [{"ticker": "005930", "name": "삼성전자", "qty": 10, "avg_price": 100.0}]}
    bal_after = {"cash": 900.0, "total_eval": 900.0, "holdings": []}
    _setup_common(monkeypatch, universe, prices, signals, [bal_holding, bal_after, bal_after])

    orders = []
    monkeypatch.setattr(bot.kis, "place_order", lambda ticker, side, qty, price=None, creds=None:
                         orders.append((ticker, side, qty)) or {"order_no": "1", "order_time": "t"})

    out = bot.run_once()
    assert out["ok"] is True
    assert len(out["sells"]) == 1
    s = out["sells"][0]
    assert (s["ticker"], s["qty"], s["reason"], s["ok"]) == ("005930", 10, "STOP_LOSS", True)
    assert orders == [("005930", "sell", 10)]
    assert bot.db.bot_position_get("005930") is None  # 매도 성공 -> 포지션 삭제


def test_sells_on_signal_flip_when_no_risk_trigger(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    universe = [{"ticker": "005930", "name": "삼성전자"}]
    prices = {"005930": [100.0, 100.0, 101.0]}  # +1% -> 리스크 룰 미발동
    signals = [_sig("005930", "삼성전자", "SELL")]
    bal_holding = {"cash": 0.0, "total_eval": 1010.0,
                   "holdings": [{"ticker": "005930", "name": "삼성전자", "qty": 10, "avg_price": 100.0}]}
    bal_after = {"cash": 1010.0, "total_eval": 1010.0, "holdings": []}
    _setup_common(monkeypatch, universe, prices, signals, [bal_holding, bal_after, bal_after])
    monkeypatch.setattr(bot.kis, "place_order", lambda *a, **k: {"order_no": "1", "order_time": "t"})

    out = bot.run_once()
    assert len(out["sells"]) == 1
    s = out["sells"][0]
    assert (s["ticker"], s["qty"], s["reason"], s["ok"]) == ("005930", 10, "SIGNAL", True)


def test_holds_position_and_tracks_peak_when_no_exit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    universe = [{"ticker": "005930", "name": "삼성전자"}]
    signals = [_sig("005930", "삼성전자", "HOLD")]
    bal = {"cash": 0.0, "total_eval": 1080.0,
           "holdings": [{"ticker": "005930", "name": "삼성전자", "qty": 10, "avg_price": 100.0}]}

    # 1회차: 오늘 종가 108 -> peak = max(avg_price=100, 108) = 108(이번 회차엔 DB에 이전 기록 없음)
    _setup_common(monkeypatch, universe, {"005930": [108.0]}, signals, [bal, bal, bal])
    out = bot.run_once()
    assert out["sells"] == []
    assert bot.db.bot_position_get("005930")["peak_price"] == 108.0

    # 2회차: 오늘 종가 105로 하락 -> peak은 이전 회차 108을 유지해야 함(트레일링스탑 기준점 보존)
    _setup_common(monkeypatch, universe, {"005930": [105.0]}, signals, [bal, bal, bal])
    out = bot.run_once()
    assert out["sells"] == []
    assert bot.db.bot_position_get("005930")["peak_price"] == 108.0


def test_reconciles_stale_db_position_not_in_kis_balance(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    bot.db.bot_position_upsert("999999", "유령종목", 5, 100.0, 100.0, "2026-01-01")

    universe = [{"ticker": "005930", "name": "삼성전자"}]
    prices = {"005930": [100.0]}
    signals = [_sig("005930", "삼성전자", "HOLD")]
    bal = {"cash": 1000.0, "total_eval": 1000.0, "holdings": []}  # KIS엔 유령종목 없음
    _setup_common(monkeypatch, universe, prices, signals, [bal, bal, bal])

    bot.run_once()
    assert bot.db.bot_position_get("999999") is None


def test_buys_top_scored_signals_respecting_slots_and_lot_size(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    universe = [
        {"ticker": "AAA", "name": "가"}, {"ticker": "BBB", "name": "나"},
        {"ticker": "CCC", "name": "다"}, {"ticker": "EXP", "name": "비싼종목"},
    ]
    prices = {"AAA": [100.0], "BBB": [100.0], "CCC": [100.0], "EXP": [999_999.0]}
    signals = [
        _sig("AAA", "가", "BUY", score=1.5),
        _sig("BBB", "나", "BUY", score=2.0),  # 가장 높은 점수 -> 우선 매수
        _sig("CCC", "다", "BUY", score=1.3),
        _sig("EXP", "비싼종목", "BUY", score=3.0),  # 점수는 1등이지만 배분금액(800)보다 비싸 스킵
    ]
    # 목표배분 = 0.08*10000 = 800원. ① 분할매수: 균형형 3분할 → 1트랜치 ≈ 266원 → 100원 종목은 2주(신규 진입).
    # EXP(999999원)는 1주도 못 사 스킵.
    bal = {"cash": 10_000.0, "total_eval": 10_000.0, "holdings": []}
    _setup_common(monkeypatch, universe, prices, signals, [bal, bal, bal])
    _set_cfg(min_buy_score=0.0, max_new_buys_per_run=10)  # 이 테스트는 점수순·정수주 검증 목적 → 선택성 완화

    orders = []
    monkeypatch.setattr(bot.kis, "place_order", lambda ticker, side, qty, price=None, creds=None:
                         orders.append((ticker, side, qty)) or {"order_no": "1", "order_time": "t"})

    out = bot.run_once()
    assert orders == [("BBB", "buy", 2), ("AAA", "buy", 2), ("CCC", "buy", 2)]  # 점수 내림차순, 1트랜치씩
    assert [b["ticker"] for b in out["buys"]] == ["BBB", "AAA", "CCC"]
    assert bot.db.bot_position_get("BBB") == {
        "ticker": "BBB", "name": "나", "qty": 2, "avg_price": 100.0,
        "peak_price": 100.0, "entry_date": bot._today(),
    }


def test_buy_drift_gate_skips_runup_and_crash(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    universe = [{"ticker": "UP", "name": "급등"}, {"ticker": "DN", "name": "급락"}, {"ticker": "OK", "name": "정상"}]
    prices = {"UP": [100.0], "DN": [100.0], "OK": [100.0]}  # 신호 기준가(종가) 모두 100
    signals = [_sig("UP", "급등", "BUY", 2.0), _sig("DN", "급락", "BUY", 2.0), _sig("OK", "정상", "BUY", 2.0)]
    bal = {"cash": 10_000.0, "total_eval": 10_000.0, "holdings": []}
    _setup_common(monkeypatch, universe, prices, signals, [bal, bal, bal])
    _set_cfg(min_buy_score=0.0, max_new_buys_per_run=10)
    # 실시간가: UP +5%(추격 상한 2% 초과), DN -8%(급락 하한 5% 초과), OK +1%(허용)
    live = {"UP": 105.0, "DN": 92.0, "OK": 101.0}
    monkeypatch.setattr(bot.kis, "current_price", lambda ticker, creds=None: live[ticker])
    orders = []
    monkeypatch.setattr(bot.kis, "place_order", lambda ticker, side, qty, price=None, creds=None:
                         orders.append((ticker, side, qty, price)) or {"order_no": "1"})

    out = bot.run_once()
    assert [b["ticker"] for b in out["buys"]] == ["OK"]      # 갭 이탈 2건 스킵, 정상 1건만 매수
    assert out["skipped_gap_buys"] == 2
    assert orders[0][0] == "OK" and orders[0][3] == 102      # 지정가 = 종가 100 × (1+2%)


def test_pyramid_adds_tranche_to_under_target_buy_holding(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    universe = [{"ticker": "AAA", "name": "가"}]
    prices = {"AAA": [100.0]}
    signals = [_sig("AAA", "가", "BUY", 2.0)]  # 여전히 BUY
    # 보유 2주(평가 200) « 목표배분 800 → 미달 → 추가 트랜치 매수. 현재가 100 ≤ 평단 100(추격 아님)
    bal = {"cash": 10_000.0, "total_eval": 10_000.0,
           "holdings": [{"ticker": "AAA", "name": "가", "qty": 2, "avg_price": 100.0}]}
    _setup_common(monkeypatch, universe, prices, signals, [bal, bal, bal])
    _set_cfg(min_buy_score=0.0, max_positions=10, max_new_buys_per_run=2)
    orders = []
    monkeypatch.setattr(bot.kis, "place_order", lambda ticker, side, qty, price=None, creds=None:
                         orders.append((ticker, side, qty)) or {"order_no": "1"})

    out = bot.run_once()
    adds = [b for b in out["buys"] if b["reason"] == "ADD"]
    assert len(adds) == 1 and adds[0]["ticker"] == "AAA"  # 목표 미달 보유분에 분할 추가
    assert orders and orders[0][1] == "buy"


def test_pyramid_skips_when_extended_above_avg(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    universe = [{"ticker": "AAA", "name": "가"}]
    prices = {"AAA": [100.0]}
    signals = [_sig("AAA", "가", "BUY", 2.0)]
    bal = {"cash": 10_000.0, "total_eval": 10_000.0,
           "holdings": [{"ticker": "AAA", "name": "가", "qty": 2, "avg_price": 100.0}]}
    _setup_common(monkeypatch, universe, prices, signals, [bal, bal, bal])
    _set_cfg(min_buy_score=0.0, max_positions=10, max_new_buys_per_run=2)
    monkeypatch.setattr(bot.kis, "current_price", lambda ticker, creds=None: 110.0)  # 평단+10% → 추격 안 함
    monkeypatch.setattr(bot.kis, "place_order", lambda *a, **k: {"order_no": "1"})

    out = bot.run_once()
    assert [b for b in out["buys"] if b["reason"] == "ADD"] == []  # 평단보다 크게 위 → 추가 스킵


def test_buys_respect_max_positions_slot_limit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    universe = [{"ticker": f"T{i}", "name": f"종목{i}"} for i in range(3)]
    prices = {f"T{i}": [100.0] for i in range(3)}
    signals = [_sig(f"T{i}", f"종목{i}", "BUY", score=float(i)) for i in range(3)]
    bal = {"cash": 10_000.0, "total_eval": 10_000.0, "holdings": []}
    _setup_common(monkeypatch, universe, prices, signals, [bal, bal, bal])
    monkeypatch.setattr(bot.kis, "place_order", lambda *a, **k: {"order_no": "1", "order_time": "t"})

    # max_positions=1로 좁혀서 슬롯 제한 확인(선택성 필터는 완화)
    _set_cfg(max_positions=1, min_buy_score=0.0, max_new_buys_per_run=10)

    out = bot.run_once()
    assert len(out["buys"]) == 1
    assert out["buys"][0]["ticker"] == "T2"  # 점수 가장 높은 것만


def test_skips_weak_buys_below_min_score(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    universe = [{"ticker": "AAA", "name": "가"}, {"ticker": "BBB", "name": "나"}]
    prices = {"AAA": [100.0], "BBB": [100.0]}
    signals = [_sig("AAA", "가", "BUY", score=1.0), _sig("BBB", "나", "BUY", score=2.0)]
    bal = {"cash": 10_000.0, "total_eval": 10_000.0, "holdings": []}
    _setup_common(monkeypatch, universe, prices, signals, [bal, bal, bal])
    _set_cfg(min_buy_score=1.6, max_new_buys_per_run=10)
    orders = []
    monkeypatch.setattr(bot.kis, "place_order", lambda ticker, side, qty, price=None, creds=None:
                         orders.append(ticker) or {"order_no": "1"})

    out = bot.run_once()
    assert orders == ["BBB"]  # AAA(1.0<1.6)는 약한 BUY라 제외
    assert out["skipped_weak_buys"] == 1


def test_max_new_buys_per_run_caps_buys(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    universe = [{"ticker": f"T{i}", "name": f"종목{i}"} for i in range(5)]
    prices = {f"T{i}": [100.0] for i in range(5)}
    signals = [_sig(f"T{i}", f"종목{i}", "BUY", score=2.0 + i) for i in range(5)]  # 전부 강한 BUY
    bal = {"cash": 100_000.0, "total_eval": 100_000.0, "holdings": []}
    _setup_common(monkeypatch, universe, prices, signals, [bal, bal, bal])
    _set_cfg(min_buy_score=1.0, max_new_buys_per_run=2, max_positions=10)
    monkeypatch.setattr(bot.kis, "place_order", lambda *a, **k: {"order_no": "1"})

    out = bot.run_once()
    assert len(out["buys"]) == 2  # 강한 BUY가 5개여도 한 사이클엔 2건만
    assert [b["ticker"] for b in out["buys"]] == ["T4", "T3"]  # 점수 상위 2개
