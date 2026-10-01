"""페이퍼 성과의 비교 가능한 증거: PIT 동일가중 경로와 날짜가 짝인 손해 경보.

현재 유니버스로 과거를 소급하지 않는다. 한 종목의 과거 봉, 계좌 평가일, 거래소 세션이
비면 숫자를 보간하거나 생존 종목만 평균하지 않고 비교를 보류한다.
"""

from __future__ import annotations

import math
import random
import re

from signal_desk import market_clock, store

_PRICE_GAP = re.compile(r"(\d{4}-\d{2}-\d{2})→(\d{4}-\d{2}-\d{2}) 가격 결측")


def session_points(curve: list[dict], market: str) -> tuple[list[dict], list[str]]:
    """거래 세션이 아닌 평가점은 비교 표본에서 뺀다. 뺀 날짜는 이름과 함께 돌려준다.

    휴장일 평가를 세션 사이에 두면 연속 세션 검사가 곡선 전체를 기권한다.
    그 점을 지우는 것이지, 비어 있는 거래일을 성과 0으로 잇지는 않는다.
    """
    kept: list[dict] = []
    dropped: list[str] = []
    for point in curve:
        day = str(point.get("date") or "")
        if market_clock.is_session(market, day):
            kept.append(point)
        else:
            dropped.append(day)
    return kept, dropped


def price_gap_bounds(reason: str | None) -> tuple[str, str] | None:
    """가격이 비어 비교가 멈춘 쌍. 세션이 비 continuous 한 이유는 여기 해당하지 않는다."""
    if not reason:
        return None
    found = _PRICE_GAP.search(reason)
    return (found.group(1), found.group(2)) if found else None


def non_session_note(dropped: list[str]) -> str:
    if not dropped:
        return ""
    shown = ", ".join(dropped[:3])
    extra = f" 외 {len(dropped) - 3}" if len(dropped) > 3 else ""
    return f"비거래일 평가 {len(dropped)}건({shown}{extra})은 비교에서 제외"


def pit_equal_weight_curve(curve: list[dict], market: str = "kr", *,
                           dated_closes: dict | None = None,
                           universe_history: dict | None = None) -> list[dict] | None:
    """매 세션 시작 시점 유니버스를 같은 비중으로 보유한 비용 전 종가 NAV(첫날=1)."""
    built, _reason = pit_equal_weight_detail(
        curve, market, dated_closes=dated_closes, universe_history=universe_history)
    return built


def pit_equal_weight_detail(curve: list[dict], market: str = "kr", *,
                            dated_closes: dict | None = None,
                            universe_history: dict | None = None) -> tuple[list[dict] | None, str | None]:
    """NAV와, 기권일 때의 첫 결손. 결손은 보간하지 않고 날짜·종목 수만 남긴다."""
    if market not in ("kr", "us") or len(curve) < 2:
        return None, "비교 불가 — 평가일 2개 미만"
    days = [str(p.get("date") or "") for p in curve]
    if days != sorted(set(days)):
        return None, "비교 불가 — 평가일 중복 또는 역순"
    for a, b in zip(days, days[1:]):
        if not market_clock.consecutive_sessions(market, a, b):
            return None, f"비교 불가 — {a}→{b} 연속 거래세션 아님"
    try:
        market_closes = (dated_closes if dated_closes is not None else
                         store.load_market_dated_closes("us") if market == "us"
                         else store.load_all_dated_closes())
        closes = {t: dict(zip(ds, ps)) for t, (ds, ps) in market_closes.items()}
        nav = 1.0
        out = [{"date": days[0], "total_eval": nav}]
        for a, b in zip(days, days[1:]):
            known_by = market_clock.previous_session(market, a)
            if universe_history is None:
                if market == "kr":
                    universe = store.universe_at(known_by) if known_by else None
                else:
                    history = store.load_us_universe_history()
                    universe = history.get(known_by) if known_by else None
            else:
                if market == "us":
                    universe = universe_history.get(known_by) if known_by else None
                else:
                    keys = sorted(k for k in universe_history if known_by and k <= known_by)
                    universe = universe_history[keys[-1]] if keys else None
            tickers = {str(u.get("ticker") or "") for u in (universe or [])}
            tickers.discard("")
            if not tickers:
                return None, f"비교 불가 — {known_by or a} 시점 구성종목 없음"
            ratios = []
            missing = []
            for ticker in sorted(tickers):
                series = closes.get(ticker) or {}
                old, new = series.get(a), series.get(b)
                if not old or not new or old <= 0 or new <= 0 or not math.isfinite(old * new):
                    missing.append(ticker)
                    continue
                ratios.append(new / old)
            if missing:
                shown = ", ".join(missing[:3])
                extra = f" 외 {len(missing) - 3}" if len(missing) > 3 else ""
                return None, f"비교 불가 — {a}→{b} 가격 결측 {len(missing)}종목 ({shown}{extra})"
            nav *= sum(ratios) / len(ratios)
            out.append({"date": b, "total_eval": nav})
        return out, None
    except Exception:  # noqa: BLE001 — 기준선 캐시 장애가 계좌 원장을 가리지 않도록 기권
        return None, "비교 불가 — 기준선 계산 실패"


def paired_harm(curve: list[dict], benchmark_curve: list[dict] | None, *,
                block_days: int = 5, resamples: int = 2000) -> dict:
    """동일 세션의 일별 로그 상대수익에 이동블록 부트스트랩 단측 95% 상한을 적용.

    이는 모형 가정에 의존하는 경고 근거이지 시장 열위의 수학적 '확정'은 아니다.
    블록은 겹쳐 재표본하고, 관측 세션 40개 미만에서는 추론하지 않는다.
    """
    out = {"ready": False, "alert": False, "reason": None, "excess_pp": None,
           "upper_pp": None, "blocks": 0, "days": len(curve),
           "method": "paired_moving_block_bootstrap", "basis": "relative_nav_pct",
           "note": "같은 거래일 봇/기준선의 상대 NAV 수익률. 단측 95% 추정 상한이 0 아래면 경고 후보."}
    if block_days < 2 or resamples < 100:
        out["reason"] = "재표본 설정 오류"
        return out
    if len(curve) - 1 < block_days * 8:
        out["reason"] = f"짝지은 거래일 {max(0, len(curve)-1)}개 — {block_days * 8}개 필요"
        return out
    if not benchmark_curve or len(curve) != len(benchmark_curve):
        out["reason"] = "날짜가 짝인 PIT 벤치마크 경로 없음"
        return out
    if any(a.get("date") != b.get("date") for a, b in zip(curve, benchmark_curve)):
        out["reason"] = "봇·벤치마크 평가일 불일치"
        return out
    daily = []
    for a, b, x, y in zip(curve, curve[1:], benchmark_curve, benchmark_curve[1:]):
        vals = [p.get("total_eval") for p in (a, b, x, y)]
        if any(not isinstance(v, (int, float)) or v <= 0 or not math.isfinite(v) for v in vals):
            out["reason"] = "평가액 결측·비정상"
            return out
        daily.append(math.log(b["total_eval"] / a["total_eval"])
                     - math.log(y["total_eval"] / x["total_eval"]))
    n = len(daily)
    rng = random.Random(1409)
    draws = []
    for _ in range(resamples):
        sample = []
        while len(sample) < n:
            start = rng.randrange(n - block_days + 1)
            sample.extend(daily[start:start + block_days])
        draws.append(sum(sample[:n]))
    draws.sort()
    upper_log = draws[math.ceil(resamples * 0.95) - 1]
    upper = math.expm1(upper_log) * 100
    observed = math.expm1(sum(daily)) * 100
    out.update(ready=True, blocks=n // block_days, excess_pp=round(observed, 2),
               upper_pp=round(upper, 2), alert=bool(upper < 0))
    if out["alert"]:
        out["reason"] = f"상대 NAV 수익률의 단측 95% 추정 상한 {out['upper_pp']}% < 0 — 손해 경고 후보"
    return out
