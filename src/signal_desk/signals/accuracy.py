"""시그널 실측 성과(realized track record) — 시스템이 '실제로 내보낸' 시그널(전 팩터·전 게이트 적용)이
이후 실현 수익률로 얼마나 맞았는지 측정한다.

engine.backtest_summary()는 가격팩터만 재현하는 시뮬레이션이라 절대값 신뢰가 어렵지만(스케일 시세 +
lookahead 위험), 이 모듈은 store.snapshot_signals가 매일 PIT로 저장한 signal_history(그날의 실제
시그널·팩터값)를 이후 종가와 조인해 계산한다 → 신뢰구축·GTM track record용 정직한 숫자. 수익률은
종목 내 비율이라 [[signal-desk-scaled-market-data]] 스케일 시세에도 불변이다.

성숙(maturity): 시그널일 다음 거래일 종가로 진입해 horizon 거래일 뒤 종가로 청산. horizon일이 아직
경과하지 않은 최근 시그널은 '미성숙'으로 집계에서 제외한다(정직한 표본). 스냅샷은 이 기능 도입일부터만
존재하므로 초기 표본은 작다 — 매일 누적된다.
"""

from __future__ import annotations

from .engine import ACTIONABLE_KINDS, BUY, SELL, STRONG_BUY, STRONG_SELL, is_buy

# 실측 트래커 기본 horizon(거래일). 20일을 헤드라인 정밀도 기준으로 쓴다.
HORIZONS = (5, 20, 60)
PRIMARY_HORIZON = 20
# qualitative는 combine 밖(shadow IC)이지만 관측용으로 남긴다. short는 점수에 실제로 들어가므로
# 빠지면 두뇌·factor_ic가 그 팩터를 영원히 못 잰다(감사 가설로 잡힌 실측 누락).
FACTOR_COLS = ("technical", "fundamental", "valuation", "reversion",
               "qualitative", "flow", "quality", "momentum", "short")
# combine()/evaluate()가 점수에 넣는 팩터 — FACTOR_COLS가 이걸 커버하는지 레드팀이 검사한다.
SCORING_FACTORS = ("technical", "fundamental", "valuation", "reversion",
                   "flow", "quality", "momentum", "short")
_MIN_IC_SAMPLES = 20  # 이보다 표본이 적으면 IC는 신뢰 불가 → None
# 정밀도는 기준선(base rate) 대비 리프트로만 판정한다. 하락장에서는 아무 종목이나 '매도'라고
# 찍어도 정밀도가 60%를 넘기 때문에, 절대값 55%는 잘한 것도 못한 것도 아니다.
# 이 값은 "우연·시장 드리프트로 설명되지 않는다"고 부를 최소 리프트(%p)다.
MIN_LIFT_PP = 3.0

# P3 정성 승격 게이트(shadow 관측 → 향후 priority/threshold 승인용). combine()과 무관.
PROMOTION_MIN_SAMPLES = 80
PROMOTION_MIN_IC = 0.03
PROMOTION_WINDOWS = 4
PROMOTION_WINDOW_MIN = 20


def _entry_index(dates: list[str], signal_date: str) -> int | None:
    """시그널일 '다음' 거래일 인덱스(진입가 근사, backtest와 동일 규약). 없으면 None."""
    for k, d in enumerate(dates):
        if d > signal_date:
            return k
    return None


def _forward_returns(dates: list[str], closes: list[float], signal_date: str,
                     horizons: tuple[int, ...]) -> dict[int, float]:
    """{horizon: 실현수익률} — 성숙한 horizon만 포함(미성숙은 키 자체를 뺀다)."""
    ei = _entry_index(dates, signal_date)
    if ei is None or ei >= len(closes):
        return {}
    entry = closes[ei]
    if not entry:
        return {}
    out = {}
    for h in horizons:
        j = ei + h
        if j < len(closes) and closes[j] is not None:
            out[h] = closes[j] / entry - 1.0
    return out


forward_returns = _forward_returns  # 같은 채점 규약을 쓰는 다른 관측 모듈용(advisor_shadow 등)


def _spearman_ic(pairs: list[tuple[float, float]]) -> float | None:
    """(factor_value, fwd_ret) 쌍의 순위상관(Spearman IC). 의존성 없이 직접 계산."""
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    n = len(xs)
    if n < _MIN_IC_SAMPLES:
        return None

    def _rank(vals: list[float]) -> list[float]:
        order = sorted(range(n), key=lambda i: vals[i])
        ranks = [0.0] * n
        i = 0
        while i < n:  # 동점은 평균 순위(ties → average rank)
            j = i
            while j < n and vals[order[j]] == vals[order[i]]:
                j += 1
            avg = (i + j - 1) / 2.0
            for k in range(i, j):
                ranks[order[k]] = avg
            i = j
        return ranks

    rx, ry = _rank(xs), _rank(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    vx = sum((rx[i] - mx) ** 2 for i in range(n))
    vy = sum((ry[i] - my) ** 2 for i in range(n))
    if vx <= 0 or vy <= 0:
        return None
    return cov / (vx * vy) ** 0.5


def _precision(rets: list[float], *, up: bool) -> float | None:
    """방향 정밀도(%) — 무변동(정확히 0)은 적중으로 세지 않는다."""
    if not rets:
        return None
    hit = sum(1 for x in rets if (x > 0 if up else x < 0))
    return round(hit / len(rets) * 100, 1)


def _ci_half_pp(pct: float | None, n: int) -> float | None:
    """비율의 95% 신뢰구간 반폭(%p) — 정규 근사. 표본이 작으면 이 폭이 리프트보다 커서
    "정밀도 60%"가 사실상 무정보임을 드러낸다(n=20·p=0.6이면 ±21%p)."""
    if pct is None or n <= 0:
        return None
    p = pct / 100.0
    return round(1.96 * (p * (1 - p) / n) ** 0.5 * 100, 1)


def realized_accuracy(
    history_rows: list[dict],
    closes_by_ticker: dict[str, tuple[list[str], list[float]]],
    horizons: tuple[int, ...] = HORIZONS,
    hit_ret: float = 0.005,
    primary: int = PRIMARY_HORIZON,
) -> dict:
    """signal_history 행 × 실현 종가 → 실측 성과.

    history_rows: [{date, ticker, kind, technical, ..., momentum}] (store.load_signal_history 행)
    closes_by_ticker: {ticker: (dates[], closes[])} 오래된→최신 (KR+US 통합)
    반환: 티어별 적중률/정밀도/평균수익(horizon별) + 헤드라인 매수/매도 정밀도와 **기준선 대비 리프트**
    + 팩터 Spearman IC + 커버리지.

    정밀도 절대값은 단독으로 쓰지 말 것 — 시장 드리프트가 그대로 섞여 있다. 판정은 `buy_lift_pp`
    /`sell_lift_pp`(기준선 초과분)와 `*_ci_pp`(표본 오차)로 한다.
    """
    # 티어별 horizon별 실현수익 누적
    by_tier = {k: {h: [] for h in horizons} for k in ACTIONABLE_KINDS}
    ic_pairs = {c: [] for c in FACTOR_COLS}  # 팩터값 → primary horizon 수익
    dates_seen: set[str] = set()
    tickers_seen: set[str] = set()
    rows_total = matured_primary = 0
    # 기준선용 — HOLD까지 포함한 전 표본의 primary horizon 수익. "아무 종목이나 찍었을 때"가
    # 몇 %였는지가 없으면 정밀도 숫자는 방향조차 해석할 수 없다.
    base_rets: list[float] = []

    for r in history_rows:
        ticker = r.get("ticker")
        sig_date = str(r.get("date"))
        kind = r.get("kind")
        rows_total += 1
        dates_seen.add(sig_date)
        series = closes_by_ticker.get(ticker)
        if not series:
            continue
        dates, closes = series
        rets = _forward_returns(dates, closes, sig_date, horizons)
        if not rets:
            continue
        tickers_seen.add(ticker)
        # 팩터 IC는 전체(HOLD 포함) 표본에서 primary horizon 수익으로
        if primary in rets:
            matured_primary += 1
            base_rets.append(rets[primary])
            for c in FACTOR_COLS:
                v = r.get(c)
                if v is not None:
                    ic_pairs[c].append((float(v), rets[primary]))
        if kind in by_tier:
            for h, ret in rets.items():
                by_tier[kind][h].append(ret)

    def _tier_stats(kind: str, h: int) -> dict:
        rets = by_tier[kind][h]
        n = len(rets)
        if not n:
            return {"n": 0, "hit_rate": None, "beat_rate": None, "avg_ret": None}
        buy = is_buy(kind)
        # 방향 정확도. 무변동(정확히 0)은 어느 쪽 적중도 아니다 — 헤드라인 정밀도와 같은 정의를
        # 써서 같은 화면에 두 규칙이 서지 않게 한다.
        hit = sum(1 for x in rets if (x > 0 if buy else x < 0))
        beat = sum(1 for x in rets if (x > hit_ret if buy else x < -hit_ret))  # 임계 초과
        return {"n": n,
                "hit_rate": round(hit / n * 100, 1),
                "beat_rate": round(beat / n * 100, 1),
                "avg_ret": round(sum(rets) / n * 100, 2)}

    tiers = {h: {k: _tier_stats(k, h) for k in ACTIONABLE_KINDS} for h in horizons}

    # 헤드라인: 매수(BUY+STRONG_BUY) 시그널의 primary horizon 방향 정밀도
    buy_rets = by_tier[BUY].get(primary, []) + by_tier[STRONG_BUY].get(primary, [])
    buy_precision = _precision(buy_rets, up=True)
    # 매도 사이드도 같은 규약으로 계산 — 숏(인버스 포함) 검토의 전제 검증용 관측치.
    # 봇·문턱·combine과 무관하고, 표본이 매수보다 훨씬 적을 수 있어 UI는 별도 가드가 필요하다.
    sell_rets = by_tier[SELL].get(primary, []) + by_tier[STRONG_SELL].get(primary, [])
    sell_precision = _precision(sell_rets, up=False)

    # 기준선(naive 예측기) — 전 표본을 "항상 매수"/"항상 매도"로 찍었을 때의 정밀도.
    # 시장 드리프트가 그대로 들어가므로 하락장이면 down_pct가 60%를 넘는 게 정상이다.
    base_up = _precision(base_rets, up=True)
    base_down = _precision(base_rets, up=False)
    buy_lift = (round(buy_precision - base_up, 1)
                if buy_precision is not None and base_up is not None else None)
    sell_lift = (round(sell_precision - base_down, 1)
                 if sell_precision is not None and base_down is not None else None)

    factor_ic = {c: (round(ic, 3) if (ic := _spearman_ic(ic_pairs[c])) is not None else None)
                 for c in FACTOR_COLS}

    return {
        "ready": matured_primary > 0,
        "horizons": list(horizons),
        "primary_horizon": primary,
        "hit_threshold_pct": round(hit_ret * 100, 2),
        "tiers": tiers,
        "buy_precision_pct": buy_precision,          # "매수 찍은 것 중 오른 비율"
        "buy_sample": len(buy_rets),
        "buy_precision_ci_pp": _ci_half_pp(buy_precision, len(buy_rets)),
        "buy_lift_pp": buy_lift,                     # 기준선 대비 초과분 — 판정은 이 값으로만
        "sell_precision_pct": sell_precision,        # "매도 찍은 것 중 내린 비율"(숏 관측용)
        "sell_sample": len(sell_rets),
        "sell_precision_ci_pp": _ci_half_pp(sell_precision, len(sell_rets)),
        "sell_lift_pp": sell_lift,
        # 기준선 — 같은 기간·같은 유니버스를 아무 방향으로 찍었을 때의 성적
        "baseline": {
            "sample": len(base_rets),
            "up_pct": base_up,                       # "항상 매수" naive 정밀도
            "down_pct": base_down,                   # "항상 매도" naive 정밀도
            "avg_ret_pct": (round(sum(base_rets) / len(base_rets) * 100, 2)
                            if base_rets else None),  # 시장 드리프트(%)
        },
        "lift_min_pp": MIN_LIFT_PP,
        "factor_ic": factor_ic,                      # Spearman IC (팩터값↑ vs 미래수익)
        "ic_min_samples": _MIN_IC_SAMPLES,
        "coverage": {
            "rows": rows_total,
            "dates": len(dates_seen),
            "from": min(dates_seen) if dates_seen else None,
            "to": max(dates_seen) if dates_seen else None,
            "tickers_matched": len(tickers_seen),
            "matured_primary": matured_primary,      # primary horizon 성숙 표본 수
            **_join_diagnosis(dates_seen, closes_by_ticker, tickers_seen, matured_primary),
        },
    }


def _join_diagnosis(dates_seen: set[str], closes_by_ticker: dict,
                    tickers_seen: set[str], matured_primary: int) -> dict:
    """왜 성숙 표본이 0인지 말한다.

    "아직 안 익었다"(정상)와 "시세가 시그널보다 오래됐다"(고장)는 화면에서 똑같이 0으로 보이는데
    대응은 정반대다. 실제로 시세 캐시가 07-03에서 멈춘 채 시그널만 07-24까지 쌓여 tickers_matched
    0이 나왔고, 그게 수집 중단인지 미성숙인지 구분할 방법이 화면에 없었다.
    """
    price_to = max((d[-1] for d, _ in closes_by_ticker.values() if d), default=None)
    signal_to = max(dates_seen) if dates_seen else None
    stale = bool(price_to and signal_to and price_to < signal_to)
    reason = None
    if matured_primary == 0 and dates_seen:
        if stale:
            reason = (f"시세가 시그널보다 오래됐습니다(시세 {price_to} < 시그널 {signal_to}) — "
                      f"성숙 대기가 아니라 수집 중단입니다")
        elif not tickers_seen:
            reason = "시그널 종목이 시세 캐시에 없습니다 — 유니버스와 시세 수집 대상이 어긋났습니다"
        else:
            reason = "아직 성숙 구간이 지나지 않았습니다(정상 — 시간이 필요합니다)"
    return {"price_data_to": price_to, "stale_prices": stale, "blocked_reason": reason}


def _qualitative_pairs(
    history_rows: list[dict],
    closes_by_ticker: dict[str, tuple[list[str], list[float]]],
    *,
    primary: int = PRIMARY_HORIZON,
) -> list[tuple[str, float, float]]:
    """PIT 정성값 × primary horizon 실현수익 쌍. (date, qualitative, fwd_ret).
    정성 None·미성숙은 제외. 미래 가격으로 정성을 재계산하지 않음."""
    out: list[tuple[str, float, float]] = []
    for r in history_rows:
        q = r.get("qualitative")
        if q is None:
            continue
        ticker = r.get("ticker")
        sig_date = str(r.get("date"))
        series = closes_by_ticker.get(ticker)
        if not series:
            continue
        dates, closes = series
        rets = _forward_returns(dates, closes, sig_date, (primary,))
        if primary not in rets:
            continue
        out.append((sig_date, float(q), rets[primary]))
    return out


def qualitative_promotion_metrics(
    history_rows: list[dict],
    closes_by_ticker: dict[str, tuple[list[str], list[float]]],
    *,
    primary: int = PRIMARY_HORIZON,
) -> dict:
    """정성 팩터 shadow 승격용 실측·워크포워드 게이트.
    combine()/점수/봇에 영향 없음 — 관측·승인 UI용."""
    pairs = _qualitative_pairs(history_rows, closes_by_ticker, primary=primary)
    n = len(pairs)
    overall_ic = _spearman_ic([(q, ret) for _, q, ret in pairs])
    overall_ic_r = round(overall_ic, 3) if overall_ic is not None else None

    windows: list[dict] = []
    sorted_pairs = sorted(pairs, key=lambda x: x[0])
    if sorted_pairs:
        chunk = max(1, n // PROMOTION_WINDOWS)
        for i in range(PROMOTION_WINDOWS):
            start = i * chunk
            end = (i + 1) * chunk if i < PROMOTION_WINDOWS - 1 else n
            wp = sorted_pairs[start:end]
            w_ic = _spearman_ic([(q, ret) for _, q, ret in wp])
            windows.append({
                "window": i + 1,
                "n": len(wp),
                "from": wp[0][0] if wp else None,
                "to": wp[-1][0] if wp else None,
                "ic": round(w_ic, 3) if w_ic is not None else None,
            })
    else:
        windows = [{"window": i + 1, "n": 0, "from": None, "to": None, "ic": None}
                   for i in range(PROMOTION_WINDOWS)]

    g_samples = n >= PROMOTION_MIN_SAMPLES
    g_ic = overall_ic is not None and overall_ic >= PROMOTION_MIN_IC
    g_wf = all(
        w["n"] >= PROMOTION_WINDOW_MIN and w["ic"] is not None and w["ic"] > 0
        for w in windows
    )
    gates = {
        "min_samples": {"pass": g_samples, "required": PROMOTION_MIN_SAMPLES, "actual": n},
        "overall_ic": {"pass": g_ic, "minimum": PROMOTION_MIN_IC, "actual": overall_ic_r},
        "walk_forward": {
            "pass": g_wf,
            "required_positive_windows": PROMOTION_WINDOWS,
            "window_min_n": PROMOTION_WINDOW_MIN,
        },
    }
    eligible = g_samples and g_ic and g_wf
    return {
        "sample_count": n,
        "overall_ic": overall_ic_r,
        "primary_horizon": primary,
        "windows": windows,
        "gates": gates,
        "eligible_for_priority_or_threshold": eligible,
        "note": "정성 점수는 종합점수·매수 임계값·페이퍼 봇에 반영되지 않습니다(shadow 관측).",
    }
