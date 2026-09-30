"""수익 개선 순서의 진척. 수익률 차이는 여기 싣지 않는다.

페이퍼 화면이 매일 연구 성과를 보여 주면 그게 곧 중간 엿보기(peeking)다.
여기 있는 것은 동결된 표본 수, 다음 look, 그리고 주문이 아직 바뀌지 않는다는 사실뿐이다.
"""

from __future__ import annotations

import datetime as dt

from signal_desk import db, market_clock
from signal_desk.signals import price_baseline_shadow, price_baseline_verdict, rotation_shadow, rotation_verdict

_NEXT = 12
_STYLES = ((900001, "conservative"), (900002, "balanced"), (900003, "aggressive"))


def _completed(market: str) -> str | None:
    return rotation_shadow.score_completed_session(market, dt.datetime.now(dt.timezone.utc))


def _r11(market: str, completed: str | None) -> dict:
    styles = []
    for uid, style in _STYLES:
        rows = db.rotation_shadow_gate_rows(uid, market, rotation_verdict.START_SESSION)
        eligible, selected = rotation_verdict.select_nonoverlap(rows, market=market)
        matured = sum(1 for _ep, end in selected if completed and end <= completed)
        styles.append({
            "style": style,
            "divergent_episodes": len(eligible),
            "nonoverlap_blocks": len(selected),
            "matured_blocks": matured,
        })
    slowest = min((s["matured_blocks"] for s in styles), default=0)
    return {
        "id": "r11",
        "order": 2,
        "title": "저회전 비교",
        "gate_id": rotation_verdict.GATE_ID,
        "live_eligible": False,
        "auto_promote": False,
        "next_look": _NEXT,
        "matured_blocks": slowest,
        "styles": styles,
        "status": "awaiting_oos" if slowest < _NEXT else "look_ready_admin_only",
        "reason": (f"비중첩 20거래일 블록 {slowest}/{_NEXT}. "
                   "비용 후 차이는 관리자 판정에서만 보고, 여기 숫자로 주문을 바꾸지 않습니다."),
    }


def _r12(market: str, completed: str | None) -> dict:
    rows = db.price_baseline_all(market, price_baseline_verdict.START_SESSION)
    selected = sorted(
        (e for e in rows if e.get("version") == price_baseline_shadow.VERSION
         and e.get("session", "") >= price_baseline_verdict.START_SESSION
         and (e.get("policies") or {}).get("price_3factor") != (e.get("policies") or {}).get("sector_momentum")),
        key=lambda e: e["session"])
    nonoverlap = 0
    matured = 0
    previous_end = None
    for episode in selected:
        sessions = market_clock.next_sessions(market, episode["session"], price_baseline_shadow.HORIZON)
        if len(sessions) != price_baseline_shadow.HORIZON or (previous_end and episode["session"] <= previous_end):
            continue
        nonoverlap += 1
        previous_end = sessions[-1]
        if completed and sessions[-1] <= completed:
            matured += 1
    return {
        "id": "r12",
        "order": 3,
        "title": "가격 3팩터 대조",
        "gate_id": price_baseline_verdict.GATE_ID,
        "live_eligible": False,
        "auto_promote": False,
        "divergent_episodes": len(selected),
        "nonoverlap_blocks": nonoverlap,
        "matured_blocks": matured,
        "next_look": _NEXT,
        "status": "awaiting_oos" if matured < _NEXT else "look_ready_admin_only",
        "reason": (f"8팩터 라이브와 가격만 쓴 바구니의 비용 후 차이는 {matured}/{_NEXT}블록 전에는 채택 근거가 아닙니다. "
                   "차이가 나도 이 화면의 수익률로 읽지 않습니다."),
    }


def for_market(market: str) -> dict:
    """한 시장의 개선 순서. 1번은 봇 장부 필드, 4번은 시그널 첫 화면의 판정 달력."""
    if market not in ("kr", "us"):
        return {"ok": False, "reason": "지원하지 않는 시장", "live_eligible": False}
    completed = _completed(market)
    counts = db.research_row_counts(market)
    return {
        "ok": True,
        "market": market,
        "live_eligible": False,
        "champion": {
            "frozen": True,
            "changes": "점수·매수 분위·청산 폭은 고정입니다. 손실을 보고 여기서 맞추지 않습니다.",
        },
        "steps": [
            _r11(market, completed),
            _r12(market, completed),
            {
                "id": "verdict",
                "order": 4,
                "title": "판정 달력",
                "live_eligible": False,
                "final_id": "pit-8factor-rank3-hold5-final",
                "reason": "가중치를 여는 것은 확정 look뿐입니다. 중간 판독이 통과해도 채택 근거가 아닙니다. 진척은 시그널 첫 줄에 있습니다.",
            },
            {
                "id": "later",
                "order": 5,
                "title": "리비전·관계·수급",
                "live_eligible": False,
                "auto_promote": False,
                "frozen_rows": {
                    "revision": counts["revision"],
                    "relation": counts["relation"],
                    "flow": counts["flow"],
                    "quality": counts["price_quality"],
                },
                "reason": "앞의 저회전·가격 대조가 첫 12블록 look을 내기 전에는 이 연구를 주문에 올리지 않습니다.",
            },
        ],
    }
