"""참고용 목표가 — 여러 앵커를 '투명하게 나열'한다(하나의 블랙박스 목표가로 뭉치지 않음, evidence-only).
매도 실행 규칙(risk.py)과 별개의 참고 지표(투자 자문·보장 아님). 데이터 없으면 해당 항목은 None.

앵커:
- value_target: 후행 PER 정상화 — target = price × (median_per/per) = median_per × 후행EPS. 0.5~2.0배 클램프.
- fwd_value_target(v2): 선행 컨센서스 EPS × 중앙값 PER = 미래이익에 '정상 배수'를 적용한 적정가(성장/역성장
  반영). 후행보다 미래지향적. 선행EPS>0일 때만(적자 컨센서스는 PER 밸류 불가). 0.5~2.0배 클램프.
- analyst_target(v2): 애널리스트 목표주가 컨센서스(네이버, KR). 독립적·시장 기반 앵커. ⚠️ 구조적 낙관
  편향(보통 상단)이라 참고용. 0.5~3.0배 클램프(밸류보다 상단 여유).
- resistance: 최근 N거래일 고점(직전 저항선).

fwd_eps·analyst_target는 KR 컨센서스 수집분에서만 채워진다(store.load_consensus_latest). US는 미제공.
"""

from __future__ import annotations

import statistics

_MED_MIN = 5           # 중앙값 신뢰 위한 최소 표본
_CLAMP_LO = 0.5        # 목표가 하한(현재가 대비)
_CLAMP_HI = 2.0        # 밸류 앵커 상한(초저PER 이상치로 과대 목표 방지)
_ANALYST_CLAMP_HI = 3.0  # 애널 앵커 상한(목표주가는 상단 여유를 더 둠)
_RESIST_WINDOW = 60    # 저항선 산정 최근 거래일


def median_per(fundamentals: dict[str, dict]) -> float | None:
    """유니버스 내 유효 PER(>0) 중앙값. 표본 부족 시 None."""
    pers = [m["per"] for m in fundamentals.values()
            if m.get("per") is not None and m["per"] > 0]
    return round(statistics.median(pers), 2) if len(pers) >= _MED_MIN else None


def sector_median_per(fundamentals: dict[str, dict], sector_map: dict[str, str],
                      min_n: int = _MED_MIN) -> dict[str, float]:
    """섹터별 유효 PER 중앙값 {sector: median} — 섹터 표본 min_n 이상만. 목표가 v2의 밸류 배수를
    유니버스 중앙값 대신 섹터별로 써서 고멀티플 섹터(반도체 등) 왜곡을 줄인다(섹터 중립화 v1)."""
    groups: dict[str, list[float]] = {}
    for t, m in fundamentals.items():
        per, sec = m.get("per"), sector_map.get(t)
        if per and per > 0 and sec:
            groups.setdefault(sec, []).append(per)
    return {sec: round(statistics.median(v), 2) for sec, v in groups.items() if len(v) >= min_n}


def _clamped(price: float, raw: float, hi: float) -> int:
    return round(max(price * _CLAMP_LO, min(price * hi, raw)))


def compute(price: float | None, per: float | None, med_per: float | None,
            closes: list[float] | None, *,
            analyst_target: float | None = None, fwd_eps: float | None = None) -> dict | None:
    """참고 목표가 dict. 계산 가능한 앵커만 채우고 전부 불가하면 None.
    반환 키: value_target/fwd_value_target/analyst_target/resistance (+ 각 *_upside_pct) + basis[]."""
    if not price or price <= 0:
        return None
    out: dict = {"basis": []}

    if per and per > 0 and med_per and med_per > 0:
        t = _clamped(price, price * (med_per / per), _CLAMP_HI)
        out["value_target"] = t
        out["value_upside_pct"] = round((t / price - 1) * 100, 1)
        out["basis"].append(f"후행 PER {per:.1f}→중앙값 {med_per:.1f} 회귀")

    if fwd_eps and fwd_eps > 0 and med_per and med_per > 0:  # 선행 밸류 앵커(v2)
        t = _clamped(price, med_per * fwd_eps, _CLAMP_HI)
        out["fwd_value_target"] = t
        out["fwd_value_upside_pct"] = round((t / price - 1) * 100, 1)
        out["basis"].append(f"선행EPS {round(fwd_eps):,}원 × 중앙값 PER {med_per:.1f}")

    if analyst_target and analyst_target > 0:  # 애널 목표주가 앵커(v2)
        t = _clamped(price, float(analyst_target), _ANALYST_CLAMP_HI)
        out["analyst_target"] = t
        out["analyst_upside_pct"] = round((t / price - 1) * 100, 1)
        out["basis"].append("애널 목표주가 컨센서스(참고·낙관편향)")

    if closes and len(closes) >= 20:
        resist = round(max(closes[-_RESIST_WINDOW:]))
        out["resistance"] = resist
        out["resistance_upside_pct"] = round((resist / price - 1) * 100, 1)
        out["basis"].append(f"최근 {min(len(closes), _RESIST_WINDOW)}일 고점")

    return out if any(out.get(k) for k in
                      ("value_target", "fwd_value_target", "analyst_target", "resistance")) else None
