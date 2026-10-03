"""Explain two preserved watchlist observations without inventing causes or returns."""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

from signal_desk.signals import observation_archive

_FACTORS = {
    "technical": "기술", "fundamental": "재무", "valuation": "가치",
    "reversion": "되돌림", "qualitative": "정성", "flow": "수급",
    "quality": "퀄리티", "momentum": "모멘텀", "short": "공매도",
}


def _latest_per_session(root: Path, market: str, ticker: str, *, max_sessions: int = 30) -> list[tuple[dict, dict]]:
    market_root = root / market
    out = []
    for directory in sorted((p for p in market_root.glob("????-??-??") if p.is_dir()), reverse=True)[:max_sessions]:
        paths = list(directory.glob("*.json"))
        if not paths:
            continue
        latest = max(paths, key=lambda path: (
            json.loads(path.read_text(encoding="utf-8"))["captured_at"], path.stem))
        manifest, frame = observation_archive.read_verified(latest)
        rows = frame.loc[frame["ticker"].astype(str) == ticker]
        if not rows.empty:
            out.append((manifest, rows.iloc[0].to_dict()))
        if len(out) == 2:
            break
    return out


def _number(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, 3) if math.isfinite(number) else None


def _reasons(value) -> list[str]:
    if not isinstance(value, str):
        return []
    try:
        raw = json.loads(value)
    except json.JSONDecodeError:
        return []
    return [str(item) for item in raw[:5]] if isinstance(raw, list) else []


def valid_identity(market: str, ticker: str) -> bool:
    return (isinstance(market, str) and isinstance(ticker, str)
            and market in {"kr", "us"} and bool(re.fullmatch(r"[A-Za-z0-9._-]{1,20}", ticker)))


def describe(root: Path, market: str, ticker: str) -> dict:
    """Compare archived observations for one favorite. Never fall back to today's mutable cache."""
    if not valid_identity(market, ticker):
        raise ValueError("invalid watchlist identity")
    try:
        found = _latest_per_session(root, market, ticker)
    except (OSError, ValueError, KeyError) as exc:
        return {"status": "archive_error", "market": market, "ticker": ticker,
                "reason": f"보존 기록 검증 실패: {type(exc).__name__}", "not_order_advice": True}
    if not found:
        return {"status": "not_recorded", "market": market, "ticker": ticker,
                "reason": "이 기능이 시작된 뒤 저장된 판단 기록이 아직 없습니다.",
                "not_order_advice": True}
    current_manifest, current = found[0]
    previous_manifest, previous = found[1] if len(found) > 1 else (None, None)
    factors = []
    if previous is not None:
        for field, label in _FACTORS.items():
            old, new = _number(previous.get(field)), _number(current.get(field))
            if old is not None and new is not None and old != new:
                factors.append({"name": label, "before": old, "after": new,
                                "delta": round(new - old, 3)})
    current_reasons = _reasons(current.get("reasons_json"))
    previous_reasons = _reasons(previous.get("reasons_json")) if previous else []
    checks = []
    if not current.get("bar_asof"):
        checks.append("이 종목의 가격 기준일을 확인하세요. 기록에 기준일이 없습니다.")
    if bool(current.get("low_coverage")) or _number(current.get("data_coverage")) is None:
        checks.append("자료 커버리지가 낮거나 확인되지 않았습니다. 빈 자료를 부정적 근거로 읽지 마세요.")
    if bool(current.get("gate_blocked")) or bool(current.get("decision_blocked")):
        checks.append("매수 차단 이유가 해소됐는지 다음 관측에서 확인하세요. 차단 해제는 주문 허가가 아닙니다.")
    if not checks:
        checks.append("다음 관측에서 점수·차단 이유가 유지되는지 확인하세요. 한 번의 변화로 추세를 단정할 수 없습니다.")
    old_score, new_score = (_number(previous.get("score")) if previous else None,
                            _number(current.get("score")))
    return {
        "status": "comparison" if previous else "first_observation",
        "market": market, "ticker": ticker, "not_order_advice": True,
        "current": {"session": current_manifest["session"], "captured_at": current_manifest["captured_at"],
                    "computed_at": current.get("computed_at"), "score": new_score,
                    "kind": current.get("kind"), "bar_asof": current.get("bar_asof"),
                    "reasons": current_reasons, "blocked": bool(current.get("gate_blocked"))
                    or bool(current.get("decision_blocked")),
                    "source_time_verified": current_manifest["source_available_at_verified"]},
        "previous": ({"session": previous_manifest["session"], "score": old_score,
                      "kind": previous.get("kind"), "reasons": previous_reasons} if previous else None),
        "change": ({"score_delta": round(new_score - old_score, 3)
                    if new_score is not None and old_score is not None else None,
                    "kind_changed": previous.get("kind") != current.get("kind"),
                    "factor_deltas": factors,
                    "new_reasons": [r for r in current_reasons if r not in previous_reasons]}
                   if previous else None),
        "next_checks": checks,
        "caveat": "두 시점의 기록 차이이며 변화의 원인·미래 수익률·매수 성공확률은 아닙니다. 원천 공개시각은 미인증입니다.",
    }
