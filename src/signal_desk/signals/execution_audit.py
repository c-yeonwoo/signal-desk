"""실제 장중 가격 원장으로 체결 청산을 재생·검증한다.

이 모듈은 전략의 수익을 다시 계산하지 않는다. 실제 청산 판정에서 기록한 리스크 설정과
관측한 틱만 사용해, 라이브 청산이 같은 규칙의 첫 발동과 일치했는지 감사한다. 불일치는
신호 성능으로 포장하지 않고 데이터/실행 결함으로 분리한다.
"""

from __future__ import annotations

from collections import defaultdict

from signal_desk.signals import execution_twin, risk

_RISK_REASONS = {"STOP_LOSS", "TAKE_PROFIT", "TRAILING"}
_RISK_FIELDS = set(risk.RiskConfig.__dataclass_fields__)


def _risk_config(event: dict) -> risk.RiskConfig | None:
    raw = (event.get("payload") or {}).get("risk")
    if not isinstance(raw, dict):
        return None
    values = {k: v for k, v in raw.items() if k in _RISK_FIELDS}
    try:
        return risk.RiskConfig(**values)
    except (TypeError, ValueError):
        return None


def audit_round_trip(entry: dict, exit_event: dict | None, quotes: list[dict]) -> dict:
    """한 진입과 뒤따른 청산을 재생한다.

    이벤트성/시그널 청산은 리스크 모듈의 기대값이 아니므로 ``match=None``으로 둔다. 틱·동결
    설정이 부족한 경우도 실패로 꾸며내지 않고 ``auditable=False``로 명시한다.
    """
    # σ 폭은 보유 중에도 최신 종가 변동성으로 재산출된다. 따라서 과거 *진입* 설정이 아니라
    # 실제 청산 판정에 남긴 설정을 우선한다. 열린 포지션은 아직 청산 이벤트가 없으므로 진입
    # 설정으로 현재까지의 관측만 재생한다.
    cfg = _risk_config(exit_event or {}) or _risk_config(entry)
    entry_price = ((exit_event or {}).get("payload") or {}).get("entry_price") or entry.get("price")
    if not cfg or not entry_price or float(entry_price) <= 0:
        return {"ticker": entry.get("ticker"), "quantity": entry.get("quantity"), "auditable": False,
                "reason": "진입 시점 리스크 설정이 없음", "match": None}
    ticks = [execution_twin.QuoteTick(int(q["ts"]), float(q["price"]))
             for q in quotes if q.get("price") and int(q["ts"]) >= int(entry["ts"])]
    if not ticks:
        return {"ticker": entry.get("ticker"), "quantity": entry.get("quantity"), "auditable": False,
                "reason": "진입 후 장중 가격 원장이 없음", "match": None}
    replay = execution_twin.replay(float(entry_price), ticks, config=cfg)
    actual_reason = ((exit_event or {}).get("payload") or {}).get("reason")
    if actual_reason is None:
        actual_reason = (exit_event or {}).get("reason")
    actual_is_risk = actual_reason in _RISK_REASONS
    expected_reason = replay.reason
    match = (actual_reason == expected_reason) if actual_is_risk and exit_event else None
    return {
        "ticker": entry.get("ticker"), "quantity": entry.get("quantity"), "auditable": True, "match": match,
        "entry_ts": entry.get("ts"), "actual_reason": actual_reason,
        "expected_reason": expected_reason,
        "expected_exit_ts": replay.exit_tick.ts if replay.exit_tick else None,
        "actual_exit_ts": (exit_event or {}).get("ts"), "observed_ticks": replay.observed,
        "peak": replay.peak,
        "note": ("비리스크 청산 — 리스크 재생 비교 대상 아님" if exit_event and not actual_is_risk
                 else "청산 전이라 재생 결과만 제공" if not exit_event else None),
    }


def _quantity(event: dict) -> int:
    try:
        return max(1, int((event.get("payload") or {}).get("qty", 1)))
    except (TypeError, ValueError):
        return 1


def audit_events(events: list[dict], quotes_for_ticker) -> list[dict]:
    """체결 원장을 수량 보존 FIFO로 짝지어 감사한다.

    단순히 첫 매수를 pop하면 부분청산 뒤 남은 수량의 후속 청산이 고아가 된다. lot마다
    remaining_qty를 보존해 하나의 매수가 여러 매도 이벤트로 나뉘어도 전부 검증한다.
    """
    entries: dict[str, list[dict]] = defaultdict(list)
    out: list[dict] = []
    for event in events:
        ticker = event.get("ticker")
        if event.get("event_type") == "filled_buy":
            entries[ticker].append({"event": event, "remaining_qty": _quantity(event)})
        elif event.get("event_type") == "filled_sell" and entries.get(ticker):
            remaining = _quantity(event)
            while remaining > 0 and entries[ticker]:
                lot = entries[ticker][0]
                used = min(remaining, lot["remaining_qty"])
                entry = {**lot["event"], "quantity": used}
                out.append(audit_round_trip(
                    entry, event, quotes_for_ticker(ticker, entry["ts"], event["ts"])))
                remaining -= used
                lot["remaining_qty"] -= used
                if lot["remaining_qty"] == 0:
                    entries[ticker].pop(0)
            # 원장이 진입 뒤에 켜졌거나 외부 수정으로 sell 수량이 더 클 수 있다. 억지로 가장
            # 가까운 buy와 짝지으면 그럴듯한 거짓 '불일치'가 생기므로, 감사 불가로 노출한다.
            if remaining > 0:
                out.append({"ticker": ticker, "quantity": remaining, "auditable": False, "match": None,
                            "reason": "대응하는 진입 체결 원장이 없음", "actual_reason":
                            (event.get("payload") or {}).get("reason"), "actual_exit_ts": event.get("ts")})
        elif event.get("event_type") == "filled_sell":
            out.append({"ticker": ticker, "quantity": _quantity(event), "auditable": False, "match": None,
                        "reason": "대응하는 진입 체결 원장이 없음", "actual_reason":
                        (event.get("payload") or {}).get("reason"), "actual_exit_ts": event.get("ts")})
    # 열린 포지션도 계속 감사한다. 아직 exit가 없다는 건 오류가 아니라 관측 중이다.
    for ticker, pending in entries.items():
        for lot in pending:
            entry = {**lot["event"], "quantity": lot["remaining_qty"]}
            out.append(audit_round_trip(entry, None, quotes_for_ticker(ticker, entry["ts"], None)))
    return out


def summary(rows: list[dict]) -> dict:
    auditable = [r for r in rows if r.get("auditable")]
    compared = [r for r in auditable if r.get("match") is not None]
    matches = sum(1 for r in compared if r["match"])
    unmatched = sum(1 for r in rows if r.get("reason") == "대응하는 진입 체결 원장이 없음")
    return {"positions": len(rows), "auditable": len(auditable), "unmatched_exits": unmatched,
            "risk_compared": len(compared),
            "matched": matches, "match_pct": round(matches / len(compared) * 100, 1) if compared else None,
            "items": rows}
