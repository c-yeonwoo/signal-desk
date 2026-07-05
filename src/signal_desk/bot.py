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
from signal_desk.signals import advisor, engine, macro, regime, risk

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


def _daily_loss_breached(uid: int, bal: dict, dry_run: bool) -> bool:
    """유저의 당일 시작 평가액 대비 손실 한도 초과 여부. 초과면 신규 매수 중단(리스크 청산은 유지)."""
    total = bal.get("total_eval")
    if not total or total <= 0:
        return False
    key = f"bot_day_equity:{uid}:{_today()}"
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
    """US(S&P500) 시그널 — 재무 없이 기술·낙폭 + KB 정성 팩터. 점수 내림차순."""
    prices = store.load_us_price_series()
    if not prices:
        return []
    sigs = engine.evaluate(store.load_us_universe(), prices, sentiment=kb.sentiment_map())
    return sorted(sigs, key=lambda s: s.score, reverse=True)


def us_state(capital: float = 10000.0) -> dict:
    """해외(US) 대시보드 상태 — 판단 미리보기 전용(계좌 미연동). 국내 봇과 동일한 결정 로직을
    US 시그널에 적용한 계획만 보여준다(실행 없음)."""
    return {"market": "us", "kis_connected": False, "balance": None,
            "us_market_hours": is_us_market_hours(), "preview": us_preview(capital),
            "live_connected": False}


def us_preview(capital: float = 10000.0, style: str | None = None) -> dict:
    """US 자동매매 '판단 미리보기'(주문 없음) — 시그널 기반 매수 후보 + 분할 진입 계획(USD).

    ⚠️ US 실주문·잔고는 KIS 해외 API를 미국장 개장 중 검증한 뒤 연결 예정(현재 미연결). 이 함수는
    국내 봇과 동일한 결정 로직(성향·분할·최소점수·악재 veto)을 US 시그널에 적용한 계획만 보여준다."""
    style = strategy.normalize(style or "balanced")
    p = strategy.preset(style)
    sigs = us_signals()
    hist = store.load_us_price_series()
    tranches = strategy.entry_tranches(style)
    tranche_alloc = capital * p["position_pct"] / tranches  # 1트랜치 배분(USD)
    eligible = [s for s in sigs if engine.is_buy(s.kind) and s.score >= p["min_buy_score"] and not s.event_risk]
    buys = []
    for s in eligible[:p["max_new_buys_per_run"] * 3]:
        closes = hist.get(s.ticker) or []
        price = closes[-1] if closes else None
        if not price:
            continue
        qty = int(tranche_alloc // price)
        if qty < 1:
            continue
        buys.append({"ticker": s.ticker, "name": us_ko.name_ko(s.ticker, s.name), "kind": s.kind, "score": round(s.score, 2),
                     "price": round(price, 2), "qty": qty, "alloc": round(qty * price, 2),
                     "qualitative": bool(s.has_qualitative)})
    return {"ready": True, "style": style, "style_label": strategy.STYLE_LABEL.get(style, style),
            "capital": capital, "tranches": tranches, "position_pct": p["position_pct"],
            "min_buy_score": p["min_buy_score"], "eligible": len(eligible), "buys": buys,
            "connected": False,
            "note": "US 실주문·잔고는 미국장 개장 시 KIS 해외 API 검증 후 연결 예정 — 현재는 미리보기 전용"}


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
    eff_cfg, adapt = signalcfg.effective_config(reg, mread)
    macro_dg = kb.macro_digest()
    context = {
        "regime": reg.get("regime"),
        "macro_bias": mread.get("bias"),
        # FRED 정량 지표 근거(CPI·금리·나스닥·VIX) — KB엔 안 넣되 LLM이 시그널 판단 시 지표로 참고
        "macro_detail": " / ".join((mread.get("reasons") or [])[:5]),
        "cycle_phase": cyc.get("phase_name"),
        "gate_applied": bool(adapt.get("bump")),  # 매수 기준이 이미 상향됐는지(LLM에 알림)
        # 미주은 시황 코멘터리(정성 내러티브) — 참고용 맥락, 개별 종목 점수엔 미반영
        "macro_note": (macro_dg["summary"] if macro_dg and macro_dg.get("fresh") else ""),
    }
    return {"eff_cfg": eff_cfg, "adapt": adapt, "context": context}


def _update_decision_outcomes(prices: dict[str, list[float]]) -> None:
    """과거 매수 의사결정의 사후수익을 확정(3일 경과분) — advisor 학습 재료."""
    now = int(datetime.datetime.now(_KST).timestamp())
    for d in db.bot_decisions_recent(60):
        if d.get("outcome_pct") is not None or d.get("action") != "buy":
            continue
        if now - d["ts"] < _OUTCOME_AGE_SEC:
            continue
        closes = prices.get(d["ticker"])
        if not closes or not d.get("decided_price"):
            continue
        outcome = (closes[-1] / d["decided_price"] - 1) * 100
        # bot_decisions_recent에 id가 없어 직접 갱신은 생략하지 않도록 id 포함 조회 필요 →
        # 여기서는 최신 조회분에 id가 없으므로, kv 기반이 아닌 별도 경로로 갱신한다.
        _set_outcome_by_match(d, outcome)


def _set_outcome_by_match(decision: dict, outcome_pct: float) -> None:
    """decisions_recent가 id를 안 주므로, ticker+ts로 정확히 한 건 갱신."""
    c = db.conn()
    c.execute("UPDATE bot_decisions SET outcome_pct=?, outcome_ts=? WHERE ticker=? AND ts=? AND outcome_pct IS NULL",
              (round(outcome_pct, 2), int(datetime.datetime.now(_KST).timestamp()), decision["ticker"], decision["ts"]))
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


def reconcile_positions(uid: int, bal: dict) -> None:
    """유저 bot_positions를 paper 잔고에 맞춘다 — 종목·수량·평단은 paper로 덮어쓰고, 트레일링용
    peak_price·entry_date만 유지(paper엔 없음). paper에 없는 포지션은 삭제. 현재가·수익률 스냅샷도 갱신."""
    ph = {h["ticker"]: h for h in bal.get("holdings", [])}
    for t in {p["ticker"] for p in db.bot_positions_all(uid)} - set(ph):
        db.bot_position_delete(uid, t)
    for t, h in ph.items():
        pos = db.bot_position_get(uid, t)
        price = h.get("price") or 0.0
        peak = max(pos["peak_price"] if pos else h["avg_price"], h["avg_price"], price)
        entry = pos["entry_date"] if pos else _today()
        db.bot_position_upsert(uid, t, h["name"], h["qty"], h["avg_price"], peak, entry,
                               last_price=price or None, last_pnl_pct=h.get("pnl_pct"))


def snapshot_positions(uid: int) -> bool:
    """유저 보유종목 현재가·수익률 스냅샷 갱신(장 종료 후 1회 등) — paper 잔고로 reconcile."""
    reconcile_positions(uid, paper.balance(uid))
    return True


def get_state(uid: int) -> dict:
    """유저 포트폴리오 탭용 종합 상태 — 봇 설정/현금·평가금액/보유종목/최근거래."""
    cfg = _cfg(uid)
    bal = paper.balance(uid)
    reconcile_positions(uid, bal)
    return {
        "enabled": cfg["enabled"],
        "config": cfg,
        "seed_cash": cfg["seed_cash"],
        "cash": bal["cash"],
        "total_eval": bal["total_eval"],
        "stock_eval": bal.get("stock_eval"),
        "invested": bal.get("invested"),
        "pnl": bal.get("pnl"),
        "pnl_pct": bal.get("pnl_pct"),
        "positions": db.bot_positions_all(uid),
        "recent_trades": db.bot_trades_recent(uid, 20),
        "reservations": db.bot_reservations_pending(uid),
        "llm_enabled": llm.available(),
        "style_label": strategy.STYLE_LABEL.get(cfg["trading_style"], cfg["trading_style"]),
        "styles": [{"key": k, "label": strategy.STYLE_LABEL[k], "desc": strategy.STYLE_DESC[k]} for k in strategy.STYLES],
        "kill_switch": config.bot_kill_switch(),
        "daily_loss_limit_pct": config.bot_daily_loss_limit_pct(),
    }


def set_enabled(uid: int, enabled: bool) -> None:
    db.user_bot_set_enabled(uid, enabled)


def set_style(uid: int, style: str) -> str:
    """유저 트레이딩 성향 변경. 정규화된 style 반환(숫자 파라미터는 조회 때 프리셋에서 파생)."""
    style = strategy.normalize(style)
    db.user_bot_set_style(uid, style)
    return style


def set_seed(uid: int, seed_cash: float) -> None:
    """유저 초기 시드 변경. 다음 초기화 때 이 금액으로 리셋된다(기존 계좌엔 즉시 반영 안 됨)."""
    db.user_bot_set_seed(uid, max(0.0, float(seed_cash)))


def reset(uid: int) -> None:
    """유저 봇 초기화 — 포지션·거래·예약·일일기준선 삭제 + 페이퍼 현금 시드로 리셋."""
    db.bot_reset(uid)


_MAX_CHASE_PCT = 0.02  # 지정가 상한(종가 대비 +2%) — 표시·계획용(paper는 종가 즉시 체결)


def run_once(uid: int, dry_run: bool = False) -> dict:
    """유저 한 사이클 실행 — 공용 시그널로 유저 페이퍼 계좌에 매매.
    dry_run=True면 주문/DB기록 없이 '무엇을 왜 매매할지' 계획만 계산(미리보기)."""
    if not dry_run and config.bot_kill_switch():
        return {"ok": False, "reason": "긴급정지(BOT_KILL_SWITCH) 활성 — 주문을 내지 않습니다."}

    bal = paper.balance(uid)
    if not dry_run:
        reconcile_positions(uid, bal)  # bot_positions(peak·entry) 미러를 paper 실측과 일치시킴(stale 정리)
    block_new_buys = _daily_loss_breached(uid, bal, dry_run)  # 일일 손실 한도 초과 시 신규 매수 중단

    universe = store.load_universe()
    prices = store.load_price_series()
    if not universe or not prices:
        return {"ok": False, "reason": "시세 데이터 없음 — /api/refresh 먼저 호출 필요"}

    fundamentals = store.load_fundamentals()
    market = _market_read(prices)  # 공용 국면 스냅샷 — 거시는 여기 게이트에서만 매수 기준에 반영
    signals = engine.evaluate(universe, prices, fundamentals, config=market["eff_cfg"], sentiment=kb.sentiment_map())
    signal_by_ticker = {s.ticker: s for s in signals}
    name_by_ticker = {u["ticker"]: u["name"] for u in universe}
    if not dry_run:
        _update_decision_outcomes(prices)  # 과거 결정 사후수익 확정(공용 학습)

    cfg = _cfg(uid)
    risk_cfg = strategy.risk_config(cfg["trading_style"], market["context"].get("regime"))
    sells: list[dict] = []
    for h in bal["holdings"]:
        ticker, qty, avg_price = h["ticker"], h["qty"], h["avg_price"]
        closes = prices.get(ticker)
        if not closes:
            continue  # 유니버스 밖 종목 — 봇 판단 대상 아님
        current_price = _live_price(ticker, closes[-1])
        pos = db.bot_position_get(uid, ticker)
        peak = max(pos["peak_price"] if pos else avg_price, current_price)

        reason = risk.check_exit(avg_price, current_price, peak, risk_cfg)
        if not reason:
            sig = signal_by_ticker.get(ticker)
            if sig and engine.is_sell(sig.kind):
                reason = "SIGNAL"

        if reason:
            pl_pct = (current_price / avg_price - 1) * 100 if avg_price else 0
            sig = signal_by_ticker.get(ticker)
            note = _sell_note(reason, qty, avg_price, current_price, pl_pct, risk_cfg)
            plan = {"ticker": ticker, "name": name_by_ticker.get(ticker, ticker), "qty": qty,
                    "reason": reason, "note": note, "price": current_price}
            if not dry_run:
                result = paper.place_order(uid, ticker, "sell", qty, price=current_price)
                if result is not None:
                    db.bot_trade_log(uid, ticker, plan["name"], "sell", qty, current_price, reason,
                                      result["order_no"], score=sig.score if sig else None, note=note)
                    db.bot_position_delete(uid, ticker)
                    plan["ok"] = True
                else:
                    plan["ok"] = False
            sells.append(plan)
        elif not dry_run:
            db.bot_position_upsert(uid, ticker, name_by_ticker.get(ticker, ticker), qty, avg_price,
                                    peak, pos["entry_date"] if pos else _today())

    bal2 = paper.balance(uid) if not dry_run else bal
    held_after = {h["ticker"] for h in bal2["holdings"]}
    available_slots = max(0, cfg["max_positions"] - len(held_after))
    slots = 0 if block_new_buys else min(available_slots, cfg["max_new_buys_per_run"])

    buys: list[dict] = []
    skipped_weak = 0
    advisor_used = False
    context = market["context"]
    cash = bal2["cash"]
    target_alloc = bal2["total_eval"] * cfg["position_pct"]
    tranches = strategy.entry_tranches(cfg["trading_style"])  # ① 분할매수 회차
    tranche_alloc = target_alloc / tranches
    if slots > 0:
        eligible = [s for s in signals if engine.is_buy(s.kind) and s.ticker not in held_after and not s.event_risk]
        strong = [s for s in eligible if s.score >= cfg["min_buy_score"]]
        skipped_weak = len(eligible) - len(strong)
        pool = sorted(strong, key=lambda s: s.score, reverse=True)[:max(slots * 3, 6)]
        pool_by = {s.ticker: s for s in pool}

        rationale_by = {}
        picks = advisor.select_buys(
            [{"ticker": s.ticker, "name": s.name, "score": s.score, "confidence": s.confidence, "reasons": s.reasons}
             for s in pool],
            context, {t: db.kb_digest_get(t) for t in pool_by}, advisor.build_lessons(), slots,
        ) if pool else None
        if picks:
            advisor_used = True
            candidates = [pool_by[p["ticker"]] for p in picks if p["ticker"] in pool_by]
            rationale_by = {p["ticker"]: p["rationale"] for p in picks}
        else:
            candidates = pool[:slots]

        for s in candidates:
            closes = prices.get(s.ticker)
            if not closes:
                continue
            live = _live_price(s.ticker, closes[-1])
            alloc = min(tranche_alloc, cash)                   # ① 목표비중을 K분할 → 이번엔 1트랜치
            qty = int(alloc // live)
            if qty < 1:
                continue  # 배분금액보다 1주가 비싸면 스킵(정수주 제약)
            quant = (f"점수 {s.score:+.2f}(≥{cfg['min_buy_score']:.1f}·신뢰도 {s.confidence:.2f}) · "
                     f"분할 1/{tranches}트랜치(약 {int(alloc):,}원) ÷ {int(live):,}원 = {qty}주")
            llm_reason = rationale_by.get(s.ticker)
            note = (f"[AI] {llm_reason} · {quant}") if llm_reason else quant
            plan = {"ticker": s.ticker, "name": s.name, "qty": qty, "price": live,
                    "reason": "SIGNAL", "note": note, "score": s.score, "ai": bool(llm_reason)}
            if not dry_run:
                result = paper.place_order(uid, s.ticker, "buy", qty, price=live, name=s.name)
                if result is not None:
                    db.bot_trade_log(uid, s.ticker, s.name, "buy", qty, live, "SIGNAL",
                                      result["order_no"], score=s.score, note=note)
                    db.bot_position_upsert(uid, s.ticker, s.name, qty, live, live, _today())
                    db.bot_decision_log(s.ticker, s.name, "buy", s.score, note, context, live)  # 공용 저널(학습)
                    cash -= qty * live
                    plan["ok"] = True
                else:
                    plan["ok"] = False
            else:
                cash -= qty * live
            buys.append(plan)

    # ① 분할매수 후속: 보유 중이고 여전히 BUY인데 목표비중 미달인 포지션에 다음 트랜치 추가.
    for h in bal2["holdings"]:
        t = h["ticker"]
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
        note = (f"분할 추가매수(목표 {int(target_alloc):,}원 대비 {int(value):,}원) · "
                f"평단 {int(avg):,}·현재 {int(live):,} · {qty}주")
        plan = {"ticker": t, "name": h["name"], "qty": qty, "price": live,
                "reason": "ADD", "note": note, "score": sig.score, "ai": False}
        if not dry_run:
            result = paper.place_order(uid, t, "buy", qty, price=live, name=h["name"])
            if result is not None:
                new_qty = h["qty"] + qty
                new_avg = round((h["qty"] * avg + qty * live) / new_qty, 2)
                pos = db.bot_position_get(uid, t)
                db.bot_trade_log(uid, t, h["name"], "buy", qty, live, "ADD", result["order_no"], score=sig.score, note=note)
                db.bot_position_upsert(uid, t, h["name"], new_qty, new_avg,
                                        max(pos["peak_price"] if pos else new_avg, live),
                                        pos["entry_date"] if pos else _today())
                cash -= qty * live
                plan["ok"] = True
            else:
                plan["ok"] = False
        else:
            cash -= qty * live
        buys.append(plan)

    final_bal = paper.balance(uid) if not dry_run else bal2
    return {
        "ok": True, "dry_run": dry_run, "skipped_weak_buys": skipped_weak,
        "skipped_gap_buys": 0, "advisor_used": advisor_used,
        "sells": sells, "buys": buys,
        "cash": final_bal["cash"], "total_eval": final_bal["total_eval"],
        "holdings": len(final_bal["holdings"]),
    }


def generate_reservations(uid: int, dry_run: bool = False) -> dict:
    """유저: 종가·거시·KB를 종합해 '다음 개장 때 살' 예약을 만든다(LLM 자문 우선)."""
    universe = store.load_universe()
    prices = store.load_price_series()
    if not universe or not prices:
        return {"ok": False, "reason": "시세 데이터 없음"}

    bal = paper.balance(uid)
    held = {h["ticker"] for h in bal["holdings"]}
    fundamentals = store.load_fundamentals()
    market = _market_read(prices)
    signals = engine.evaluate(universe, prices, fundamentals, config=market["eff_cfg"], sentiment=kb.sentiment_map())
    cfg = _cfg(uid)
    slots = min(max(0, cfg["max_positions"] - len(held)), cfg["max_new_buys_per_run"])
    context = market["context"]

    strong = [s for s in signals if engine.is_buy(s.kind) and s.score >= cfg["min_buy_score"]
              and s.ticker not in held and not s.event_risk]
    pool = sorted(strong, key=lambda s: s.score, reverse=True)[:max(slots * 3, 6)]
    pool_by = {s.ticker: s for s in pool}
    picks = advisor.select_buys(
        [{"ticker": s.ticker, "name": s.name, "score": s.score, "confidence": s.confidence, "reasons": s.reasons}
         for s in pool],
        context, {t: db.kb_digest_get(t) for t in pool_by}, advisor.build_lessons(), slots,
    ) if (pool and slots > 0) else None

    if picks:
        chosen = [(pool_by[p["ticker"]], p["rationale"]) for p in picks if p["ticker"] in pool_by]
    else:
        chosen = [(s, None) for s in pool[:slots]]

    reservations = []
    if not dry_run:
        db.bot_reservations_clear_pending(uid)
    for s, rationale in chosen:
        closes = prices.get(s.ticker)
        if not closes:
            continue
        target = closes[-1]
        reason = (f"[AI] {rationale}" if rationale else f"점수 {s.score:+.2f}") + \
                 f" · 국면 {context.get('regime')}/거시 {context.get('macro_bias')} · 목표가 {int(target):,}원(+{_MAX_CHASE_PCT*100:.0f}%까지 추격)"
        reservations.append({"ticker": s.ticker, "name": s.name, "side": "buy", "target_price": target, "reason": reason})
        if not dry_run:
            db.bot_reservation_add(uid, s.ticker, s.name, "buy", target, _MAX_CHASE_PCT, reason)
    return {"ok": True, "dry_run": dry_run, "context": context, "reservations": reservations}


def execute_reservations(uid: int, dry_run: bool = False) -> dict:
    """유저: pending 예약을 실행. 현재가가 목표가+추격허용폭 이내면 매수, 초과면 스킵."""
    pending = db.bot_reservations_pending(uid)
    if not pending:
        return {"ok": True, "executed": [], "note": "대기 중인 예약 없음"}

    prices = store.load_price_series()
    bal = paper.balance(uid)
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
                             "note": f"현재가 {int(price):,}원 > 상한 {int(ceiling):,}원 — 추격 안 함"})
            if not dry_run:
                db.bot_reservation_resolve(r["id"], "skipped_price")
            continue
        qty = int(min(target_alloc, cash) // price)
        if qty < 1:
            executed.append({"ticker": r["ticker"], "name": r["name"], "status": "skipped_cash", "note": "잔고 부족"})
            if not dry_run:
                db.bot_reservation_resolve(r["id"], "skipped_cash")
            continue
        note = f"예약 실행 — {r['reason']} · 현재가 {int(price):,}원 × {qty}주"
        if not dry_run:
            result = paper.place_order(uid, r["ticker"], "buy", qty, price=price, name=r["name"])
            if result is not None:
                db.bot_trade_log(uid, r["ticker"], r["name"], "buy", qty, price, "RESERVATION", result["order_no"], note=note)
                db.bot_position_upsert(uid, r["ticker"], r["name"], qty, price, price, _today())
                db.bot_reservation_resolve(r["id"], "filled")
                cash -= qty * price
                executed.append({"ticker": r["ticker"], "name": r["name"], "status": "filled", "qty": qty, "note": note})
            else:
                db.bot_reservation_resolve(r["id"], "order_failed")
                executed.append({"ticker": r["ticker"], "name": r["name"], "status": "order_failed"})
        else:
            executed.append({"ticker": r["ticker"], "name": r["name"], "status": "would_fill", "qty": qty, "note": note})
    return {"ok": True, "dry_run": dry_run, "executed": executed}
