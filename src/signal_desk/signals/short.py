"""공매도 팩터 — 최근 공매도 거래비중이 높으면 하방 압력(정보 우위 공매도)으로 보고 매수기여를 깎는다.

한 방향(하방 리스크) 팩터: 비중이 임계 미만이면 중립(가중치 0으로 제외 — 다른 팩터 희석 방지),
임계를 넘으면 음(-)의 강도로 penalize. 정밀도 우선(과잉 매수 억제)에 부합. KR만(미국은 소스 없음
→ 자동 제외). 공매도량은 주수라 스케일 시세와 무관([[signal-desk-scaled-market-data]]).
"""

from __future__ import annotations

_BASELINE = 0.06   # 시장 평균 공매도 비중 근사(이 수준은 페널티 0의 기준점)
_MIN = 0.08        # 이보다 낮으면 노이즈 → 중립(제외)
_SCALE = 0.16      # (비중-기준)/scale 로 [-1,0] 정규화 — 22%면 이미 -1로 포화


def component(short: dict | None, weight: float) -> tuple[float, float, list[str], float | None, bool]:
    """short={short_ratio, short_vol, total_vol, days}|None → (norm[-1,0], weight, reasons, ratio, has_short)."""
    if not short or short.get("short_ratio") is None:
        return 0.0, 0.0, [], None, False
    ratio = float(short["short_ratio"])
    if ratio < _MIN:                       # 공매도 비중 낮음 → 중립(제외, 매수 인플레 방지)
        return 0.0, 0.0, [], ratio, False
    norm = max(-1.0, -(ratio - _BASELINE) / _SCALE)
    reason = f"[공매도] 최근 {short.get('days', '?')}일 공매도 거래비중 {ratio * 100:.1f}% — 하방 압력(매수기여 감점)"
    return norm, weight, [reason], ratio, True
