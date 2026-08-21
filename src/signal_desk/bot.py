"""자동매매봇 — 공용 시그널 → 리스크 판정 → 유저별 자체 모의계좌(paper) 가상 체결.

멀티테넌트: 시그널·국면·거시 '판단'은 공용(사이클당 1회 계산해 전 유저 공유), 계좌·on/off·성향·
실행·리스크·사이징은 유저별. 각 유저는 자기 페이퍼 계좌(현금·보유·거래내역)를 갖고 원하는 시점에
켜고 끄고 초기화하고 시드를 바꾼다. KIS 실계좌 연동은 제거(단일 계정이라 유저별 격리 불가).

paper 계좌가 포지션의 진실원천(broker.paper). peak_price·entry_date만 트레일링스탑용으로
bot_positions(uid)에 따로 보관한다(paper 잔고에서 매 회차 reconcile).
"""

from __future__ import annotations

import datetime
import logging
from zoneinfo import ZoneInfo

from signal_desk import config, db, kb, llm, signalcfg, store, strategy
from signal_desk.broker import paper
from signal_desk.reference import cycle, us_ko
from signal_desk.signals import (
    advisor, advisor_shadow, engine, execution_gate, macro, regime, risk, vol_sizing,
)
from signal_desk.signals import decision as decmod

log = logging.getLogger("signal_desk.bot")

_KST = ZoneInfo("Asia/Seoul")
_MARKET_OPEN = datetime.time(9, 0)
_MARKET_CLOSE = datetime.time(15, 20)  # 동시호가 등 마감 직전 여유 두고 컷오프
_OUTCOME_AGE_SEC = 3 * 24 * 3600  # 의사결정 후 3일 지나면 사후수익 확정(학습 재료)


def is_market_hours(now: datetime.datetime | None = None) -> bool:
    """평일 09:00~15:20(KST)만 True — 참고용(paper는 종가 기준이라 장 시간 무관하게 돈다)."""
    now = now or datetime.datetime.now(_KST)
    if now.weekday() >= 5:  # 토(5)/일(6)
        return False
    return _MARKET_OPEN <= now.time() <= _MARKET_CLOSE


def _today() -> str:
    return datetime.date.today().isoformat()


def _cfg(uid: int) -> dict:
    """유저 봇 설정 + 성향 프리셋 숫자 파라미터(max_positions/position_pct/min_buy_score/max_new_buys_per_run)."""
    u = db.user_bot_get(uid)
    return {**u, **strategy.bot_params(u["trading_style"])}


def _daily_loss_breached(uid: int, bal: dict, dry_run: bool, market: str = "kr") -> bool:
    """유저의 당일 시작 평가액 대비 손실 한도 초과 여부(시장별). 초과면 신규 매수 중단(리스크 청산은 유지)."""
    total = bal.get("total_eval")
    if not total or total <= 0:
        return False
    key = f"bot_day_equity:{uid}:{market}:{_today()}"
    start = db.kv_get(key)
    if start is None:
        if not dry_run:
            db.kv_set(key, total)  # 당일 기준선 기록
        return False
    limit = config.bot_daily_loss_limit_pct()
    breached = total < float(start) * (1 - limit)
    if breached:
        log.warning("일일 손실 한도 초과 — 신규 매수 중단(시작 %.0f → 현재 %.0f, 한도 -%.0f%%)",
                    float(start), total, limit * 100)
    return breached


# 미국 정규장(대략) — 서머타임 EDT 기준 22:30~05:00 KST, EST면 23:30~06:00. 넉넉히 22:30~06:00로 근사.
_US_OPEN = datetime.time(22, 30)
_US_CLOSE = datetime.time(6, 0)

def is_us_market_hours(now: datetime.datetime | None = None) -> bool:
    """미국 정규장 시간(KST 근사 22:30~06:00, 미 평일)인지. 자정을 넘기므로 두 구간으로 판정.
    실주문 야간 루프 게이트용(현재 US 실주문 미연결 — 미리보기만)."""
    now = now or datetime.datetime.now(_KST)
    t, wd = now.time(), now.weekday()
    if t >= _US_OPEN:      # KST 밤(당일 저녁) = 미국장 시작 → 미 평일이면 KST 월~금
        return wd < 5
    if t <= _US_CLOSE:     # KST 새벽 = 전날 미국장 연장 → KST 화~토 새벽
        return 1 <= wd <= 5
    return False


def us_signals() -> list:
    """US(S&P500) 시그널 — api._us_signals 캐시 재사용(장중 봇+API 동시 evaluate로 OOM 나는 것 방지).
    순환 import 피하려고 함수 내부에서 api를 가져온다. 점수 내림차순."""
    from signal_desk import api
    return sorted(api._us_signals().values(), key=lambda s: s.score, reverse=True)


def _live_price(ticker: str, fallback: float) -> float:
    """현재가(가격캐시 종가). paper는 종가 기준이라 캐시가 곧 체결가. 없으면 fallback."""
    live = paper.current_price(ticker)
    return live if live else fallback


def _market_read(prices: dict[str, list[float]]) -> dict:
    """시장 국면 단일 스냅샷 — 한 사이클에 한 번만 계산해 공유(중복 계상·중복 계산 방지).

    거시(FRED)·국면(국내 breadth)은 여기서 '매수 임계값 게이트'로 딱 한 번 반영된다(eff_cfg).
    context(regime/macro/cycle)는 LLM·저널에 넘기는 '참고 맥락'일 뿐, 게이트에서 이미 반영됐으므로
    LLM이 이를 근거로 재차 감점하지 않도록 advisor 프롬프트가 명시한다(이중 반영 방지)."""
    reg = regime.classify(prices)
    macro_ind = store.load_macro()
    mread = macro.read(macro_ind)
    cyc = cycle.position(macro_ind)
    eff_cfg, adapt = signalcfg.effective_config(reg, mread, flow_result=store.load_market_flow())
    macro_dg = kb.macro_digest()
    cfg_snap = signalcfg.get_dict()
    # 설정 지문 — 의사결정 저널이 '어떤 가중/임계로 샀는지' 사후 추적(전체 dict는 비대하니 핵심만)
    config_fp = {
        "buy_threshold": cfg_snap.get("buy_threshold"),
        "strong_buy_threshold": cfg_snap.get("strong_buy_threshold"),
        "regime_adaptive": cfg_snap.get("regime_adaptive"),
        "w_mom": cfg_snap.get("weight_momentum"),
        "w_flow": cfg_snap.get("weight_flow"),
        "w_short": cfg_snap.get("weight_short"),
        "w_fund": cfg_snap.get("weight_fundamental"),
    }
    context = {
        "regime": reg.get("regime"),
        "macro_bias": mread.get("bias"),
        # FRED 정량 지표 근거(CPI·금리·나스닥·VIX) — KB엔 안 넣되 LLM이 시그널 판단 시 지표로 참고
        "macro_detail": " / ".join((mread.get("reasons") or [])[:5]),
        "cycle_phase": cyc.get("phase_name"),
        "gate_applied": bool(adapt.get("bump")),  # 매수 기준이 이미 상향됐는지(LLM에 알림)
        "effective_buy_threshold": adapt.get("effective_buy_threshold"),
        "bump": adapt.get("bump") or 0.0,
        "bump_reasons": list(adapt.get("reasons") or []),
        # rank 모드: 국면은 문턱이 아니라 총 익스포저로 반영된다(자격 대신 크기)
        "selection_mode": adapt.get("mode"),
        "exposure": adapt.get("exposure"),
        "exposure_reasons": list(adapt.get("exposure_reasons") or []),
        "config_fp": config_fp,
        # 미주은 시황 코멘터리(정성 내러티브) — 참고용 맥락, 개별 종목 점수엔 미반영
        "macro_note": (macro_dg["summary"] if macro_dg and macro_dg.get("fresh") else ""),
    }
    return {"eff_cfg": eff_cfg, "adapt": adapt, "context": context}


# 채점 지평(거래일). 이 값을 고정해야 base rate 를 같은 관례로 만들 수 있다.
OUTCOME_HORIZON_DAYS = 3


def _update_decision_outcomes(prices: dict[str, list[float]]) -> None:
    """과거 매수 판단의 사후수익을 **고정 지평**으로 확정한다.

    2026-08-05 진단: 옛 코드가 `closes[-1]`(오늘 종가)을 썼다. 그러면 보유 기간이 "채점 루프가
    실제로 돌 때까지"가 되어 판단마다 달라진다(실측 3.0~6.1 달력일). **지평이 섞인 비율에는
    비교 가능한 base rate 를 붙일 수 없다** — 승률 42.3% 옆에 아무 기준선도 못 적은 이유였다.

    이제 `판단일 다음 거래일 종가 진입 → OUTCOME_HORIZON_DAYS 거래일 뒤 종가 청산`으로 잰다.
    `accuracy.py` 와 같은 관례이므로 유니버스 base rate 와 직접 비교된다. 진입가로 `decided_price`
    (장중 체결가)를 쓰지 않는 이유도 그것이다 — 유니버스 쪽에 대응물이 없다.
    청산일 종가가 아직 없으면 **건너뛴다**(대기). 지금 채점하려고 오늘 종가를 쓰면 옛 버그다.
    """
    dated = store.load_all_dated_closes()
    for d in db.bot_decisions_recent(120):
        if d.get("outcome_pct") is not None or d.get("action") != "buy":
            continue
        pair = dated.get(d["ticker"])
        if not pair:
            continue
        dates, closes = pair
        dec_date = datetime.datetime.fromtimestamp(d["ts"], _KST).strftime("%Y-%m-%d")
        # 판단일 이후 첫 거래일이 진입, 그로부터 h거래일 뒤가 청산.
        entry_i = next((i for i, dt in enumerate(dates) if dt > dec_date), None)
        if entry_i is None:
            continue
        exit_i = entry_i + OUTCOME_HORIZON_DAYS
        if exit_i >= len(dates):
            continue                                  # 아직 안 익었다 — 다음 실행에서
        entry, exit_px = closes[entry_i], closes[exit_i]
        if not entry or not exit_px:
            continue
        _set_outcome_by_match(d, (exit_px / entry - 1) * 100,
                              horizon_days=OUTCOME_HORIZON_DAYS,
                              entry_date=dates[entry_i], exit_date=dates[exit_i])


def _set_outcome_by_match(decision: dict, outcome_pct: float, *, horizon_days: int,
                          entry_date: str, exit_date: str) -> None:
    """decisions_recent가 id를 안 주므로, ticker+ts로 정확히 한 건 갱신.

    `horizon_days`·`entry_date`·`exit_date`를 함께 남긴다 — 지평이 없는 행은 스코어카드가
    리프트 계산에서 뺀다(비교 대상이 없는 비율이라).
    """
    c = db.conn()
    c.execute("UPDATE bot_decisions SET outcome_pct=?, outcome_ts=?, horizon_days=?, "
              "entry_date=?, exit_date=? WHERE ticker=? AND ts=? AND outcome_pct IS NULL",
              (round(outcome_pct, 2), int(datetime.datetime.now(_KST).timestamp()),
               int(horizon_days), entry_date, exit_date, decision["ticker"], decision["ts"]))
    c.commit()
    c.close()


def _sell_note(reason: str, qty: int, avg_price: float, current_price: float,
               pl_pct: float, risk_cfg: "risk.RiskConfig") -> str:
    """매도 사유를 사람이 읽는 한 줄 근거로. 왜 지금(타이밍)·얼마나(수량)를 함께 남긴다."""
    trigger = {
        "STOP_LOSS": f"손절선 {risk_cfg.stop_loss_pct * 100:.0f}% 이탈",
        "TAKE_PROFIT": f"익절선 +{risk_cfg.take_profit_pct * 100:.0f}% 도달",
        "TRAILING": f"고점 대비 {risk_cfg.trailing_from_peak_pct * 100:.0f}% 되돌림(트레일링)",
        "SIGNAL": "시그널 SELL 전환",
    }.get(reason, reason)
    return (f"{trigger} — 평단 {int(avg_price):,}원 → 현재 {int(current_price):,}원"
            f"({pl_pct:+.1f}%), 보유 전량 {qty}주 청산")


def reconcile_positions(uid: int, bal: dict, market: str = "kr") -> None:
    """유저 bot_positions(시장별)를 paper 잔고에 맞춘다 — 종목·수량·평단은 paper로 덮어쓰고, 트레일링용
    peak_price·entry_date만 유지(paper엔 없음). paper에 없는 포지션은 삭제. 현재가·수익률 스냅샷도 갱신."""
    ph = {h["ticker"]: h for h in bal.get("holdings", [])}
    for t in {p["ticker"] for p in db.bot_positions_all(uid, market)} - set(ph):
        db.bot_position_delete(uid, t)
    for t, h in ph.items():
        pos = db.bot_position_get(uid, t)
        price = h.get("price") or 0.0
        peak = max(pos["peak_price"] if pos else h["avg_price"], h["avg_price"], price)
        entry = pos["entry_date"] if pos else _today()
        db.bot_position_upsert(uid, t, h["name"], h["qty"], h["avg_price"], peak, entry,
                               last_price=price or None, last_pnl_pct=h.get("pnl_pct"), market=market)


def snapshot_positions(uid: int, market: str = "kr") -> bool:
    """유저 보유종목 현재가·수익률 스냅샷 갱신(시장별) — paper 잔고로 reconcile."""
    reconcile_positions(uid, paper.balance(uid, market), market)
    return True


def ledger_state(style: str = "balanced", market: str = "kr") -> dict:
    """트레이딩(성향별 레퍼런스 봇) 상태 — 현금·평가금액·보유종목·최근거래.

    개인 페이퍼 계좌는 제거됐다(2026-07-27). 리셋·시드 변경이 가능한 장부는 track record가 아니다:
    성적이 나쁘면 초기화하면 그만이라 남은 장부는 항상 좋아 보인다. 트레이딩은 리셋 불가·시드
    고정이라 그 편향이 없다."""
    ensure_reference_bots()
    style = strategy.normalize(style)
    uid = next((u for u, s in REFERENCE_BOTS.items() if s == style), None)
    if uid is None:
        return {"error": f"알 수 없는 성향: {style}"}
    return {**_state(uid, market), "style": style,
            "label": strategy.STYLE_LABEL.get(style, style)}


def _return_block(bal: dict, seed: float | None) -> dict:
    """시드 대비 **총수익률**과 실현·평가 분해. 장부의 헤드라인은 이것이어야 한다.

    total_eval = cash + stock_eval 이므로
        총손익   = total_eval − seed
        평가손익 = stock_eval − invested        (= bal["pnl"], 보유분만)
        실현손익 = 총손익 − 평가손익 = cash + invested − seed

    실현손익을 안 내면 손절이 장부에서 사라진다 — 이 리포가 백테스트에서 경계하는 생존편향과
    같은 병이 장부에서 재발한 것이다("리셋할 수 있는 장부는 track record가 아니다").
    """
    seed = float(seed or 0.0)
    if not seed:
        return {"total_return_pct": None, "total_pnl": None, "realized_pnl": None,
                "unrealized_pnl": bal.get("pnl")}
    total = float(bal.get("total_eval") or 0.0)
    unreal = float(bal.get("pnl") or 0.0)
    total_pnl = total - seed
    return {
        "total_return_pct": round(total_pnl / seed * 100, 2),
        "total_pnl": round(total_pnl, 2),
        "realized_pnl": round(total_pnl - unreal, 2),
        "unrealized_pnl": round(unreal, 2),
    }


def _state(uid: int, market: str = "kr") -> dict:
    """봇 계좌 종합 상태(시장별) — 설정/현금·평가금액/보유종목/최근거래."""
    cfg = _cfg(uid)
    bal = paper.balance(uid, market)
    reconcile_positions(uid, bal, market)
    seed_cash = cfg["seed_cash_us"] if market == "us" else cfg["seed_cash"]
    return {
        "enabled": cfg["enabled"],
        "config": cfg,
        "market": market,
        "currency": "USD" if market == "us" else "KRW",
        "seed_cash": seed_cash,
        "cash": bal["cash"],
        "total_eval": bal["total_eval"],
        "stock_eval": bal.get("stock_eval"),
        "invested": bal.get("invested"),
        # **장부는 실현손실까지 세야 한다.** `bal["pnl"]` 은 **지금 들고 있는 것**의 평가손익뿐이라,
        # 손실을 확정하고 팔면 그 손실이 `pnl` 에서 통째로 사라지고 남은 승자가 비율을 올린다 —
        # 리셋 버튼 없이도 장부가 저절로 좋아 보이는 **생존편향**이다. 실측(2026-08-16, 균형형):
        # 시드 1,000만 → 총평가 965만(**−3.48%**) 인데 카드에는 **+5.75%** 가 떠 있었다.
        # 그리고 같은 계좌가 `/api/reference-performance` 에서는 −3.48%로 나왔다 — 같은 것을
        # 두 곳에서 조립하면 갈라지고, 그 차이는 어느 화면에도 안 뜬다.
        "pnl": bal.get("pnl"),                 # 평가손익(보유분) — 보조 지표
        "pnl_pct": bal.get("pnl_pct"),         # 평가손익률(분모=매입금액) — 보조 지표
        **_return_block(bal, seed_cash),
        "positions": db.bot_positions_all(uid, market),
        "recent_trades": db.bot_trades_recent(uid, 20, market),
        "reservations": db.bot_reservations_pending(uid, market),
        "llm_enabled": llm.available(),
        "style_label": strategy.STYLE_LABEL.get(cfg["trading_style"], cfg["trading_style"]),
        "styles": [{"key": k, "label": strategy.STYLE_LABEL[k], "desc": strategy.STYLE_DESC[k]} for k in strategy.STYLES],
        "rotation": strategy.rotation_params(cfg["trading_style"]),
        "kill_switch": config.bot_kill_switch(),
        "daily_loss_limit_pct": config.bot_daily_loss_limit_pct(),
    }


def set_style(uid: int, style: str) -> str:
    """봇 성향 지정(레퍼런스 봇 부트스트랩·테스트 전용). 외부 노출 경로는 없다."""
    style = strategy.normalize(style)
    db.user_bot_set_style(uid, style)
    return style


def reset(uid: int) -> None:
    """봇 계좌 초기화 — 포지션·거래·예약·일일기준선 삭제 + 페이퍼 현금 시드로 리셋.
    트레이딩에는 이 경로가 노출되지 않는다(리셋 가능한 장부는 증거가 아니다)."""
    db.bot_reset(uid)


_MAX_CHASE_PCT = 0.02  # 지정가 상한(종가 대비 +2%) — 표시·계획용(paper는 종가 즉시 체결)


def _market_signals(market: str, mr: dict):
    """(universe, prices, signals, name_by_ticker) — 시장별. kr은 재무+국면게이트, us는 us_signals."""
    if market == "us":
        prices = store.load_us_price_series()
        us_uni = store.load_us_universe()
        sigs = us_signals()  # engine.evaluate(us universe, us prices, sentiment) — 재무 없음
        names = {u["ticker"]: us_ko.name_ko(u["ticker"], u["name"]) for u in us_uni}
        return us_uni, prices, sigs, names
    universe = store.load_universe()
    prices = store.load_price_series()
    fundamentals = store.load_fundamentals()
    # 입력은 UI(api._signals)와 같은 한 벌을 쓴다(store.kr_engine_inputs) — 따로 나열하면
    # 한쪽에만 팩터가 빠져 화면의 '매수 후보'와 실제 매수가 갈라진다.
    sigs = engine.evaluate(universe, prices, fundamentals, config=mr["eff_cfg"],
                           **store.kr_engine_inputs())
    execution_gate.apply_from_store(sigs, market="kospi", today=_today())
    return universe, prices, sigs, {u["ticker"]: u["name"] for u in universe}


def recent_sold_tickers(uid: int, market: str, style: str) -> set[str]:
    """쿨다운 중인 종목 — **방금 판 것을 다시 사지 않는다**(핑퐁 방지).

    `strategy.ROTATION_PRESETS[*]["cooldown_days"]` 가 이미 3·5·7일로 정해져 있었는데
    **로테이션 경로에만 걸려 있었다**(2026-08-22 실측). 일반 매수 경로에는 없어서:

        08-21 13:31  sell LyondellBasell @66.06 TRAILING
        08-21 13:31  buy  LyondellBasell @66.06 SIGNAL     ← 같은 분, 쿨다운 7일 무시

    트레일링·손절은 제대로 작동한다 — 문제는 **팔자마자 점수가 그대로라 다시 사는 것**이다.
    손실을 확정하고 왕복 비용까지 물면서 같은 자리로 돌아간다.

    이 리포가 반복해서 겪은 "게이트가 한 경로에만 걸려 있다"의 재발이라, 계산을 **여기 한
    곳**으로 모으고 후보를 고르는 모든 자리가 이 함수를 부르게 한다(레드팀이 대조한다).
    """
    days = strategy.rotation_params(style)["cooldown_days"]
    if not days:
        return set()
    now_ts = int(datetime.datetime.now(_KST).timestamp())
    horizon = days * 24 * 3600
    return {t["ticker"] for t in db.bot_trades_recent(uid, 200, market)
            if t["side"] == "sell" and (now_ts - t["ts"]) < horizon}


def _conviction_rotate(uid, market, signals, signal_by_ticker, holdings, held_after,
                       cash, tranche_alloc, tranches, cfg, name_by_ticker, prices, unit,
                       sells, buys, rotated_out, dry_run, rp):
    """약한 보유 → 더 강한 후보 교체. rp=성향별 로테이션 정책. 갱신된 cash 반환.
    sells/buys/held_after/rotated_out 갱신."""
    warned = store.load_warned_tickers() if market == "kr" else set()
    recent_sold = recent_sold_tickers(uid, market, cfg["trading_style"])
    cand = sorted([s for s in signals if engine.is_buy(s.kind) and s.ticker not in held_after
                   and not s.event_risk and s.ticker not in warned
                   and s.score >= cfg["min_buy_score"] and s.ticker not in recent_sold],
                  key=lambda s: s.score, reverse=True)
    if not cand:
        return cash

    today = datetime.date.today()
    weak = []  # (score, holding, live_price) — 교체 가능한 약한 보유
    for h in holdings:
        sig = signal_by_ticker.get(h["ticker"])
        if sig is None:
            continue  # 유니버스 밖 — 판단 불가, 유지
        if rp["only_cooled"] and engine.is_buy(sig.kind):
            continue  # 아직 BUY면 순위 낮아도 유지(식은 것만 청산 후보 — 안정형)
        pos = db.bot_position_get(uid, h["ticker"])
        entry = pos["entry_date"] if pos else None
        if entry:
            try:
                if (today - datetime.date.fromisoformat(entry)).days < rp["min_hold_days"]:
                    continue  # 최소 보유일 미달 → 유지
            except ValueError:
                pass
        live = _live_price(h["ticker"], (prices.get(h["ticker"]) or [h["avg_price"]])[-1])
        pnl = (live / h["avg_price"] - 1) if h["avg_price"] else 0.0
        if pnl < rp["max_loss_pct"]:
            continue  # 큰 손실 중 → 손절선에 맡기고 교체 제외(손실 확정 회피)
        weak.append((sig.score, h, live))
    weak.sort(key=lambda x: x[0])

    n_rot = 0
    for best in cand:
        if n_rot >= rp["max_per_run"] or not weak:
            break
        weak_score, wh, wlive = weak[0]
        if best.score - weak_score < rp["min_gap"]:
            break  # 격차 부족(정렬돼 있으니 이후 후보도 부족) → 중단
        wt, wqty = wh["ticker"], wh["qty"]
        pl_pct = (wlive / wh["avg_price"] - 1) * 100 if wh["avg_price"] else 0
        bname = name_by_ticker.get(best.ticker, best.name)
        snote = (f"컨빅션 로테이션 — 보유 점수 {weak_score:+.2f} 약화, {bname}({best.score:+.2f})로 교체 · "
                 f"평단 {int(wh['avg_price']):,}→현재 {int(wlive):,}{unit}({pl_pct:+.1f}%) {wqty}주 청산")
        splan = {"ticker": wt, "name": wh["name"], "qty": wqty, "reason": "ROTATE_OUT", "note": snote, "price": wlive}
        if not dry_run:
            if paper.place_order(uid, wt, "sell", wqty, price=wlive, market=market) is None:
                weak.pop(0)
                continue
            db.bot_trade_log(uid, wt, wh["name"], "sell", wqty, wlive, "ROTATE_OUT", "PAPER",
                             score=weak_score, note=snote, market=market)
            db.bot_position_delete(uid, wt)
            splan["ok"] = True
        cash += wqty * wlive
        sells.append(splan)
        rotated_out.add(wt)
        held_after.discard(wt)

        blive = _live_price(best.ticker, (prices.get(best.ticker) or [0])[-1])
        alloc = min(tranche_alloc, cash)
        bqty = int(alloc // blive) if blive else 0
        if bqty >= 1:
            bnote = (f"컨빅션 로테이션 진입 — 점수 {best.score:+.2f}(교체된 보유 대비 +{best.score - weak_score:.2f}) · "
                     f"1/{tranches}트랜치(약 {int(alloc):,}{unit}) ÷ {int(blive):,}{unit} = {bqty}주")
            bplan = {"ticker": best.ticker, "name": bname, "qty": bqty, "price": blive,
                     "reason": "ROTATE_IN", "note": bnote, "score": best.score, "ai": False}
            if not dry_run:
                if paper.place_order(uid, best.ticker, "buy", bqty, price=blive, name=best.name, market=market) is not None:
                    db.bot_trade_log(uid, best.ticker, bname, "buy", bqty, blive, "ROTATE_IN", "PAPER",
                                     score=best.score, note=bnote, market=market)
                    db.bot_position_upsert(uid, best.ticker, bname, bqty, blive, blive, _today(), market=market)
                    bplan["ok"] = True
                else:
                    bplan["ok"] = False
            cash -= bqty * blive
            buys.append(bplan)
            held_after.add(best.ticker)
        weak.pop(0)
        n_rot += 1
    return cash


def run_once(uid: int, dry_run: bool = False, market: str = "kr",
             sells_only: bool = False) -> dict:
    """유저 한 사이클 실행(시장별 페이퍼 계좌) — 공용 시그널로 매매. market: 'kr'|'us'.
    dry_run=True면 주문/DB기록 없이 '무엇을 왜 매매할지' 계획만 계산(미리보기).

    `sells_only=True` 면 **보유 점검(손절·트레일링·목표가·시그널 매도)만** 하고 매수 단계를
    통째로 건너뛴다. 빠른 틱(5분)이 쓰는 모드다.

    **왜 나누나** — 두 단계의 성질이 다르다:

    - 매도·손절·트레일링은 **가격에 반응**한다. 자주 볼수록 실익이 있고 비용은 0이다
      (브로커 시세는 어차피 받는다).
    - 매수 선별은 `advisor`(Opus)를 부르고, 그 입력인 점수는 **일봉 종가 기반**이라 5분마다
      다시 계산해도 후보가 거의 그대로다. 자주 부르면 **같은 판단에 돈만 더 낸다.**

    즉 "봇을 더 자주 돌린다"를 통째로 하면 유료 부분이 배수로 늘어난다. 나누면 실익만 가져간다.
    """
    if not dry_run and config.bot_kill_switch():
        return {"ok": False, "reason": "긴급정지(BOT_KILL_SWITCH) 활성 — 주문을 내지 않습니다."}
    unit = "$" if market == "us" else "원"

    bal = paper.balance(uid, market)
    if not dry_run:
        reconcile_positions(uid, bal, market)  # bot_positions(peak·entry) 미러를 paper 실측과 일치(stale 정리)
    block_new_buys = _daily_loss_breached(uid, bal, dry_run, market)

    kr_prices = store.load_price_series()
    mr = _market_read(kr_prices) if kr_prices else {"eff_cfg": None, "context": {}}  # 공용 국면(거시·advisor 참고)
    universe, prices, signals, name_by_ticker = _market_signals(market, mr)
    if not universe or not prices:
        return {"ok": False, "reason": "시세 데이터 없음 — /api/refresh 먼저 호출 필요"}
    signal_by_ticker = {s.ticker: s for s in signals}
    if not dry_run and market == "kr":
        _update_decision_outcomes(prices)  # 과거 결정 사후수익 확정(공용 학습, 국내 기준)

    cfg = _cfg(uid)
    risk_cfg = strategy.risk_config(cfg["trading_style"], mr["context"].get("regime"))
    sells: list[dict] = []
    for h in bal["holdings"]:
        ticker, qty, avg_price = h["ticker"], h["qty"], h["avg_price"]
        closes = prices.get(ticker)
        if not closes:
            continue  # 유니버스 밖 종목 — 봇 판단 대상 아님
        current_price = _live_price(ticker, closes[-1])
        pos = db.bot_position_get(uid, ticker)
        peak = max(pos["peak_price"] if pos else avg_price, current_price)
        sig = signal_by_ticker.get(ticker)

        # Decision 정책 청산(최우선) — confirmed+eligible 이벤트만(P2).
        # exit=전량, trim=절반. 그 외엔 아래 리스크/시그널.
        sell_qty, reason = qty, None
        dec = getattr(sig, "decision", None) if sig else None
        if dec is None and sig:
            dec = decmod.decision_from_legacy(
                event_risk=sig.event_risk, event_severity=sig.event_severity,
                event_note=sig.event_note)
        if dec and dec.holding_action == "exit":
            reason, sell_qty = "EVENT", qty
        elif dec and dec.holding_action == "trim":
            reason, sell_qty = "EVENT_TRIM", max(1, qty // 2)
        if not reason:
            reason = risk.check_exit(avg_price, current_price, peak, risk_cfg)
        if not reason and sig and engine.is_sell(sig.kind):
            reason = "SIGNAL"

        if reason:
            pl_pct = (current_price / avg_price - 1) * 100 if avg_price else 0
            if reason in ("EVENT", "EVENT_TRIM"):
                note = (f"{decmod.decision_reason(dec)} · "
                        f"평단 {int(avg_price):,}→현재 {int(current_price):,}{unit}({pl_pct:+.1f}%), {sell_qty}주")
            else:
                note = _sell_note(reason, sell_qty, avg_price, current_price, pl_pct, risk_cfg)
            plan = {"ticker": ticker, "name": name_by_ticker.get(ticker, ticker), "qty": sell_qty,
                    "reason": reason, "note": note, "price": current_price}
            if not dry_run:
                result = paper.place_order(uid, ticker, "sell", sell_qty, price=current_price, market=market)
                if result is not None:
                    db.bot_trade_log(uid, ticker, plan["name"], "sell", sell_qty, current_price, reason,
                                      result["order_no"], score=sig.score if sig else None, note=note, market=market)
                    if reason in ("EVENT", "EVENT_TRIM") and dec:
                        db.bot_decision_log(
                            ticker, plan["name"], reason, sig.score if sig else None,
                            note,
                            {"event_id": dec.event_id, "policy_version": dec.policy_version,
                             "holding_action": dec.holding_action, "severity": dec.severity,
                             "uid": uid, "qty": sell_qty},
                            current_price,
                        )
                    remaining = qty - sell_qty
                    if remaining > 0:  # 부분청산 — 잔여 포지션 유지(평단·진입일 보존)
                        db.bot_position_upsert(uid, ticker, plan["name"], remaining, avg_price, peak,
                                                pos["entry_date"] if pos else _today(), market=market)
                    else:
                        db.bot_position_delete(uid, ticker)
                    plan["ok"] = True
                else:
                    plan["ok"] = False
            sells.append(plan)
        elif not dry_run:
            db.bot_position_upsert(uid, ticker, name_by_ticker.get(ticker, ticker), qty, avg_price,
                                    peak, pos["entry_date"] if pos else _today(), market=market)

    bal2 = paper.balance(uid, market) if not dry_run else bal
    held_after = {h["ticker"] for h in bal2["holdings"]}
    available_slots = max(0, cfg["max_positions"] - len(held_after))
    # `sells_only` 면 매수 자리를 0으로 둔다. **분기를 새로 만들지 않고 slots=0으로 막는다** —
    # 아래 매수 블록에는 진입 기록·로그·반환 필드가 얽혀 있어서 통째로 건너뛰면 반환 모양이
    # 갈라지고, 빠른 틱과 느린 틱이 **다른 형태의 결과**를 남기게 된다(집계가 어긋난다).
    slots = 0 if (block_new_buys or sells_only) else min(available_slots,
                                                        cfg["max_new_buys_per_run"])

    buys: list[dict] = []
    skipped_weak = 0
    advisor_used = False
    context = mr["context"]
    cash = bal2["cash"]
    target_alloc = bal2["total_eval"] * cfg["position_pct"]
    tranches = strategy.entry_tranches(cfg["trading_style"])  # ① 분할매수 회차
    tranche_alloc = target_alloc / tranches

    # 국면 = '얼마나 살까'. 총 투자금 상한을 국면 익스포저로 정한다(문턱 상향 대신 — 자격을 0으로
    # 만들면 그 국면에서 무엇이 통하는지 배울 수 없다). 하한이 있어 완전 정지는 없다.
    eng_cfg = mr["eff_cfg"] or signalcfg.get_config()
    exposure = float((context or {}).get("exposure") or 1.0)
    invest_cap = bal2["total_eval"] * exposure
    invested = max(0.0, bal2["total_eval"] - cash)
    room = max(0.0, invest_cap - invested)
    if room <= 0:
        slots = 0
    if slots > 0:
        warned = store.load_warned_tickers() if market == "kr" else set()  # 토스 경고 veto(국내)
        if eng_cfg.selection_mode == "rank" and any(s.rank is None for s in signals):
            # 엔진을 안 거쳐 들어온 시그널(외부 조립·테스트)도 같은 기준으로 순위를 매긴다
            engine.apply_cross_sectional(
                sorted(signals, key=lambda s: s.score, reverse=True), eng_cfg)
        # **방금 판 종목은 쿨다운 동안 안 산다.** 이 줄이 없어서 트레일링·손절로 팔고 같은
        # 분에 다시 사는 일이 실제로 벌어졌다(2026-08-21 LyondellBasell sell→buy @66.06,
        # 안정형 쿨다운 7일). 계산은 `recent_sold_tickers` 한 곳에서만 한다 — 로테이션
        # 경로와 갈라지면 한쪽만 고쳐진다.
        cooled = recent_sold_tickers(uid, market, cfg["trading_style"])
        eligible = [s for s in signals if engine.is_buy(s.kind) and s.ticker not in held_after
                    and not s.event_risk and s.ticker not in warned
                    and s.ticker not in cooled]
        if eng_cfg.selection_mode == "rank":
            # 분위 모드: 절대 점수 하한(min_buy_score) 대신 성향별로 매수권을 좁힌다.
            width = strategy.rank_top_pct(cfg["trading_style"], eng_cfg.rank_top_pct)
            width_k = engine.rank_slots(len(signals), width)
            strong = [s for s in eligible if s.rank is not None and s.rank <= width_k]
        else:
            strong = [s for s in eligible if s.score >= cfg["min_buy_score"]]
        skipped_weak = len(eligible) - len(strong)
        pool = sorted(strong, key=lambda s: s.score, reverse=True)[:max(slots * 3, 6)]
        pool_by = {s.ticker: s for s in pool}

        rationale_by = {}
        advice = None
        if pool:
            try:
                g = advisor_shadow.gate(
                    style=cfg.get("trading_style"),
                    summary=advisor_shadow.cached_summary())
            except Exception as e:
                log.warning("advisor gate 계산 실패(%s) — 선별 유지", type(e).__name__)
                g = {"active": True, "fallback": "abstain"}
            advice = advisor.advise(
                [{"ticker": s.ticker, "name": s.name, "score": s.score,
                  "confidence": s.confidence, "reasons": s.reasons} for s in pool],
                context, {t: db.kb_digest_get(t) for t in pool_by},
                advisor.build_lessons(), slots,
                style=cfg.get("trading_style"), gate=g,
            )
        picks = advice.picks if advice else None
        if picks:
            advisor_used = True
            candidates = [pool_by[p["ticker"]] for p in picks if p["ticker"] in pool_by]
            rationale_by = {p["ticker"]: p["rationale"] for p in picks}
        elif picks is None:
            candidates = pool[:slots]      # 사용 불가·kill→score → 결정론적 점수순 폴백
        else:
            # 기권·kill→abstain: 폴백 매수로 뒤집지 않는다.
            advisor_used = True
            candidates = []
        if not dry_run and pool:
            try:
                advisor_shadow.record(
                    uid=uid, market=market, slots=slots, picks=picks,
                    style=cfg.get("trading_style"),
                    pool=[{"ticker": s.ticker, "score": s.score} for s in pool],
                    detail=({
                        "reason": advice.reason, "vetoed": advice.vetoed,
                        "primary": advice.primary, "killed": advice.killed,
                    } if advice else None),
                    outcome_override=(advice.outcome if advice and advice.killed else None),
                )
            except Exception as e:
                log.warning("advisor shadow 기록 실패: %s", type(e).__name__)

        ref_vol = vol_sizing.median_vol(prices, [c.ticker for c in candidates])
        for s in candidates:
            closes = prices.get(s.ticker)
            if not closes:
                continue
            live = _live_price(s.ticker, closes[-1])
            vscale = vol_sizing.scale(vol_sizing.realized_vol(closes), ref_vol)
            alloc = min(tranche_alloc * vscale, cash, room)  # ① 분할∩익스포저 · 고변동↓
            qty = int(alloc // live)
            if qty < 1:
                continue  # 배분금액보다 1주가 비싸면 스킵(정수주 제약)
            basis = (f"시장 상위 {s.rank_pct:.1f}%"
                     if eng_cfg.selection_mode == "rank" and s.rank_pct is not None
                     else f"≥{cfg['min_buy_score']:.1f}")
            vol_note = f" · vol×{vscale:.2f}" if abs(vscale - 1.0) > 0.02 else ""
            quant = (f"점수 {s.score:+.2f}({basis}·신뢰도 {s.confidence:.2f}) · "
                     f"익스포저 {exposure * 100:.0f}%{vol_note} · "
                     f"분할 1/{tranches}트랜치(약 {int(alloc):,}{unit}) ÷ {int(live):,}{unit} = {qty}주")
            llm_reason = rationale_by.get(s.ticker)
            note = (f"[AI] {llm_reason} · {quant}") if llm_reason else quant
            plan = {"ticker": s.ticker, "name": name_by_ticker.get(s.ticker, s.name), "qty": qty, "price": live,
                    "reason": "SIGNAL", "note": note, "score": s.score, "ai": bool(llm_reason)}
            if not dry_run:
                result = paper.place_order(uid, s.ticker, "buy", qty, price=live, name=s.name, market=market)
                if result is not None:
                    db.bot_trade_log(uid, s.ticker, name_by_ticker.get(s.ticker, s.name), "buy", qty, live, "SIGNAL",
                                      result["order_no"], score=s.score, note=note, market=market)
                    db.bot_position_upsert(uid, s.ticker, name_by_ticker.get(s.ticker, s.name), qty, live, live,
                                            _today(), market=market)
                    from signal_desk.signals import pick_reason as _pr
                    buy_ctx = {**(context or {}), "pick": _pr.from_signal(s),
                               "uid": uid, "qty": qty, "market": market}
                    db.bot_decision_log(s.ticker, s.name, "buy", s.score, note, buy_ctx, live)
                    cash -= qty * live
                    room -= qty * live
                    plan["ok"] = True
                else:
                    plan["ok"] = False
            else:
                cash -= qty * live
                room -= qty * live
            buys.append(plan)

    # 매수 0건이어도 한 줄 저널 — 나중에 '왜 안 샀는지'·설정 버전 추적(공용, 유저 무관)
    if slots > 0 and not buys and not dry_run:
        n_buy = sum(1 for s in signals if engine.is_buy(s.kind))
        basis = (f"매수권 상위 {eng_cfg.rank_top_pct}%" if eng_cfg.selection_mode == "rank"
                 else f"유효문턱 {(context or {}).get('effective_buy_threshold')}")
        db.bot_decision_log(
            "-", "(요약)", "idle", None,
            f"매수 체결 0 · BUY시그널 {n_buy}건 · {basis} · 익스포저 {exposure * 100:.0f}%"
            f"(여유 {int(room):,}) · 슬롯 {slots}",
            {**(context or {}), "advisor_used": advisor_used, "skipped_weak": skipped_weak,
             "buy_signals": n_buy, "slots": slots, "exposure": exposure, "room": round(room)},
            0.0,
        )

    # 컨빅션 로테이션 — 약한 보유를 더 강한 후보로 교체. 기준·행동강령은 성향별(strategy.ROTATION_PRESETS).
    # 자리가 꽉 찼을 때(모든 성향), 또는 자리 남아도 현금 부족 시 선제 교체(공격형 when_slots_free).
    rotated_out: set[str] = set()
    rp = strategy.rotation_params(cfg["trading_style"])
    want_rotation = available_slots == 0 or (rp["when_slots_free"] and cash < tranche_alloc)
    if not block_new_buys and want_rotation:
        cash = _conviction_rotate(uid, market, signals, signal_by_ticker, bal2["holdings"], held_after,
                                  cash, tranche_alloc, tranches, cfg, name_by_ticker, prices, unit,
                                  sells, buys, rotated_out, dry_run, rp)

    # ① 분할매수 후속: 보유 중이고 여전히 BUY인데 목표비중 미달인 포지션에 다음 트랜치 추가.
    for h in bal2["holdings"]:
        t = h["ticker"]
        if t in rotated_out:
            continue  # 방금 로테이션으로 청산 → 재매수 금지
        sig = signal_by_ticker.get(t)
        if not (sig and engine.is_buy(sig.kind)) or sig.event_risk:
            continue
        closes = prices.get(t)
        if not closes:
            continue
        avg = h["avg_price"]
        live = _live_price(t, closes[-1])
        value = h["qty"] * live
        if value >= target_alloc * 0.95:       # 이미 목표비중 도달 → 추가 없음
            continue
        if live > avg * (1 + _MAX_CHASE_PCT):   # 평단보다 크게 위면 추격 안 함(다음 눌림에)
            continue
        add_amt = min(tranche_alloc, target_alloc - value, cash)
        qty = int(add_amt // live)
        if qty < 1:
            continue
        note = (f"분할 추가매수(목표 {int(target_alloc):,}{unit} 대비 {int(value):,}{unit}) · "
                f"평단 {int(avg):,}·현재 {int(live):,} · {qty}주")
        plan = {"ticker": t, "name": h["name"], "qty": qty, "price": live,
                "reason": "ADD", "note": note, "score": sig.score, "ai": False}
        if not dry_run:
            result = paper.place_order(uid, t, "buy", qty, price=live, name=h["name"], market=market)
            if result is not None:
                new_qty = h["qty"] + qty
                new_avg = round((h["qty"] * avg + qty * live) / new_qty, 2)
                pos = db.bot_position_get(uid, t)
                db.bot_trade_log(uid, t, h["name"], "buy", qty, live, "ADD", result["order_no"],
                                  score=sig.score, note=note, market=market)
                db.bot_position_upsert(uid, t, h["name"], new_qty, new_avg,
                                        max(pos["peak_price"] if pos else new_avg, live),
                                        pos["entry_date"] if pos else _today(), market=market)
                cash -= qty * live
                plan["ok"] = True
            else:
                plan["ok"] = False
        else:
            cash -= qty * live
        buys.append(plan)

    final_bal = paper.balance(uid, market) if not dry_run else bal2
    if not dry_run:  # 일별 자산 스냅샷(track record 자산곡선) — 같은 날 재실행 시 마지막 값으로 갱신
        db.bot_equity_record(uid, market, _today(), final_bal["total_eval"],
                             final_bal["cash"], final_bal.get("invested") or 0.0)
    return {
        "ok": True, "dry_run": dry_run, "skipped_weak_buys": skipped_weak,
        "skipped_gap_buys": 0, "advisor_used": advisor_used,
        "sells": sells, "buys": buys,
        "cash": final_bal["cash"], "total_eval": final_bal["total_eval"],
        "holdings": len(final_bal["holdings"]),
    }


def performance(uid: int, market: str = "kr") -> dict:
    """봇 track record — 자산곡선 + 총수익률·기간·최대낙폭·거래수. seed 대비 성과(실현+미실현)."""
    curve = db.bot_equity_curve(uid, market)
    cfg = _cfg(uid)
    seed = float(cfg["seed_cash_us"] if market == "us" else cfg["seed_cash"]) or 0.0
    bal = paper.balance(uid, market)
    total = bal["total_eval"]
    ret_pct = round((total / seed - 1) * 100, 2) if seed else None
    # 최대낙폭(자산곡선 기준)
    mdd, peak = 0.0, None
    for p in curve:
        te = p["total_eval"]
        peak = te if peak is None else max(peak, te)
        if peak:
            mdd = min(mdd, te / peak - 1)
    trades = db.bot_trades_recent(uid, 500, market)
    sells = [t for t in trades if t["side"] == "sell"]
    bench = benchmark_return_pct(curve, market)
    return {
        "market": market, "currency": "USD" if market == "us" else "KRW",
        "seed": seed, "total_eval": total, "return_pct": ret_pct,
        # **기준선 없는 수익률은 판정이 아니다.** 실측(2026-08-16) 공격형 +1.58%가 좋아 보였지만
        # 같은 28일 동안 유니버스 평균은 그보다 훨씬 위였다 — 시장을 못 따라간 것이다.
        # 이 리포의 1번 지표 규칙(기준선과 리프트를 항상 함께)을 장부에도 적용한다.
        "benchmark_return_pct": bench,
        "excess_return_pct": (round(ret_pct - bench, 2)
                              if (ret_pct is not None and bench is not None) else None),
        "max_drawdown_pct": round(mdd * 100, 2), "days": len(curve),
        "n_trades": len(trades), "n_sells": len(sells),
        "curve": curve,
    }


def benchmark_return_pct(curve: list[dict], market: str = "kr") -> float | None:
    """봇과 **같은 기간·같은 모집단**의 동일가중 수익률. 못 만들면 None(추측하지 않는다).

    모집단은 그 시장의 유니버스 전체다 — 봇이 고른 종목이 아니라 **고를 수 있었던 종목**이라야
    "골라서 나아졌나"를 묻는 대조군이 된다. 기간은 자산곡선의 첫날~마지막날로 맞춘다(지평·기간이
    하나라도 다르면 리프트는 거짓이다).
    """
    if len(curve) < 2:
        return None
    d0, d1 = curve[0]["date"], curve[-1]["date"]
    if d0 >= d1:
        return None
    try:
        uni = {u["ticker"] for u in (store.load_us_universe() if market == "us"
                                     else store.load_universe())}
        closes = store.load_all_dated_closes()
    except Exception:                                  # noqa: BLE001 — 장부는 계속 보여야 한다
        return None
    rets = []
    for t in uni:
        pair = closes.get(t)
        if not pair:
            continue
        dates, px = pair
        idx = {d: i for i, d in enumerate(dates)}
        # 그 날짜가 없으면 **이전 거래일**로 — 미래를 당겨쓰지 않는다.
        i0 = idx.get(d0) or next((i for i in range(len(dates) - 1, -1, -1) if dates[i] <= d0), None)
        i1 = idx.get(d1) or next((i for i in range(len(dates) - 1, -1, -1) if dates[i] <= d1), None)
        if i0 is None or i1 is None or i1 <= i0 or not px[i0]:
            continue
        rets.append(px[i1] / px[i0] - 1)
    return round(sum(rets) / len(rets) * 100, 2) if rets else None


# 공용 레퍼런스 봇 — 성향별 시스템 계정(로그인 유저와 별개). track record를 공개로 쌓아 시그널 신뢰의
# 증거로 쓴다(숏폼 소재·멤버십 세일즈). uid는 실유저(1부터 증가)와 안 겹치게 큰 값.
REFERENCE_BOTS = {900001: "conservative", 900002: "balanced", 900003: "aggressive"}


def ensure_reference_bots() -> None:
    """레퍼런스 봇 부트스트랩 — 없으면 생성하고 성향 지정·활성화(백그라운드 루프가 자동 운용)."""
    for uid, style in REFERENCE_BOTS.items():
        u = db.user_bot_get(uid)  # 없으면 기본값으로 생성
        if u["trading_style"] != style:
            db.user_bot_set_style(uid, style)
        if not u["enabled"]:
            db.user_bot_set_enabled(uid, True)


def reference_performance(market: str = "kr") -> dict:
    """3개 레퍼런스 봇(안정·균형·공격)의 공개 track record — 자산곡선·수익률·MDD."""
    ensure_reference_bots()
    bots = []
    for uid, style in REFERENCE_BOTS.items():
        bots.append({"style": style, "label": strategy.STYLE_LABEL.get(style, style),
                     **performance(uid, market)})
    return {"market": market, "currency": "USD" if market == "us" else "KRW", "bots": bots}


def generate_reservations(uid: int, dry_run: bool = False, market: str = "kr") -> dict:
    """유저: 종가·거시·KB를 종합해 '다음 개장 때 살' 예약을 만든다(LLM 자문 우선). 시장별(kr|us)."""
    unit = "$" if market == "us" else "원"
    kr_prices = store.load_price_series()
    mr = _market_read(kr_prices) if kr_prices else {"eff_cfg": None, "context": {}}
    universe, prices, signals, name_by_ticker = _market_signals(market, mr)
    if not universe or not prices:
        return {"ok": False, "reason": "시세 데이터 없음"}

    bal = paper.balance(uid, market)
    held = {h["ticker"] for h in bal["holdings"]}
    cfg = _cfg(uid)
    slots = min(max(0, cfg["max_positions"] - len(held)), cfg["max_new_buys_per_run"])
    context = mr["context"]

    warned = store.load_warned_tickers() if market == "kr" else set()  # 토스 경고 veto(국내)
    # 호출처가 아직 없지만 **같은 선별 로직**이므로 게이트를 지금 붙인다 — 나중에 배선할 때
    # 이 경로만 쿨다운이 없으면 그게 곧 세 번째 갈라짐이다.
    cooled = recent_sold_tickers(uid, market, cfg["trading_style"])
    strong = [s for s in signals if engine.is_buy(s.kind) and s.score >= cfg["min_buy_score"]
              and s.ticker not in cooled
              and s.ticker not in held and not s.event_risk and s.ticker not in warned]
    pool = sorted(strong, key=lambda s: s.score, reverse=True)[:max(slots * 3, 6)]
    pool_by = {s.ticker: s for s in pool}
    if pool and slots > 0:
        try:
            g = advisor_shadow.gate(
                style=cfg.get("trading_style"),
                summary=advisor_shadow.cached_summary())
        except Exception:
            g = {"active": True, "fallback": "abstain"}
        advice = advisor.advise(
            [{"ticker": s.ticker, "name": s.name, "score": s.score,
              "confidence": s.confidence, "reasons": s.reasons} for s in pool],
            context, {t: db.kb_digest_get(t) for t in pool_by},
            advisor.build_lessons(), slots,
            style=cfg.get("trading_style"), gate=g,
        )
        picks = advice.picks
    else:
        picks = None

    if picks:
        chosen = [(pool_by[p["ticker"]], p["rationale"]) for p in picks if p["ticker"] in pool_by]
    elif picks is None:
        chosen = [(s, None) for s in pool[:slots]]   # 사용 불가·kill→score → 점수순 폴백
    else:
        chosen = []                                  # 기권·kill→abstain → 예약 0건

    reservations = []
    if not dry_run:
        db.bot_reservations_clear_pending(uid, market)
    for s, rationale in chosen:
        closes = prices.get(s.ticker)
        if not closes:
            continue
        target = closes[-1]
        name = name_by_ticker.get(s.ticker, s.name)
        reason = (f"[AI] {rationale}" if rationale else f"점수 {s.score:+.2f}") + \
                 f" · 국면 {context.get('regime')}/거시 {context.get('macro_bias')} · 목표가 {int(target):,}{unit}(+{_MAX_CHASE_PCT*100:.0f}%까지 추격)"
        reservations.append({"ticker": s.ticker, "name": name, "side": "buy", "target_price": target, "reason": reason})
        if not dry_run:
            db.bot_reservation_add(uid, s.ticker, name, "buy", target, _MAX_CHASE_PCT, reason, market=market)
    return {"ok": True, "dry_run": dry_run, "market": market, "context": context, "reservations": reservations}


def execute_reservations(uid: int, dry_run: bool = False, market: str = "kr") -> dict:
    """유저: pending 예약을 실행(시장별). 현재가가 목표가+추격허용폭 이내면 매수, 초과면 스킵."""
    unit = "$" if market == "us" else "원"
    pending = db.bot_reservations_pending(uid, market)
    if not pending:
        return {"ok": True, "market": market, "executed": [], "note": "대기 중인 예약 없음"}

    prices = store.load_us_price_series() if market == "us" else store.load_price_series()
    bal = paper.balance(uid, market)
    cfg = _cfg(uid)
    cash = bal["cash"]
    target_alloc = bal["total_eval"] * cfg["position_pct"]
    executed = []
    for r in pending:
        closes = prices.get(r["ticker"])
        if not closes:
            if not dry_run:
                db.bot_reservation_resolve(r["id"], "no_data")
            continue
        price = closes[-1]
        ceiling = r["target_price"] * (1 + r["max_chase_pct"])
        if price > ceiling:
            executed.append({"ticker": r["ticker"], "name": r["name"], "status": "skipped_price",
                             "note": f"현재가 {int(price):,}{unit} > 상한 {int(ceiling):,}{unit} — 추격 안 함"})
            if not dry_run:
                db.bot_reservation_resolve(r["id"], "skipped_price")
            continue
        qty = int(min(target_alloc, cash) // price)
        if qty < 1:
            executed.append({"ticker": r["ticker"], "name": r["name"], "status": "skipped_cash", "note": "잔고 부족"})
            if not dry_run:
                db.bot_reservation_resolve(r["id"], "skipped_cash")
            continue
        note = f"예약 실행 — {r['reason']} · 현재가 {int(price):,}{unit} × {qty}주"
        if not dry_run:
            result = paper.place_order(uid, r["ticker"], "buy", qty, price=price, name=r["name"], market=market)
            if result is not None:
                db.bot_trade_log(uid, r["ticker"], r["name"], "buy", qty, price, "RESERVATION", result["order_no"], note=note, market=market)
                db.bot_position_upsert(uid, r["ticker"], r["name"], qty, price, price, _today(), market=market)
                db.bot_reservation_resolve(r["id"], "filled")
                cash -= qty * price
                executed.append({"ticker": r["ticker"], "name": r["name"], "status": "filled", "qty": qty, "note": note})
            else:
                db.bot_reservation_resolve(r["id"], "order_failed")
                executed.append({"ticker": r["ticker"], "name": r["name"], "status": "order_failed"})
        else:
            executed.append({"ticker": r["ticker"], "name": r["name"], "status": "would_fill", "qty": qty, "note": note})
    return {"ok": True, "dry_run": dry_run, "market": market, "executed": executed}


# 손해 경보 문턱 — 초과수익 **상한**이 이 값 아래로 확정되면 경고한다. 0이 아니라 살짝
# 아래인 이유: 정확히 0 근처는 늘 걸려 매주 우는 늑대가 된다(신선도 오탐에서 배운 것).
HARM_ALERT_UPPER_PP = 0.0
_HARM_MIN_BLOCKS = 4       # 블록이 이보다 적으면 분산을 못 재고 방향만 보인다


def harm_alert(curve: list[dict], *, seed: float, benchmark_pct: float | None,
               block_days: int = 5, z: float = 1.645) -> dict:
    """**"좋다"보다 "나쁘다"가 훨씬 빨리 결론난다.** 초과수익 상한이 0 아래면 경고.

    한쪽(z=1.645, 단측 95%)만 보는 이유: 실계좌로 따라 사는 사람에게 실무적으로 중요한 질문은
    "이게 시장보다 나은가"가 아니라 **"이게 나를 깎아먹고 있나"** 다. 양측으로 재면 손해를
    확인하는 데 필요한 표본이 늘어난다.

    블록(기본 5거래일)으로 묶는 이유는 정확도 판정과 같다 — 일별 자산곡선은 자기상관이 강해
    독립 관측이 아니다.
    """
    out = {"ready": False, "reason": None, "alert": False, "excess_pp": None,
           "upper_pp": None, "blocks": 0, "days": len(curve or []),
           "note": "시장 대비 초과수익의 단측 상한. 상한이 0 아래면 '시장보다 못하다'가 확정된 것."}
    if not curve or len(curve) < block_days * _HARM_MIN_BLOCKS or not seed:
        out["reason"] = (f"자산곡선 {len(curve or [])}일 — "
                         f"블록 {_HARM_MIN_BLOCKS}개({block_days * _HARM_MIN_BLOCKS}일) 필요")
        return out
    if benchmark_pct is None:
        out["reason"] = "벤치마크를 만들 수 없다 — 기준선 없는 수익률은 판정이 아니다"
        return out
    # 블록별 봇 수익률(겹치지 않게) → 시장 블록 평균을 빼 초과수익 분포를 만든다.
    pts = [p["total_eval"] for p in curve]
    rets = []
    for i in range(0, len(pts) - block_days, block_days):
        a, b = pts[i], pts[i + block_days]
        if a:
            rets.append((b / a - 1) * 100)
    if len(rets) < _HARM_MIN_BLOCKS:
        out["reason"] = f"독립 블록 {len(rets)}개 — {_HARM_MIN_BLOCKS}개 필요"
        return out
    n_blocks = len(rets)
    bench_per_block = benchmark_pct / n_blocks          # 같은 창을 같은 수로 나눈다
    ex = [r - bench_per_block for r in rets]
    m = sum(ex) / n_blocks
    var = sum((x - m) ** 2 for x in ex) / (n_blocks - 1)
    se = (var / n_blocks) ** 0.5
    upper = m + z * se
    out.update(ready=True, blocks=n_blocks,
               excess_pp=round(m * n_blocks, 2),          # 전체 창 기준으로 되돌려 표시
               upper_pp=round(upper * n_blocks, 2),
               alert=bool(upper < HARM_ALERT_UPPER_PP / max(1, n_blocks)))
    if out["alert"]:
        out["reason"] = (f"초과수익 상한 {out['upper_pp']}%p < 0 — "
                         f"블록 {n_blocks}개에서 시장보다 못한 것이 확정됐다")
    return out
