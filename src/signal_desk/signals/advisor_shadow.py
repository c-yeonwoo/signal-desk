"""봇 LLM 자문 shadow — advisor 선별 vs 점수순 폴백을 나란히 기록하고 나중에 채점한다.

`advisor.select_buys()`는 LLM이 봇의 매수 종목에 실제로 영향을 주는 유일한 경로인데,
"그날 점수순으로 그냥 샀을 때보다 나았는가"를 재는 지표가 없었다. 이 모듈은 매 회차의
LLM 선택과 결정론적 폴백 선택을 함께 남기고, 이후 실현수익으로 둘을 비교한다.

겹치는 종목은 두 방식 모두 사는 것이므로 비교에서 상쇄된다 — 갈린 종목(llm_only vs
base_only)만이 advisor의 순효과다. 채점 규약(다음 거래일 진입·horizon 성숙)은
`accuracy`와 동일하게 맞춘다.

관측·채점 전용: 선별 로직·수량·문턱·리스크 규칙에는 개입하지 않는다.
"""

from __future__ import annotations

import datetime
import json
import logging
from typing import Any
from zoneinfo import ZoneInfo

from signal_desk.signals.accuracy import PRIMARY_HORIZON, forward_returns

log = logging.getLogger("signal_desk.advisor_shadow")

KEEP_DAYS = 90
POOL_KEEP = 12      # 파일 비대 방지 — 상위 후보만 남긴다
MIN_SAMPLES = 20    # 갈린 종목이 이만큼 성숙해야 우열을 말한다(accuracy와 동일 기준)


def _kst_today() -> str:
    return datetime.datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat()


def _path():
    from signal_desk.store import CACHE_DIR
    return CACHE_DIR / "advisor_shadow.json"


def _load() -> dict[str, Any]:
    path = _path()
    if not path.exists():
        return {}
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
        return blob if isinstance(blob, dict) else {}
    except Exception:
        log.warning("advisor_shadow.json 파싱 실패 — 새로 시작")
        return {}


def _save(blob: dict[str, Any]) -> None:
    from signal_desk.store import CACHE_DIR

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    keep = sorted(blob.keys())[-KEEP_DAYS:]
    blob = {k: blob[k] for k in keep}
    path = _path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(blob, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


def record(
    *,
    uid: int,
    market: str,
    pool: list[dict],
    picks: list[dict] | None,
    slots: int,
    date: str | None = None,
) -> bool:
    """한 회차의 선별 결과를 남긴다. pool은 점수 내림차순([{ticker,score}]).

    같은 (날짜·유저·시장)의 첫 회차만 남긴다 — 봇 루프가 하루에 여러 번 도는데,
    그날 실제로 자리를 잡은 결정은 첫 회차이고 이후는 잔여 슬롯 관측이라 표본이 편향된다.
    반환: 새로 기록했으면 True.
    """
    if not pool or slots <= 0:
        return False
    date = date or _kst_today()
    blob = _load()
    day = blob.setdefault(date, [])
    if any(r.get("uid") == uid and r.get("market") == market for r in day):
        return False
    baseline = [p["ticker"] for p in pool[:slots]]
    llm = [p["ticker"] for p in (picks or [])]
    day.append({
        "uid": uid,
        "market": market,
        "slots": slots,
        "advisor_used": bool(llm),
        "pool": [{"t": p["ticker"], "s": round(float(p["score"]), 3)} for p in pool[:POOL_KEEP]],
        "baseline": baseline,
        "llm": llm,
        # 겹친 것은 상쇄되므로 갈린 쪽만 채점 대상
        "llm_only": [t for t in llm if t not in baseline],
        "base_only": [t for t in baseline if t not in llm],
    })
    _save(blob)
    return True


def _avg(rets: list[float]) -> float | None:
    return round(sum(rets) / len(rets) * 100, 2) if rets else None


def summary(
    closes_by_ticker: dict[str, tuple[list[str], list[float]]],
    *,
    horizon: int = PRIMARY_HORIZON,
    min_samples: int = MIN_SAMPLES,
    limit_days: int = 30,
) -> dict[str, Any]:
    """LLM 선별 vs 점수순 폴백 실측 비교 — 관리자 관측용. 승격 게이트 아님."""
    blob = _load()
    if not blob:
        return {"ready": False, "days": [], "message": "advisor shadow 기록 없음(봇 회차마다 누적)"}

    llm_only_rets: list[float] = []
    base_only_rets: list[float] = []
    llm_all_rets: list[float] = []
    base_all_rets: list[float] = []
    runs = used = divergent = 0
    days: list[dict[str, Any]] = []

    def _rets_for(tickers: list[str], date: str) -> list[float]:
        out = []
        for t in tickers:
            series = closes_by_ticker.get(t)
            if not series:
                continue
            r = forward_returns(series[0], series[1], date, (horizon,))
            if horizon in r:
                out.append(r[horizon])
        return out

    for date in sorted(blob.keys()):
        recs = blob[date] or []
        day_div = 0
        for rec in recs:
            runs += 1
            if not rec.get("advisor_used"):
                continue
            used += 1
            lo, bo = rec.get("llm_only") or [], rec.get("base_only") or []
            if lo or bo:
                divergent += 1
                day_div += 1
            llm_only_rets += _rets_for(lo, date)
            base_only_rets += _rets_for(bo, date)
            llm_all_rets += _rets_for(rec.get("llm") or [], date)
            base_all_rets += _rets_for(rec.get("baseline") or [], date)
        days.append({"date": date, "runs": len(recs), "divergent": day_div})

    lo_avg, bo_avg = _avg(llm_only_rets), _avg(base_only_rets)
    matured = min(len(llm_only_rets), len(base_only_rets))
    return {
        "ready": matured > 0,
        "horizon": horizon,
        "runs": runs,
        "advisor_used_runs": used,
        "divergent_runs": divergent,
        "llm_only": {"n": len(llm_only_rets), "avg_ret_pct": lo_avg},
        "base_only": {"n": len(base_only_rets), "avg_ret_pct": bo_avg},
        "llm_all": {"n": len(llm_all_rets), "avg_ret_pct": _avg(llm_all_rets)},
        "baseline_all": {"n": len(base_all_rets), "avg_ret_pct": _avg(base_all_rets)},
        "delta_pct": (round(lo_avg - bo_avg, 2)
                      if lo_avg is not None and bo_avg is not None else None),
        "matured_pairs": matured,
        "min_samples": min_samples,
        # 표본이 차기 전 delta 부호로 advisor on/off를 판단하지 않는다
        "verdict_ready": matured >= min_samples,
        "days": days[-limit_days:],
        "disclaimer": "advisor shadow · 관측 전용 · 선별 로직·수량·문턱 미변경",
    }
