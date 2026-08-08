"""**시그널 발동 전** 이미 얼마나 올랐나 — 선반영 관측.

## 왜 필요한가 (2026-08-08 실측)

DART 공시가 SoT인데 찌라시는 공시보다 앞선다. 그래서 시그널이 뜬 시점엔 이미 움직인 뒤일 수
있고, 실측으로 그렇다:

    최근 5거래일 수익률 중위   상위 10종목 +12.12%  ·  중위 20종목 +5.74%
    최근 10거래일             상위 +1.12%          ·  중위 +3.74%

10일로 넓히면 오히려 낮다 — **최근 며칠에 몰린 급등** 뒤에 붙는다는 뜻이다. 설계의 결과이기도
하다(모멘텀 가중 0.30 + 추세 게이트가 하락추세를 막으니 정의상 "이미 오른 것"만 남는다).

## 기존 `entry_quality` 로는 잴 수 없다

`entry_quality.run_up_pct` 는 **우리 발동일부터**를 잰다. 발동 당일은 정의상 0이라 항상
`fresh` 다(실측: 매수권 2종목 둘 다 `run_up_pct:0 · age_days:0 · fresh`). 그 게이트는
"일주일째 매수권인데 +25% 갔다"(우리 신호가 낡은 경우)를 잡지, **"우리가 보기 전에 이미
올랐다"** 는 못 잡는다. 재는 대상이 다르다.

## 절대값이 아니라 분위로 낸다

`+12%` 는 시장 상황에 따라 뜻이 다르다 — 전 종목이 오른 날의 +12%와 시장이 빠진 날의 +12%는
같은 사건이 아니다. 그래서 **같은 날 전 종목 대비 백분위**를 함께 낸다. 절대값만 쓰면
문턱이 국면마다 다른 것을 재게 된다.

## 이 모듈은 판단하지 않는다

값을 낼 뿐 `kind`·점수·봇을 건드리지 않는다. 사전 상승이 큰 매수가 실제로 나쁜지는
**아직 측정되지 않았다** — 실현 18건 · 리프트 −5.3%p · 신뢰구간 ±23%p 로 무정보다.
먼저 재고, 유의해지면 그때 게이트를 만든다. 순서를 바꾸면 곡선 맞추기다.
"""

from __future__ import annotations

# 며칠을 "직전"으로 볼 것인가. 5거래일 = 약 1주 — 찌라시→공시 사이 지연을 담는 창이다.
# 창을 바꾸면 재는 대상이 바뀌므로 상수로 고정하고 산출물에 실어 보낸다.
PRE_WINDOW_DAYS = 5


def _num(v) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def trailing_return_pct(closes: list[float], *, window: int = PRE_WINDOW_DAYS,
                        end_index: int | None = None) -> float | None:
    """`end_index` 종가 기준 직전 `window` 거래일 수익률(%). 봉이 모자라면 None.

    `end_index` 를 주면 그 시점 기준으로 잰다 — **발동일 기준 사전 상승**을 재려면 필수다
    (오늘 기준으로 재면 발동 후 움직임이 섞인다).
    """
    if not closes:
        return None
    n = len(closes)
    i = n - 1 if end_index is None else int(end_index)
    if i < window or i >= n:
        return None
    a, b = _num(closes[i - window]), _num(closes[i])
    if a is None or b is None or a <= 0:
        return None
    return (b / a - 1.0) * 100.0


def _pctile(value: float, population: list[float]) -> float | None:
    """`population` 안에서 `value` 의 백분위(0~100). 표본이 적으면 None."""
    if value is None or len(population) < 20:
        return None
    below = sum(1 for x in population if x < value)
    return round(below / len(population) * 100.0, 1)


def annotate(rows: list[dict], *, closes_by: dict[str, list[float]],
             dates_by: dict[str, list[str]] | None = None,
             window: int = PRE_WINDOW_DAYS) -> list[dict]:
    """각 행에 `pre_move` 를 붙인다. **매수권만이 아니라 전 종목에** 붙인다.

    전 종목에 붙이는 이유: 백분위를 내려면 모집단이 필요하고, 나중에 채점할 때 "사전 상승이
    큰 매수 vs 작은 매수"를 비교하려면 **산 것과 안 산 것 양쪽**의 분포가 있어야 한다.

    발동일(`entry.fire_date`)이 있으면 그 날 기준으로, 없으면 오늘 기준으로 잰다 —
    어느 쪽인지 `basis` 로 밝힌다(같은 필드에 다른 뜻을 섞으면 나중에 못 가른다).
    """
    dates_by = dates_by or {}
    # ① 모집단 — 오늘 기준 직전 수익률(백분위의 분모). 발동일 기준과 섞지 않는다.
    pop: list[float] = []
    today_ret: dict[str, float] = {}
    for r in rows:
        tk = str(r.get("ticker") or "")
        v = trailing_return_pct(closes_by.get(tk) or [], window=window)
        if v is not None:
            today_ret[tk] = v
            pop.append(v)

    for r in rows:
        tk = str(r.get("ticker") or "")
        closes = closes_by.get(tk) or []
        entry = r.get("entry") or {}
        fire = entry.get("fire_date")
        idx, basis = None, "today"
        if fire:
            ds = [str(d)[:10] for d in (dates_by.get(tk) or [])]
            if str(fire)[:10] in ds:
                idx, basis = ds.index(str(fire)[:10]), "fire_date"
        val = (trailing_return_pct(closes, window=window, end_index=idx)
               if idx is not None else today_ret.get(tk))
        if val is None:
            r["pre_move"] = {"ready": False, "window_days": window,
                             "reason": f"직전 {window}거래일 봉이 모자랍니다"}
            continue
        r["pre_move"] = {
            "ready": True,
            "window_days": window,
            "basis": basis,                 # fire_date | today — 뜻이 다르므로 밝힌다
            "as_of": str(fire)[:10] if basis == "fire_date" else None,
            "run_up_pct": round(val, 2),
            # 절대값은 국면에 따라 뜻이 다르다 — 같은 날 전 종목 대비 위치를 함께 낸다.
            "pctile": _pctile(val, pop),
            "population": len(pop),
        }
    return rows


def summary(rows: list[dict], *, buy_kinds=("BUY", "STRONG_BUY")) -> dict:
    """매수권 vs 전체의 사전 상승 비교. **판정하지 않는다** — 관측값과 표본만 낸다.

    실제로 나쁜지는 실현 수익으로 채점해야 하고(`accuracy.diff_verdict`), 지금 표본으로는
    판정할 수 없다. 여기서 판정 문구를 내면 없는 근거가 생긴다.
    """
    def _med(xs):
        s = sorted(xs)
        n = len(s)
        return None if not n else (s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2)

    allv, buyv = [], []
    for r in rows:
        pm = r.get("pre_move") or {}
        if not pm.get("ready"):
            continue
        allv.append(pm["run_up_pct"])
        if str(r.get("kind")) in buy_kinds:
            buyv.append(pm["run_up_pct"])
    med_all, med_buy = _med(allv), _med(buyv)
    return {
        "window_days": PRE_WINDOW_DAYS,
        "n_all": len(allv), "n_buy": len(buyv),
        "median_all_pct": None if med_all is None else round(med_all, 2),
        "median_buy_pct": None if med_buy is None else round(med_buy, 2),
        "gap_pp": (None if (med_all is None or med_buy is None)
                   else round(med_buy - med_all, 2)),
        "note": ("매수권이 시장보다 얼마나 더 오른 뒤에 잡혔나 — **관측값이고 판정이 아니다.** "
                 "이게 성과에 나쁜지는 실현 수익으로 채점해야 하며, 현재 표본으로는 판정할 수 "
                 "없다(실현 18건 · 리프트 −5.3%p · 신뢰구간 ±23%p)."),
    }
