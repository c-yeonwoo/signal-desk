"""최근 이슈 흐름(#6) — 이슈 → 쌍갈림(path/alt) → 파급 → 관심 종목.

뉴스·KB 핫이슈의 파급을 학습·시황 읽기용으로 펼친다. 가설 검증이 아님.
관리자 수동 refresh 시에만 Sonnet이 문장 생성. 이슈·갈래 %는 룰.
관심 종목(action)은 밸류체인 대표 티커로 룰 부착. 일일 자동 LLM 없음.
"""

from __future__ import annotations

import datetime
import json
import logging
import re
from collections import Counter
from typing import Any
from zoneinfo import ZoneInfo

from signal_desk import db, kb_search
from signal_desk.reference import cycle, valuechain
from signal_desk.signals import macro as macro_mod
from signal_desk.signals import regime as regime_mod

log = logging.getLogger("signal_desk.hypothesis")

_KV_KEY = "hypo:v4:latest"
_DISCLAIMER = (
    "학습·시황 읽기용 · 뉴스·KB 최근 이슈 흐름 · 수동 생성 · "
    "관심%·갈래%는 이슈/쌍 안 상대값 · 종목은 대표 후보(추천 아님) · "
    "예측·투자권유 아님 · 시그널과 별개"
)
# 트리 연결 의미 — 검증 아님, 파급 갈래
_EDGE_KO = {
    "if": "이슈",
    "path": "이렇게 이어질 때",
    "alt": "다르게 갈 때",
    "action": "다시 볼 종목",
    # 레거시 캐시 호환
    "then": "이렇게 이어질 때",
    "and": "이렇게 이어질 때",
    "but": "다르게 갈 때",
}

_ALLOWED_METRICS = frozenset({
    "NASDAQCOM", "VIXCLS", "CPIAUCSL", "FEDFUNDS", "macro_bias", "regime",
})
_ALLOWED_OPS = frozenset({
    "==", "in", ">=", "<=", ">", "<", "chg>", "chg<", "chg>=", "chg<=",
})
_ALLOWED_AFFINITY = frozenset({"risk_on", "consumer", "risk_off"})
_ALLOWED_EDGES = frozenset({"if", "path", "alt", "then", "and", "but"})
_STOP = frozenset({
    "있다", "하다", "되다", "이다", "및", "등", "위해", "대한", "관련", "오늘", "어제",
    "기자", "속보", "단독", "the", "and", "for", "with", "from",
})
_ID_RE = re.compile(r"[^a-z0-9_]+")
_SECTOR_ALIASES = {
    "반도체": "semiconductor", "메모리": "semiconductor", "hbm": "semiconductor",
    "ai": "ai_datacenter", "ai칩": "ai_datacenter", "데이터센터": "ai_datacenter",
    "전력": "power_nuclear", "원전": "power_nuclear", "방산": "defense", "방어": "defense",
    "에너지": "energy", "금융": "finance", "은행": "finance", "유통": "retail",
    "소비": "retail", "내수": "retail", "통신": "telecom", "바이오": "bio",
    "자동차": "auto", "배터리": "battery", "로봇": "robotics", "화장품": "cosmetics",
}

# 폴백 큐레이션 (디버그·테스트용 — refresh 경로에서는 쓰지 않음)
_TEMPLATES: list[dict[str, Any]] = [
    {
        "id": "ai_capex",
        "label": "AI·데이터센터 투자",
        "detail": "설비투자가 이어진다는 뉴스·기대가 큰 이슈예요.",
        "edge": "if",
        "assumptions": [
            "데이터센터·AI 설비투자가 이어진다",
            "반도체 수요가 아직 꺾이지 않는다",
            "성장·기술주에 돈이 더 모인다",
        ],
        "sector_keys": ["semiconductor", "ai_datacenter", "power_nuclear", "robotics"],
        "evidence_query": "AI 데이터센터 반도체 HBM 설비투자 CAPEX",
        "affinity": "risk_on",
        "children": [
            {
                "id": "ai_capex_then_growth",
                "kind": "fork",
                "edge": "path",
                "label": "그 투자가 이어지고 시장도 차분하면",
                "assumptions": ["성장·기술 선호가 유지되면 변동성이 낮게 유지된다"],
                "conditions": [
                    {"metric": "NASDAQCOM", "op": "chg>", "threshold": 0, "label": "나스닥 상승"},
                    {"metric": "VIXCLS", "op": "<", "threshold": 20, "label": "VIX < 20"},
                ],
                "children": [
                    {
                        "id": "ai_capex_out_tech",
                        "kind": "outcome",
                        "edge": "path",
                        "label": "그러면 반도체·AI·전력 쪽을 더 볼 만함",
                        "assumptions": ["설비투자 이야기가 이어질 때 상대 주목 업종"],
                        "sector_keys": ["semiconductor", "ai_datacenter", "power_nuclear"],
                        "evidence_query": "AI 데이터센터 반도체 HBM 설비투자",
                    },
                ],
            },
            {
                "id": "ai_capex_but_vol",
                "kind": "fork",
                "edge": "alt",
                "label": "투자는 있어도 변동성이 커지면",
                "assumptions": ["성장 테마여도 공포·거시 악화가 겹치면 쉬어간다"],
                "conditions": [
                    {"metric": "VIXCLS", "op": ">=", "threshold": 25, "label": "VIX ≥ 25"},
                    {"metric": "macro_bias", "op": "==", "threshold": "비우호", "label": "거시 비우호"},
                ],
                "children": [
                    {
                        "id": "ai_capex_out_pause",
                        "kind": "outcome",
                        "edge": "alt",
                        "label": "그러면 성장주는 쉬고 방어·금융을 볼 만함",
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
        "label": "물가·금리와 소비",
        "detail": "물가·금리 움직임이 소비·내수에 미치는 이야기가 커요.",
        "edge": "if",
        "assumptions": [
            "물가 안정·금리 부담 완화로 소비 여력이 돌아온다",
            "성장주 쏠림이 쉬어가고 내수·소비재가 상대 주목받는다",
            "정책 초점이 설비투자보다 물가·가계 쪽에 기운다",
        ],
        "sector_keys": ["retail", "cosmetics", "telecom", "finance"],
        "evidence_query": "소비 내수 물가 금리인하 필수소비재 유통",
        "affinity": "consumer",
        "children": [
            {
                "id": "consumer_then_easing",
                "kind": "fork",
                "edge": "path",
                "label": "물가가 안정되고 금리 부담이 줄면",
                "assumptions": ["물가·금리 방향이 소비에 우호적일 때"],
                "conditions": [
                    {"metric": "CPIAUCSL", "op": "chg<=", "threshold": 0, "label": "CPI 안정·하락"},
                    {"metric": "FEDFUNDS", "op": "chg<=", "threshold": 0, "label": "기준금리 동결·인하"},
                ],
                "children": [
                    {
                        "id": "consumer_out_retail",
                        "kind": "outcome",
                        "edge": "path",
                        "label": "그러면 유통·화장품·통신을 더 볼 만함",
                        "assumptions": ["소비 회복 이야기에 맞는 업종"],
                        "sector_keys": ["retail", "cosmetics", "telecom"],
                        "evidence_query": "소비 내수 유통 필수소비재",
                    },
                ],
            },
            {
                "id": "consumer_but_inflate",
                "kind": "fork",
                "edge": "alt",
                "label": "물가가 다시 오르면",
                "assumptions": ["물가가 다시 올라가면 소비 회복이 미뤄진다"],
                "conditions": [
                    {"metric": "CPIAUCSL", "op": "chg>", "threshold": 0, "label": "CPI 상승"},
                ],
                "children": [
                    {
                        "id": "consumer_out_delay",
                        "kind": "outcome",
                        "edge": "alt",
                        "label": "그러면 소비 테마는 미루고 금리·금융을 볼 만함",
                        "assumptions": ["내수보다 물가·금리 확인이 우선"],
                        "sector_keys": ["finance", "telecom"],
                        "evidence_query": "인플레 물가 재가속 소비둔화",
                    },
                ],
            },
        ],
    },
    {
        "id": "risk_off",
        "label": "시장 불안·위험 회피",
        "detail": "변동성이 커져 위험한 자산에서 돈이 빠질 수 있다는 이슈예요.",
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
                "kind": "fork",
                "edge": "path",
                "label": "변동성이 커지고 시장이 약해지면",
                "assumptions": ["공포·조정 국면에서 안전한 쪽으로 돈이 이동"],
                "conditions": [
                    {"metric": "VIXCLS", "op": ">=", "threshold": 20, "label": "VIX ≥ 20"},
                    {"metric": "regime", "op": "in", "threshold": ["약세", "조정"], "label": "국면 약세·조정"},
                ],
                "children": [
                    {
                        "id": "risk_off_out_def",
                        "kind": "outcome",
                        "edge": "path",
                        "label": "그러면 방산·에너지·통신을 더 볼 만함",
                        "assumptions": ["위험 회피 시 상대 강세 업종"],
                        "sector_keys": ["defense", "energy", "telecom"],
                        "evidence_query": "방어주 배당 에너지 안전자산",
                    },
                ],
            },
            {
                "id": "risk_off_and_nasdaq",
                "kind": "fork",
                "edge": "alt",
                "label": "나스닥까지 같이 빠지면",
                "assumptions": ["위험자산 전반이 같이 빠지면 회피가 강화된다"],
                "conditions": [
                    {"metric": "NASDAQCOM", "op": "chg<", "threshold": 0, "label": "나스닥 하락"},
                    {"metric": "VIXCLS", "op": ">=", "threshold": 20, "label": "VIX ≥ 20"},
                ],
                "children": [
                    {
                        "id": "risk_off_out_cash",
                        "kind": "outcome",
                        "edge": "alt",
                        "label": "그러면 성장주보다 방어·금융을 볼 만함",
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


def _kst_now_iso() -> str:
    return datetime.datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M")


def _normalize(weights: dict[str, float]) -> dict[str, int]:
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
    return min(1.0, 0.2 + 0.2 * n), evidence[:5]


def _sector_nodes(keys: list[str]) -> list[dict]:
    out = []
    for k in keys:
        sec = next((s for s in valuechain.sectors() if s["key"] == k), None)
        if not sec:
            continue
        out.append({"key": k, "name": sec["name"], "summary": sec.get("summary") or ""})
    return out


def _tickers_for_sectors(keys: list[str], limit: int = 6) -> list[dict]:
    """밸류체인 국내 대표 티커. 추천이 아니라 학습용 관심 후보."""
    out: list[dict] = []
    seen: set[str] = set()
    for k in keys or []:
        sec = valuechain.sector(k) if hasattr(valuechain, "sector") else None
        if not sec:
            sec = next((s for s in valuechain.sectors() if s["key"] == k), None)
        if not sec:
            continue
        for st in sec.get("stages") or []:
            for c in st.get("domestic") or []:
                tk = c.get("ticker")
                if not tk or tk in seen:
                    continue
                seen.add(tk)
                out.append({
                    "ticker": tk,
                    "name": c.get("name") or tk,
                    "sector": sec.get("name") or k,
                    "vc_key": k,
                })
                if len(out) >= limit:
                    return out
    return out


def _make_action_node(parent_id: str, sector_keys: list[str], edge: str) -> dict:
    watches = _tickers_for_sectors(sector_keys, limit=6)
    if watches:
        names = "·".join(w["name"] for w in watches[:4])
        label = f"다시 볼 종목 · {names}"
    else:
        label = "다시 볼 종목 · 관련 업종 대표주"
    return {
        "id": f"{parent_id}_act",
        "parent_id": parent_id,
        "kind": "action",
        "edge": "action",
        "edge_ko": _EDGE_KO["action"],
        "label": label,
        "detail": (
            "이 갈래를 공부할 때 시그널 탭에서 더 보면 좋은 대표 종목이에요. "
            "매수 추천이 아닙니다."
        ),
        "support_pct": None,
        "branch_pct": None,
        "emphasized": False,
        "assumptions": ["학습용 관심 종목 · 매수 추천 아님"],
        "conditions": [],
        "current": {},
        "sector_keys": list(sector_keys or []),
        "sectors": _sector_nodes(sector_keys or []),
        "watch_tickers": watches,
        "evidence": [],
        "evidence_n": 0,
        "children": [],
    }


def _sector_key_set() -> set[str]:
    return {s["key"] for s in valuechain.sectors()}


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
    return ind.get("value") if ind.get("value") is not None else ind.get("change")


def _cond_ok(cond: dict, *, indicators, macro_bias, regime_name) -> bool | None:
    metric = cond.get("metric") or ""
    op = cond.get("op") or "=="
    thr = cond.get("threshold")
    if op.startswith("chg"):
        by = {i["key"]: i for i in (indicators or [])}
        val = (by.get(metric) or {}).get("change")
        if val is None:
            return None
        real_op = op[3:]
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


def _condition_snapshot(conditions: list[dict], *, indicators, macro_bias, regime_name) -> dict:
    """참고 지표의 현재값 스냅샷(해설용). 검증 상태 아님."""
    current: dict[str, Any] = {}
    for c in conditions or []:
        m = c.get("metric") or ""
        if not m:
            continue
        if m == "macro_bias":
            current[m] = macro_bias
        elif m == "regime":
            current[m] = regime_name
        else:
            by = {i["key"]: i for i in (indicators or [])}
            ind = by.get(m) or {}
            current[m] = {"value": ind.get("value"), "change": ind.get("change")}
    return current


def _fork_weight(conditions: list[dict], *, indicators, macro_bias, regime_name) -> float:
    """쌍갈림 안 상대 무게. 조건이 지금 지표와 겹칠수록 높음(예측 확률 아님)."""
    if not conditions:
        return 0.5
    scores: list[float] = []
    for c in conditions:
        ok = _cond_ok(c, indicators=indicators, macro_bias=macro_bias, regime_name=regime_name)
        if ok is True:
            scores.append(1.0)
        elif ok is False:
            scores.append(0.15)
        else:
            scores.append(0.5)
    return sum(scores) / len(scores)


def _assign_pair_weights(nodes: list[dict]) -> None:
    """형제 fork 2개에 branch_pct(합 100)·emphasized 부여."""
    forks = [n for n in nodes if n.get("kind") == "fork"]
    if len(forks) < 2:
        for n in forks:
            n["branch_pct"] = 100
            n["emphasized"] = True
        return
    # 항상 첫 두 갈래만 쌍으로 정규화
    pair = forks[:2]
    weights = {n["id"]: max(0.05, float(n.get("_w") or 0.5)) for n in pair}
    pct = _normalize(weights)
    top_id = max(pair, key=lambda n: pct[n["id"]])["id"]
    for n in pair:
        n["branch_pct"] = pct[n["id"]]
        n["emphasized"] = n["id"] == top_id
        n.pop("_w", None)
    for n in forks[2:]:
        n["branch_pct"] = 0
        n["emphasized"] = False
        n.pop("_w", None)


def _slug(s: str, fallback: str) -> str:
    base = (s or "").strip().lower().replace(" ", "_").replace("·", "_").replace("-", "_")
    # 한글 라벨이면 fallback 사용
    if re.search(r"[가-힣]", base) or not base:
        return fallback
    out = _ID_RE.sub("_", base).strip("_")
    return out[:48] or fallback


def _map_sector_key(raw: str) -> str | None:
    allowed = _sector_key_set()
    k = (raw or "").strip()
    if not k:
        return None
    if k in allowed:
        return k
    low = k.lower()
    if low in allowed:
        return low
    alias = _SECTOR_ALIASES.get(k) or _SECTOR_ALIASES.get(low)
    if alias in allowed:
        return alias
    for s in valuechain.sectors():
        name = s.get("name") or ""
        if k == name or k in name or name in k:
            return s["key"]
        tags = s.get("tags") or []
        if k in tags or low in {t.lower() for t in tags if isinstance(t, str)}:
            return s["key"]
    return None


def _filter_sectors(keys: list | None) -> list[str]:
    out: list[str] = []
    for k in keys or []:
        mapped = _map_sector_key(str(k)) if k is not None else None
        if mapped and mapped not in out:
            out.append(mapped)
    return out[:6]


def _as_str_list(val: Any, *, limit: int = 4) -> list[str]:
    if isinstance(val, str) and val.strip():
        return [val.strip()[:120]]
    if isinstance(val, list):
        return [str(a)[:120] for a in val if a][:limit]
    return []


def _coerce_affinity(val: Any) -> str:
    if val in _ALLOWED_AFFINITY:
        return str(val)
    if isinstance(val, (int, float)):
        return "risk_on" if float(val) >= 0.45 else "risk_off"
    s = str(val or "").lower()
    if "consumer" in s or "소비" in s or "물가" in s:
        return "consumer"
    if "off" in s or "위험" in s or "방어" in s:
        return "risk_off"
    return "risk_on"


def _coerce_edge(val: Any) -> str:
    """path|alt 로 정규화. 레거시 then/and/but 도 수용."""
    s = str(val or "path").strip().lower()
    if s in ("path", "alt"):
        return s
    if s in ("then", "and", "그러면", "다음", "이어서", "그리고", "동시에"):
        return "path"
    if s in ("but", "alt", "그런데", "하지만", "다만", "반대", "다른"):
        return "alt"
    return "path"


def _short_issue_label(label: str) -> tuple[str, str]:
    """이슈 제목은 짧게. '~한다는 이야기' 꼬리 제거. (짧은제목, 원문)"""
    raw = (label or "").strip()
    s = re.sub(
        r"(?:이?라는|한다는|된다는|(?:다|라)[는은])\s*(?:이야기|기대|전망|논란)$",
        "",
        raw,
    ).strip()
    # "투자가 이어진"처럼 조사+동사 잔여 제거 → 짧은 명사 제목
    s = re.sub(r"(이|가|을|를|은|는)\s*[가-힣A-Za-z0-9]{2,12}$", "", s).strip(" ·,")
    s = re.sub(r"\s*이야기$", "", s).strip(" ·,")
    short = (s or raw)[:28]
    return short, raw


def _strip_json_fence(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t, flags=re.I)
        t = re.sub(r"\s*```$", "", t)
    return t.strip()


def _close_truncated_json(s: str) -> str:
    """max_tokens 등으로 잘린 JSON을 스택으로 닫아 복구 시도."""
    in_str = False
    escape = False
    stack: list[str] = []
    str_start = -1
    for i, ch in enumerate(s):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
                str_start = -1
            continue
        if ch == '"':
            in_str = True
            str_start = i
        elif ch == "{":
            stack.append("}")
        elif ch == "[":
            stack.append("]")
        elif ch in ("}", "]") and stack and stack[-1] == ch:
            stack.pop()
    out = s
    if in_str and str_start >= 0:
        # 미완성 문자열은 버리고 이전 콤마·콜론까지 정리
        out = s[:str_start].rstrip()
        if out.endswith(":"):
            out += " null"
        else:
            out = re.sub(r",\s*$", "", out)
    else:
        out = out.rstrip()
        out = re.sub(r",\s*$", "", out)
        if out.endswith(":"):
            out += " null"
    while stack:
        out += stack.pop()
    return out


def _parse_llm_json(text: str | None) -> dict | None:
    """코드펜스·잡텍스트·트레일링 콤마·잘림을 tolerantly 파싱."""
    if not text or not str(text).strip():
        return None
    t = _strip_json_fence(str(text))
    candidates: list[str] = [t]
    start = t.find("{")
    if start != -1:
        end = t.rfind("}")
        if end > start:
            candidates.append(t[start:end + 1])
        # 잘린 응답: 닫는 } 없거나 불완전 → 복구 후보
        candidates.append(_close_truncated_json(t[start:]))
    seen: set[str] = set()
    for cand in candidates:
        if not cand or cand in seen:
            continue
        seen.add(cand)
        for variant in (cand, re.sub(r",\s*([}\]])", r"\1", cand)):
            if variant in seen and variant != cand:
                continue
            seen.add(variant)
            try:
                obj = json.loads(variant)
                if isinstance(obj, dict):
                    return obj
            except Exception:
                continue
    return None


def _filter_conditions(raw: list | None) -> list[dict]:
    out = []
    for c in raw or []:
        if not isinstance(c, dict):
            continue
        metric = c.get("metric")
        op = c.get("op")
        if metric not in _ALLOWED_METRICS or op not in _ALLOWED_OPS:
            continue
        thr = c.get("threshold")
        if op == "in" and not isinstance(thr, list):
            continue
        out.append({
            "metric": metric,
            "op": op,
            "threshold": thr,
            "label": str(c.get("label") or metric)[:80],
        })
    return out[:4]


def _corpus_docs(limit: int = 120) -> list[dict]:
    """거시 우선 + 최근 문서. confirmed만."""
    docs: list[dict] = []
    seen: set[int] = set()
    for batch in (
        db.kb_documents(ticker="_MARKET", limit=60),
        db.kb_documents(doc_class="시황", limit=40),
        db.kb_documents(limit=limit),
    ):
        for d in batch or []:
            did = d.get("id")
            if did is not None and did in seen:
                continue
            if did is not None:
                seen.add(int(did))
            if (d.get("status") or "confirmed") not in ("confirmed", "", None):
                continue
            docs.append(d)
            if len(docs) >= limit:
                return docs
    return docs


def _keyword_tf(docs: list[dict], top_n: int = 24) -> list[tuple[str, int]]:
    ctr: Counter[str] = Counter()
    hangul_word = re.compile(r"[가-힣]{2,8}")
    for d in docs:
        text = f"{d.get('title') or ''} {d.get('summary') or ''}"
        for w in hangul_word.findall(text):
            if w not in _STOP:
                ctr[w] += 1
        for t in kb_search._tokenize(text):
            if len(t) < 3 or t in _STOP or t.isdigit():
                continue
            # 한글 2그램은 노이즈 많음 → 영문·숫자 위주 추가
            if re.fullmatch(r"[a-z0-9]+", t):
                ctr[t] += 1
    return ctr.most_common(top_n)


def _build_prompt_bundle() -> dict[str, Any]:
    docs = _corpus_docs()
    keywords = _keyword_tf(docs)
    headlines = []
    for d in docs[:30]:
        t = (d.get("title") or "").strip()
        if t:
            headlines.append(t[:120])
    macro = db.kb_digest_get("_MARKET") or {}
    prev = db.kv_get(_KV_KEY)
    prev_labels = []
    if isinstance(prev, dict):
        for ch in ((prev.get("tree") or {}).get("children") or []):
            if ch.get("label"):
                prev_labels.append(ch["label"])
    return {
        "keywords": keywords,
        "headlines": headlines,
        "macro_summary": (macro.get("summary") or "")[:500],
        "macro_points": list(macro.get("points") or [])[:6],
        "prev_labels": prev_labels[:6],
        "sector_keys": sorted(_sector_key_set()),
    }


def _default_outcome(tid: str, edge: str, sector_keys: list[str], eq: str) -> dict:
    sec_names = [s["name"] for s in _sector_nodes(sector_keys[:3])]
    sec_txt = "·".join(sec_names) if sec_names else "관련 업종"
    return {
        "id": f"{tid}_out", "kind": "outcome", "edge": edge,
        "label": f"그러면 {sec_txt} 쪽을 더 볼 만함", "detail": "",
        "assumptions": [], "sector_keys": sector_keys[:3],
        "evidence_query": eq, "children": [],
    }


def _validate_fork_child(
    th: dict, *, bid: str, j: int, edge: str, sector_keys: list[str], eq: str,
) -> dict | None:
    tlabel = str(th.get("label") or th.get("plain") or "").strip()[:48]
    if not tlabel:
        return None
    tid = _slug(str(th.get("id") or ""), f"{bid}_fork_{j}")
    tcond = _filter_conditions(th.get("conditions"))
    outs: list[dict] = []
    for k, oc in enumerate([c for c in (th.get("children") or []) if isinstance(c, dict)][:1]):
        olabel = str(oc.get("label") or oc.get("plain") or "").strip()[:48]
        if not olabel:
            continue
        oid = _slug(str(oc.get("id") or ""), f"{tid}_out_{k}")
        osec = _filter_sectors(oc.get("sector_keys")) or sector_keys[:3]
        outs.append({
            "id": oid, "kind": "outcome", "edge": edge, "label": olabel,
            "detail": str(oc.get("detail") or "").strip()[:240],
            "assumptions": _as_str_list(oc.get("assumptions"), limit=3),
            "sector_keys": osec,
            "evidence_query": str(oc.get("evidence_query") or eq)[:120],
            "children": [],
        })
    if not outs:
        outs = [_default_outcome(tid, edge, sector_keys, eq)]
    return {
        "id": tid, "kind": "fork", "edge": edge, "label": tlabel,
        "detail": str(th.get("detail") or "").strip()[:240],
        "assumptions": _as_str_list(th.get("assumptions"), limit=3),
        "conditions": tcond, "children": outs,
    }


def _ensure_fork_pair(
    children: list[dict], *, bid: str, sector_keys: list[str], eq: str,
) -> list[dict]:
    """항상 path·alt 쌍. 한쪽만 있으면 반대 갈래를 보강."""
    path = next((c for c in children if c.get("edge") == "path"), None)
    alt = next((c for c in children if c.get("edge") == "alt"), None)
    if path is None and children:
        path = {**children[0], "edge": "path", "kind": "fork"}
    if alt is None and len(children) >= 2:
        alt = {**children[1], "edge": "alt", "kind": "fork"}
    if path is None:
        tid = f"{bid}_fork_path"
        path = {
            "id": tid, "kind": "fork", "edge": "path",
            "label": "이 방향으로 이어질 때", "detail": "",
            "assumptions": [], "conditions": [],
            "children": [_default_outcome(tid, "path", sector_keys, eq)],
        }
    if alt is None:
        tid = f"{bid}_fork_alt"
        alt = {
            "id": tid, "kind": "fork", "edge": "alt",
            "label": "다른 방향으로 갈 때", "detail": "",
            "assumptions": [], "conditions": [],
            "children": [_default_outcome(tid, "alt", sector_keys, eq)],
        }
    if path["id"] == alt["id"]:
        alt = {**alt, "id": f"{alt['id']}_alt"}
    return [path, alt]


def _validate_llm_branches(raw: dict | None) -> list[dict] | None:
    """이슈 1~4개. 각 이슈는 path/alt 쌍갈림 강제. 형식 어겨도 최대한 살림."""
    if not isinstance(raw, dict):
        return None
    branches = raw.get("branches") or raw.get("issues") or raw.get("ifs") or []
    if not isinstance(branches, list) or not branches:
        return None
    branches = branches[:2]  # 출력 짧게 — JSON 잘림 방지
    out: list[dict] = []
    used_ids: set[str] = set()
    for i, b in enumerate(branches):
        if not isinstance(b, dict):
            continue
        label_raw = str(b.get("label") or b.get("plain") or b.get("title") or "").strip()
        if not label_raw:
            continue
        label, label_full = _short_issue_label(label_raw)
        detail = str(b.get("detail") or "").strip()[:240]
        if not detail and label_full and label_full != label:
            detail = f"{label_full}."[:240]
        bid = _slug(str(b.get("id") or ""), f"issue_{i}")
        if bid in used_ids:
            bid = f"{bid}_{i}"
        used_ids.add(bid)
        affinity = _coerce_affinity(b.get("affinity"))
        sector_keys = _filter_sectors(b.get("sector_keys"))
        if not sector_keys:
            sector_keys = ["finance", "telecom"]
        eq = str(b.get("evidence_query") or label)[:120]
        assumptions = _as_str_list(b.get("assumptions"), limit=4) or [label]
        children_raw = [c for c in (b.get("children") or []) if isinstance(c, dict)][:4]
        parsed: list[dict] = []
        for j, th in enumerate(children_raw):
            edge = _coerce_edge(th.get("edge"))
            # 첫 번째는 path, 둘 번째는 alt 우선 (edge 누락 시)
            if j == 0 and "edge" not in th:
                edge = "path"
            elif j == 1 and "edge" not in th:
                edge = "alt"
            node = _validate_fork_child(
                th, bid=bid, j=j, edge=edge, sector_keys=sector_keys, eq=eq,
            )
            if node:
                parsed.append(node)
        children = _ensure_fork_pair(parsed, bid=bid, sector_keys=sector_keys, eq=eq)
        out.append({
            "id": bid, "label": label, "detail": detail, "edge": "if",
            "assumptions": assumptions, "sector_keys": sector_keys,
            "evidence_query": eq, "affinity": affinity, "children": children,
        })
    return out if out else None


def _llm_draft_templates() -> tuple[list[dict] | None, str | None, str | None]:
    """Sonnet으로 트리 초안. (templates, model, fail_reason)."""
    from signal_desk import llm as llm_mod
    if not llm_mod.available():
        return None, None, "ANTHROPIC_API_KEY가 없습니다. 서버 .env를 확인한 뒤 재시작하세요."
    bundle = _build_prompt_bundle()
    # 프롬프트·출력 짧게 — 쌍갈림 JSON 잘림 방지
    sector_list = ", ".join(bundle["sector_keys"][:12])
    kw_line = ", ".join(f"{w}" for w, _ in bundle["keywords"][:10])
    heads = "\n".join(f"- {h}" for h in bundle["headlines"][:10])
    prev = ", ".join(bundle["prev_labels"][:4]) or "(없음)"
    points = "\n".join(f"- {p}" for p in bundle["macro_points"][:3]) or "(없음)"
    system = (
        "최근 이슈 흐름 JSON 에디터. 학습용. 가설검증·투자권유·확률% 금지. "
        "이슈 1~2개. 각 이슈 children는 path·alt 정확히 2개. 각 fork 아래 outcome 1개. "
        "이슈 label은 짧은 제목만. 해설은 detail·assumptions·하위 label에. "
        "영문·리스크온/오프·CAPEX·VIX 단독 제목 금지(지표는 conditions에만). "
        "종목명은 넣지 말 것(서버가 붙임). JSON 객체만."
    )
    user = (
        f"요약: {(bundle['macro_summary'] or '')[:280] or '(없음)'}\n"
        f"points:\n{points}\n"
        f"키워드: {kw_line or '(없음)'}\n"
        f"헤드라인:\n{heads or '- (없음)'}\n"
        f"직전: {prev}\n"
        f"sector_keys: {sector_list}\n"
        "metric: NASDAQCOM,VIXCLS,CPIAUCSL,FEDFUNDS,macro_bias,regime | "
        "op: chg>,chg<,>=,<=,==,in | affinity(내부용): risk_on|consumer|risk_off\n"
        "라벨 규칙:\n"
        "- 이슈 label: 짧은 명사 제목만(예: 거품 경계, AI 투자, 금리·물가). "
        "'~한다는 이야기/기대/전망' 같은 말꼬리 금지. 해설은 detail에 쉽게.\n"
        "- path/alt: 쉬운 연결 문장(예: 그 강세가 더 이어지면 / 우려가 커지면)\n"
        "- outcome: '그러면 ○○ 쪽을 더 볼 만함'\n"
        "필드: assumptions≤2, conditions≤2, 이슈 detail≤60자. 종목명 금지.\n"
        '스키마:{"branches":[{"label":"","detail":"","affinity":"risk_on",'
        '"assumptions":[""],"sector_keys":["semiconductor"],"evidence_query":"",'
        '"children":[{"label":"","edge":"path","assumptions":[],'
        '"conditions":[{"metric":"VIXCLS","op":"<","threshold":20,"label":""}],'
        '"children":[{"label":"","sector_keys":["semiconductor"],"evidence_query":""}]},'
        '{"label":"","edge":"alt","assumptions":[],'
        '"conditions":[{"metric":"VIXCLS","op":">=","threshold":25,"label":""}],'
        '"children":[{"label":"","sector_keys":["defense"],"evidence_query":""}]}]}]}'
    )
    model = llm_mod.DIGEST_QUALITY_MODEL
    text = llm_mod.complete(system, user, max_tokens=4096, model=model)
    if not text:
        return None, model, "LLM 응답이 비었습니다. 잠시 후 다시 시도하세요."
    raw = _parse_llm_json(text)
    if raw is None:
        log.warning(
            "hypothesis LLM JSON 파싱 실패 len=%d head=%r tail=%r",
            len(text), text[:120], text[-120:],
        )
        return None, model, "LLM JSON 파싱 실패(형식 깨짐). 다시 생성해 보세요."
    validated = _validate_llm_branches(raw)
    if not validated:
        log.warning("hypothesis LLM JSON 검증 실패: keys=%s", list(raw.keys())[:8])
        return None, model, "LLM JSON 구조가 맞지 않습니다. 다시 생성해 보세요."
    return validated, model, None


def _build_child_node(
    tmpl: dict,
    *,
    parent_id: str,
    indicators,
    macro_bias,
    regime_name,
    evidence_cache: dict[str, tuple[float, list]],
) -> dict:
    raw_kind = tmpl.get("kind") or "fork"
    # 레거시 then → fork
    kind = "fork" if raw_kind in ("then", "fork") else raw_kind
    node_id = tmpl["id"]
    conditions = list(tmpl.get("conditions") or [])
    current = _condition_snapshot(
        conditions, indicators=indicators, macro_bias=macro_bias, regime_name=regime_name,
    )
    sector_keys = list(tmpl.get("sector_keys") or [])
    eq = tmpl.get("evidence_query")
    evidence: list[dict] = []
    if eq:
        if eq not in evidence_cache:
            evidence_cache[eq] = _evidence_for(eq)
        _, evidence = evidence_cache[eq]
    children = [
        _build_child_node(
            c, parent_id=node_id, indicators=indicators, macro_bias=macro_bias,
            regime_name=regime_name, evidence_cache=evidence_cache,
        )
        for c in (tmpl.get("children") or [])
    ]
    edge = _coerce_edge(tmpl.get("edge")) if kind == "fork" else (tmpl.get("edge") or "path")
    if kind == "outcome":
        edge = _coerce_edge(tmpl.get("edge")) if tmpl.get("edge") else "path"
    if kind == "outcome" and not any(c.get("kind") == "action" for c in children):
        children.append(_make_action_node(node_id, sector_keys, edge))
    node = {
        "id": node_id,
        "parent_id": parent_id,
        "kind": kind,
        "edge": edge,
        "edge_ko": _EDGE_KO.get(edge, edge),
        "label": tmpl["label"],
        "detail": tmpl.get("detail") or "",
        "support_pct": None,
        "branch_pct": None,
        "emphasized": False,
        "assumptions": list(tmpl.get("assumptions") or []),
        "conditions": conditions,
        "current": current,
        "sector_keys": sector_keys,
        "sectors": _sector_nodes(sector_keys),
        "watch_tickers": list(tmpl.get("watch_tickers") or []),
        "evidence": evidence,
        "evidence_n": len(evidence),
        "children": children,
    }
    if kind == "fork":
        node["_w"] = _fork_weight(
            conditions, indicators=indicators, macro_bias=macro_bias, regime_name=regime_name,
        )
    return node


def build(*, templates: list[dict] | None = None, store_prices=None, store_macro=None,
          source: str = "fallback", model: str | None = None) -> dict:
    """템플릿(+룰 점수)으로 트리 생성. templates 없으면 폴백 _TEMPLATES."""
    from signal_desk import store

    tmpl_list = templates if templates is not None else _TEMPLATES
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

    for t in tmpl_list:
        affinity = t.get("affinity") if t.get("affinity") in _ALLOWED_AFFINITY else "risk_on"
        m = _metric_score(affinity, macro_bias=macro_bias, regime_name=regime_name,
                          phase_key=phase_key, indicators=indicators or [])
        c = _cycle_score(t.get("sector_keys") or [], lead_tags)
        eq = t.get("evidence_query") or t.get("label") or ""
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
        _assign_pair_weights(children)

        if_nodes.append({
            "id": t["id"],
            "parent_id": "root",
            "kind": "if",
            "edge": "if",
            "edge_ko": "이슈",
            "label": t["label"],
            "detail": t.get("detail") or "",
            "support_pct": 0,
            "assumptions": t.get("assumptions") or [],
            "conditions": [],
            "current": {},
            "sector_keys": t.get("sector_keys") or [],
            "sectors": _sector_nodes(t.get("sector_keys") or []),
            "evidence": evidence,
            "evidence_n": len(evidence),
            "watch_metrics": watch,
            "affinity": affinity,
            "scores": {"metric": round(m, 3), "kb": round(k, 3), "cycle": round(c, 3),
                       "raw": round(w, 3)},
            "children": children,
        })

    pct = _normalize(raw_w)
    for node in if_nodes:
        node["support_pct"] = pct[node["id"]]

    active_id = max(if_nodes, key=lambda n: n["support_pct"])["id"] if if_nodes else None

    root = {
        "id": "root",
        "parent_id": None,
        "kind": "root",
        "edge": None,
        "label": "최근 이슈 흐름",
        "detail": "뉴스·KB 관심 이슈 → 이렇게/다르게 이어질 때 → 업종 파급 → 다시 볼 종목",
        "support_pct": 100,
        "assumptions": [],
        "conditions": [],
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
        "generated_at": _kst_now_iso(),
        "source": source,
        "model": model,
        "trigger": "manual",
        "disclaimer": _DISCLAIMER,
        "tree": root,
        "context": {
            "macro_bias": macro_bias, "regime": regime_name,
            "cycle_phase": phase_name, "lead_sectors": lead_tags,
            "active_if": active_id,
        },
    }


def refresh() -> dict:
    """관리자 수동 전용. Sonnet 성공 시에만 kv 저장. 실패 시 기존 캐시 유지 + ready:false."""
    templates, model, fail = _llm_draft_templates()
    if not templates:
        prev = get(build_if_missing=False)
        return {
            "ready": False,
            "reason": fail or "이슈 흐름 생성에 실패했습니다.",
            "model": model,
            "kept_previous": bool(prev.get("ready")),
        }
    data = build(templates=templates, source="llm", model=model)
    db.kv_set(_KV_KEY, data)
    return data


def get(*, build_if_missing: bool = False) -> dict:
    """캐시만. 자동 생성·LLM 호출 없음."""
    for key, src in ((_KV_KEY, None), ("hypo:v3:latest", "legacy_v3"), ("hypo:v2:latest", "legacy_v2")):
        cached = db.kv_get(key)
        if isinstance(cached, dict) and cached.get("ready") and cached.get("tree"):
            kids = (cached.get("tree") or {}).get("children") or []
            if kids and kids[0].get("kind") == "if":
                if src:
                    return {**cached, "source": cached.get("source") or src}
                return cached
    # build_if_missing는 무시 — 자동 생성·LLM 금지(비용). 수동 refresh만 생성.
    return {
        "ready": False,
        "reason": "최근 이슈 흐름이 아직 없습니다. 관리자가 흐름을 생성하면 여기에 표시됩니다.",
    }
