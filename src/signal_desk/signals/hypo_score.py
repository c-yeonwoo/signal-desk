"""이슈 흐름 사후 채점 — 지목한 업종이 그 뒤 실제로 시장을 이겼나.

## 왜 필요했나

`최근 이슈 흐름`은 LLM이 그럴듯한 갈래를 그려 주지만 **맞았는지 아무도 보지 않았다.**
그리고 볼 수도 없었다 — `kv:hypo:v4:latest` 1슬롯 덮어쓰기라 지난 흐름이 매번 파괴됐다
(`harness_last.json` 1슬롯으로 이미 겪은 병이다). 이력 없이는 정확도도 없다.

## 무엇을 재고 무엇을 안 재나

- **잰다**: 흐름이 지목한 업종의 대표 종목이, 흐름을 만든 다음 거래일부터 h거래일 동안
  **전 종목 평균(기준선)보다** 더 올랐나. `accuracy` 규약과 같은 진입·청산이다.
- **안 잰다**: 조건(`VIXCLS < 20` 등)의 충족 여부. 거시 캐시가 **스냅샷뿐**이라 과거 시점의
  지표값을 복원할 수 없다. 지어내지 않고 `conditions_scored: False`로 밝힌다.

## 기준선 없는 비율은 내지 않는다

"지목한 업종이 +3% 올랐다"는 상승장에서 아무 의미가 없다. 전 종목 평균 대비 **리프트**만
판정에 쓰고, 표본이 적으면 `판정 불가`가 기본값이다(`accuracy.diff_verdict` 공유).
"""

from __future__ import annotations

from signal_desk.signals import accuracy

# 채점 지평 — 이슈 흐름은 수 주 단위 서사라 실측 채점(20거래일)과 같은 지평을 쓴다.
# 지평을 기능마다 다르게 두면 리프트를 비교할 수 없다(2026-08-05에 이걸로 없는 숫자를 만들었다).
HORIZON_DAYS = 20
_MIN_RUNS = 5          # 이보다 적으면 어떤 판정도 하지 않는다


def _forward_pct(dates: list[str], closes: list[float], built_date: str,
                 horizon: int) -> float | None:
    """`accuracy` 와 **같은 규약** — 다음 거래일 진입 → h거래일 뒤 종가. 미성숙이면 None."""
    rets = accuracy._forward_returns(dates, closes, built_date, (horizon,))
    v = rets.get(horizon)
    return None if v is None else v * 100.0


def score(runs: list[dict], closes_by_ticker: dict, *,
          horizon: int = HORIZON_DAYS, min_runs: int = _MIN_RUNS) -> dict:
    """이력 채점. `runs` 는 `db.hypo_runs_recent()` 형태(built_at·sectors·tickers).

    반환: 흐름별 결과 + 전체 리프트 판정. **표본이 적으면 값 대신 이유를 낸다.**
    """
    rows: list[dict] = []
    picked: list[float] = []
    baseline: list[float] = []
    matured = unmatured = 0

    for r in runs:
        day = str(r.get("as_of") or (r.get("built_at") or "")[:10])
        tks = [t for t in (r.get("tickers") or []) if t]
        if not day or not tks:
            continue
        got: list[float] = []
        for t in tks:
            series = closes_by_ticker.get(t)
            if not series:
                continue
            v = _forward_pct(series[0], series[1], day, horizon)
            if v is not None:
                got.append(v)
        # 같은 날 전 종목 평균이 기준선 — "그 날 아무 종목이나 들었으면" 이다.
        # **지목 종목도 이 평균에 포함된다**(시장 평균의 정의). 리프트를 조금 낮추는 쪽이고,
        # 빼면 기준선이 "지목하지 않은 것들"이 되어 비교 대상이 바뀐다 — 보수적인 쪽을 고른다.
        base: list[float] = []
        for t, series in closes_by_ticker.items():
            v = _forward_pct(series[0], series[1], day, horizon)
            if v is not None:
                base.append(v)
        if not got or not base:
            unmatured += 1
            rows.append({"id": r.get("id"), "built_at": r.get("built_at"), "as_of": day,
                         "sectors": r.get("sectors") or [], "n_tickers": len(tks),
                         "picked_pct": None, "baseline_pct": None, "lift_pp": None,
                         "blocked_reason": f"{horizon}거래일이 아직 안 지났습니다"})
            continue
        matured += 1
        p = sum(got) / len(got)
        b = sum(base) / len(base)
        picked.append(p)
        baseline.append(b)
        rows.append({"id": r.get("id"), "built_at": r.get("built_at"), "as_of": day,
                     "sectors": r.get("sectors") or [], "n_tickers": len(got),
                     "picked_pct": round(p, 2), "baseline_pct": round(b, 2),
                     "lift_pp": round(p - b, 2), "blocked_reason": None})

    # 판정은 `accuracy.diff_verdict` 를 **공유**한다 — 통계 구현을 두 곳에 두면 갈라진다.
    verdict = accuracy.diff_verdict(picked, baseline, min_samples=min_runs)
    return {
        "horizon_days": horizon,
        "runs_total": len(runs), "matured": matured, "unmatured": unmatured,
        "min_runs": min_runs,
        "rows": rows,
        # 성숙 표본이 요건 미달이면 값을 내보내지 않는다 — 매번 보이면 그게 곧 다중검정이다.
        "mean_picked_pct": round(sum(picked) / len(picked), 2) if len(picked) >= min_runs else None,
        "mean_baseline_pct": round(sum(baseline) / len(baseline), 2) if len(baseline) >= min_runs else None,
        "lift_pp": round((sum(picked) / len(picked)) - (sum(baseline) / len(baseline)), 2)
        if len(picked) >= min_runs else None,
        "verdict": verdict,
        "blocked_reason": None if matured >= min_runs else (
            f"성숙한 흐름 {matured}/{min_runs}건 — 판정하려면 흐름이 더 쌓이고 "
            f"각각 {horizon}거래일이 지나야 합니다"),
        # 조건 채점은 하지 않는다는 사실을 **밝힌다**(지어내지 않는다).
        "conditions_scored": False,
        "conditions_note": "조건(VIX·CPI 등)의 사후 충족 여부는 채점하지 않습니다 — 거시 캐시가 "
                           "현재 스냅샷뿐이라 과거 시점 지표값을 복원할 수 없습니다.",
        "basis": f"흐름이 지목한 업종 대표 종목의 다음 거래일 진입 → {horizon}거래일 수익률, "
                 f"같은 날 전 종목 평균(기준선) 대비 리프트",
    }


def staleness(built_at: str | None, *, half_life_days: float = 4.0,
              stale_after_days: float = 7.0, today: str | None = None) -> dict:
    """흐름의 나이 → **판정**. 원시 타임스탬프는 "12일 전"을 말해주지 않는다.

    반감기 4일은 KB 검색이 뉴스에 쓰는 값과 같다(`kb_search`의 유형별 반감기) — 뉴스·시황에서
    뽑은 서사이므로 그 수명을 따른다. 기능마다 다른 수명을 쓰면 같은 자료가 화면마다 다른
    신선도를 갖는다.
    """
    import datetime as _dt
    if not built_at:
        return {"age_days": None, "stale": True, "fresh_pct": 0,
                "label": "생성된 흐름이 없습니다", "half_life_days": half_life_days}
    try:
        d = _dt.date.fromisoformat(str(built_at)[:10])
    except ValueError:
        return {"age_days": None, "stale": True, "fresh_pct": 0,
                "label": "생성 시각을 읽을 수 없습니다", "half_life_days": half_life_days}
    now = _dt.date.fromisoformat(today) if today else _dt.date.today()
    age = (now - d).days
    fresh = 0.5 ** (age / half_life_days) if half_life_days > 0 else 0.0
    stale = age >= stale_after_days
    if age <= 0:
        label = "오늘 만든 흐름"
    elif stale:
        label = f"{age}일 전 흐름 — 다시 생성 권장"
    else:
        label = f"{age}일 전 흐름"
    return {"age_days": age, "stale": stale, "fresh_pct": round(fresh * 100, 1),
            "label": label, "half_life_days": half_life_days,
            "stale_after_days": stale_after_days}
