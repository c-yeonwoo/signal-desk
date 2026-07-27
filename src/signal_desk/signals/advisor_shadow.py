"""봇 LLM 자문 shadow — advisor 선별 vs 점수순 폴백을 나란히 기록하고 나중에 채점한다.

`advisor.select_buys()`는 LLM이 봇의 매수 종목에 실제로 영향을 주는 유일한 경로인데,
"그날 점수순으로 그냥 샀을 때보다 나았는가"를 재는 지표가 없었다. 이 모듈은 매 회차의
LLM 선택과 결정론적 폴백 선택을 함께 남기고, 이후 실현수익으로 둘을 비교한다.

겹치는 종목은 두 방식 모두 사는 것이므로 비교에서 상쇄된다 — 갈린 종목(llm_only vs
base_only)만이 advisor의 순효과다. 채점 규약(다음 거래일 진입·horizon 성숙)은
`accuracy`와 동일하게 맞춘다.

**공정 비교는 순위 매칭 paired**: `base_only`는 항상 점수 상위·`llm_only`는 그 아래라
unpaired 평균차는 advisor에게 구조적으로 불리하다. 회차 안 pool 순위로 교체 쌍을 맞춘
`paired_delta`가 실력 판정의 정본이고, unpaired `delta_pct`는 하위호환·진단용으로 남긴다.

관측·채점 전용: 선별 로직·수량·문턱·리스크 규칙에는 개입하지 않는다.
"""

from __future__ import annotations

import datetime
import json
import logging
from typing import Any
from zoneinfo import ZoneInfo

from signal_desk.signals.accuracy import (
    PRIMARY_HORIZON,
    forward_returns,
    mean_diff_se_pp,
    paired_mean_diff_se_pp,
)

log = logging.getLogger("signal_desk.advisor_shadow")

KEEP_DAYS = 90
POOL_KEEP = 12      # 파일 비대 방지 — 상위 후보만 남긴다
# 표시를 시작할 최소 표본. **판정 기준이 아니다** — 20쌍은 통계적으로 아무것도 보장하지 않는다.
# σ(20거래일 수익 표준편차)=12%면 unpaired 20표본 평균차의 표준오차가 ±3.8%p라서, 유의하려면
# 리프트가 ±7.4%p를 넘어야 한다(그 정도 알파는 실력보다 운·버그를 먼저 의심할 크기다).
# 그래서 판정은 표본 수가 아니라 관측된 분산으로 계산한 `delta_significant`로 한다.
MIN_SAMPLES = 20
# bot.REFERENCE_BOTS와 동일 — bot을 import하면 순환. 과거 기록에 style이 없을 때 uid로 폴백.
_REF_STYLE = {900001: "conservative", 900002: "balanced", 900003: "aggressive"}
_STYLES = ("conservative", "balanced", "aggressive")


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


def _style_of(rec: dict) -> str | None:
    s = rec.get("style")
    if s in _STYLES:
        return s
    return _REF_STYLE.get(rec.get("uid"))


def record(
    *,
    uid: int,
    market: str,
    pool: list[dict],
    picks: list[dict] | None,
    slots: int,
    style: str | None = None,
    date: str | None = None,
) -> bool:
    """한 회차의 선별 결과를 남긴다. pool은 점수 내림차순([{ticker,score}]).

    같은 (날짜·유저·시장)의 첫 회차만 남긴다 — 봇 루프가 하루에 여러 번 도는데,
    그날 실제로 자리를 잡은 결정은 첫 회차이고 이후는 잔여 슬롯 관측이라 표본이 편향된다.

    picks의 세 상태를 구분해 기록한다(`outcome`):
      - 종목 리스트 → `picked`
      - `[]` → `abstained`. LLM이 "살 게 없다"고 판단한 회차. 점수순은 샀으므로 비교 대상이다.
      - `None` → `unavailable`. 키 없음·실패. advisor 성적으로 세면 안 된다.
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
    outcome = "unavailable" if picks is None else ("picked" if picks else "abstained")
    row = {
        "uid": uid,
        "market": market,
        "slots": slots,
        "outcome": outcome,
        "advisor_used": outcome != "unavailable",
        "pool": [{"t": p["ticker"], "s": round(float(p["score"]), 3)} for p in pool[:POOL_KEEP]],
        "baseline": baseline,
        "llm": llm,
        # 겹친 것은 상쇄되므로 갈린 쪽만 채점 대상
        "llm_only": [t for t in llm if t not in baseline],
        "base_only": [t for t in baseline if t not in llm],
    }
    st = style if style in _STYLES else _REF_STYLE.get(uid)
    if st:
        row["style"] = st
    day.append(row)
    _save(blob)
    return True


def _avg(rets: list[float]) -> float | None:
    return round(sum(rets) / len(rets) * 100, 2) if rets else None


def _se_pp(a: list[float], b: list[float]) -> float | None:
    """두 평균 차이의 표준오차(%p) — 구현은 accuracy 한 곳에 두고 공유한다(shadow마다 따로 짜면
    그 차이가 판정 차이로 둔갑한다)."""
    return mean_diff_se_pp(a, b)


def _rank_map(pool: list[dict]) -> dict[str, int]:
    """pool 순서 = 점수 순위(0이 최상위). 과거 기록의 {t,s} 형태."""
    out: dict[str, int] = {}
    for i, p in enumerate(pool or []):
        t = p.get("t") or p.get("ticker")
        if t is not None and t not in out:
            out[t] = i
    return out


def _pair_diffs(
    rec: dict,
    date: str,
    rets_for,
) -> list[float]:
    """회차 안 순위 매칭 교체 쌍의 (ret_llm − ret_base) 목록.

    base_only·llm_only를 pool 순위 오름차순으로 정렬해 zip한다 — 같은 '자리'끼리 맞춘다.
    기권은 LLM=현금 0%로, 성숙한 base_only마다 0−base 쌍을 만든다.
    """
    outcome = rec.get("outcome") or ("picked" if rec.get("advisor_used") else "unavailable")
    lo = list(rec.get("llm_only") or [])
    bo = list(rec.get("base_only") or [])
    if not bo and not lo:
        return []
    if outcome == "abstained":
        return [0.0 - r for r in rets_for(bo, date)]
    rank = _rank_map(rec.get("pool") or [])
    # pool이 비어 있으면(구형 기록) 원래 리스트 순서를 순위로 본다
    lo_s = sorted(lo, key=lambda t: rank.get(t, 10_000))
    bo_s = sorted(bo, key=lambda t: rank.get(t, 10_000))
    n = min(len(lo_s), len(bo_s))
    diffs: list[float] = []
    for i in range(n):
        lr, br = rets_for([lo_s[i]], date), rets_for([bo_s[i]], date)
        if lr and br:
            diffs.append(lr[0] - br[0])
    return diffs


def _diff_stats(diffs: list[float], *, min_samples: int) -> dict[str, Any]:
    n = len(diffs)
    delta = round(sum(diffs) / n * 100, 2) if n else None
    se = paired_mean_diff_se_pp(diffs)
    ci95 = round(1.96 * se, 2) if se is not None else None
    significant = bool(delta is not None and ci95 is not None and abs(delta) > ci95)
    if n == 0:
        reason = "성숙한 교체 쌍 없음 — horizon 경과 대기"
    elif n < min_samples:
        reason = f"표시 시작 표본 미달({n}/{min_samples}) — 판정 불가"
    elif not significant:
        reason = "리프트가 오차 범위 안 — 무정보"
    else:
        reason = None
    return {
        "paired_n": n,
        "paired_delta_pct": delta,
        "paired_delta_se_pp": se,
        "paired_delta_ci95_pp": ci95,
        "paired_delta_significant": significant,
        "paired_verdict_ready": bool(n >= min_samples and significant),
        "paired_blocked_reason": reason,
    }


def _empty_bucket() -> dict[str, Any]:
    return {
        "runs": 0, "advisor_used_runs": 0, "abstained_runs": 0, "divergent_runs": 0,
        "llm_only_rets": [], "base_only_rets": [], "paired_diffs": [],
    }


def summary(
    closes_by_ticker: dict[str, tuple[list[str], list[float]]],
    *,
    horizon: int = PRIMARY_HORIZON,
    min_samples: int = MIN_SAMPLES,
    limit_days: int = 30,
) -> dict[str, Any]:
    """LLM 선별 vs 점수순 폴백 실측 비교 — 관리자 관측용. 승격 게이트 아님.

    판정의 정본은 `paired_*`(회차 안 순위 매칭). unpaired `delta_pct`는 구조 편향이 남아
    진단용으로만 읽는다.
    """
    blob = _load()
    if not blob:
        return {"ready": False, "days": [], "message": "advisor shadow 기록 없음(봇 회차마다 누적)"}

    llm_only_rets: list[float] = []
    base_only_rets: list[float] = []
    llm_all_rets: list[float] = []
    base_all_rets: list[float] = []
    paired_diffs: list[float] = []
    runs = used = divergent = abstained = 0
    days: list[dict[str, Any]] = []
    by_style: dict[str, dict[str, Any]] = {s: _empty_bucket() for s in _STYLES}

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
            # outcome 없는 과거 기록은 advisor_used로 유추(기권 구분 도입 전 데이터)
            outcome = rec.get("outcome") or ("picked" if rec.get("advisor_used") else "unavailable")
            if outcome == "unavailable":
                continue
            used += 1
            lo, bo = rec.get("llm_only") or [], rec.get("base_only") or []
            if lo or bo:
                divergent += 1
                day_div += 1
            base_scored = _rets_for(bo, date)
            if outcome == "abstained":
                abstained += 1
                # 기권은 "현금 보유"다. LLM 쪽을 비워두면 기권이 채점에서 사라져 delta가
                # 폴백에 유리하게 기운다 → 갈린 종목 수만큼 0% 수익으로 채운다.
                lo_scored = [0.0] * len(base_scored)
            else:
                lo_scored = _rets_for(lo, date)
            llm_only_rets += lo_scored
            base_only_rets += base_scored
            llm_all_rets += _rets_for(rec.get("llm") or [], date)
            base_all_rets += _rets_for(rec.get("baseline") or [], date)
            diffs = _pair_diffs(rec, date, _rets_for)
            paired_diffs += diffs

            st = _style_of(rec)
            if st:
                b = by_style[st]
                b["runs"] += 1
                b["advisor_used_runs"] += 1
                if outcome == "abstained":
                    b["abstained_runs"] += 1
                if lo or bo:
                    b["divergent_runs"] += 1
                b["llm_only_rets"] += lo_scored
                b["base_only_rets"] += base_scored
                b["paired_diffs"] += diffs
        days.append({"date": date, "runs": len(recs), "divergent": day_div})

    lo_avg, bo_avg = _avg(llm_only_rets), _avg(base_only_rets)
    matured = min(len(llm_only_rets), len(base_only_rets))
    delta = (round(lo_avg - bo_avg, 2)
             if lo_avg is not None and bo_avg is not None else None)
    se = _se_pp(llm_only_rets, base_only_rets)
    ci95 = round(1.96 * se, 2) if se is not None else None
    significant = (delta is not None and ci95 is not None and abs(delta) > ci95)
    paired = _diff_stats(paired_diffs, min_samples=min_samples)

    style_out: dict[str, Any] = {}
    for s, b in by_style.items():
        if not b["runs"]:
            continue
        lo_a, bo_a = _avg(b["llm_only_rets"]), _avg(b["base_only_rets"])
        m = min(len(b["llm_only_rets"]), len(b["base_only_rets"]))
        d = (round(lo_a - bo_a, 2) if lo_a is not None and bo_a is not None else None)
        style_out[s] = {
            "runs": b["runs"],
            "advisor_used_runs": b["advisor_used_runs"],
            "abstained_runs": b["abstained_runs"],
            "divergent_runs": b["divergent_runs"],
            "llm_only": {"n": len(b["llm_only_rets"]), "avg_ret_pct": lo_a},
            "base_only": {"n": len(b["base_only_rets"]), "avg_ret_pct": bo_a},
            "delta_pct": d,
            "matured_smaller_side": m,
            **_diff_stats(b["paired_diffs"], min_samples=min_samples),
        }

    return {
        "ready": matured > 0 or paired["paired_n"] > 0,
        "horizon": horizon,
        "runs": runs,
        "advisor_used_runs": used,
        "abstained_runs": abstained,
        "divergent_runs": divergent,
        "llm_only": {"n": len(llm_only_rets), "avg_ret_pct": lo_avg},
        "base_only": {"n": len(base_only_rets), "avg_ret_pct": bo_avg},
        "llm_all": {"n": len(llm_all_rets), "avg_ret_pct": _avg(llm_all_rets)},
        "baseline_all": {"n": len(base_all_rets), "avg_ret_pct": _avg(base_all_rets)},
        # unpaired — 구조 편향 있음. 하위호환·진단용.
        "delta_pct": delta,
        "delta_se_pp": se,
        "delta_ci95_pp": ci95,
        "delta_significant": significant,
        "matured_smaller_side": matured,
        "matured_pairs": matured,              # 하위호환(이름만 pair — 실제로는 작은 쪽 표본)
        "min_samples": min_samples,
        "sample_target_reached": matured >= min_samples,
        "verdict_ready": matured >= min_samples and significant,
        # paired — 판정 정본
        **paired,
        "by_style": style_out,
        "verdict_note": ("판정의 정본은 paired_delta(회차 안 순위 매칭)다. "
                         "unpaired delta_pct는 base_only=점수 상위·llm_only=그 아래라 "
                         "advisor에게 구조적으로 불리하므로 실력으로 읽지 말 것. "
                         "표본 20은 표시 시작 기준이며 판정 근거가 아니다 — "
                         "부호는 |delta| > ci95일 때만 읽는다."),
        "days": days[-limit_days:],
        "disclaimer": "advisor shadow · 관측 전용 · 선별 로직·수량·문턱 미변경",
    }
