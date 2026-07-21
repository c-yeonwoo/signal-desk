"""기후 시그널 — 이슈 흐름(emphasized 갈래) 파급을 기존 점수에 얹은 실험 뱃지.

기존 combine/kind/봇/문턱과 완전 격리. UI 표시·관측 전용.
문장·LLM 의견은 쓰지 않고 sector_keys·watch_tickers·branch_pct·affinity만 쓴다.
"""

from __future__ import annotations

import datetime
import logging
from typing import Any
from zoneinfo import ZoneInfo

from signal_desk.reference import valuechain
from signal_desk.signals import engine as eng
from signal_desk.signals import hypothesis

log = logging.getLogger("signal_desk.climate")

# 체감 실험 — 관리자 튜닝 전 고정
_ALPHA = 0.8
_Q_CAP = 0.6
_STALE_DAYS = 7

_LABEL_KO = {
    eng.STRONG_BUY: "맑음+",
    eng.BUY: "맑음",
    eng.HOLD: "흐림",
    eng.SELL: "비",
    eng.STRONG_SELL: "폭풍",
}

# risk_off 강조 시 역풍을 줄 성장 업종(outcome에 없어도)
_GROWTH_KEYS = frozenset({
    "semiconductor", "ai_datacenter", "battery", "robotics", "game", "entertainment",
})


def _kst_today() -> datetime.date:
    return datetime.datetime.now(ZoneInfo("Asia/Seoul")).date()


def _parse_as_of(payload: dict) -> datetime.date | None:
    raw = payload.get("as_of") or (payload.get("generated_at") or "")[:10]
    if not raw:
        return None
    try:
        return datetime.date.fromisoformat(str(raw)[:10])
    except Exception:
        return None


def _is_stale(payload: dict) -> bool:
    d = _parse_as_of(payload)
    if d is None:
        return True
    return (_kst_today() - d).days > _STALE_DAYS


def _vc_keys_for_ticker(ticker: str) -> set[str]:
    out: set[str] = set()
    for s in valuechain.sectors():
        for st in s.get("stages") or []:
            for bag in ("domestic", "overseas"):
                for c in st.get(bag) or []:
                    if c.get("ticker") == ticker:
                        out.add(s["key"])
    return out


def _ticker_index() -> dict[str, set[str]]:
    """ticker -> vc keys (국내·해외)."""
    idx: dict[str, set[str]] = {}
    for s in valuechain.sectors():
        key = s["key"]
        for st in s.get("stages") or []:
            for bag in ("domestic", "overseas"):
                for c in st.get(bag) or []:
                    tk = c.get("ticker")
                    if not tk:
                        continue
                    idx.setdefault(tk, set()).add(key)
    return idx


def _extract_impacts(tree: dict) -> list[dict[str, Any]]:
    """emphasized fork의 outcome만. sign=+1(순풍), risk_off면 성장업종 역풍 추가."""
    impacts: list[dict[str, Any]] = []
    for issue in tree.get("children") or []:
        if issue.get("kind") != "if":
            continue
        support = max(0.0, min(1.0, (issue.get("support_pct") or 0) / 100.0))
        affinity = issue.get("affinity") or "risk_on"
        issue_label = issue.get("label") or ""
        for fork in issue.get("children") or []:
            if fork.get("kind") not in ("fork", "then"):
                continue
            if not fork.get("emphasized"):
                continue
            bp = fork.get("branch_pct")
            bp_f = 0.5 if bp is None else max(0.0, min(1.0, float(bp) / 100.0))
            weight = support * (0.35 + 0.65 * bp_f)
            edge = fork.get("edge") or "path"
            fork_label = fork.get("label") or ""
            for oc in fork.get("children") or []:
                if oc.get("kind") != "outcome":
                    continue
                sectors = list(oc.get("sector_keys") or [])
                watches = [
                    w.get("ticker") for w in (oc.get("watch_tickers") or [])
                    if isinstance(w, dict) and w.get("ticker")
                ]
                # action 잎에 watch가 있을 수 있음
                for ch in oc.get("children") or []:
                    if ch.get("kind") == "action":
                        for w in ch.get("watch_tickers") or []:
                            if isinstance(w, dict) and w.get("ticker"):
                                watches.append(w["ticker"])
                impacts.append({
                    "sign": 1.0,
                    "weight": weight,
                    "sector_keys": sectors,
                    "tickers": list(dict.fromkeys(watches)),
                    "affinity": affinity,
                    "edge": edge,
                    "issue_label": issue_label,
                    "fork_label": fork_label,
                    "outcome_label": oc.get("label") or "",
                })
            # risk_off가 지금 더 가까운 갈래면 성장 업종에 역풍
            if affinity == "risk_off":
                impacts.append({
                    "sign": -1.0,
                    "weight": weight * 0.7,
                    "sector_keys": sorted(_GROWTH_KEYS),
                    "tickers": [],
                    "affinity": affinity,
                    "edge": edge,
                    "issue_label": issue_label,
                    "fork_label": fork_label,
                    "outcome_label": "성장·위험선호 업종 역풍",
                })
    return impacts


def _q_for_ticker(
    ticker: str,
    impacts: list[dict],
    vc_keys: set[str],
) -> tuple[float | None, str]:
    if not impacts:
        return None, ""
    acc = 0.0
    reasons: list[str] = []
    for imp in impacts:
        hit = ticker in (imp.get("tickers") or []) or bool(vc_keys & set(imp.get("sector_keys") or []))
        if not hit:
            continue
        delta = float(imp["sign"]) * float(imp["weight"])
        acc += delta
        bit = imp.get("outcome_label") or imp.get("fork_label") or imp.get("issue_label")
        if bit and bit not in reasons:
            reasons.append(str(bit)[:40])
    if not reasons and abs(acc) < 1e-9:
        return None, ""
    q = max(-_Q_CAP, min(_Q_CAP, acc))
    return round(q, 3), " · ".join(reasons[:2])


def evaluate_ticker(
    ticker: str,
    score_base: float,
    *,
    impacts: list[dict] | None = None,
    vc_index: dict[str, set[str]] | None = None,
    hypo: dict | None = None,
) -> dict | None:
    """기존 점수 + 기후 q → 실험 kind. 없으면 None (뱃지 숨김)."""
    if hypo is None:
        hypo = hypothesis.get(build_if_missing=False)
    if not isinstance(hypo, dict) or not hypo.get("ready") or not hypo.get("tree"):
        return None
    if _is_stale(hypo):
        return None
    if impacts is None:
        impacts = _extract_impacts(hypo["tree"])
    if not impacts:
        return None
    if vc_index is None:
        vc_keys = _vc_keys_for_ticker(ticker)
    else:
        vc_keys = vc_index.get(ticker) or set()
    q, reason = _q_for_ticker(ticker, impacts, vc_keys)
    if q is None:
        return None
    score_c = max(-3.0, min(3.0, float(score_base) + _ALPHA * q))
    kind = eng.classify(score_c)
    return {
        "ready": True,
        "label": "기후",
        "kind": kind,
        "kind_ko": _LABEL_KO.get(kind, kind),
        "score": round(score_c, 2),
        "q": q,
        "alpha": _ALPHA,
        "base_score": round(float(score_base), 2),
        "reason": reason,
        "as_of": hypo.get("as_of"),
        "disclaimer": "기존 시그널과 별개 · 이슈 흐름 파급 실험 · 봇 미반영",
    }


def annotate_rows(rows: list[dict]) -> list[dict]:
    """리스트/상세 dict에 climate 필드 부착. score·kind 필드는 수정하지 않음."""
    hypo = hypothesis.get(build_if_missing=False)
    if not isinstance(hypo, dict) or not hypo.get("ready") or not hypo.get("tree") or _is_stale(hypo):
        for r in rows:
            r["climate"] = None
        return rows
    impacts = _extract_impacts(hypo["tree"])
    vc_index = _ticker_index()
    for r in rows:
        tk = r.get("ticker")
        sc = r.get("score")
        if not tk or sc is None:
            r["climate"] = None
            continue
        r["climate"] = evaluate_ticker(
            tk, float(sc), impacts=impacts, vc_index=vc_index, hypo=hypo,
        )
    return rows


def _shadow_path():
    from signal_desk.store import CACHE_DIR
    return CACHE_DIR / "climate_shadow.json"


def snapshot_shadow(signals, date: str | None = None) -> int:
    """일별 기후 vs 기존 kind 관측 스냅샷(봇 미연동). 같은 날 덮어씀. 반환: 기후 부착 건수."""
    import json

    from signal_desk.store import CACHE_DIR

    if not signals:
        return 0
    date = date or _kst_today().isoformat()
    hypo = hypothesis.get(build_if_missing=False)
    if not isinstance(hypo, dict) or not hypo.get("ready") or not hypo.get("tree") or _is_stale(hypo):
        return 0
    impacts = _extract_impacts(hypo["tree"])
    vc_index = _ticker_index()
    rows: list[dict[str, Any]] = []
    diverge = 0
    for s in signals:
        c = evaluate_ticker(
            s.ticker, float(s.score), impacts=impacts, vc_index=vc_index, hypo=hypo,
        )
        if not c:
            continue
        base_kind = getattr(s, "kind", None) or ""
        if c["kind"] != base_kind:
            diverge += 1
        rows.append({
            "ticker": s.ticker,
            "base_kind": base_kind,
            "base_score": round(float(s.score), 3),
            "clim_kind": c["kind"],
            "clim_score": c["score"],
            "q": c["q"],
        })
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _shadow_path()
    blob: dict[str, Any] = {}
    if path.exists():
        try:
            blob = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            blob = {}
    blob[date] = {
        "as_of": hypo.get("as_of"),
        "n": len(rows),
        "diverge": diverge,
        "rows": rows,
    }
    # 최근 90일만 유지
    keep = sorted(blob.keys())[-90:]
    blob = {k: blob[k] for k in keep}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(blob, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)
    log.info("기후 shadow 스냅샷 %s · %d건(기존 kind와 다름 %d)", date, len(rows), diverge)
    return len(rows)


def shadow_summary(*, limit_days: int = 14) -> dict[str, Any]:
    """관측용 요약 — 승격 게이트 아님. UI 뱃지와 무관."""
    import json

    path = _shadow_path()
    if not path.exists():
        return {"ready": False, "days": [], "message": "기후 shadow 스냅샷 없음(마감 후 적재)"}
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"ready": False, "days": [], "message": "climate_shadow.json 파싱 실패"}
    days = []
    for d in sorted(blob.keys())[-limit_days:]:
        rec = blob[d] or {}
        n = int(rec.get("n") or 0)
        div = int(rec.get("diverge") or 0)
        days.append({
            "date": d,
            "n": n,
            "diverge": div,
            "diverge_pct": round(div / n * 100, 1) if n else None,
            "as_of": rec.get("as_of"),
        })
    return {
        "ready": True,
        "days": days,
        "disclaimer": "기후 shadow · 기존 시그널과 별개 · 봇 미연동 · 승격 전 관측",
    }
