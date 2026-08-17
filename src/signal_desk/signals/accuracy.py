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

import datetime
import math

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
# 종합점수 IC — 보드/proof가 읽는 키. 입력 팩터가 아니라 산출물이라 SCORING_FACTORS 밖.
IC_EXTRA_COLS = ("score",)
_MIN_IC_SAMPLES = 20  # 행 단위 표본 최소치 — 정밀도·diff_verdict용(한 행 = 한 판단)
# IC는 **날짜 단위**로 센다. 같은 날 200종목은 하나의 관측에 가깝다(횡단면이 서로 독립이 아니다) —
# store.REVISION_MIN_TESTABLE_DATES와 같은 규약. 행으로 세면 하루치 200건이 표본 200이 되어
# 문턱을 즉시 통과하고, 실제로 `short IC −0.148`이 **단 하루**(200/2200행)에서 나왔다.
_MIN_IC_DATES = 20
# 하루 횡단면에 이보다 적은 종목이 있으면 그 날의 IC는 노이즈다 → 그 날짜를 버린다.
_MIN_IC_BREADTH = 10
# 정밀도는 기준선(base rate) 대비 리프트로만 판정한다. 하락장에서는 아무 종목이나 '매도'라고
# 찍어도 정밀도가 60%를 넘기 때문에, 절대값 55%는 잘한 것도 못한 것도 아니다.
# 이 값은 "우연·시장 드리프트로 설명되지 않는다"고 부를 최소 리프트(%p)다.
MIN_LIFT_PP = 3.0


def _finite_float(v) -> float | None:
    """IC용 스칼라. None·NaN·inf는 제외 — NaN은 `is not None`을 통과한 뒤
    Spearman 정렬/동점 루프를 비정상적으로 느리게 만든다(실측: valuation NaN ≈ 멈춤)."""
    if v is None:
        return None
    try:
        fv = float(v)
    except (TypeError, ValueError):
        return None
    return fv if math.isfinite(fv) else None

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


def _ranks(vals: list[float]) -> list[float]:
    """동점은 평균 순위(ties → average rank)."""
    n = len(vals)
    order = sorted(range(n), key=lambda i: vals[i])
    out = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j < n and vals[order[j]] == vals[order[i]]:
            j += 1
        avg = (i + j - 1) / 2.0
        for k in range(i, j):
            out[order[k]] = avg
        i = j
    return out


def _spearman(pairs: list[tuple[float, float]], *, min_n: int) -> float | None:
    """(factor_value, fwd_ret) 쌍의 순위상관. 의존성 없이 직접 계산."""
    n = len(pairs)
    if n < min_n:
        return None
    rx = _ranks([p[0] for p in pairs])
    ry = _ranks([p[1] for p in pairs])
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    vx = sum((rx[i] - mx) ** 2 for i in range(n))
    vy = sum((ry[i] - my) ** 2 for i in range(n))
    if vx <= 0 or vy <= 0:
        return None
    return cov / (vx * vy) ** 0.5


def _spearman_ic(pairs: list[tuple[float, float]]) -> float | None:
    """행 단위 pooled Spearman.

    **팩터 IC에는 쓰지 말 것** — 날짜를 섞으면 시장 드리프트가 상관으로 들어온다
    (상승일엔 전 종목 수익이 양수라, 팩터 분포에 날짜별 수준 차이만 있어도 상관이 생긴다).
    팩터 IC는 `cross_sectional_ic`를 쓴다. 여기 남긴 이유는 정성 shadow처럼
    **애초에 횡단면이 아닌** 관측(`qualitative_promotion_metrics`)이 이 계약을 쓰기 때문이다.
    """
    return _spearman(pairs, min_n=_MIN_IC_SAMPLES)


# ── 검정 통계 ─────────────────────────────────────────────────────────────────
# IC 하나만 내보내면 크기가 판별력처럼 읽힌다. 실측: `momentum −0.341`이 가중 0.30을 정당화하는
# 근거로 쓰였는데 그 값에는 n·CI·t·p가 없었고, 날짜로 세면 10일(비중첩 2일)이었다.


def _betacf(a: float, b: float, x: float) -> float:
    """정규화 불완전베타의 연분수(Lentz). scipy 없이 t-분포 p를 내기 위한 최소 구현."""
    MAXIT, EPS, FPMIN = 300, 3e-16, 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < FPMIN:
        d = FPMIN
    d = 1.0 / d
    h = d
    for m in range(1, MAXIT + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < EPS:
            break
    return h


def _betainc(a: float, b: float, x: float) -> float:
    """정규화 불완전베타 I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbeta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    lfront = lbeta + a * math.log(x) + b * math.log1p(-x)
    if x < (a + 1.0) / (a + b + 2.0):
        return math.exp(lfront) * _betacf(a, b, x) / a
    return 1.0 - math.exp(lfront) * _betacf(b, a, 1.0 - x) / b


def t_two_sided_p(t: float | None, dof: int) -> float | None:
    """Student-t 양측 p. dof가 작을 때 정규근사는 p를 과소평가한다(n=10에서 30% 이상)."""
    if t is None or dof <= 0:
        return None
    if not math.isfinite(t):
        return 0.0
    return _betainc(dof / 2.0, 0.5, dof / (dof + t * t))


def _newey_west_se(xs: list[float], lag: int) -> dict:
    """평균의 Newey-West(Bartlett) 표준오차 — `{se, se_naive, degenerate, floored}`.

    h거래일 수익률을 매일 계산하면 창이 h−1일씩 겹쳐 IC 시계열이 자기상관을 갖는다. 소박한 SE는
    그만큼 작아지고 t는 부풀려진다(실측: 10일 h5에서 momentum 소박한 t = −5.18, 비중첩 2일로는 −1.62).

    **SE 하한은 소박한 SE로 둔다.** NW가 더 작게 나오는 경우가 있었다(reversion: 소박한 t −6.85 →
    NW t −10.68) — 음의 자기상관을 추정한 결과인데, n=10에서 lag 4까지 자기공분산을 추정한 값을
    믿고 iid보다 **더 정밀하다**고 주장하는 셈이다. 이건 통계적 항등식이 아니라 **보수성 선택**이고,
    적용 여부를 `floored`로 남긴다. 게이트가 읽을 수 없을 때는 막는 쪽이 안전하다는 규칙과 같은 이유다.
    """
    n = len(xs)
    if n < 2:
        return {"se": None, "se_naive": None, "degenerate": False, "floored": False}
    m = sum(xs) / n
    dev = [x - m for x in xs]
    g0 = sum(d * d for d in dev) / n
    naive = (sum(d * d for d in dev) / (n - 1) / n) ** 0.5   # 표본분산(n−1) 기반 평균의 SE
    if g0 <= 0:
        return {"se": 0.0, "se_naive": 0.0, "degenerate": False, "floored": False}
    lag = max(0, min(int(lag), n - 1))
    s = g0
    for j in range(1, lag + 1):
        gj = sum(dev[i] * dev[i - j] for i in range(j, n)) / n
        s += 2.0 * (1.0 - j / (lag + 1.0)) * gj
    if s <= 0:                                              # 자기공분산 합이 음수로 발산(소표본)
        return {"se": naive, "se_naive": naive, "degenerate": True, "floored": True}
    nw = (s / n) ** 0.5
    return {"se": max(nw, naive), "se_naive": naive,
            "degenerate": False, "floored": bool(nw < naive)}


def cross_sectional_ic(pairs_by_date: dict[str, list[tuple[float, float]]], *,
                       horizon: int, min_dates: int = _MIN_IC_DATES,
                       min_breadth: int = _MIN_IC_BREADTH) -> dict:
    """날짜별 횡단면 Spearman IC → 시계열의 평균·SE·t·p.

    반환 `ic`는 **날짜 수 요건을 채웠을 때만** 값이고, 아니면 None이며 `blocked_reason`이 이유를 말한다.
    `significant`가 False인 IC로 가중치를 바꾸면 곡선 맞추기다 — 소비자(brain_proposals)가 이 키를 본다.
    """
    dates = sorted(pairs_by_date)
    series: list[tuple[str, float, int]] = []
    thin = 0
    for d in dates:
        pairs = pairs_by_date[d]
        if len(pairs) < min_breadth:
            thin += 1
            continue
        ic = _spearman(pairs, min_n=min_breadth)
        if ic is not None:
            series.append((d, ic, len(pairs)))
    n = len(series)
    vals = [v for _, v, _ in series]
    breadths = sorted(b for _, _, b in series)
    out = {
        "ic": None,
        "n_dates": n,
        "n_pairs": sum(b for _, _, b in series),
        "thin_dates": thin,
        "breadth_median": (breadths[n // 2] if n else None),
        "horizon": horizon,
        "min_dates": min_dates,
        "ic_mean": (round(sum(vals) / n, 4) if n else None),
        "ic_std": None, "ic_ir": None, "se": None, "se_naive": None, "ci95": None,
        "t": None, "p": None, "significant": False, "zero_variance": False,
        "nw_lag": max(0, int(horizon) - 1), "nw_degenerate": False, "se_floored": False,
        # 겹치는 창을 몇 개의 독립 관측으로 볼 수 있는지 — "10일"과 "10관측"을 혼동하지 않게 같이 낸다.
        "independent_dates": (n // max(1, int(horizon)) if n else 0),
        "blocked_reason": None,
    }
    if n == 0:
        out["blocked_reason"] = (f"횡단면 IC 날짜 0개 — 성숙 스냅샷 대기"
                                 + (f" (종목 부족으로 버린 날짜 {thin}개)" if thin else ""))
        return out
    mean = sum(vals) / n
    if n > 1:
        var = sum((v - mean) ** 2 for v in vals) / (n - 1)
        out["ic_std"] = round(var ** 0.5, 4)
        if var > 0:
            out["ic_ir"] = round(mean / var ** 0.5, 3)
    nw = _newey_west_se(vals, out["nw_lag"])
    se = nw["se"]
    out["nw_degenerate"] = nw["degenerate"]
    out["se_floored"] = nw["floored"]
    out["se_naive"] = (round(nw["se_naive"], 4) if nw["se_naive"] is not None else None)
    if se is not None and se > 0:
        out["se"] = round(se, 4)
        out["ci95"] = round(1.96 * se, 4)
        t = mean / se
        out["t"] = round(t, 2)
        p = t_two_sided_p(t, n - 1)
        out["p"] = (round(p, 4) if p is not None else None)
    elif se == 0.0:
        # 날짜별 IC가 전부 동일 → 분산 0. 극한에서 t = ±∞ 이므로 p = 0 이 맞다.
        # (평균도 0이면 정보가 없으므로 p = 1.) 합성 픽스처에서만 나오는 경계이지만,
        # 여기서 None으로 두면 완벽한 팩터가 `무유의`로 찍혀 검사가 거짓 실패를 낸다.
        # inf는 JSON으로 내보내면 표준이 아니라 유한값으로 클램프하고 사실을 플래그로 남긴다.
        out["se"] = 0.0
        out["ci95"] = 0.0
        out["zero_variance"] = True
        out["t"] = (999.99 if mean > 0 else -999.99) if mean else None
        out["p"] = (0.0 if mean else 1.0)
    if n < min_dates:
        out["blocked_reason"] = (
            f"IC 날짜 {n}/{min_dates}일 — 판정 불가"
            f"(h{horizon} 중첩이라 독립 관측 ≈ {out['independent_dates']}개)")
        return out
    out["ic"] = round(mean, 4)
    out["significant"] = bool(out["p"] is not None and out["p"] < 0.05)
    if not out["significant"]:
        out["blocked_reason"] = "IC가 0과 구분 불가 — 가중치 근거로 쓸 수 없음"
    return out



def ic_eta(*, recorded_dates: int, matured_dates: int, need: int, horizon: int,
           today: datetime.date | None = None) -> dict:
    """팩터 IC가 **언제 측정 가능해지는지**. 축적만 하는 데이터엔 판정 날짜를 붙인다.

    `store.consensus_readiness` 와 같은 규약이다 — 조건 없는 축적은 영원히 안 본다.
    실측 동기(2026-08-06): `quality` 는 스냅샷 기록이 **오늘 시작**해 `n_dates=0` 이고
    화면은 `성숙 스냅샷 대기` 만 말했다. 그게 3일 뒤인지 두 달 뒤인지 알 수 없었다.

    두 단계로 막힌다:
      ① 기록  — 팩터 값이 스냅샷에 실린 날짜 수(`recorded_dates`). 하루 1개씩 는다.
      ② 성숙  — 그 날짜의 h거래일 뒤 종가가 있어야 IC 쌍이 된다(`matured_dates`).

    그래서 남은 거래일 = (need까지 더 기록할 날짜) + (need번째 날짜가 성숙할 때까지).
    두 번째 항은 이미 흐른 만큼 뺀다 — 스냅샷은 거래일마다 하나이므로 `recorded − need` 가
    그 경과 거래일이다.

    **측정 가능 ≠ 반영.** 판별력이 판정 불가인 동안 가중치·부호는 만지지 않는다.
    """
    recorded, matured = int(recorded_dates), int(matured_dates)
    need, horizon = int(need), int(horizon)
    if matured >= need:
        return {"recorded_dates": recorded, "eta_trading_days": 0, "eta_date": None,
                "blocked_by": None}
    more_records = max(0, need - recorded)
    elapsed = max(0, recorded - need)                  # need번째 날짜가 기록된 뒤 흐른 거래일
    ripen = max(0, horizon - elapsed)
    eta = more_records + ripen
    # 무엇이 막고 있는지 — "기록이 모자라다"와 "익기를 기다린다"는 다른 상태다.
    blocked_by = "record" if more_records else "ripen"
    d = today or datetime.date.today()
    left = eta
    while left > 0:                                    # 휴일 미반영 추정(영업일 기준)
        d += datetime.timedelta(days=1)
        if d.weekday() < 5:
            left -= 1
    return {"recorded_dates": recorded, "eta_trading_days": eta,
            "eta_date": d.isoformat() if eta else None, "blocked_by": blocked_by}

def mean_diff_se_pp(a: list[float], b: list[float]) -> float | None:
    """두 평균 차이의 표준오차(%p) — 관측 분산 기반. 양쪽 모두 2개 이상일 때만.
    "표본 N개 모이면 판정"이라는 착각을 막기 위해 모든 shadow가 이 한 구현을 공유한다."""
    if len(a) < 2 or len(b) < 2:
        return None

    def _var(xs: list[float]) -> float:
        m = sum(xs) / len(xs)
        return sum((x - m) ** 2 for x in xs) / (len(xs) - 1)

    return round((_var(a) / len(a) + _var(b) / len(b)) ** 0.5 * 100, 2)


def paired_mean_diff_se_pp(diffs: list[float]) -> float | None:
    """쌍별 차이(a−b, 수익률 비율) 목록 평균의 표준오차(%p).

    unpaired `mean_diff_se_pp`와 달리 같은 회차·같은 순위에서 맞춘 교체 쌍의 SE다.
    advisor shadow처럼 '상위 vs 하위' 구조 편향을 빼려면 이쪽을 쓴다."""
    if len(diffs) < 2:
        return None
    m = sum(diffs) / len(diffs)
    var = sum((x - m) ** 2 for x in diffs) / (len(diffs) - 1)
    return round((var / len(diffs)) ** 0.5 * 100, 2)


def diff_verdict(a: list[float], b: list[float], *, min_samples: int = _MIN_IC_SAMPLES) -> dict:
    """두 수익률 집합(a=검증 대상, b=대조군)의 평균차 판정 — shadow 공용.

    판정은 표본 수가 아니라 유의성으로 한다: 부호는 |delta| > CI95일 때만 읽고, 그 전까지는
    `blocked_reason`이 이유를 말한다. 관측만 쌓고 사람이 들여다봐야 아는 shadow는 안 보게 되므로,
    `verdict_ready`가 True면 관리자 화면에 판정 알림으로 떠야 한다."""
    avg_a = round(sum(a) / len(a) * 100, 2) if a else None
    avg_b = round(sum(b) / len(b) * 100, 2) if b else None
    delta = round(avg_a - avg_b, 2) if avg_a is not None and avg_b is not None else None
    se = mean_diff_se_pp(a, b)
    ci95 = round(1.96 * se, 2) if se is not None else None
    significant = bool(delta is not None and ci95 is not None and abs(delta) > ci95)
    matured = min(len(a), len(b))
    if matured == 0:
        reason = "성숙 표본 없음 — horizon 경과 대기"
    elif matured < min_samples:
        reason = f"표시 시작 표본 미달({matured}/{min_samples}) — 판정 불가"
    elif not significant:
        reason = "리프트가 오차 범위 안 — 무정보"
    else:
        reason = None
    return {"n": len(a), "n_control": len(b), "avg_pct": avg_a, "control_avg_pct": avg_b,
            "delta_pct": delta, "delta_se_pp": se, "delta_ci95_pp": ci95,
            "delta_significant": significant, "matured": matured, "min_samples": min_samples,
            "verdict_ready": bool(matured >= min_samples and significant),
            "blocked_reason": reason}


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
    ic_cols = FACTOR_COLS + IC_EXTRA_COLS
    # horizon별 IC 쌍 — primary가 아직 안 익어도 짧은 horizon으로 팩터 판별력을 본다.
    # **날짜로 한 겹 더 나눈다**: 팩터 IC는 그 날 종목 간 순위상관(횡단면)이라, 날짜를 섞으면
    # 시장 드리프트가 상관으로 들어온다. `{horizon: {factor: {date: [(값, 수익)]}}}`.
    ic_pairs: dict[int, dict[str, dict[str, list]]] = {h: {c: {} for c in ic_cols} for h in horizons}
    # 팩터별 **기록된** 날짜 — 성숙(가격) 여부와 무관하다. `ic_pairs` 는 익은 것만 담으므로
    # 이것 없이는 "아직 안 익음"과 "아예 기록이 안 됨"을 가를 수 없다(실측: quality 1일 · short 2일).
    # 날짜별 폭(비어 있지 않은 종목 수)도 센다 — 횡단면 IC는 폭 요건도 있다.
    breadth_by_factor: dict[str, dict[str, int]] = {c: {} for c in ic_cols}
    dates_seen: set[str] = set()
    tickers_seen: set[str] = set()
    rows_total = matured_primary = 0
    # 기준선용 — HOLD까지 포함한 전 표본의 horizon별 수익.
    base_by_h: dict[int, list[float]] = {h: [] for h in horizons}

    for r in history_rows:
        ticker = r.get("ticker")
        sig_date = str(r.get("date"))
        kind = r.get("kind")
        rows_total += 1
        dates_seen.add(sig_date)
        # **가격 유무 판단보다 먼저** 센다 — 아래 `continue` 뒤에 두면 기록 수가 과소집계된다.
        for c in ic_cols:
            if _finite_float(r.get(c)) is not None:
                breadth_by_factor[c][sig_date] = breadth_by_factor[c].get(sig_date, 0) + 1
        series = closes_by_ticker.get(ticker)
        if not series:
            continue
        dates, closes = series
        rets = _forward_returns(dates, closes, sig_date, horizons)
        if not rets:
            continue
        tickers_seen.add(ticker)
        if primary in rets:
            matured_primary += 1
        for h, ret in rets.items():
            base_by_h[h].append(ret)
            for c in ic_cols:
                fv = _finite_float(r.get(c))
                if fv is not None:
                    ic_pairs[h][c].setdefault(sig_date, []).append((fv, ret))
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

    def _side_stats(h: int) -> dict:
        buy_rets = by_tier[BUY].get(h, []) + by_tier[STRONG_BUY].get(h, [])
        sell_rets = by_tier[SELL].get(h, []) + by_tier[STRONG_SELL].get(h, [])
        base = base_by_h[h]
        buy_precision = _precision(buy_rets, up=True)
        sell_precision = _precision(sell_rets, up=False)
        base_up = _precision(base, up=True)
        base_down = _precision(base, up=False)
        return {
            "horizon": h,
            "buy_precision_pct": buy_precision,
            "buy_sample": len(buy_rets),
            "buy_precision_ci_pp": _ci_half_pp(buy_precision, len(buy_rets)),
            "buy_lift_pp": (round(buy_precision - base_up, 1)
                            if buy_precision is not None and base_up is not None else None),
            "sell_precision_pct": sell_precision,
            "sell_sample": len(sell_rets),
            "sell_precision_ci_pp": _ci_half_pp(sell_precision, len(sell_rets)),
            "sell_lift_pp": (round(sell_precision - base_down, 1)
                             if sell_precision is not None and base_down is not None else None),
            "baseline": {
                "sample": len(base),
                "up_pct": base_up,
                "down_pct": base_down,
                "avg_ret_pct": (round(sum(base) / len(base) * 100, 2) if base else None),
            },
        }

    by_horizon = {h: _side_stats(h) for h in horizons}
    # 헤드라인: primary가 익으면 그대로, 아니면 성숙 표본이 있는 가장 긴 horizon으로 임시 표시.
    # (h20 대기 중에도 fund/flow IC·단기 리프트를 볼 수 있어야 가중치를 돌릴 수 있다.)
    headline_h = primary if matured_primary > 0 else next(
        (h for h in sorted(horizons, reverse=True) if base_by_h[h]), None)
    head = by_horizon.get(headline_h) if headline_h is not None else None
    base_rets = base_by_h.get(primary) or []

    ic_h = headline_h if headline_h is not None else (
        primary if primary in ic_pairs else next(iter(ic_pairs), None))
    # 팩터 IC는 날짜별 횡단면 → 시계열 검정. `factor_ic`는 **요건을 채운 날에만** 값이 들어간다
    # (같은 shape을 유지해 소비자 계약은 그대로, 값의 의미만 정직해진다).
    factor_ic_stats = ({c: cross_sectional_ic(ic_pairs[ic_h][c], horizon=ic_h) for c in ic_cols}
                       if ic_h is not None else
                       {c: cross_sectional_ic({}, horizon=primary) for c in ic_cols})
    # **언제 측정 가능해지는지**를 같이 낸다. `blocked_reason` 만으로는 3일 뒤인지 두 달 뒤인지
    # 알 수 없고, 모르면 아무도 안 기다린다(=조건 없는 축적).
    _eta_h = ic_h if ic_h is not None else primary
    for c, st in factor_ic_stats.items():
        wide = sum(1 for n in breadth_by_factor[c].values() if n >= _MIN_IC_BREADTH)
        st.update(ic_eta(recorded_dates=wide, matured_dates=st.get("n_dates") or 0,
                         need=_MIN_IC_DATES, horizon=_eta_h))
    factor_ic = {c: s["ic"] for c, s in factor_ic_stats.items()}

    return {
        "ready": matured_primary > 0 or bool(headline_h and base_by_h[headline_h]),
        "horizons": list(horizons),
        "primary_horizon": primary,
        "headline_horizon": headline_h,
        "primary_ready": matured_primary > 0,
        "hit_threshold_pct": round(hit_ret * 100, 2),
        "tiers": tiers,
        "by_horizon": by_horizon,
        # 임시 헤드라인(primary 미성숙)에서 매수 표본이  Tiny면 리프트를 숨긴다(n=1 노이즈).
        **_headline_side(head, primary_ready=matured_primary > 0),
        "baseline": (head or {}).get("baseline") or {
            "sample": len(base_rets), "up_pct": None, "down_pct": None, "avg_ret_pct": None,
        },
        "lift_min_pp": MIN_LIFT_PP,
        # 날짜별 횡단면 Spearman IC의 시계열 평균. 요건 미달·무유의면 None(값 대신 이유를 낸다).
        "factor_ic": factor_ic,
        # 판정에 필요한 것 전부 — n_dates·ci95·t·p·significant·blocked_reason.
        # 크기만 내보내면 그 크기가 판별력처럼 읽힌다.
        "factor_ic_stats": factor_ic_stats,
        "factor_ic_horizon": ic_h,
        "ic_min_samples": _MIN_IC_SAMPLES,
        "ic_min_dates": _MIN_IC_DATES,
        "coverage": _coverage_block(
            rows_total, dates_seen, tickers_seen, matured_primary,
            closes_by_ticker, base_by_h, horizons, primary, headline_h),
    }


def _headline_side(head: dict | None, *, primary_ready: bool) -> dict:
    """헤드라인 매수/매도 칸.

    primary가 익기 전 임시 horizon에서는 표본 < IC 최소치면 정밀도·리프트를 숨긴다.
    (실측: BUY 1건에 buy_lift -40%p가 보드에 뜨던 것.) primary 성숙 후에는 작은 표본도
    숫자를 내고 CI 반폭으로 읽는다(기존 계약).
    """
    if not head:
        return {
            "buy_precision_pct": None, "buy_sample": 0, "buy_precision_ci_pp": None,
            "buy_lift_pp": None,
            "sell_precision_pct": None, "sell_sample": 0, "sell_precision_ci_pp": None,
            "sell_lift_pp": None,
        }
    buy_n = head.get("buy_sample") or 0
    sell_n = head.get("sell_sample") or 0
    buy_ok = primary_ready or buy_n >= _MIN_IC_SAMPLES
    sell_ok = primary_ready or sell_n >= _MIN_IC_SAMPLES
    return {
        "buy_precision_pct": head.get("buy_precision_pct") if buy_ok else None,
        "buy_sample": buy_n,
        "buy_precision_ci_pp": head.get("buy_precision_ci_pp") if buy_ok else None,
        "buy_lift_pp": head.get("buy_lift_pp") if buy_ok else None,
        "sell_precision_pct": head.get("sell_precision_pct") if sell_ok else None,
        "sell_sample": sell_n,
        "sell_precision_ci_pp": head.get("sell_precision_ci_pp") if sell_ok else None,
        "sell_lift_pp": head.get("sell_lift_pp") if sell_ok else None,
    }


def _coverage_block(rows_total, dates_seen, tickers_seen, matured_primary,
                    closes_by_ticker, base_by_h, horizons, primary, headline_h) -> dict:
    diag = _join_diagnosis(dates_seen, closes_by_ticker, tickers_seen, matured_primary)
    interim_note = None
    if matured_primary == 0 and headline_h and headline_h != primary:
        interim_note = (f"primary h{primary} 미성숙 — 임시로 h{headline_h} "
                        f"({len(base_by_h[headline_h])}표본) 표시")
        # 임시 헤드라인이 있으면 '0=고장'으로 읽히지 않게 차단 문구를 비운다.
        diag["blocked_reason"] = None
    return {
        "rows": rows_total,
        "dates": len(dates_seen),
        "from": min(dates_seen) if dates_seen else None,
        "to": max(dates_seen) if dates_seen else None,
        "tickers_matched": len(tickers_seen),
        "matured_primary": matured_primary,
        "matured_by_horizon": {str(h): len(base_by_h[h]) for h in horizons},
        "interim_note": interim_note,
        **diag,
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


# ---------- 사전등록된 실측 정확도 판정 (prereg accuracy_looks 전용) ----------
#
# 헤드라인 실측은 h20이고 거기엔 아직 매수 표본이 0이다. 실제 매매는 단기(봇 3일·하네스 5일)라
# 그 지평의 판별력을 **미리 못 박고** 재는 경로가 필요하다. 지평을 나중에 고르면 그건 측정이
# 아니라 고르기다(지평 3개 중 좋아 보이는 하나 = 우연 통과 확률 14%).
#
# 통계 주의 두 가지 — 둘 다 소박한 이항 SE를 **과소평가** 쪽으로 틀리게 한다:
#   ① 같은 날 여러 종목을 사면 그 판단들은 독립이 아니다(시장 드리프트 공유).
#   ② h거래일 수익을 매일 재면 창이 h−1일씩 겹친다.
# 그래서 **날짜를 h일 블록으로 묶어** 블록 안에서 평균 내고, 블록 간 분산으로 SE를 낸다.
# 블록끼리는 창이 안 겹치므로 독립에 가깝다(IC에서 Newey-West를 쓴 것과 같은 이유).


def buy_hits_by_date(history_rows: list[dict],
                     closes_by_ticker: dict[str, tuple[list[str], list[float]]],
                     *, horizon: int, hit_ret: float = 0.005,
                     from_date: str | None = None) -> dict[str, list[bool]]:
    """{날짜: [매수 판단이 hit_ret 이상 올랐는가]} — 성숙한 것만.

    `from_date`가 있으면 그 **이후** 날짜만 — 사전등록 OOS 구간을 여기서 자른다.
    """
    out: dict[str, list[bool]] = {}
    for r in history_rows:
        if not is_buy(str(r.get("kind") or "")):
            continue
        d = str(r.get("date"))
        if from_date and d < str(from_date):
            continue
        series = closes_by_ticker.get(r.get("ticker"))
        if not series:
            continue
        rets = _forward_returns(series[0], series[1], d, (horizon,))
        if horizon in rets:
            out.setdefault(d, []).append(rets[horizon] >= hit_ret)
    return out


def block_lift_verdict(hits_by_date: dict[str, list[bool]], *, baseline_pct: float | None,
                       baseline_sample: int, horizon: int, z: float,
                       min_lift_pp: float) -> dict:
    """겹치지 않는 h일 블록으로 묶어 리프트 하한을 낸다. 하한 > min_lift_pp 여야 통과.

    소박한 이항 SE를 쓰지 않는 이유는 위 주석 참고 — 같은 날 군집과 창 중첩 때문에 과소평가된다.
    `z`는 Šidák 보정된 다중검정 문턱에서 온다(등록 look이 많을수록 커진다).
    """
    dates = sorted(hits_by_date)
    n = sum(len(v) for v in hits_by_date.values())
    blocks: list[float] = []
    for i in range(0, len(dates), max(1, horizon)):
        chunk = [h for d in dates[i:i + horizon] for h in hits_by_date[d]]
        if chunk:
            blocks.append(sum(chunk) / len(chunk) * 100.0)
    if not blocks or baseline_pct is None:
        return {"n": n, "n_blocks": len(blocks), "precision_pct": None, "lift_pp": None,
                "se_pp": None, "lift_lower_pp": None, "passes": False,
                "blocked_reason": "성숙 표본 없음" if not blocks else "기준선 없음"}
    prec = sum(blocks) / len(blocks)
    lift = prec - float(baseline_pct)
    if len(blocks) < 2:
        return {"n": n, "n_blocks": len(blocks), "precision_pct": round(prec, 2),
                "lift_pp": round(lift, 2), "se_pp": None, "lift_lower_pp": None,
                "passes": False, "blocked_reason": "독립 블록 1개 — 분산을 잴 수 없다"}
    m = prec
    var = sum((b - m) ** 2 for b in blocks) / (len(blocks) - 1)
    se = (var / len(blocks)) ** 0.5
    # 기준선 자체의 표본오차도 더한다 — 크지 않지만 빼면 관대한 쪽으로 틀린다.
    if baseline_sample and baseline_sample > 1:
        p0 = float(baseline_pct) / 100.0
        se = (se ** 2 + (p0 * (1 - p0) / baseline_sample) * 10000.0) ** 0.5
    lower = lift - z * se
    return {"n": n, "n_blocks": len(blocks), "precision_pct": round(prec, 2),
            "lift_pp": round(lift, 2), "se_pp": round(se, 2),
            "lift_lower_pp": round(lower, 2), "z": round(z, 3),
            "passes": bool(lower > float(min_lift_pp)), "blocked_reason": None}


# ---------- 횡단면 IC 기반 사전등록 판정 (prereg ic_looks 전용) ----------
#
# **왜 포트폴리오 수익률이 아니라 IC인가(2026-08-17 실측).** 같은 데이터로 두 통계량을 재봤다:
#
#     포트폴리오 기간수익   +0.253%/기간 (219기간)  t = 0.89   ← 200종목 중 6개만 씀
#     횡단면 IC            +0.0200 (독립 210일)     t = 1.56   ← 200종목 전부 씀
#
# IC가 1.75배 효율적이다(= 같은 t에 3배 적은 표본). 정보의 대부분을 버리던 것을 되찾는 것이라
# 공짜다. 그래도 **둘 다 유의하지 않다** — 확정에는 여전히 오래 걸린다는 사실은 안 바뀐다.
#
# 노이즈의 성질도 재 두었다: IC 날짜별 표준편차 0.186 인데 200종목 유한표본에서 오는 이론값은
# 1/√199 = 0.071 뿐이다. 나머지 0.172 는 **국면에 따라 팩터가 먹기도 안 먹기도 하는 실제
# 변동**이라 종목을 더 넣어도 안 줄어든다. "표본을 늘리면 된다"가 통하지 않는 이유다.


def ic_series(history_rows: list[dict],
              closes_by_ticker: dict[str, tuple[list[str], list[float]]],
              *, horizon: int, col: str = "score",
              from_date: str | None = None, min_breadth: int = 30) -> dict[str, float]:
    """{날짜: 그 날 횡단면 스피어만 IC}. 폭이 `min_breadth` 미만인 날은 뺀다.

    **날짜가 관측 단위다** — 같은 날 200종목은 하나의 관측이다(pooled 상관이 IC를 속인 함정).
    """
    by_date: dict[str, list[tuple[float, float]]] = {}
    for r in history_rows:
        d = str(r.get("date"))
        if from_date and d < str(from_date):
            continue
        s = _finite_float(r.get(col))
        series = closes_by_ticker.get(r.get("ticker"))
        if s is None or not series:
            continue
        rets = _forward_returns(series[0], series[1], d, (horizon,))
        if horizon in rets:
            by_date.setdefault(d, []).append((s, rets[horizon]))
    out: dict[str, float] = {}
    for d, pairs in by_date.items():
        if len(pairs) < min_breadth:
            continue
        ic = _spearman(pairs, min_n=min_breadth)
        if ic is not None:
            out[d] = ic
    return out


def ic_verdict(ics: dict[str, float], *, horizon: int, z: float,
               min_independent: int, mie: float) -> dict:
    """IC 시계열 → 판정. 귀무가설은 **IC = 0**이고, `mie` 는 표본 크기만 정한다.

    `mie`(최소 관심 우위)를 판정 문턱으로 쓰지 않는 이유: 그러면 "0.08 미만은 없는 것"이 되어
    작지만 진짜인 우위를 기각한다. `mie` 는 **얼마나 오래 볼지**를 정하는 값이고, 판정은
    언제나 "0보다 큰가"다.

    독립 관측은 `날짜 수 / horizon` 이다 — h거래일 수익을 매일 재면 창이 h−1일씩 겹친다
    (중첩 창의 t는 부풀려진다는 규칙을 IC에도 그대로 적용).
    """
    n_dates = len(ics)
    n_ind = n_dates / max(1, horizon)
    prog = {"dates": n_dates, "independent": round(n_ind, 1),
            "min_independent": min_independent,
            "remaining_independent": max(0.0, round(min_independent - n_ind, 1)),
            "remaining_trading_days": max(0, int(round((min_independent - n_ind) * horizon))),
            "met": n_ind >= min_independent}
    if n_dates < 2:
        return {"status": "pending", "verdict": "판정 보류",
                "verdict_why": f"IC 날짜 {n_dates}일 — 분산을 잴 수 없다",
                "requirement": prog, "ic": None, "t": None, "ic_lower": None, "mie": mie}
    vals = list(ics.values())
    m = sum(vals) / len(vals)
    var = sum((v - m) ** 2 for v in vals) / (len(vals) - 1)
    se = (var / n_ind) ** 0.5 if n_ind > 0 else None
    t = (m / se) if se else None
    if not prog["met"]:
        # **요건 미달이면 수치를 비운다.** 매주 IC가 보이면 매주 보게 되고 그게 곧 다중검정이다.
        return {"status": "pending", "verdict": "판정 보류",
                "verdict_why": (f"독립 관측 {n_ind:.1f}/{min_independent}개 "
                                f"(거래일 {n_dates}일 · h{horizon} 중첩 보정)"),
                "requirement": prog, "ic": None, "t": None, "ic_lower": None, "mie": mie}
    lower = m - z * se
    return {"status": "decided",
            "verdict": "판별력 있음" if lower > 0 else "판별력 없음",
            "verdict_why": (f"IC {m:+.4f} · 하한 {lower:+.4f} · t {t:.2f} "
                            f"(문턱 z {z:.2f} · 독립 {n_ind:.1f}개)"),
            "requirement": prog, "ic": round(m, 4), "t": round(t, 2),
            "ic_lower": round(lower, 4), "se": round(se, 4), "mie": mie}


def ic_progress(ics: dict[str, float], *, horizon: int, z: float,
                min_independent: int, mie: float) -> dict:
    """**주간 트랙** — 판정이 아니라 진척과 조기 기각 경계.

    요건 미달 동안 `ic_verdict` 는 수치를 비우지만, 여기서는 t 추이를 보여준다. 둘의 차이가
    규약이다: **판정은 1회·엄격**, **진척은 상시·구속력 없음**. 진척을 근거로 파라미터를
    바꾸면 그게 곧 다중검정이므로 `binding=False` 를 명시해 실어 보낸다.
    """
    n_dates, vals = len(ics), list(ics.values())
    n_ind = n_dates / max(1, horizon)
    out = {"binding": False, "dates": n_dates, "independent": round(n_ind, 1),
           "min_independent": min_independent, "mie": mie,
           "t": None, "ic": None, "projected_t_at_target": None,
           "futile": False, "futility_reason": None,
           "note": "진척 관측이다 — 판정도 아니고 파라미터 변경 근거도 아니다"}
    if len(vals) < 2 or n_ind <= 0:
        return out
    m = sum(vals) / len(vals)
    var = sum((v - m) ** 2 for v in vals) / (len(vals) - 1)
    sd = var ** 0.5
    se = (var / n_ind) ** 0.5
    out["ic"], out["t"] = round(m, 4), round(m / se, 2) if se else None
    # 지금 추세가 그대로 이어지면 요건 시점의 t는 얼마인가 — t는 √N으로 자란다.
    if se and min_independent > 0:
        out["projected_t_at_target"] = round(m / (sd / min_independent ** 0.5), 2)
        # **조기 기각**: 요건 시점까지 남은 관측이 전부 와도 문턱을 넘으려면 앞으로 평균
        # IC가 얼마여야 하나. 그 값이 최소관심우위의 2배를 넘으면 기다릴 이유가 없다.
        need_total = z * sd / min_independent ** 0.5 * min_independent      # Σ IC 필요합
        have = m * n_ind
        left = max(0.0, min_independent - n_ind)
        if left > 0:
            need_future = (need_total - have) / left
            out["required_future_ic"] = round(need_future, 4)
            if need_future > 2 * mie:
                out["futile"] = True
                out["futility_reason"] = (
                    f"남은 {left:.0f}관측이 평균 IC {need_future:.3f}로 와야 문턱을 넘는다 — "
                    f"최소관심우위({mie})의 2배 초과라 기다릴 근거가 없다")
    return out
