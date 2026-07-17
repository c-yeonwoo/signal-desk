"""경기 사이클(회복→확장→둔화→수축)과 국면별 섹터 로테이션 — 큐레이션 지식 + 현재위치 추정.

경기순환은 물가(인플레↔디플레)와 금리 흐름에 따라 4국면을 돈다는 고전적 "섹터 로테이션
시계(sector rotation clock)"를 한국·미국 시장 관점으로 정리했다. 어떤 산업이 각 국면에서
상대적으로 주도하는지는 확정 예측이 아니라 경향(참고용) — 표현 수위는 BACKLOG 규제 메모 준수.

현재 위치는 FRED 거시지표(CPI 방향·미 금리 방향)로 근사한다. 원시 판정(`raw_position`)은
지표 부호가 조금만 바뀌어도 둔화↔회복이 뒤집힐 수 있어, 공개 API·KB 타깃은
`position()`의 히스테리시스(확정 국면)를 쓴다.
"""

from __future__ import annotations

from datetime import date

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

# 원시 판정이 N일 연속 달라야 확정 국면 전환 — KB 타깃·UI가 날마다 흔들리지 않게.
_CONFIRM_DAYS = 7
_STATE_KEY = "cycle_stable_phase"


def phases() -> list[dict]:
    return PHASES


def _macro_by_key(indicators: list[dict]) -> dict:
    return {i["key"]: i for i in indicators}


def raw_position(macro_indicators: list[dict]) -> dict:
    """FRED 지표로 현재 경기국면을 즉시 근사(히스테리시스 없음).

    규칙: 물가 방향(CPI) × 미 금리 방향. 물가↑금리↑=확장(과열이면 둔화),
    물가↓금리↓=수축(나스닥 반등 시 회복), 물가↓금리↑=둔화, 물가↑금리↓=회복.
    """
    m = _macro_by_key(macro_indicators)
    cpi = m.get("CPIAUCSL")
    fed = m.get("FEDFUNDS")
    ten = m.get("DGS10")
    nas = m.get("NASDAQCOM")
    if not cpi or (fed is None and ten is None):
        return {"ready": False, "phase_key": None, "phase_name": None, "x": None,
                "reasons": [], "lead_sectors": [], "raw_phase_key": None}

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
            "reasons": reasons, "lead_sectors": list(p["lead_sectors"]),
            "raw_phase_key": key}


def stabilize(raw: dict, state: dict | None, today: date | None = None,
              confirm_days: int = _CONFIRM_DAYS) -> tuple[dict, dict]:
    """원시 국면 → 확정 국면. 순수 함수(상태 in/out).

    - 첫 관측: raw를 바로 확정
    - raw == 확정: pending 해제
    - raw ≠ 확정: pending 시작/유지, confirm_days일 연속 동일 raw면 전환
    - ready=False: 기존 확정 유지(없으면 raw 그대로)
    """
    today = today or date.today()
    today_s = today.isoformat()
    state = dict(state or {})
    out = dict(raw)

    if not raw.get("ready") or not raw.get("phase_key"):
        if state.get("phase_key") and state["phase_key"] in _BY_KEY:
            p = _BY_KEY[state["phase_key"]]
            out = {
                "ready": True, "phase_key": p["key"], "phase_name": p["name"], "x": p["x"],
                "reasons": list(raw.get("reasons") or []) + ["거시 일시 공백 — 직전 확정 국면 유지"],
                "lead_sectors": list(p["lead_sectors"]),
                "raw_phase_key": raw.get("raw_phase_key") or raw.get("phase_key"),
            }
        out["stable"] = True
        out["pending_phase_key"] = None
        out["pending_days"] = 0
        out["confirm_days"] = confirm_days
        out["stable_since"] = state.get("switched_at")
        return out, state

    raw_key = raw["phase_key"]
    stable_key = state.get("phase_key")

    if not stable_key or stable_key not in _BY_KEY:
        # 최초 확정
        p = _BY_KEY[raw_key]
        new_state = {"phase_key": raw_key, "switched_at": today_s,
                     "pending_key": None, "pending_since": None}
        out.update({
            "phase_key": p["key"], "phase_name": p["name"], "x": p["x"],
            "lead_sectors": list(p["lead_sectors"]),
            "raw_phase_key": raw_key, "stable": True,
            "pending_phase_key": None, "pending_days": 0,
            "confirm_days": confirm_days, "stable_since": today_s,
        })
        out["reasons"] = list(raw.get("reasons") or []) + ["국면 최초 확정"]
        return out, new_state

    if raw_key == stable_key:
        new_state = {**state, "pending_key": None, "pending_since": None}
        p = _BY_KEY[stable_key]
        out.update({
            "phase_key": p["key"], "phase_name": p["name"], "x": p["x"],
            "lead_sectors": list(p["lead_sectors"]),
            "raw_phase_key": raw_key, "stable": True,
            "pending_phase_key": None, "pending_days": 0,
            "confirm_days": confirm_days, "stable_since": state.get("switched_at"),
        })
        return out, new_state

    # 전환 대기
    pending_key = state.get("pending_key")
    pending_since = state.get("pending_since")
    if pending_key != raw_key or not pending_since:
        pending_key, pending_since = raw_key, today_s
    try:
        since_d = date.fromisoformat(pending_since)
    except ValueError:
        since_d = today
        pending_since = today_s
    pending_days = (today - since_d).days

    if pending_days >= confirm_days:
        p = _BY_KEY[raw_key]
        new_state = {"phase_key": raw_key, "switched_at": today_s,
                     "pending_key": None, "pending_since": None}
        out.update({
            "phase_key": p["key"], "phase_name": p["name"], "x": p["x"],
            "lead_sectors": list(p["lead_sectors"]),
            "raw_phase_key": raw_key, "stable": True,
            "pending_phase_key": None, "pending_days": 0,
            "confirm_days": confirm_days, "stable_since": today_s,
        })
        out["reasons"] = list(raw.get("reasons") or []) + [
            f"원시 국면 '{p['name']}' {confirm_days}일 유지 → 확정 전환"
        ]
        return out, new_state

    # 아직 확정 유지
    p = _BY_KEY[stable_key]
    pend = _BY_KEY.get(raw_key)
    new_state = {**state, "pending_key": pending_key, "pending_since": pending_since}
    out.update({
        "phase_key": p["key"], "phase_name": p["name"], "x": p["x"],
        "lead_sectors": list(p["lead_sectors"]),
        "raw_phase_key": raw_key, "stable": False,
        "pending_phase_key": raw_key,
        "pending_days": pending_days,
        "confirm_days": confirm_days,
        "stable_since": state.get("switched_at"),
    })
    left = confirm_days - pending_days
    out["reasons"] = list(raw.get("reasons") or []) + [
        f"원시 관측 '{pend['name'] if pend else raw_key}' — "
        f"확정까지 약 {left}일 더 같아야 전환(현재 확정: {p['name']})"
    ]
    return out, new_state


def position(macro_indicators: list[dict], *, persist: bool = True,
             today: date | None = None, confirm_days: int = _CONFIRM_DAYS) -> dict:
    """확정 국면(히스테리시스). persist=True면 db.kv에 상태 저장."""
    raw = raw_position(macro_indicators)
    state = None
    if persist:
        from signal_desk import db
        state = db.kv_get(_STATE_KEY)
    out, new_state = stabilize(raw, state, today=today, confirm_days=confirm_days)
    if persist and new_state != state:
        from signal_desk import db
        db.kv_set(_STATE_KEY, new_state)
    return out


def risk_sentiment(macro_indicators: list[dict]) -> dict:
    """VIX 기반 탐욕/공포 soft 축 — CNN Fear&Greed 대체(이미 FRED에 있음).

    반환: {label: calm|neutral|fear, vix, kb_hint_phase_key?}
    - fear(VIX>25): KB에 수축(방어) 주도섹터를 soft 보강
    - calm(VIX<16): KB에 확장 주도섹터 soft 보강(과열·이벤트 감시)
    """
    m = _macro_by_key(macro_indicators)
    vix = m.get("VIXCLS")
    if not vix or vix.get("value") is None:
        return {"label": "neutral", "vix": None, "kb_hint_phase_key": None}
    v = float(vix["value"])
    if v > 25:
        return {"label": "fear", "vix": v, "kb_hint_phase_key": "contraction"}
    if v < 16:
        return {"label": "calm", "vix": v, "kb_hint_phase_key": "expansion"}
    return {"label": "neutral", "vix": v, "kb_hint_phase_key": None}


def lead_sectors_for(phase_key: str | None) -> list[str]:
    if not phase_key or phase_key not in _BY_KEY:
        return []
    return list(_BY_KEY[phase_key]["lead_sectors"])
