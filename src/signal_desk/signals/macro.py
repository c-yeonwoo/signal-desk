"""거시 시황 해석 — FRED 원지표(CPI/금리/나스닥/VIX)를 주식시장 우호/비우호 판단으로 요약.

한국 증시가 미 물가·금리·나스닥에 연동되는 점을 이용해, 시장 국면(regime, 국내 breadth)과
별개의 "거시 축"으로 시황을 읽는다. 개별 종목 시그널 팩터가 아니라 시장 전체 오버레이다 —
자동매매봇/유저가 "지금 진입 타이밍인가"를 볼 때 참고하는 맥락 정보.

규칙(단순·설명가능 우선, LLM 없음):
- CPI YoY 하락 = 우호(디스인플레 → 금리인하 기대), 3% 초과 + 상승 = 비우호
- 기준금리/10년물 하락 = 우호, 상승 = 비우호
- 나스닥 상승 = 우호(위험선호 → 한국 동조), 하락 = 비우호
- VIX 20 미만 = 우호(안도), 25 초과 = 비우호(공포)
"""

from __future__ import annotations

CPI_HOT = 3.0  # YoY 3% 초과면 물가 부담 구간
VIX_CALM = 20.0
VIX_FEAR = 25.0


def read(indicators: list[dict], extra: list[dict] | None = None) -> dict:
    """macro_indicators() 결과를 받아 {bias, score, reasons, indicators}로 요약. score는 [-1,1].

    각 지표가 주식시장에 우호(+1)/비우호(-1)/중립(0) 표를 던지고 평균낸다. 지표별 favor는
    입력 dict에 실어 돌려줘(UI 화살표 색상용) 판단 로직을 프론트에 중복시키지 않는다.
    지표가 하나도 없으면 ready=False로 폴백한다.
    """
    by_key = {i["key"]: i for i in indicators}
    favor: dict[str, int] = {}
    reasons: list[str] = []

    cpi = by_key.get("CPIAUCSL")
    if cpi:
        if cpi["value"] > CPI_HOT and (cpi["change"] or 0) > 0:
            favor["CPIAUCSL"] = -1
            reasons.append(f"[거시] 미 CPI {cpi['value']:.1f}% — 물가 부담·반등, 비우호")
        elif (cpi["change"] or 0) < 0:
            favor["CPIAUCSL"] = 1
            reasons.append(f"[거시] 미 CPI {cpi['value']:.1f}% — 둔화 흐름, 금리인하 기대")
        else:
            favor["CPIAUCSL"] = 0

    for key, name in (("FEDFUNDS", "미 기준금리"), ("DGS10", "미 10년물")):
        rate = by_key.get(key)
        if rate and rate["change"] is not None:
            if rate["change"] > 0:
                favor[key] = -1
                reasons.append(f"[거시] {name} {rate['value']:.2f}% 상승 — 유동성 부담")
            elif rate["change"] < 0:
                favor[key] = 1
                reasons.append(f"[거시] {name} {rate['value']:.2f}% 하락 — 우호")

    nas = by_key.get("NASDAQCOM")
    if nas and nas["change"] is not None:
        if nas["change"] > 0:
            favor["NASDAQCOM"] = 1
            reasons.append(f"[거시] 나스닥 {nas['change']:+.1f}% — 위험선호, 한국 동조 기대")
        elif nas["change"] < 0:
            favor["NASDAQCOM"] = -1
            reasons.append(f"[거시] 나스닥 {nas['change']:+.1f}% — 위험회피")

    vix = by_key.get("VIXCLS")
    if vix:
        if vix["value"] > VIX_FEAR:
            favor["VIXCLS"] = -1
            reasons.append(f"[거시] VIX {vix['value']:.1f} — 공포 구간")
        elif vix["value"] < VIX_CALM:
            favor["VIXCLS"] = 1
            reasons.append(f"[거시] VIX {vix['value']:.1f} — 안도 구간")

    annotated = [{**i, "favor": favor.get(i["key"], 0)} for i in indicators]

    # 사전 판정된 추가 지표(예: ECOS 한국 거시 — favor·reason 포함)를 그대로 합류시킨다.
    for e in (extra or []):
        annotated.append(e)
        if e.get("favor"):
            favor[e["key"]] = e["favor"]
        if e.get("reason"):
            reasons.append(e["reason"])

    if not favor:
        return {"ready": False, "bias": None, "score": None, "reasons": [], "indicators": annotated}

    score = round(sum(favor.values()) / len(favor), 2)
    bias = "우호" if score >= 0.25 else "비우호" if score <= -0.25 else "중립"
    return {"ready": True, "bias": bias, "score": score, "reasons": reasons, "indicators": annotated}
