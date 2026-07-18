"""시황 가설(#6) — 브랜치 지지도·근거 뉴스·관심 섹터 렌즈.

시그널 엔진·페이퍼 봇과 독립. %는 예측 확률이 아니라 형제 상대 지지도.
점수 = 0.5*지표일치 + 0.3*KB근거 + 0.2*사이클정합 → sibling 정규화.
"""

from __future__ import annotations

import datetime
import logging
from typing import Any
from zoneinfo import ZoneInfo

from signal_desk import db, kb_search
from signal_desk.reference import cycle, valuechain
from signal_desk.signals import macro as macro_mod
from signal_desk.signals import regime as regime_mod

log = logging.getLogger("signal_desk.hypothesis")

_KV_KEY = "hypo:v1:latest"
_DISCLAIMER = (
    "가설·학습용 · 예측·투자권유 아님 · 숫자는 지지도(상대 가중)이며 시장이 그렇게 될 확률이 아닙니다 "
    "· 시그널과 별개 레이어"
)

# P0 고정 템플릿 3갈래 — sector_keys는 valuechain key
_BRANCHES: list[dict[str, Any]] = [
    {
        "id": "ai_capex",
        "label": "AI·CAPEX 지속",
        "assumptions": [
            "데이터센터·AI 설비투자가 이어진다",
            "반도체 사이클이 아직 꺾이지 않는다",
            "위험선호(성장·기술)가 유지된다",
        ],
        "sector_keys": ["semiconductor", "ai_datacenter", "power_nuclear", "robotics"],
        "evidence_query": "AI 데이터센터 반도체 HBM 설비투자 CAPEX",
        "affinity": "risk_on",  # 우호 거시·확장/회복·나스닥↑
    },
    {
        "id": "consumer_shift",
        "label": "정책·물가·소비 쪽 이동",
        "assumptions": [
            "물가 안정·금리 부담 완화로 소비 여력이 돌아온다",
            "AI·성장주 쏠림이 쉬어가고 내수·소비재가 상대 주목받는다",
            "정책 초점이 설비투자보다 물가·가계 쪽에 기운다",
        ],
        "sector_keys": ["retail", "cosmetics", "telecom", "finance"],
        "evidence_query": "소비 내수 물가 금리인하 필수소비재 유통",
        "affinity": "consumer",  # 디스인플레·금리↓·방어/내수
    },
    {
        "id": "risk_off",
        "label": "리스크오프",
        "assumptions": [
            "변동성·불확실성이 커져 위험자산 선호가 줄어든다",
            "방어·배당·현금성 자산이 상대 강세를 보인다",
            "공격적 성장 테마는 후순위가 된다",
        ],
        "sector_keys": ["defense", "energy", "telecom", "finance"],
        "evidence_query": "안전자산 변동성 VIX 방어주 배당 침체 우려",
        "affinity": "risk_off",
    },
]


def _kst_today() -> str:
    return datetime.datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat()


def _normalize(weights: dict[str, float]) -> dict[str, int]:
    """상대 가중 → 합 100 정수 %. 잔여는 최대 가중 가지에 몰아 합을 맞춤."""
    total = sum(max(0.0, w) for w in weights.values()) or 1.0
    raw = {k: max(0.0, w) / total * 100.0 for k, w in weights.items()}
    pct = {k: int(v) for k, v in raw.items()}
    drift = 100 - sum(pct.values())
    if drift and pct:
        top = max(pct, key=lambda k: raw[k])
        pct[top] += drift
    return pct


def _metric_score(affinity: str, *, macro_bias: str | None, regime_name: str | None,
                  phase_key: str | None, indicators: list[dict]) -> float:
    """[0,1] 지표 일치도."""
    by = {i["key"]: i for i in (indicators or [])}
    nas = by.get("NASDAQCOM") or {}
    vix = by.get("VIXCLS") or {}
    nas_up = (nas.get("change") or 0) > 0
    vix_val = vix.get("value")
    fear = vix_val is not None and vix_val >= 25
    calm = vix_val is not None and vix_val < 20
    score = 0.35  # 바닥

    if affinity == "risk_on":
        if macro_bias == "우호":
            score += 0.25
        elif macro_bias == "비우호":
            score -= 0.1
        if phase_key in ("recovery", "expansion"):
            score += 0.2
        if nas_up:
            score += 0.15
        if calm:
            score += 0.1
        if fear:
            score -= 0.15
        if regime_name in ("강세", "과열"):
            score += 0.1
    elif affinity == "consumer":
        if macro_bias == "우호":
            score += 0.1  # 디스인플레·금리↓는 소비에도 우호
        if phase_key in ("slowdown", "contraction", "recovery"):
            score += 0.2
        if not nas_up:
            score += 0.1  # 성장주 쉬어갈 때 상대 관심
        if regime_name in ("조정", "약세", "중립"):
            score += 0.15
    elif affinity == "risk_off":
        if macro_bias == "비우호":
            score += 0.25
        if phase_key in ("contraction", "slowdown"):
            score += 0.25
        if fear:
            score += 0.2
        if not nas_up:
            score += 0.1
        if regime_name in ("약세", "조정"):
            score += 0.15
    return max(0.0, min(1.0, score))


def _cycle_score(sector_keys: list[str], lead_tags: list[str]) -> float:
    """사이클 주도섹터 태그 ↔ 가지 VC 태그 겹침 [0,1]."""
    if not lead_tags or not sector_keys:
        return 0.25
    lead_keys = {valuechain.key_for_tag(t) for t in lead_tags}
    lead_keys.discard(None)
    branch_tags: set[str] = set()
    for k in sector_keys:
        sec = next((s for s in valuechain.sectors() if s["key"] == k), None)
        if sec:
            branch_tags.update(sec.get("tags") or [])
    if not lead_keys and not branch_tags:
        return 0.25
    # key 겹침 또는 tag 이름 겹침
    key_hit = len(lead_keys & set(sector_keys))
    tag_hit = len(set(lead_tags) & branch_tags)
    hits = key_hit + tag_hit
    if hits <= 0:
        return 0.15
    return min(1.0, 0.35 + 0.25 * hits)


def _evidence_for(query: str, k: int = 5) -> tuple[float, list[dict]]:
    """KB 검색 → (kb_score[0,1], evidence list with url/source/published)."""
    try:
        hits = kb_search.retrieve(query, k=k)
    except Exception as e:
        log.warning("hypothesis KB 검색 실패: %s", type(e).__name__)
        hits = []
    if not hits:
        return 0.05, []
    # id → source/published 보강
    by_id: dict[int, dict] = {}
    try:
        for d in db.kb_documents(limit=2000):
            if d.get("id") is not None:
                by_id[int(d["id"])] = d
    except Exception:
        pass
    evidence = []
    for h in hits:
        meta = by_id.get(int(h["id"])) if h.get("id") is not None else None
        url = (h.get("url") or (meta or {}).get("url") or "").strip()
        if not url:
            continue
        evidence.append({
            "title": h.get("title") or "(제목 없음)",
            "url": url,
            "source": (meta or {}).get("source") or h.get("doc_class") or "",
            "published": (meta or {}).get("published") or "",
            "ticker": h.get("ticker"),
        })
    n = len(evidence)
    kb_score = min(1.0, 0.2 + 0.2 * n)  # 0건은 위에서 처리, 1~4건 스케일
    return kb_score, evidence[:5]


def _sector_nodes(keys: list[str]) -> list[dict]:
    out = []
    for k in keys:
        sec = next((s for s in valuechain.sectors() if s["key"] == k), None)
        if not sec:
            continue
        out.append({"key": k, "name": sec["name"], "summary": sec.get("summary") or ""})
    return out


def _watch_metrics(*, macro_bias, regime_name, phase_name, indicators) -> list[dict]:
    by = {i["key"]: i for i in (indicators or [])}
    rows = [
        {"key": "macro_bias", "label": "거시 편향", "value": macro_bias or "–"},
        {"key": "regime", "label": "시장 국면", "value": regime_name or "–"},
        {"key": "cycle", "label": "경기 사이클", "value": phase_name or "–"},
    ]
    for key, label in (("NASDAQCOM", "나스닥"), ("VIXCLS", "VIX"), ("CPIAUCSL", "미 CPI")):
        ind = by.get(key)
        if not ind:
            continue
        chg = ind.get("change")
        val = ind.get("value")
        if key == "NASDAQCOM" and chg is not None:
            rows.append({"key": key, "label": label, "value": f"{chg:+.1f}%"})
        elif val is not None:
            unit = "%" if key != "VIXCLS" else ""
            rows.append({"key": key, "label": label, "value": f"{val:.1f}{unit}"})
    return rows


def build(*, store_prices=None, store_macro=None) -> dict:
    """현재 지표·KB·사이클로 트리 생성. store 의존은 호출측에서 주입해도 되고 기본은 store 모듈."""
    from signal_desk import store

    as_of = _kst_today()
    indicators = store_macro if store_macro is not None else store.load_macro()
    mread = macro_mod.read(indicators or [])
    macro_bias = mread.get("bias") if mread.get("ready") else None

    prices = store_prices if store_prices is not None else store.load_price_series()
    try:
        reg = regime_mod.classify(prices) if prices else {"ready": False}
    except Exception:
        reg = {"ready": False}
    regime_name = reg.get("regime") if reg.get("ready") else None

    pos = cycle.position(indicators or [], persist=False)
    phase_key = pos.get("phase_key") if pos.get("ready") else None
    phase_name = pos.get("phase_name") if pos.get("ready") else None
    lead_tags = list(pos.get("lead_sectors") or []) if pos.get("ready") else []

    watch = _watch_metrics(macro_bias=macro_bias, regime_name=regime_name,
                           phase_name=phase_name, indicators=indicators or [])

    raw_w: dict[str, float] = {}
    branch_payload: dict[str, dict] = {}
    for b in _BRANCHES:
        m = _metric_score(b["affinity"], macro_bias=macro_bias, regime_name=regime_name,
                          phase_key=phase_key, indicators=indicators or [])
        c = _cycle_score(b["sector_keys"], lead_tags)
        k, evidence = _evidence_for(b["evidence_query"])
        w = 0.5 * m + 0.3 * k + 0.2 * c
        # 근거 0건이면 소폭 감점(설계: 빈약 시 지지도↓)
        if not evidence:
            w *= 0.7
        raw_w[b["id"]] = w
        branch_payload[b["id"]] = {
            "id": b["id"], "label": b["label"], "parent_id": "root",
            "assumptions": b["assumptions"],
            "sector_keys": b["sector_keys"],
            "sectors": _sector_nodes(b["sector_keys"]),
            "evidence": evidence,
            "evidence_n": len(evidence),
            "watch_metrics": watch,
            "scores": {"metric": round(m, 3), "kb": round(k, 3), "cycle": round(c, 3),
                       "raw": round(w, 3)},
        }

    pct = _normalize(raw_w)
    children = []
    for b in _BRANCHES:
        node = branch_payload[b["id"]]
        node["support_pct"] = pct[b["id"]]
        children.append(node)

    root = {
        "id": "root", "parent_id": None, "label": "향후 3~6개월 관심 렌즈",
        "support_pct": 100, "assumptions": [], "sectors": [], "evidence": [],
        "watch_metrics": watch, "children": children,
    }
    return {
        "ready": True,
        "as_of": as_of,
        "disclaimer": _DISCLAIMER,
        "tree": root,
        "context": {
            "macro_bias": macro_bias, "regime": regime_name,
            "cycle_phase": phase_name, "lead_sectors": lead_tags,
        },
    }


def refresh() -> dict:
    """재점수 후 kv 캐시. 일일 훅·관리자 수동 공통."""
    data = build()
    db.kv_set(_KV_KEY, data)
    return data


def get(*, build_if_missing: bool = True) -> dict:
    """캐시 우선. 없거나 ready 아니면 재생성."""
    cached = db.kv_get(_KV_KEY)
    if isinstance(cached, dict) and cached.get("ready") and cached.get("tree"):
        return cached
    if not build_if_missing:
        return {"ready": False, "reason": "시황 가설 캐시가 없습니다. 관리자가 새로고침하세요."}
    return refresh()
