"""자동매매봇 — 유저별 페이퍼 계좌 + 공용 시그널. 실제 paper 브로커로 검증."""

import json

from signal_desk import bot, db
from signal_desk.broker import paper
from signal_desk.signals.engine import SignalResult

UID = 7


def _sig(ticker, name, kind, score=0.0):
    return SignalResult(ticker=ticker, name=name, score=score, kind=kind, confidence=0.5,
                        technical_score=0.0, fundamental_score=0.0, has_fundamental=False, reasons=[])


def _cfg_stub(**over):
    base = {"enabled": True, "trading_style": "balanced", "seed_cash": 10_000_000,
            "max_positions": 10, "position_pct": 0.08, "min_buy_score": 1.6, "max_new_buys_per_run": 2}
    base.update(over)
    return base


def _setup(monkeypatch, universe, prices, signals, **cfg):
    monkeypatch.setattr(bot.store, "load_universe", lambda: universe)
    monkeypatch.setattr(bot.store, "load_price_series", lambda: prices)
    monkeypatch.setattr(bot.store, "load_us_price_series", lambda: {})
    monkeypatch.setattr(bot.store, "load_fundamentals", lambda: {})
    monkeypatch.setattr(bot.engine, "evaluate", lambda *a, **k: signals)
    monkeypatch.setattr(bot, "_cfg", lambda uid: _cfg_stub(**cfg))
    monkeypatch.setattr(bot, "_market_read", lambda prices: {"eff_cfg": None, "adapt": {}, "context": {"regime": "중립"}})


def _seed(cash, positions=None):
    db.kv_set(f"paper_account:{UID}", json.dumps({"cash": cash, "positions": positions or {}}))


def test_no_price_data(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _setup(monkeypatch, [], {}, [])
    _seed(10_000.0)
    out = bot.run_once(UID)
    assert out["ok"] is False and "시세 데이터" in out["reason"]


def test_dry_run_places_no_orders(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _setup(monkeypatch, [{"ticker": "AAA", "name": "가"}], {"AAA": [100.0]},
           [_sig("AAA", "가", "BUY", 2.5)], min_buy_score=0.0)
    _seed(10_000.0)
    out = bot.run_once(UID, dry_run=True)
    assert out["ok"] and out["dry_run"] and [b["ticker"] for b in out["buys"]] == ["AAA"]
    assert db.bot_positions_all(UID) == [] and paper.balance(UID)["cash"] == 10_000.0  # 계좌 미변경


def test_sells_on_stop_loss(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _setup(monkeypatch, [{"ticker": "005930", "name": "삼성전자"}], {"005930": [100.0, 100.0, 90.0]},
           [_sig("005930", "삼성전자", "HOLD")])
    _seed(0.0, {"005930": {"name": "삼성전자", "qty": 10, "avg_price": 100.0}})
    out = bot.run_once(UID)
    assert len(out["sells"]) == 1
    s = out["sells"][0]
    assert (s["ticker"], s["qty"], s["reason"], s["ok"]) == ("005930", 10, "STOP_LOSS", True)
    assert db.bot_position_get(UID, "005930") is None            # 청산 → 포지션 삭제
    assert paper.balance(UID)["cash"] == 900.0                    # 10주 × 90 회수


def test_sells_on_signal_flip(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _setup(monkeypatch, [{"ticker": "005930", "name": "삼성전자"}], {"005930": [100.0, 100.0, 101.0]},
           [_sig("005930", "삼성전자", "SELL")])
    _seed(0.0, {"005930": {"name": "삼성전자", "qty": 10, "avg_price": 100.0}})
    out = bot.run_once(UID)
    assert out["sells"][0]["reason"] == "SIGNAL" and out["sells"][0]["ok"] is True


def test_holds_and_tracks_peak(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    pos = {"005930": {"name": "삼성전자", "qty": 10, "avg_price": 100.0}}
    _setup(monkeypatch, [{"ticker": "005930", "name": "삼성전자"}], {"005930": [108.0]},
           [_sig("005930", "삼성전자", "HOLD")])
    _seed(0.0, pos)
    bot.run_once(UID)
    assert db.bot_position_get(UID, "005930")["peak_price"] == 108.0
    # 하락해도 peak 유지(트레일링 기준점 보존)
    _setup(monkeypatch, [{"ticker": "005930", "name": "삼성전자"}], {"005930": [105.0]},
           [_sig("005930", "삼성전자", "HOLD")])
    bot.run_once(UID)
    assert db.bot_position_get(UID, "005930")["peak_price"] == 108.0


def test_reconciles_stale_position(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db.bot_position_upsert(UID, "999999", "유령", 5, 100.0, 100.0, "2026-01-01")  # paper엔 없음
    _setup(monkeypatch, [{"ticker": "005930", "name": "삼성"}], {"005930": [100.0]}, [_sig("005930", "삼성", "HOLD")])
    _seed(1000.0)
    bot.run_once(UID)
    assert db.bot_position_get(UID, "999999") is None            # paper에 없으면 정리


def test_buys_top_scored_respecting_slots_and_lot(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    universe = [{"ticker": t, "name": n} for t, n in
                [("AAA", "가"), ("BBB", "나"), ("CCC", "다"), ("EXP", "비싼")]]
    prices = {"AAA": [100.0], "BBB": [100.0], "CCC": [100.0], "EXP": [999_999.0]}
    signals = [_sig("AAA", "가", "BUY", 1.5), _sig("BBB", "나", "BUY", 2.0),
               _sig("CCC", "다", "BUY", 1.3), _sig("EXP", "비싼", "BUY", 3.0)]
    _setup(monkeypatch, universe, prices, signals, min_buy_score=0.0, max_new_buys_per_run=10)
    _seed(10_000.0)  # 목표배분 800, 균형형 3분할 → 1트랜치 ~266 → 100원 종목 2주. EXP는 1주도 못 사 스킵
    out = bot.run_once(UID)
    assert [b["ticker"] for b in out["buys"]] == ["BBB", "AAA", "CCC"]   # 점수 내림차순
    p = db.bot_position_get(UID, "BBB")
    assert (p["ticker"], p["qty"], p["avg_price"]) == ("BBB", 2, 100.0)


def test_pyramid_adds_to_under_target_holding(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _setup(monkeypatch, [{"ticker": "AAA", "name": "가"}], {"AAA": [100.0]},
           [_sig("AAA", "가", "BUY", 2.0)], min_buy_score=0.0)
    _seed(10_000.0, {"AAA": {"name": "가", "qty": 2, "avg_price": 100.0}})  # 평가 200 « 목표 800
    out = bot.run_once(UID)
    adds = [b for b in out["buys"] if b["reason"] == "ADD"]
    assert len(adds) == 1 and adds[0]["ticker"] == "AAA"


def test_buys_respect_max_positions(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    universe = [{"ticker": f"T{i}", "name": f"종목{i}"} for i in range(3)]
    _setup(monkeypatch, universe, {f"T{i}": [100.0] for i in range(3)},
           [_sig(f"T{i}", f"종목{i}", "BUY", float(i)) for i in range(3)],
           min_buy_score=0.0, max_positions=1, max_new_buys_per_run=10)
    _seed(10_000.0)
    out = bot.run_once(UID)
    assert len(out["buys"]) == 1 and out["buys"][0]["ticker"] == "T2"  # 점수 최고 1개만


def test_skips_weak_buys(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _setup(monkeypatch, [{"ticker": "AAA", "name": "가"}, {"ticker": "BBB", "name": "나"}],
           {"AAA": [100.0], "BBB": [100.0]},
           [_sig("AAA", "가", "BUY", 1.0), _sig("BBB", "나", "BUY", 2.0)],
           min_buy_score=1.6, max_new_buys_per_run=10)
    _seed(10_000.0)
    out = bot.run_once(UID)
    assert [b["ticker"] for b in out["buys"]] == ["BBB"] and out["skipped_weak_buys"] == 1


def test_max_new_buys_caps(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    universe = [{"ticker": f"T{i}", "name": f"종목{i}"} for i in range(5)]
    _setup(monkeypatch, universe, {f"T{i}": [100.0] for i in range(5)},
           [_sig(f"T{i}", f"종목{i}", "BUY", 2.0 + i) for i in range(5)],
           min_buy_score=1.0, max_new_buys_per_run=2, max_positions=10)
    _seed(100_000.0)
    out = bot.run_once(UID)
    assert [b["ticker"] for b in out["buys"]] == ["T4", "T3"]   # 상위 2개만
