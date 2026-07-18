"""시황 가설(#6) — 배타적 IF → then 지표 분기 → outcome 트리.

시그널 엔진·페이퍼 봇과 독립. Layer0 %는 예측 확률이 아니라 배타 IF 간 상대 지지도.
LLM/Opus로 가지를 만들지 않음 — 큐레이션 템플릿 + 실지표 status.
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

_KV_KEY = "hypo:v2:latest"
_DISCLAIMER = (
    "가설·학습용 · 지지도(배타 가설 간 상대 가중) · IF 분기 · 예측·투자권유 아님 "
    "· 시그널과 별개 레이어"
)

# 큐레이션 IF → then/and/but → outcome (깊이 ≤3)
_TEMPLATES: list[dict[str, Any]] = [
    {
        "id": "ai_capex",
        "label": "AI·CAPEX 지속",
        "edge": "if",
        "assumptions": [
            "데이터센터·AI 설비투자가 이어진다",
            "반도체 사이클이 아직 꺾이지 않는다",
            "위험선호(성장·기술)가 유지된다",
        ],
        "sector_keys": ["semiconductor", "ai_datacenter", "power_nuclear", "robotics"],
        "evidence_query": "AI 데이터센터 반도체 HBM 설비투자 CAPEX",
        "affinity": "risk_on",
        "children": [
            {
                "id": "ai_capex_then_growth",
                "kind": "then",
                "edge": "then",
                "label": "나스닥 상대강세 · VIX 진정",
                "assumptions": ["성장·기술 선호가 유지되면 위험지표가 안정권을 지킨다"],
                "conditions": [
                    {"metric": "NASDAQCOM", "op": "chg>", "threshold": 0, "label": "나스닥 상승"},
                    {"metric": "VIXCLS", "op": "<", "threshold": 20, "label": "VIX < 20"},
                ],
                "children": [
                    {
                        "id": "ai_capex_out_tech",
                        "kind": "outcome",
                        "edge": "then",
                        "label": "반도체·AI·전력 인프라 관심",
                        "assumptions": ["CAPEX 서사가 이어질 때 상대 주목 섹터"],
                        "sector_keys": ["semiconductor", "ai_datacenter", "power_nuclear"],
                        "evidence_query": "AI 데이터센터 반도체 HBM 설비투자",
                    },
                ],
            },
            {
                "id": "ai_capex_but_vol",
                "kind": "then",
                "edge": "but",
                "label": "VIX 급등 · 거시 비우호 동시",
                "assumptions": ["성장 테마여도 변동성·거시 악화가 겹치면 숨고르기"],
                "conditions": [
                    {"metric": "VIXCLS", "op": ">=", "threshold": 25, "label": "VIX ≥ 25"},
                    {"metric": "macro_bias", "op": "==", "threshold": "비우호", "label": "거시 비우호"},
                ],
                "children": [
                    {
                        "id": "ai_capex_out_pause",
                        "kind": "outcome",
                        "edge": "then",
                        "label": "성장 테마 숨고르기 · 방어 상대",
                        "assumptions": ["테크 쏠림이 쉬어가고 방어·현금성이 상대 강세"],
                        "sector_keys": ["defense", "telecom", "finance"],
                        "evidence_query": "변동성 VIX 성장주 조정 방어주",
                    },
                ],
            },
        ],
    },
    {
        "id": "consumer_shift",
        "label": "정책·물가·소비 쪽 이동",
        "edge": "if",
        "assumptions": [
            "물가 안정·금리 부담 완화로 소비 여력이 돌아온다",
            "AI·성장주 쏠림이 쉬어가고 내수·소비재가 상대 주목받는다",
            "정책 초점이 설비투자보다 물가·가계 쪽에 기운다",
        ],
        "sector_keys": ["retail", "cosmetics", "telecom", "finance"],
        "evidence_query": "소비 내수 물가 금리인하 필수소비재 유통",
        "affinity": "consumer",
        "children": [
            {
                "id": "consumer_then_easing",
                "kind": "then",
                "edge": "then",
                "label": "CPI 안정 · 금리 부담 완화",
                "assumptions": ["디스인플레·금리 방향이 소비에 우호적일 때"],
                "conditions": [
                    {"metric": "CPIAUCSL", "op": "chg<=", "threshold": 0, "label": "CPI 안정·하락"},
                    {"metric": "FEDFUNDS", "op": "chg<=", "threshold": 0, "label": "기준금리 동결·인하"},
                ],
                "children": [
                    {
                        "id": "consumer_out_retail",
                        "kind": "outcome",
                        "edge": "then",
                        "label": "내수·소비재·유통 관심",
                        "assumptions": ["소비 회복 서사에 맞는 섹터 렌즈"],
                        "sector_keys": ["retail", "cosmetics", "telecom"],
                        "evidence_query": "소비 내수 유통 필수소비재",
                    },
                ],
            },
            {
                "id": "consumer_but_inflate",
                "kind": "then",
                "edge": "but",
                "label": "물가 재가속",
                "assumptions": ["물가가 다시 올라가면 소비 회복이 미뤄진다"],
                "conditions": [
                    {"metric": "CPIAUCSL", "op": "chg>", "threshold": 0, "label": "CPI 상승"},
                ],
                "children": [
                    {
                        "id": "consumer_out_delay",
                        "kind": "outcome",
                        "edge": "then",
                        "label": "소비 회복 지연 · 관망",
                        "assumptions": ["내수 테마보다 물가·금리 확인이 우선"],
                        "sector_keys": ["finance", "telecom"],
                        "evidence_query": "인플레 물가 재가속 소비둔화",
                    },
                ],
            },
        ],
    },
    {
        "id": "risk_off",
        "label": "리스크오프",
        "edge": "if",
        "assumptions": [
            "변동성·불확실성이 커져 위험자산 선호가 줄어든다",
            "방어·배당·현금성 자산이 상대 강세를 보인다",
            "공격적 성장 테마는 후순위가 된다",
        ],
        "sector_keys": ["defense", "energy", "telecom", "finance"],
        "evidence_query": "안전자산 변동성 VIX 방어주 배당 침체 우려",
        "affinity": "risk_off",
        "children": [
            {
                "id": "risk_off_then_fear",
                "kind": "then",
                "edge": "then",
                "label": "VIX 상승 · 국면 약세/조정",
                "assumptions": ["공포·조정 국면에서 위험회피가 우세"],
                "conditions": [
                    {"metric": "VIXCLS", "op": ">=", "threshold": 20, "label": "VIX ≥ 20"},
                    {"metric": "regime", "op": "in", "threshold": ["약세", "조정"], "label": "국면 약세·조정"},
                ],
                "children": [
                    {
                        "id": "risk_off_out_def",
                        "kind": "outcome",
                        "edge": "then",
                        "label": "방어·에너지·배당 관심",
                        "assumptions": ["위험회피 시 상대 강세 섹터"],
                        "sector_keys": ["defense", "energy", "telecom"],
                        "evidence_query": "방어주 배당 에너지 안전자산",
                    },
                ],
            },
            {
                "id": "risk_off_and_nasdaq",
                "kind": "then",
                "edge": "and",
                "label": "나스닥 하락 동반",
                "assumptions": ["위험자산 전반이 같이 빠지면 회피가 강화된다"],
                "conditions": [
                    {"metric": "NASDAQCOM", "op": "chg<", "threshold": 0, "label": "나스닥 하락"},
                    {"metric": "VIXCLS", "op": ">=", "threshold": 20, "label": "VIX ≥ 20"},
                ],
                "children": [
                    {
                        "id": "risk_off_out_cash",
                        "kind": "outcome",
                        "edge": "then",
                        "label": "위험자산 회피 강화",
                        "assumptions": ["성장·위험 테마보다 현금성·방어 우선"],
                        "sector_keys": ["defense", "finance", "energy"],
                        "evidence_query": "위험회피 주식조정 안전자산 현금",
                    },
                ],
            },
        ],
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
    score = 0.35

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
            score += 0.1
        if phase_key in ("slowdown", "contraction", "recovery"):
            score += 0.2
        if not nas_up:
            score += 0.1
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
    kb_score = min(1.0, 0.2 + 0.2 * n)
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
    for key, label in (("NASDAQCOM", "나스닥"), ("VIXCLS", "VIX"), ("CPIAUCSL", "미 CPI"),
                       ("FEDFUNDS", "연준 기준금리")):
        ind = by.get(key)
        if not ind:
            continue
        chg = ind.get("change")
        val = ind.get("value")
        if key == "NASDAQCOM" and chg is not None:
            rows.append({"key": key, "label": label, "value": f"{chg:+.1f}%"})
        elif val is not None:
            unit = "%" if key not in ("VIXCLS",) else ""
            rows.append({"key": key, "label": label, "value": f"{val:.1f}{unit}"})
    return rows


def _ctx_value(metric: str, *, indicators: list[dict], macro_bias, regime_name) -> Any:
    if metric == "macro_bias":
        return macro_bias
    if metric == "regime":
        return regime_name
    by = {i["key"]: i for i in (indicators or [])}
    ind = by.get(metric) or {}
    if metric in ("NASDAQCOM",) or str(metric).endswith("_chg"):
        return ind.get("change")
    return ind.get("value") if ind.get("value") is not None else ind.get("change")


def _cond_ok(cond: dict, *, indicators, macro_bias, regime_name) -> bool | None:
    """조건 충족 여부. 값 없으면 None(미관측)."""
    metric = cond.get("metric") or ""
    op = cond.get("op") or "=="
    thr = cond.get("threshold")
    # chg 계열은 change 필드
    if op.startswith("chg"):
        by = {i["key"]: i for i in (indicators or [])}
        val = (by.get(metric) or {}).get("change")
        if val is None:
            return None
        real_op = op[3:]  # >, <, >=, <=
        if real_op == ">":
            return val > thr
        if real_op == "<":
            return val < thr
        if real_op == ">=":
            return val >= thr
        if real_op == "<=":
            return val <= thr
        return None
    val = _ctx_value(metric, indicators=indicators, macro_bias=macro_bias, regime_name=regime_name)
    if val is None:
        return None
    if op == "==":
        return val == thr
    if op == "in":
        return val in (thr or [])
    if op == ">=":
        return val >= thr
    if op == "<=":
        return val <= thr
    if op == ">":
        return val > thr
    if op == "<":
        return val < thr
    return None


def _eval_status(conditions: list[dict], *, indicators, macro_bias, regime_name) -> tuple[str, dict]:
    """조건 리스트 → status + current 스냅샷."""
    current: dict[str, Any] = {}
    if not conditions:
        return "n/a", current
    results: list[bool | None] = []
    for c in conditions:
        m = c.get("metric") or ""
        if m == "macro_bias":
            current[m] = macro_bias
        elif m == "regime":
            current[m] = regime_name
        else:
            by = {i["key"]: i for i in (indicators or [])}
            ind = by.get(m) or {}
            current[m] = {
                "value": ind.get("value"),
                "change": ind.get("change"),
            }
        results.append(_cond_ok(c, indicators=indicators, macro_bias=macro_bias,
                                regime_name=regime_name))
    known = [r for r in results if r is not None]
    if not known:
        return "watching", current
    if all(known) and None not in results:
        return "aligned", current
    if any(r is False for r in known):
        # 일부만 맞으면 watching, 전부 틀리면 diverging
        if all(r is False for r in known):
            return "diverging", current
        return "watching", current
    return "watching", current


def _build_child_node(
    tmpl: dict,
    *,
    parent_id: str,
    indicators,
    macro_bias,
    regime_name,
    evidence_cache: dict[str, tuple[float, list]],
) -> dict:
    kind = tmpl.get("kind") or "then"
    node_id = tmpl["id"]
    conditions = list(tmpl.get("conditions") or [])
    status, current = _eval_status(
        conditions, indicators=indicators, macro_bias=macro_bias, regime_name=regime_name,
    )
    sector_keys = list(tmpl.get("sector_keys") or [])
    eq = tmpl.get("evidence_query")
    evidence: list[dict] = []
    if eq:
        if eq not in evidence_cache:
            evidence_cache[eq] = _evidence_for(eq)
        _, evidence = evidence_cache[eq]
    # outcome: 자식 조건이 없으면 부모 then status를 물려받을 수 있게 n/a 유지
    if kind == "outcome" and not conditions:
        status = "n/a"

    children = [
        _build_child_node(
            c, parent_id=node_id, indicators=indicators, macro_bias=macro_bias,
            regime_name=regime_name, evidence_cache=evidence_cache,
        )
        for c in (tmpl.get("children") or [])
    ]
    # outcome의 status: 자식 없으면 부모 then과 동일하게 보이도록 — 호출측에서 세팅하지 않음.
    # then 아래 outcome은 부모 status를 복사하면 UI가 읽기 쉬움.
    if kind == "outcome" and status == "n/a":
        # 부모 평가는 이 함수 밖에서 — 여기서는 children 없는 leaf만 n/a
        pass

    return {
        "id": node_id,
        "parent_id": parent_id,
        "kind": kind,
        "edge": tmpl.get("edge") or "then",
        "label": tmpl["label"],
        "support_pct": None,
        "assumptions": list(tmpl.get("assumptions") or []),
        "conditions": conditions,
        "status": status,
        "current": current,
        "sector_keys": sector_keys,
        "sectors": _sector_nodes(sector_keys),
        "evidence": evidence,
        "evidence_n": len(evidence),
        "children": children,
    }


def _inherit_outcome_status(node: dict) -> None:
    """then → outcome: outcome에 조건이 없으면 부모 status를 상속."""
    for ch in node.get("children") or []:
        if ch.get("kind") == "outcome" and ch.get("status") == "n/a":
            ch["status"] = node.get("status") or "watching"
        _inherit_outcome_status(ch)


def build(*, store_prices=None, store_macro=None) -> dict:
    """현재 지표·KB·사이클로 IF 트리 생성."""
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

    evidence_cache: dict[str, tuple[float, list]] = {}
    raw_w: dict[str, float] = {}
    if_nodes: list[dict] = []

    for t in _TEMPLATES:
        m = _metric_score(t["affinity"], macro_bias=macro_bias, regime_name=regime_name,
                          phase_key=phase_key, indicators=indicators or [])
        c = _cycle_score(t["sector_keys"], lead_tags)
        eq = t["evidence_query"]
        if eq not in evidence_cache:
            evidence_cache[eq] = _evidence_for(eq)
        k, evidence = evidence_cache[eq]
        w = 0.5 * m + 0.3 * k + 0.2 * c
        if not evidence:
            w *= 0.7
        raw_w[t["id"]] = w

        children = [
            _build_child_node(
                ch, parent_id=t["id"], indicators=indicators or [],
                macro_bias=macro_bias, regime_name=regime_name,
                evidence_cache=evidence_cache,
            )
            for ch in (t.get("children") or [])
        ]
        for ch in children:
            _inherit_outcome_status(ch)

        if_nodes.append({
            "id": t["id"],
            "parent_id": "root",
            "kind": "if",
            "edge": "if",
            "label": t["label"],
            "support_pct": 0,  # filled after normalize
            "assumptions": t["assumptions"],
            "conditions": [],
            "status": "n/a",
            "current": {},
            "sector_keys": t["sector_keys"],
            "sectors": _sector_nodes(t["sector_keys"]),
            "evidence": evidence,
            "evidence_n": len(evidence),
            "watch_metrics": watch,
            "scores": {"metric": round(m, 3), "kb": round(k, 3), "cycle": round(c, 3),
                       "raw": round(w, 3)},
            "children": children,
        })

    pct = _normalize(raw_w)
    for node in if_nodes:
        node["support_pct"] = pct[node["id"]]

    # 지지도 최대 IF
    active_id = max(if_nodes, key=lambda n: n["support_pct"])["id"] if if_nodes else None

    root = {
        "id": "root",
        "parent_id": None,
        "kind": "root",
        "edge": None,
        "label": "향후 3~6개월 시황 가설",
        "support_pct": 100,
        "assumptions": [],
        "conditions": [],
        "status": "n/a",
        "current": {},
        "sectors": [],
        "evidence": [],
        "watch_metrics": watch,
        "active_if": active_id,
        "children": if_nodes,
    }
    return {
        "ready": True,
        "as_of": as_of,
        "disclaimer": _DISCLAIMER,
        "tree": root,
        "context": {
            "macro_bias": macro_bias, "regime": regime_name,
            "cycle_phase": phase_name, "lead_sectors": lead_tags,
            "active_if": active_id,
        },
    }


def refresh() -> dict:
    """재점수 후 kv 캐시. 일일 훅·관리자 수동 공통."""
    data = build()
    db.kv_set(_KV_KEY, data)
    return data


def get(*, build_if_missing: bool = True) -> dict:
    """캐시 우선. 없거나 ready 아니면 재생성. v1 캐시는 무시."""
    cached = db.kv_get(_KV_KEY)
    if isinstance(cached, dict) and cached.get("ready") and cached.get("tree"):
        # v2 shape: root.children[].kind == if
        kids = (cached.get("tree") or {}).get("children") or []
        if kids and kids[0].get("kind") == "if":
            return cached
    if not build_if_missing:
        return {"ready": False, "reason": "시황 가설 캐시가 없습니다. 관리자가 새로고침하세요."}
    return refresh()
