"""자동매매봇 — 시그널 → 리스크 판정 → KIS 모의투자 주문 실행 (BACKLOG #7).

KIS 모의계좌는 서비스 전체가 공유하는 단일 데모 계좌(brightdesk의 "Track A: 자동 운용"과 동일
개념 — 유저별 계좌가 아니라 서버 env 하나). 그래서 `db.py`의 bot_* 테이블도 uid로 스코프하지 않는다.

주문 실행 후에는 항상 KIS 잔고(`broker.kis.balance()`)를 다시 조회해 우리 DB의 포지션을
덮어쓴다 — 우리 계산이 아니라 KIS를 source of truth로 삼는다. 진입 후 고점(peak_price)만은
KIS가 안 줘서 우리가 매 회차 직접 갱신·보관한다(리스크 엔진의 트레일링스탑 판정에 필요).
"""

from __future__ import annotations

import datetime
import logging
from zoneinfo import ZoneInfo

from signal_desk import config, db, kb, llm, signalcfg, store, strategy
from signal_desk.broker import kis
from signal_desk.reference import cycle
from signal_desk.signals import advisor, engine, macro, regime, risk

log = logging.getLogger("signal_desk.bot")

_KST = ZoneInfo("Asia/Seoul")
_MARKET_OPEN = datetime.time(9, 0)
_MARKET_CLOSE = datetime.time(15, 20)  # 동시호가 등 마감 직전 여유 두고 컷오프
_OUTCOME_AGE_SEC = 3 * 24 * 3600  # 의사결정 후 3일 지나면 사후수익 확정(학습 재료)


def is_market_hours(now: datetime.datetime | None = None) -> bool:
    """평일 09:00~15:20(KST)만 True — 장 시간 밖에서는 자동 루프가 조용히 스킵."""
    now = now or datetime.datetime.now(_KST)
    if now.weekday() >= 5:  # 토(5)/일(6)
        return False
    return _MARKET_OPEN <= now.time() <= _MARKET_CLOSE


def _today() -> str:
    return datetime.date.today().isoformat()


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


def us_preview(capital: float = 10000.0, style: str | None = None) -> dict:
    """US 자동매매 '판단 미리보기'(주문 없음) — 시그널 기반 매수 후보 + 분할 진입 계획(USD).

    ⚠️ US 실주문·잔고는 KIS 해외 API를 미국장 개장 중 검증한 뒤 연결 예정(현재 미연결). 이 함수는
    국내 봇과 동일한 결정 로직(성향·분할·최소점수·악재 veto)을 US 시그널에 적용한 계획만 보여준다."""
    style = strategy.normalize(style or db.bot_config_get()["trading_style"])
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
        buys.append({"ticker": s.ticker, "name": s.name, "kind": s.kind, "score": round(s.score, 2),
                     "price": round(price, 2), "qty": qty, "alloc": round(qty * price, 2),
                     "qualitative": bool(s.has_qualitative)})
    return {"ready": True, "style": style, "style_label": strategy.STYLE_LABEL.get(style, style),
            "capital": capital, "tranches": tranches, "position_pct": p["position_pct"],
            "min_buy_score": p["min_buy_score"], "eligible": len(eligible), "buys": buys,
            "connected": False,
            "note": "US 실주문·잔고는 미국장 개장 시 KIS 해외 API 검증 후 연결 예정 — 현재는 미리보기 전용"}


_CRASH_FLOOR_PCT = 0.05  # 신호 기준가(종가) 대비 실시간가 -5% 이상 급락이면 매수 스킵(악재 갭 의심)


def _live_price(ticker: str, creds: dict, fallback: float, dry_run: bool) -> float:
    """판단 시점 실시간 현재가(장중 갭 대응). dry_run·조회 실패 시 캐시 종가로 폴백."""
    if dry_run:
        return fallback
    live = kis.current_price(ticker, creds)
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
    context = {
        "regime": reg.get("regime"),
        "macro_bias": mread.get("bias"),
        "cycle_phase": cyc.get("phase_name"),
        "gate_applied": bool(adapt.get("bump")),  # 매수 기준이 이미 상향됐는지(LLM에 알림)
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


def reconcile_positions(bal: dict) -> None:
    """내부 DB 포지션을 KIS 잔고(= source of truth)에 맞춘다 — 종목·수량·평단은 KIS로 덮어쓰고,
    트레일링용 peak_price만 우리가 유지(기존 peak과 현재 평단 중 큰 값). KIS에 없는 DB 포지션은 삭제.

    매수 시 DB엔 주문 추정가를 기록하지만 실제 체결 평단은 KIS가 정확하므로, 이 재동기화로
    대시보드·리스크 판단이 항상 KIS 실측과 일치하게 한다(봇이 꺼져 있어도 조회 시점에 맞춤)."""
    kis_h = {h["ticker"]: h for h in bal.get("holdings", [])}
    for t in {p["ticker"] for p in db.bot_positions_all()} - set(kis_h):
        db.bot_position_delete(t)  # KIS에서 사라진 포지션 정리
    for t, h in kis_h.items():
        pos = db.bot_position_get(t)
        peak = max(pos["peak_price"] if pos else h["avg_price"], h["avg_price"])
        entry = pos["entry_date"] if pos else _today()
        db.bot_position_upsert(t, h["name"], h["qty"], h["avg_price"], peak, entry)


def get_state() -> dict:
    """포트폴리오 탭용 종합 상태 — 봇 설정/현금·평가금액/보유종목/최근거래."""
    cfg = db.bot_config_get()
    creds = config.kis_credentials()
    bal = kis.balance(creds) if creds else None
    if bal is not None:
        reconcile_positions(bal)  # 조회 시점에 DB를 KIS 실측과 일치시킴(평단 등)
    return {
        "enabled": cfg["enabled"],
        "config": cfg,
        "kis_connected": bal is not None,
        "cash": bal["cash"] if bal else None,
        "total_eval": bal["total_eval"] if bal else None,
        "stock_eval": bal.get("stock_eval") if bal else None,
        "invested": bal.get("invested") if bal else None,
        "pnl": bal.get("pnl") if bal else None,
        "pnl_pct": bal.get("pnl_pct") if bal else None,  # KIS 집계 기반 실제 총손익률
        "positions": db.bot_positions_all(),
        "recent_trades": db.bot_trades_recent(20),
        "reservations": db.bot_reservations_pending(),
        "llm_enabled": llm.available(),
        "style_label": strategy.STYLE_LABEL.get(cfg["trading_style"], cfg["trading_style"]),
        "styles": [{"key": k, "label": strategy.STYLE_LABEL[k], "desc": strategy.STYLE_DESC[k]} for k in strategy.STYLES],
    }


def set_enabled(enabled: bool) -> None:
    db.bot_config_set_enabled(enabled)


def set_style(style: str) -> str:
    """트레이딩 성향 변경(프리셋 파라미터 함께 적용). 정규화된 style 반환."""
    style = strategy.normalize(style)
    db.bot_config_set_style(style, strategy.bot_params(style))
    return style


def run_once(dry_run: bool = False) -> dict:
    """한 사이클 실행.

    실주문은 항상 장 시간(평일 09:00~15:20 KST)에만 나간다 — force로 우회하지 않는다.
    dry_run=True면 주문/DB기록 없이 '무엇을 왜 매매할지' 계획만 계산해 반환한다(장 시간 무관,
    안전한 미리보기). 계획에는 정량 근거(점수·수량 산정)가 담긴다.
    """
    creds = config.kis_credentials()
    if not creds:
        return {"ok": False, "reason": "KIS 인증정보 없음(.env 확인)"}
    if not dry_run and not is_market_hours():
        return {"ok": False, "reason": "장 시간이 아닙니다(평일 09:00~15:20 KST에만 실주문). '판단 미리보기'로 계획만 확인하세요."}

    bal = kis.balance(creds)
    if bal is None:
        return {"ok": False, "reason": "KIS 잔고조회 실패"}

    universe = store.load_universe()
    prices = store.load_price_series()
    if not universe or not prices:
        return {"ok": False, "reason": "시세 데이터 없음 — /api/refresh 먼저 호출 필요"}

    fundamentals = store.load_fundamentals()
    market = _market_read(prices)  # 국면 스냅샷 1회 — 거시는 여기 게이트에서만 매수 기준에 반영
    signals = engine.evaluate(universe, prices, fundamentals, config=market["eff_cfg"], sentiment=kb.sentiment_map())
    signal_by_ticker = {s.ticker: s for s in signals}
    name_by_ticker = {u["ticker"]: u["name"] for u in universe}
    if not dry_run:
        _update_decision_outcomes(prices)  # 과거 결정 사후수익 확정(학습)

    cfg = db.bot_config_get()
    held_tickers = {h["ticker"] for h in bal["holdings"]}
    # DB에는 있지만 KIS 잔고엔 없는 포지션(외부에서 청산됨 등) 정리 — KIS가 source of truth
    if not dry_run:
        for ticker in {p["ticker"] for p in db.bot_positions_all()} - held_tickers:
            db.bot_position_delete(ticker)

    # 성향별 손절/익절/트레일링 — 횡보·약세 국면이면 '중간 실현'용 타이트 익절(③)
    risk_cfg = strategy.risk_config(cfg["trading_style"], market["context"].get("regime"))
    sells: list[dict] = []
    for h in bal["holdings"]:
        ticker, qty, avg_price = h["ticker"], h["qty"], h["avg_price"]
        closes = prices.get(ticker)
        if not closes:
            continue  # 유니버스 밖 종목(수동 보유 등) — 우리 봇 판단 대상 아님
        current_price = _live_price(ticker, creds, closes[-1], dry_run)  # 실시간가로 손절/익절/트레일링 판단
        pos = db.bot_position_get(ticker)
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
                result = kis.place_order(ticker, "sell", qty, creds=creds)
                if result is not None:  # 체결된 주문만 기록·반영(장 밖 유령거래 방지)
                    db.bot_trade_log(ticker, plan["name"], "sell", qty, current_price, reason,
                                      result["order_no"], score=sig.score if sig else None, note=note)
                    db.bot_position_delete(ticker)
                    plan["ok"] = True
                else:
                    log.warning("매도 주문 실패: %s", ticker)
                    plan["ok"] = False
            sells.append(plan)
        elif not dry_run:
            db.bot_position_upsert(ticker, name_by_ticker.get(ticker, ticker), qty, avg_price,
                                    peak, pos["entry_date"] if pos else _today())

    # 매도 반영된 최신 잔고로 매수 슬롯 계산(모의투자는 즉시체결 가정)
    bal2 = (kis.balance(creds) or bal) if not dry_run else bal
    held_after = {h["ticker"] for h in bal2["holdings"]}
    available_slots = max(0, cfg["max_positions"] - len(held_after))
    # 한 사이클 신규 매수 개수 제한 — 시그널 BUY가 많아도 한꺼번에 다 사지 않는다
    slots = min(available_slots, cfg["max_new_buys_per_run"])

    buys: list[dict] = []
    skipped_weak = 0
    skipped_gap = 0  # 신호가 대비 실시간가가 급등/급락 이탈해 스킵한 건수
    advisor_used = False
    context = market["context"]  # 국면 스냅샷(1회 계산분 재사용)
    cash = bal2["cash"]
    target_alloc = bal2["total_eval"] * cfg["position_pct"]
    tranches = strategy.entry_tranches(cfg["trading_style"])  # ① 분할매수 회차
    tranche_alloc = target_alloc / tranches                  # 신규 진입은 1트랜치만(나머지는 다음 사이클에)
    if slots > 0:
        # min_buy_score 이상인 강한 BUY만 후보 — 약한 BUY는 매수하지 않음. 최근 악재(event_risk)는 제외
        eligible = [s for s in signals if engine.is_buy(s.kind) and s.ticker not in held_after and not s.event_risk]
        strong = [s for s in eligible if s.score >= cfg["min_buy_score"]]
        skipped_weak = len(eligible) - len(strong)
        pool = sorted(strong, key=lambda s: s.score, reverse=True)[:max(slots * 3, 6)]
        pool_by = {s.ticker: s for s in pool}

        # 하이브리드: 가드레일 통과 후보(pool) 안에서 LLM이 최종 선별(있으면). 없으면 점수순.
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
            ref = closes[-1]                                   # 신호가 본 종가 = 기준가
            live = _live_price(s.ticker, creds, ref, dry_run)  # 판단 시점 실시간가
            drift = (live / ref - 1) if ref else 0.0
            # 갭 게이트: 급등하면 추격 안 함, 급락하면 악재 의심 → 신호 무효 간주하고 스킵
            if drift > _MAX_CHASE_PCT or drift < -_CRASH_FLOOR_PCT:
                skipped_gap += 1
                continue
            limit_price = round(ref * (1 + _MAX_CHASE_PCT))    # 지정가 상한(종가+추격허용) → 상단 초과 체결 방지
            alloc = min(tranche_alloc, cash)                   # ① 목표비중을 K분할 → 이번엔 1트랜치
            qty = int(alloc // live)                           # 수량은 실시간가 기준
            if qty < 1:
                continue  # 배분금액보다 1주 가격이 비싸면 스킵(정수주 제약)
            quant = (f"점수 {s.score:+.2f}(≥{cfg['min_buy_score']:.1f}·신뢰도 {s.confidence:.2f}) · "
                     f"분할 1/{tranches}트랜치(약 {int(alloc):,}원) ÷ {int(live):,}원 = {qty}주 · "
                     f"지정가 {limit_price:,}원(종가 {int(ref):,}·현재 {int(live):,}, {drift * 100:+.1f}%)")
            llm_reason = rationale_by.get(s.ticker)
            note = (f"[AI] {llm_reason} · {quant}") if llm_reason else quant
            plan = {"ticker": s.ticker, "name": s.name, "qty": qty, "price": live, "limit_price": limit_price,
                    "reason": "SIGNAL", "note": note, "score": s.score, "ai": bool(llm_reason)}
            if not dry_run:
                result = kis.place_order(s.ticker, "buy", qty, price=limit_price, creds=creds)  # 지정가 주문
                if result is not None:  # 체결된 주문만 기록·반영
                    db.bot_trade_log(s.ticker, s.name, "buy", qty, live, "SIGNAL",
                                      result["order_no"], score=s.score, note=note)
                    db.bot_position_upsert(s.ticker, s.name, qty, live, live, _today())
                    db.bot_decision_log(s.ticker, s.name, "buy", s.score, note, context, live)  # 저널(학습)
                    cash -= qty * live
                    plan["ok"] = True
                else:
                    log.warning("매수 주문 실패: %s", s.ticker)
                    plan["ok"] = False
            else:
                cash -= qty * live
            buys.append(plan)

    # ① 분할매수 후속: 보유 중이고 여전히 BUY인데 목표비중 미달인 포지션에 다음 트랜치 추가.
    #   평단 근처·이하에서만 담아 '물타기'가 아닌 목표까지의 규율적 분할(손절 대상은 위 매도에서 이미 처리됨).
    for h in bal2["holdings"]:
        t = h["ticker"]
        sig = signal_by_ticker.get(t)
        if not (sig and engine.is_buy(sig.kind)) or sig.event_risk:
            continue
        closes = prices.get(t)
        if not closes:
            continue
        avg = h["avg_price"]
        live = _live_price(t, creds, closes[-1], dry_run)
        value = h["qty"] * live
        if value >= target_alloc * 0.95:       # 이미 목표비중 도달 → 추가 없음
            continue
        if live > avg * (1 + _MAX_CHASE_PCT):   # 평단보다 크게 위면 추격 안 함(다음 눌림에)
            continue
        add_amt = min(tranche_alloc, target_alloc - value, cash)
        qty = int(add_amt // live)
        if qty < 1:
            continue
        limit_price = round(live * (1 + _MAX_CHASE_PCT))
        note = (f"분할 추가매수(목표 {int(target_alloc):,}원 대비 {int(value):,}원) · "
                f"평단 {int(avg):,}·현재 {int(live):,} · {qty}주 @지정가 {limit_price:,}")
        plan = {"ticker": t, "name": h["name"], "qty": qty, "price": live, "limit_price": limit_price,
                "reason": "ADD", "note": note, "score": sig.score, "ai": False}
        if not dry_run:
            result = kis.place_order(t, "buy", qty, price=limit_price, creds=creds)
            if result is not None:
                new_qty = h["qty"] + qty
                new_avg = round((h["qty"] * avg + qty * live) / new_qty, 2)
                pos = db.bot_position_get(t)
                db.bot_trade_log(t, h["name"], "buy", qty, live, "ADD", result["order_no"], score=sig.score, note=note)
                db.bot_position_upsert(t, h["name"], new_qty, new_avg,
                                        max(pos["peak_price"] if pos else new_avg, live),
                                        pos["entry_date"] if pos else _today())
                cash -= qty * live
                plan["ok"] = True
            else:
                plan["ok"] = False
        else:
            cash -= qty * live
        buys.append(plan)

    final_bal = (kis.balance(creds) or bal2) if not dry_run else bal2
    return {
        "ok": True, "dry_run": dry_run, "skipped_weak_buys": skipped_weak,
        "skipped_gap_buys": skipped_gap, "advisor_used": advisor_used,
        "sells": sells, "buys": buys,
        "cash": final_bal["cash"], "total_eval": final_bal["total_eval"],
        "holdings": len(final_bal["holdings"]),
    }


_MAX_CHASE_PCT = 0.02  # 예약 목표가 대비 +2%까지는 추격 매수, 그 이상 오르면 스킵(놓침)


def generate_reservations(dry_run: bool = False) -> dict:
    """장 마감 후: 종가·거시·KB를 종합해 '다음 개장 때 살' 예약 주문을 만든다(LLM 자문 우선).
    목표가는 당일 종가, 추격 허용폭은 +2%. 기존 pending은 새로 만들기 전에 만료 처리."""
    creds = config.kis_credentials()
    if not creds:
        return {"ok": False, "reason": "KIS 인증정보 없음"}
    universe = store.load_universe()
    prices = store.load_price_series()
    if not universe or not prices:
        return {"ok": False, "reason": "시세 데이터 없음"}

    bal = kis.balance(creds)
    held = {h["ticker"] for h in bal["holdings"]} if bal else set()
    fundamentals = store.load_fundamentals()
    market = _market_read(prices)  # 국면 스냅샷 1회(예약도 동일 규칙 — 거시는 게이트에서만)
    signals = engine.evaluate(universe, prices, fundamentals, config=market["eff_cfg"], sentiment=kb.sentiment_map())
    cfg = db.bot_config_get()
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
        db.bot_reservations_clear_pending()
    for s, rationale in chosen:
        closes = prices.get(s.ticker)
        if not closes:
            continue
        target = closes[-1]
        reason = (f"[AI] {rationale}" if rationale else f"점수 {s.score:+.2f}") + \
                 f" · 국면 {context.get('regime')}/거시 {context.get('macro_bias')} · 목표가 {int(target):,}원(+{_MAX_CHASE_PCT*100:.0f}%까지 추격)"
        reservations.append({"ticker": s.ticker, "name": s.name, "side": "buy", "target_price": target, "reason": reason})
        if not dry_run:
            db.bot_reservation_add(s.ticker, s.name, "buy", target, _MAX_CHASE_PCT, reason)
    return {"ok": True, "dry_run": dry_run, "context": context, "reservations": reservations}


def execute_reservations(dry_run: bool = False) -> dict:
    """개장 시: pending 예약을 실행. 현재가가 목표가+추격허용폭 이내면 매수, 초과(급등해 놓침)면 스킵.
    (고도화 여지: 놓친 종목 대신 다른 후보 물색 — 지금은 스킵/만료로 단순화)"""
    creds = config.kis_credentials()
    if not creds:
        return {"ok": False, "reason": "KIS 인증정보 없음"}
    if not dry_run and not is_market_hours():
        return {"ok": False, "reason": "장 시간이 아님"}
    pending = db.bot_reservations_pending()
    if not pending:
        return {"ok": True, "executed": [], "note": "대기 중인 예약 없음"}

    prices = store.load_price_series()
    bal = kis.balance(creds)
    if bal is None:
        return {"ok": False, "reason": "KIS 잔고조회 실패"}
    cfg = db.bot_config_get()
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
        if price > ceiling:  # 개장가가 목표가+추격폭 초과 → 놓침, 스킵
            executed.append({"ticker": r["ticker"], "name": r["name"], "status": "skipped_price",
                             "note": f"개장가 {int(price):,}원 > 상한 {int(ceiling):,}원 — 추격 안 함"})
            if not dry_run:
                db.bot_reservation_resolve(r["id"], "skipped_price")
            continue
        qty = int(min(target_alloc, cash) // price)
        if qty < 1:
            executed.append({"ticker": r["ticker"], "name": r["name"], "status": "skipped_cash", "note": "잔고 부족"})
            if not dry_run:
                db.bot_reservation_resolve(r["id"], "skipped_cash")
            continue
        note = f"예약 실행 — {r['reason']} · 개장가 {int(price):,}원 × {qty}주"
        if not dry_run:
            result = kis.place_order(r["ticker"], "buy", qty, creds=creds)
            if result is not None:
                db.bot_trade_log(r["ticker"], r["name"], "buy", qty, price, "RESERVATION", result["order_no"], note=note)
                db.bot_position_upsert(r["ticker"], r["name"], qty, price, price, _today())
                db.bot_reservation_resolve(r["id"], "filled")
                cash -= qty * price
                executed.append({"ticker": r["ticker"], "name": r["name"], "status": "filled", "qty": qty, "note": note})
            else:
                db.bot_reservation_resolve(r["id"], "order_failed")
                executed.append({"ticker": r["ticker"], "name": r["name"], "status": "order_failed"})
        else:
            executed.append({"ticker": r["ticker"], "name": r["name"], "status": "would_fill", "qty": qty, "note": note})
    return {"ok": True, "dry_run": dry_run, "executed": executed}
