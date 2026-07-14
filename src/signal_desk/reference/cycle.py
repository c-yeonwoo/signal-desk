"""경기 사이클(회복→확장→둔화→수축)과 국면별 섹터 로테이션 — 큐레이션 지식 + 현재위치 추정.

경기순환은 물가(인플레↔디플레)와 금리 흐름에 따라 4국면을 돈다는 고전적 "섹터 로테이션
시계(sector rotation clock)"를 한국·미국 시장 관점으로 정리했다. 어떤 산업이 각 국면에서
상대적으로 주도하는지는 확정 예측이 아니라 경향(참고용) — 표현 수위는 BACKLOG 규제 메모 준수.

현재 위치(`position`)는 FRED 거시지표(CPI 방향·미 금리 방향)로 근사 추정한다. 지수·선행지표가
붙으면 정교화 여지가 크지만, 이미 가진 데이터만으로 "지금 대략 어느 국면인가"를 보여준다.
"""

from __future__ import annotations

# order: 사인파 x축 배치(0~1)의 국면 중심. 회복 저점 부근 → 확장 상승 → 둔화 고점 → 수축 하강.
PHASES = [
    {
        "key": "recovery",
        "name": "회복",
        "order": 1,
        "x": 0.125,
        "rate": "저금리 · 인하 마무리",
        "inflation": "저인플레",
        "desc": "침체 저점을 지나 완화적 통화정책과 풍부한 유동성으로 경기민감·성장주가 먼저 반등하는 국면.",
        "lead_sectors": ["반도체", "IT/인터넷", "2차전지", "자동차"],
    },
    {
        "key": "expansion",
        "name": "확장",
        "order": 2,
        "x": 0.375,
        "rate": "금리 상승 전환",
        "inflation": "인플레 상승",
        "desc": "수요·설비투자 확대로 실적이 개선되고 물가가 오르며 중앙은행이 금리를 올리기 시작. 경기민감·실적주가 주도.",
        "lead_sectors": ["산업재/기계", "소재/화학", "반도체", "은행·금융"],
    },
    {
        "key": "slowdown",
        "name": "둔화(과열)",
        "order": 3,
        "x": 0.625,
        "rate": "고금리 정점",
        "inflation": "고인플레",
        "desc": "성장은 둔화되지만 물가는 아직 높아 긴축이 정점에 이르는 국면. 인플레 수혜·방어 성격이 섞여 원자재·에너지·방산이 상대 강세.",
        "lead_sectors": ["에너지/원자재", "방산", "전력/원전", "필수소비재"],
    },
    {
        "key": "contraction",
        "name": "수축(침체)",
        "order": 4,
        "x": 0.875,
        "rate": "금리 인하 전환",
        "inflation": "디스인플레 · 디플레 우려",
        "desc": "수요 위축과 이익 감소로 금리 인하 사이클이 시작되는 국면. 경기 방어주와 금리인하 수혜(성장·리츠)가 상대적으로 견조.",
        "lead_sectors": ["전력/원전", "통신", "헬스케어/바이오", "필수소비재"],
    },
]

_BY_KEY = {p["key"]: p for p in PHASES}


def phases() -> list[dict]:
    return PHASES


def _macro_by_key(indicators: list[dict]) -> dict:
    return {i["key"]: i for i in indicators}


def position(macro_indicators: list[dict]) -> dict:
    """FRED 지표로 현재 경기국면을 근사 추정. 반환: {ready, phase_key, phase_name, x, reasons}.

    규칙(단순·설명가능): 물가 방향(CPI 상승/하락)과 미 금리 방향(기준금리·10년물 상승/하락)의
    조합으로 4국면에 매핑한다. 물가↑금리↑=확장(과열이면 둔화), 물가↓금리↓=수축(나스닥 반등 시 회복),
    물가↓금리↑=둔화, 물가↑금리↓=회복. 지표가 부족하면 ready=False.
    """
    m = _macro_by_key(macro_indicators)
    cpi = m.get("CPIAUCSL")
    fed = m.get("FEDFUNDS")
    ten = m.get("DGS10")
    nas = m.get("NASDAQCOM")
    if not cpi or (fed is None and ten is None):
        return {"ready": False, "phase_key": None, "phase_name": None, "x": None,
                "reasons": [], "lead_sectors": []}

    cpi_rising = (cpi.get("change") or 0) > 0
    cpi_hot = cpi["value"] > 3.0
    rate_changes = [x["change"] for x in (fed, ten) if x and x.get("change") is not None]
    rates_rising = sum(rate_changes) > 0 if rate_changes else False
    nas_up = bool(nas and (nas.get("change") or 0) > 0)

    reasons = [
        f"미 CPI {cpi['value']:.1f}% ({'상승' if cpi_rising else '둔화'})",
        f"미 금리 {'상승' if rates_rising else '하락/보합'}",
    ]

    if cpi_rising and rates_rising:
        key = "slowdown" if cpi_hot else "expansion"
        reasons.append("물가·금리 동반 상승 — " + ("고물가 긴축 정점(둔화)" if cpi_hot else "경기 확장 국면"))
    elif not cpi_rising and not rates_rising:
        key = "recovery" if nas_up else "contraction"
        reasons.append("물가·금리 동반 하락 — " + ("증시 반등 동반, 회복 초입" if nas_up else "수요 위축, 수축 국면"))
    elif not cpi_rising and rates_rising:
        key = "slowdown"
        reasons.append("물가는 둔화되나 금리 부담 지속 — 둔화 국면")
    else:  # cpi_rising and not rates_rising
        key = "recovery"
        reasons.append("금리 완화 속 물가 반등 — 회복 국면")

    p = _BY_KEY[key]
    return {"ready": True, "phase_key": key, "phase_name": p["name"], "x": p["x"],
            "reasons": reasons, "lead_sectors": p["lead_sectors"]}
