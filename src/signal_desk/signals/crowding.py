"""매수권 섹터 편중 경고 — crowded trade lite.

전체 Barra 없이, 오늘 매수권(BUY/STRONG_BUY)의 섹터 집중도만 본다.
섹터맵에 없는 종목은 '미분류'로 모이는데, 그건 crowded trade가 아니라 **데이터 공백**이다.

**기준선 없는 비율은 판정이 아니다(2026-08-16).** 예전엔 절대 40%로 `crowded` 를 판정했다.
유니버스 자체의 섹터 분포를 안 보므로 두 방향으로 틀린다:

- 유니버스가 원래 한 섹터에 쏠려 있으면 **정상인데 경고**가 뜬다. 실측 미국 매수권에서
  산업재는 10%였는데 유니버스가 17%다 — 절대값만 보면 "10% 차지"지만 실제로는 **덜 뽑혔다**.
- 표본이 작으면 **우연이 경고로 읽힌다**. 국내 매수권은 4종목인데 그중 2개가 같은 섹터면
  50%다. 유니버스에서 흔한 섹터라면 무작위로 뽑아도 자주 나오는 값이다.

그래서 유니버스 기준선(base rate)과 리프트를 함께 내고, 판정은 **초기하 p-value**로 한다 —
"무작위로 n종목 뽑았을 때 이 섹터가 k개 이상 나올 확률". 의존성 0으로 계산한다.
"""

from __future__ import annotations

import math
from collections import Counter

from signal_desk.reference import sectors
from signal_desk.signals.engine import is_buy

_UNMAPPED = "미분류"
_MIN_N = 3          # 이보다 적으면 어떤 분포도 우연과 구분되지 않는다
_MAX_P = 0.05       # 우연으로 이만큼 드물어야 편중이라 부른다
# p가 작아도 기준선과 큰 차이가 없으면 실무적으로 편중이 아니다. 실측 분포(2026-08-16)로
# 정했다 — 미국 매수권 리프트가 금융 +25 · 소재 +15 · 에너지 +6 · 유틸 +4 · 산업재 −7%p 였다.
_MIN_LIFT_PP = 15.0


def hypergeom_sf(k: int, N: int, K: int, n: int) -> float:
    """P(X >= k) — 유니버스 N개 중 그 섹터가 K개일 때 n개를 무작위로 뽑아 k개 이상 나올 확률.

    "우연히 몰린 것"과 "몰아서 고른 것"을 가르는 유일한 방법이다. 표준 라이브러리만 쓴다
    (이 리포는 통계 유틸에 외부 의존성을 두지 않는다 — `accuracy.t_two_sided_p`와 같은 규약).
    """
    if not (0 <= K <= N and 0 <= n <= N) or N <= 0:
        return 1.0
    k = max(k, max(0, n - (N - K)))
    hi = min(n, K)
    if k > hi:
        return 0.0
    total = math.comb(N, n)
    if total == 0:
        return 1.0
    return sum(math.comb(K, i) * math.comb(N - K, n - i) for i in range(k, hi + 1)) / total


def assess(signals, *, warn_pct: float = 40.0) -> dict:
    """signals: SignalResult 또는 {ticker,kind} dict — **유니버스 전체**를 넘긴다.

    매수권은 이 안에서 골라내고, 나머지가 기준선(base rate)이 된다. 매수권만 넘기면
    기준선을 만들 수 없어 예전처럼 절대 비율만 남는다.

    반환: {n_buy, top_sector, top_pct, base_pct, lift_pp, p_value, significant,
           warn, data_quality, note, distribution, baseline}
    """
    by_ticker: dict = {}
    buys: list[str] = []
    for s in signals or []:
        t = getattr(s, "ticker", None) or (s.get("ticker") if isinstance(s, dict) else None)
        if not t:
            continue
        by_ticker[t] = s
        kind = getattr(s, "kind", None) or (s.get("kind") if isinstance(s, dict) else None)
        if kind and is_buy(kind):
            buys.append(t)
    n = len(buys)
    if n == 0:
        return {"n_buy": 0, "top_sector": None, "top_pct": None, "base_pct": None,
                "lift_pp": None, "p_value": None, "significant": False, "warn": False,
                "data_quality": False, "note": "매수권 없음", "distribution": {}, "baseline": {}}

    # dict 행에 sector가 있으면(미국 유니버스 등) 그걸 쓰고, 없으면 KR 큐레이션 맵
    def _sec(s, t):
        if isinstance(s, dict) and s.get("sector"):
            return s["sector"]
        return sectors.sector_of(t) or _UNMAPPED

    cnt = Counter(_sec(by_ticker.get(t), t) for t in buys)
    top_sec, top_n = cnt.most_common(1)[0]
    top_pct = round(top_n / n * 100.0, 1)

    # 기준선 — 유니버스에서 그 섹터가 차지하는 비중. 유니버스를 못 받으면(매수권만 넘어온
    # 옛 호출) None으로 두고 **판정하지 않는다**. 모르는 채로 경고하면 그게 절대 비율이다.
    universe = Counter(_sec(s, t) for t, s in by_ticker.items())
    N = sum(universe.values())
    base_pct = lift_pp = p_value = None
    significant = False
    if N > n:                                    # 유니버스가 매수권보다 커야 기준선이 있다
        base_pct = round(universe[top_sec] / N * 100.0, 1)
        lift_pp = round(top_pct - base_pct, 1)
        p_value = round(hypergeom_sf(top_n, N, universe[top_sec], n), 4)
        significant = (n >= _MIN_N and p_value <= _MAX_P and lift_pp >= _MIN_LIFT_PP)

    # 미분류 쏠림 = 맵 공백(가짜 crowded). 진짜 업종 집중만 warn.
    unmapped_heavy = top_sec == _UNMAPPED and top_pct >= warn_pct and n >= _MIN_N
    data_quality = unmapped_heavy
    if base_pct is None:
        # 기준선을 만들 수 없다 — 예전 절대 문턱으로 되돌아가되 **모른다고 말한다**.
        warn = (not data_quality) and top_pct >= warn_pct and n >= _MIN_N
        note = (f"매수권 {n}종목 중 {_UNMAPPED} {top_pct}% — 섹터맵 공백(편중 아님)" if data_quality
                else f"매수권 {n}종목 · 최대 {top_sec} {top_pct}% (유니버스 기준선 없음 — 판정 보류)")
        return {"n_buy": n, "top_sector": top_sec, "top_pct": top_pct, "base_pct": None,
                "lift_pp": None, "p_value": None, "significant": False, "warn": warn,
                "data_quality": data_quality, "distribution": dict(cnt.most_common(5)),
                "baseline": {}, "note": note}

    warn = (not data_quality) and significant
    lift_txt = f"{lift_pp:+.1f}%p"
    if data_quality:
        note = f"매수권 {n}종목 중 {_UNMAPPED} {top_pct}% — 섹터맵 공백(편중 아님)"
    elif warn:
        note = (f"매수권 {n}종목 중 {top_sec} {top_pct}% "
                f"(유니버스 {base_pct}% · 리프트 {lift_txt} · p={p_value}) — crowded")
    elif n < _MIN_N:
        note = f"매수권 {n}종목 · 최대 {top_sec} {top_pct}% — 표본이 적어 판정 보류"
    else:
        note = (f"매수권 {n}종목 · 최대 {top_sec} {top_pct}% "
                f"(유니버스 {base_pct}% · 리프트 {lift_txt} · p={p_value})")
    return {
        "n_buy": n,
        "top_sector": top_sec,
        "top_pct": top_pct,
        "base_pct": base_pct,
        "lift_pp": lift_pp,
        "p_value": p_value,
        "significant": significant,
        "warn": warn,
        "data_quality": data_quality,
        "distribution": dict(cnt.most_common(5)),
        # 화면이 매수권 비율만 보면 기준선이 안 보인다 — 같은 섹터의 유니버스 비중을 함께 낸다.
        "baseline": {sec: round(universe[sec] / N * 100.0, 1) for sec, _ in cnt.most_common(5)},
        "note": note,
    }
