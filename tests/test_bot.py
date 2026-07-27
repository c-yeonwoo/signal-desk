"""자동매매봇 — 유저별 페이퍼 계좌 + 공용 시그널. 실제 paper 브로커로 검증."""

import json

from signal_desk import bot, db
from signal_desk.broker import paper
from signal_desk.signals.engine import SignalConfig, SignalResult

UID = 7


def _sig(ticker, name, kind, score=0.0):
    return SignalResult(ticker=ticker, name=name, score=score, kind=kind, confidence=0.5,
                        technical_score=0.0, fundamental_score=0.0, has_fundamental=False, reasons=[])


def _cfg_stub(**over):
    base = {"enabled": True, "trading_style": "balanced", "seed_cash": 10_000_000,
            "max_positions": 10, "position_pct": 0.08, "min_buy_score": 1.6, "max_new_buys_per_run": 2}
    base.update(over)
    return base


def _setup(monkeypatch, universe, prices, signals, mode="absolute", exposure=1.0,
           rank_top_pct=3.0, **cfg):
    """mode: 매수권 선정 방식. 기존 테스트는 절대문턱(min_buy_score) 기준으로 쓰였으므로
    기본을 "absolute"로 두고, 횡단면 분위 동작은 아래 rank 전용 테스트에서 검증한다."""
    monkeypatch.setattr(bot.store, "load_universe", lambda: universe)
    monkeypatch.setattr(bot.store, "load_price_series", lambda: prices)
    monkeypatch.setattr(bot.store, "load_us_price_series", lambda: {})
    monkeypatch.setattr(bot.store, "load_fundamentals", lambda: {})
    monkeypatch.setattr(bot.engine, "evaluate", lambda *a, **k: signals)
    monkeypatch.setattr(bot, "_cfg", lambda uid: _cfg_stub(**cfg))
    eng_cfg = SignalConfig(selection_mode=mode, rank_top_pct=rank_top_pct)
    monkeypatch.setattr(bot.signalcfg, "get_config", lambda: eng_cfg)
    monkeypatch.setattr(bot, "_market_read", lambda prices: {
        "eff_cfg": eng_cfg, "adapt": {}, "context": {"regime": "중립", "exposure": exposure}})


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


def test_advisor_abstention_buys_nothing(tmp_path, monkeypatch):
    """LLM이 빈 배열을 반환하면(기권) 점수순 폴백으로 뒤집지 않는다 — 하락장 강제 매수 방지."""
    monkeypatch.chdir(tmp_path)
    _setup(monkeypatch, [{"ticker": "AAA", "name": "가"}, {"ticker": "BBB", "name": "나"}],
           {"AAA": [100.0], "BBB": [100.0]},
           [_sig("AAA", "가", "BUY", 2.4), _sig("BBB", "나", "BUY", 2.0)], min_buy_score=0.0)
    _seed(10_000_000.0)
    monkeypatch.setattr(
        bot.advisor, "advise",
        lambda *a, **k: bot.advisor.BuyAdvice([], reason="test_abstain"))
    out = bot.run_once(UID)
    assert out["ok"] and out["buys"] == []
    assert db.bot_positions_all(UID) == []
    # 기권은 advisor의 판단이므로 shadow에서 폴백 회차로 세지 않는다
    from signal_desk.signals import advisor_shadow
    s = advisor_shadow.summary({})
    assert s["advisor_used_runs"] == 1 and s["abstained_runs"] == 1


def test_advisor_unavailable_falls_back_to_score_order(tmp_path, monkeypatch):
    """None(키 없음·실패)은 기권이 아니다 — 결정론적 점수순 폴백을 그대로 쓴다."""
    monkeypatch.chdir(tmp_path)
    _setup(monkeypatch, [{"ticker": "AAA", "name": "가"}, {"ticker": "BBB", "name": "나"}],
           {"AAA": [100.0], "BBB": [100.0]},
           [_sig("AAA", "가", "BUY", 2.4), _sig("BBB", "나", "BUY", 2.0)], min_buy_score=0.0)
    _seed(10_000_000.0)
    monkeypatch.setattr(
        bot.advisor, "advise",
        lambda *a, **k: bot.advisor.BuyAdvice(None, reason="test_unavailable"))
    out = bot.run_once(UID)
    assert [b["ticker"] for b in out["buys"]] == ["AAA", "BBB"]  # 점수순 상위 2(슬롯)
    from signal_desk.signals import advisor_shadow
    s = advisor_shadow.summary({})
    assert s["advisor_used_runs"] == 0 and s["abstained_runs"] == 0


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


def test_records_advisor_shadow_only_on_real_runs(tmp_path, monkeypatch):
    """LLM 선별 vs 점수순 폴백 관측 — dry_run은 표본에 넣지 않는다."""
    from signal_desk.signals import advisor_shadow

    monkeypatch.chdir(tmp_path)
    _setup(monkeypatch, [{"ticker": "AAA", "name": "가"}, {"ticker": "BBB", "name": "나"}],
           {"AAA": [100.0], "BBB": [100.0]},
           [_sig("AAA", "가", "BUY", 2.0), _sig("BBB", "나", "BUY", 1.7)], min_buy_score=0.0)
    _seed(10_000.0)
    bot.run_once(UID, dry_run=True)
    assert advisor_shadow.summary({})["ready"] is False   # 기록 없음
    bot.run_once(UID)
    out = advisor_shadow.summary({})
    assert out["runs"] == 1 and out["advisor_used_runs"] == 0  # LLM 키 없음 → 폴백으로 기록
    # 성향이 기록에 남아 성향별 집계가 가능하다(레퍼런스 uid 폴백 포함)
    blob = advisor_shadow._load()
    day = next(iter(blob.values()))
    assert day[0].get("style") in ("conservative", "balanced", "aggressive")


def test_rank_mode_buys_relative_best_even_when_all_below_old_threshold(tmp_path, monkeypatch):
    """분위 모드: 점수가 전부 옛 문턱(1.6) 아래여도 시장 상위는 매수한다.

    2026-07-26 진단의 핵심 — 관측 최고점수 1.91 < 유효문턱 2.0~2.4라서 10거래일간 매수 1건이었다.
    상대 순위로 고르면 시장이 나빠도 후보가 비지 않는다.
    """
    monkeypatch.chdir(tmp_path)
    universe = [{"ticker": f"T{i}", "name": f"종목{i}"} for i in range(20)]
    prices = {f"T{i}": [100.0] for i in range(20)}
    # 전 종목 0.6~1.1 — 절대문턱 모드였다면 매수 0건
    signals = [_sig(f"T{i}", f"종목{i}", "HOLD", 0.6 + i * 0.025) for i in range(20)]
    _setup(monkeypatch, universe, prices, signals, mode="rank", rank_top_pct=10.0,
           max_new_buys_per_run=10, min_buy_score=1.6)
    _seed(10_000_000.0)
    out = bot.run_once(UID)
    # 균형형 성향 분위 2% × 20종목 → 최소 1종목. 최고점수 종목(T19)이 뽑힌다.
    assert [b["ticker"] for b in out["buys"]] == ["T19"]
    assert "시장 상위" in out["buys"][0]["note"]


def test_rank_mode_respects_min_score_floor_in_crash(tmp_path, monkeypatch):
    """폭락장에서 '최악 중 최선'은 사지 않는다 — 분위 안이어도 최소점수 미달이면 제외."""
    monkeypatch.chdir(tmp_path)
    universe = [{"ticker": f"T{i}", "name": f"종목{i}"} for i in range(10)]
    signals = [_sig(f"T{i}", f"종목{i}", "HOLD", -1.0 + i * 0.05) for i in range(10)]
    _setup(monkeypatch, universe, {f"T{i}": [100.0] for i in range(10)}, signals,
           mode="rank", rank_top_pct=30.0, max_new_buys_per_run=10)
    _seed(10_000_000.0)
    assert bot.run_once(UID)["buys"] == []


def test_exposure_caps_total_invested(tmp_path, monkeypatch):
    """국면 익스포저가 총 투자금 상한이다 — 문턱을 올려 자격을 0으로 만드는 대신 크기를 줄인다."""
    monkeypatch.chdir(tmp_path)
    universe = [{"ticker": f"T{i}", "name": f"종목{i}"} for i in range(10)]
    signals = [_sig(f"T{i}", f"종목{i}", "BUY", 2.0 - i * 0.01) for i in range(10)]
    _setup(monkeypatch, universe, {f"T{i}": [100.0] for i in range(10)}, signals,
           mode="rank", rank_top_pct=100.0, exposure=0.02,
           min_buy_score=0.0, max_new_buys_per_run=10, position_pct=0.5)
    _seed(10_000.0)                       # 익스포저 2% → 투자 상한 200원 = 2주
    out = bot.run_once(UID)
    spent = sum(b["qty"] * b["price"] for b in out["buys"])
    assert 0 < spent <= 200.0
    assert "익스포저 2%" in out["buys"][0]["note"]


def test_zero_room_blocks_buys_but_not_sells(tmp_path, monkeypatch):
    """익스포저를 이미 다 쓴 상태면 신규 매수는 없다(청산은 익스포저와 무관하게 진행)."""
    monkeypatch.chdir(tmp_path)
    _setup(monkeypatch, [{"ticker": "AAA", "name": "가"}, {"ticker": "BBB", "name": "나"}],
           {"AAA": [100.0, 100.0, 90.0], "BBB": [100.0]},
           [_sig("AAA", "가", "HOLD", 0.0), _sig("BBB", "나", "BUY", 2.0)],
           mode="rank", exposure=0.15, min_buy_score=0.0)
    _seed(0.0, {"AAA": {"name": "가", "qty": 10, "avg_price": 100.0}})
    out = bot.run_once(UID)
    assert out["buys"] == []                        # 현금 0 + 익스포저 여유 0
    assert out["sells"][0]["reason"] == "STOP_LOSS"  # 손절은 그대로


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


def test_conviction_rotation_swaps_weak_for_strong(tmp_path, monkeypatch):
    """포트폴리오가 꽉 찼을 때, 약한 보유(HOLD)를 훨씬 강한 후보(BUY)로 교체."""
    monkeypatch.chdir(tmp_path)
    _setup(monkeypatch, [{"ticker": "WEAK", "name": "약"}, {"ticker": "STRONG", "name": "강"}],
           {"WEAK": [100.0, 100.0], "STRONG": [50.0, 50.0]},
           [_sig("WEAK", "약", "HOLD", 0.2), _sig("STRONG", "강", "BUY", 2.0)],
           max_positions=1, min_buy_score=1.0)  # 슬롯 1개 → WEAK 보유로 꽉 참
    _seed(0.0, {"WEAK": {"name": "약", "qty": 100, "avg_price": 100.0}})
    db.bot_position_upsert(UID, "WEAK", "약", 100, 100.0, 100.0, "2020-01-01")  # 최소 보유일 경과

    out = bot.run_once(UID)
    assert out["ok"]
    assert any(s["reason"] == "ROTATE_OUT" and s["ticker"] == "WEAK" for s in out["sells"])
    assert any(b["reason"] == "ROTATE_IN" and b["ticker"] == "STRONG" for b in out["buys"])
    tickers = {p["ticker"] for p in db.bot_positions_all(UID, "kr")}
    assert "WEAK" not in tickers and "STRONG" in tickers  # 교체 완료


def test_rotation_skips_within_min_hold(tmp_path, monkeypatch):
    """최소 보유일 미달이면 더 강한 후보가 있어도 교체하지 않는다(잦은 교체 방지)."""
    monkeypatch.chdir(tmp_path)
    _setup(monkeypatch, [{"ticker": "WEAK", "name": "약"}, {"ticker": "STRONG", "name": "강"}],
           {"WEAK": [100.0, 100.0], "STRONG": [50.0, 50.0]},
           [_sig("WEAK", "약", "HOLD", 0.2), _sig("STRONG", "강", "BUY", 2.0)],
           max_positions=1, min_buy_score=1.0)
    _seed(0.0, {"WEAK": {"name": "약", "qty": 100, "avg_price": 100.0}})
    db.bot_position_upsert(UID, "WEAK", "약", 100, 100.0, 100.0, bot._today())  # 오늘 진입 → 보유일 0

    out = bot.run_once(UID)
    assert not any(s["reason"] == "ROTATE_OUT" for s in out["sells"])   # 교체 없음
    assert {p["ticker"] for p in db.bot_positions_all(UID, "kr")} == {"WEAK"}


def _evsig(ticker, name, severity, note="악재"):
    return SignalResult(ticker=ticker, name=name, score=0.0, kind="HOLD", confidence=0.5,
                        technical_score=0.0, fundamental_score=0.0, has_fundamental=False,
                        reasons=[], event_risk=True, event_note=note, event_severity=severity)


def test_event_critical_sells_full(tmp_path, monkeypatch):
    """critical 악재(상장폐지 등) → 보유 전량 즉시 청산(가격 하락 전이라도)."""
    monkeypatch.chdir(tmp_path)
    _setup(monkeypatch, [{"ticker": "AAA", "name": "가"}], {"AAA": [100.0, 100.0]},
           [_evsig("AAA", "가", "critical", "상장폐지 — 감사의견 거절")])
    _seed(0.0, {"AAA": {"name": "가", "qty": 10, "avg_price": 100.0}})
    out = bot.run_once(UID)
    assert any(s["reason"] == "EVENT" and s["qty"] == 10 for s in out["sells"])
    assert db.bot_positions_all(UID, "kr") == []  # 전량 청산


def test_event_serious_trims_half(tmp_path, monkeypatch):
    """serious 악재(어닝쇼크 등) → 절반만 부분 청산, 나머지는 보유 유지."""
    monkeypatch.chdir(tmp_path)
    _setup(monkeypatch, [{"ticker": "AAA", "name": "가"}], {"AAA": [100.0, 100.0]},
           [_evsig("AAA", "가", "serious", "어닝쇼크 — 4분기 적자")])
    _seed(0.0, {"AAA": {"name": "가", "qty": 10, "avg_price": 100.0}})
    out = bot.run_once(UID)
    assert any(s["reason"] == "EVENT_TRIM" and s["qty"] == 5 for s in out["sells"])
    pos = db.bot_positions_all(UID, "kr")
    assert len(pos) == 1 and pos[0]["qty"] == 5  # 절반 잔여


def test_rotation_conservative_keeps_low_buy(tmp_path, monkeypatch):
    """안정형(only_cooled): 아직 BUY인 보유는 순위 낮아도 교체 안 함(식은 것만 청산 후보)."""
    monkeypatch.chdir(tmp_path)
    _setup(monkeypatch, [{"ticker": "LOWBUY", "name": "약매수"}, {"ticker": "STRONG", "name": "강"}],
           {"LOWBUY": [100.0, 100.0], "STRONG": [50.0, 50.0]},
           [_sig("LOWBUY", "약매수", "BUY", 1.3), _sig("STRONG", "강", "BUY", 2.6)],
           trading_style="conservative", max_positions=1, min_buy_score=1.0)
    _seed(0.0, {"LOWBUY": {"name": "약매수", "qty": 100, "avg_price": 100.0}})
    db.bot_position_upsert(UID, "LOWBUY", "약매수", 100, 100.0, 100.0, "2020-01-01")
    out = bot.run_once(UID)
    assert not any(s["reason"] == "ROTATE_OUT" for s in out["sells"])   # BUY라 유지
    assert {p["ticker"] for p in db.bot_positions_all(UID, "kr")} == {"LOWBUY"}


def test_rotation_aggressive_smaller_gap(tmp_path, monkeypatch):
    """공격형(min_gap 0.6): 균형형(1.0)이면 안 갈 격차(0.7)에서도 교체."""
    monkeypatch.chdir(tmp_path)
    _setup(monkeypatch, [{"ticker": "WEAK", "name": "약"}, {"ticker": "STRONG", "name": "강"}],
           {"WEAK": [100.0, 100.0], "STRONG": [50.0, 50.0]},
           [_sig("WEAK", "약", "HOLD", 0.5), _sig("STRONG", "강", "BUY", 1.2)],  # 격차 0.7
           trading_style="aggressive", max_positions=1, min_buy_score=1.0)
    _seed(0.0, {"WEAK": {"name": "약", "qty": 100, "avg_price": 100.0}})
    db.bot_position_upsert(UID, "WEAK", "약", 100, 100.0, 100.0, "2020-01-01")
    out = bot.run_once(UID)
    assert any(s["reason"] == "ROTATE_OUT" and s["ticker"] == "WEAK" for s in out["sells"])
    assert "STRONG" in {p["ticker"] for p in db.bot_positions_all(UID, "kr")}
