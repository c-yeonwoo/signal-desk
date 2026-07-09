"""거장 전략 스크린 — 유명 투자자의 '스타일'을 규칙으로 옮겨 우리 유니버스를 필터링한다.

Stockopedia의 Guru Screen과 같은 취지의 '교육용 프리셋'이다. 13F 실제 보유(gurus.py)와 달리
여기 결과는 그 거장이 실제로 산 종목이 아니라 "그 사람의 공개된 투자 원칙을 숫자 기준으로
바꿨을 때 지금 걸리는 종목"이다 — 학습·탐색용이며 추천·자문이 아니다(규제 톤).

각 스크린은 순수 함수 술어의 모음이라 fundamentals 스냅샷만 있으면 KR/US 공통으로 돈다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Criterion:
    label: str                       # 표시용 조건 문구
    test: Callable[[dict], bool]     # metrics(dict) → 통과 여부


@dataclass(frozen=True)
class Screen:
    key: str
    name: str          # 거장·전략명
    style: str         # 한 줄 스타일 요약
    note: str          # 왜 이 기준인지(교육)
    criteria: tuple[Criterion, ...]


def _pos(v) -> bool:
    return isinstance(v, (int, float)) and v > 0


# 조건은 전부 '데이터가 있고 & 기준 충족'일 때만 True — 결측은 미충족으로 본다(보수적).
SCREENS: tuple[Screen, ...] = (
    Screen(
        key="buffett",
        name="워런 버핏식 · 우량 가치",
        style="꾸준히 돈 잘 버는 우량주를 적정가에",
        note="높은 자기자본이익률(ROE)로 '돈 버는 힘'을, 낮은 부채로 안정성을, 과하지 않은 PER로 가격을 본다.",
        criteria=(
            Criterion("ROE ≥ 15%", lambda m: _pos(m.get("roe")) and m["roe"] >= 15),
            Criterion("부채비율 ≤ 100%", lambda m: _pos(m.get("debt_ratio")) and m["debt_ratio"] <= 100),
            Criterion("0 < PER ≤ 25", lambda m: _pos(m.get("per")) and m["per"] <= 25),
        ),
    ),
    Screen(
        key="graham",
        name="벤저민 그레이엄식 · 심층 가치",
        style="자산·이익 대비 확실히 싼 종목(안전마진)",
        note="스승 격인 가치투자 원조. 낮은 PER·PBR로 '싼값'을, 낮은 부채로 '망하지 않을 안전마진'을 본다.",
        criteria=(
            Criterion("0 < PER ≤ 15", lambda m: _pos(m.get("per")) and m["per"] <= 15),
            Criterion("0 < PBR ≤ 1.5", lambda m: _pos(m.get("pbr")) and m["pbr"] <= 1.5),
            Criterion("부채비율 ≤ 50%", lambda m: _pos(m.get("debt_ratio")) and m["debt_ratio"] <= 50),
        ),
    ),
    Screen(
        key="lynch",
        name="피터 린치식 · 성장 가치(GARP)",
        style="성장하는데 아직 안 비싼 종목",
        note="이익이 크는 만큼만 PER를 준다는 생각. 매출 성장률 대비 PER이 낮으면(PEG↓) 성장을 싸게 사는 셈.",
        criteria=(
            Criterion("매출성장률 ≥ 12%", lambda m: _pos(m.get("revenue_growth")) and m["revenue_growth"] >= 12),
            Criterion("ROE ≥ 12%", lambda m: _pos(m.get("roe")) and m["roe"] >= 12),
            Criterion("PER ≤ 성장률×1.5 (PEG≈저평가)",
                      lambda m: _pos(m.get("per")) and _pos(m.get("revenue_growth"))
                      and m["per"] <= m["revenue_growth"] * 1.5),
        ),
    ),
)

SCREEN_BY_KEY = {s.key: s for s in SCREENS}


def matches(screen: Screen, metrics: dict) -> list[str] | None:
    """metrics가 screen의 '모든' 조건을 통과하면 통과한 조건 문구 목록, 아니면 None."""
    passed = [c.label for c in screen.criteria if c.test(metrics)]
    return passed if len(passed) == len(screen.criteria) else None


def run(fundamentals: dict[str, dict]) -> dict[str, list[str]]:
    """스크린 key → 통과 종목코드 목록. 결과 표시·시그널 병합은 상위(api)에서."""
    out: dict[str, list[str]] = {}
    for s in SCREENS:
        out[s.key] = [t for t, m in fundamentals.items() if m and matches(s, m)]
    return out
