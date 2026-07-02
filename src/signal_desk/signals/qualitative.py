"""정성적 팩터 — KB 다이제스트의 투자심리(sentiment)를 종합 시그널의 한 팩터로.

사용자 방침(앞선 결정): '정성적 판단을 하나의 팩터 점수로만' 반영하고, 매매 실행/리스크 권한은
결정론적 코드가 유지한다. sentiment는 이미 [-1,1]로 정규화돼 있어 그대로 팩터 점수로 쓴다.
KB 데이터(뉴스·영상)가 없으면 이 팩터는 아예 제외된다(가중치 0 → 나머지 팩터끼리 재정규화).
"""

from __future__ import annotations


def component(entry: dict | None, weight: float) -> tuple[float, float, list[str], float | None, bool]:
    """entry: {score[-1,1], reasons[]} 또는 None. 반환: (정규화점수, 가중치, 근거, 원점수, has)."""
    if not entry:
        return 0.0, 0.0, [], None, False
    score = max(-1.0, min(1.0, float(entry.get("score", 0.0))))
    reasons = list(entry.get("reasons", []))
    return score, weight, reasons, round(score, 2), True
