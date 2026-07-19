"""시황 가설(#6) — 배타적 IF → then → outcome.

관리자 수동 refresh 시에만 Sonnet이 트리 문장을 생성. 지지도·status는 룰.
일일 자동 LLM 호출 없음. GET은 캐시만(없으면 ready:false).
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

_KV_KEY = "hypo:v3:latest"
_DISCLAIMER = (
    "가설·학습용 · 뉴스·KB 핫이슈 · 수동 생성 · 관심 비중(이슈 간 상대) "
    "· 예측·투자권유 아님 · 시그널과 별개 레이어"
)
# 노드/UI용 — then·but 직역이 아니라 "이 가정이 지금 데이터와 어떤지"
_STATUS_KO = {
    "aligned": "지금 이 가정에 가까움",
    "watching": "아직 갈림길",
    "diverging": "지금 이 가정과 안 맞음",
    "n/a": "",
}
# 상세 패널용 — 트리 연결 의미 (직역 then/but 아님)
_EDGE_KO = {
    "if": "이슈",
    "then": "이 이슈가 이어질 때",
    "and": "이와 함께 겹칠 때",
    "but": "반대로 꺾일 때",
}

_ALLOWED_METRICS = frozenset({
    "NASDAQCOM", "VIXCLS", "CPIAUCSL", "FEDFUNDS", "macro_bias", "regime",
})
_ALLOWED_OPS = frozenset({
    "==", "in", ">=", "<=", ">", "<", "chg>", "chg<", "chg>=", "chg<=",
})
_ALLOWED_AFFINITY = frozenset({"risk_on", "consumer", "risk_off"})
_ALLOWED_EDGES = frozenset({"if", "then", "and", "but"})
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


def _eval_status(conditions: list[dict], *, indicators, macro_bias, regime_name) -> tuple[str, dict]:
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
            current[m] = {"value": ind.get("value"), "change": ind.get("change")}
        results.append(_cond_ok(c, indicators=indicators, macro_bias=macro_bias,
                                regime_name=regime_name))
    known = [r for r in results if r is not None]
    if not known:
        return "watching", current
    if all(known) and None not in results:
        return "aligned", current
    if any(r is False for r in known):
        if all(r is False for r in known):
            return "diverging", current
        return "watching", current
    return "watching", current


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
    s = str(val or "then").strip().lower()
    if s in _ALLOWED_EDGES and s != "if":
        return s
    if s in ("그러면", "다음", "이어서"):
        return "then"
    if s in ("그런데", "하지만", "다만", "but"):
        return "but"
    if s in ("그리고", "동시에", "and"):
        return "and"
    return "then"


def _parse_llm_json(text: str | None) -> dict | None:
    """코드펜스·잡텍스트·트레일링 콤마를 tolerantly 파싱."""
    if not text or not str(text).strip():
        return None
    t = str(text).strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t, flags=re.I)
        t = re.sub(r"\s*```$", "", t)
    candidates = [t]
    start, end = t.find("{"), t.rfind("}")
    if start != -1 and end > start:
        candidates.append(t[start:end + 1])
    for cand in candidates:
        for variant in (cand, re.sub(r",\s*([}\]])", r"\1", cand)):
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


def _validate_llm_branches(raw: dict | None) -> list[dict] | None:
    """핫이슈 1~4개. LLM이 형식을 조금 어겨도 최대한 살린다."""
    if not isinstance(raw, dict):
        return None
    branches = raw.get("branches") or raw.get("issues") or raw.get("ifs") or []
    if not isinstance(branches, list) or not branches:
        return None
    branches = branches[:4]
    out: list[dict] = []
    used_ids: set[str] = set()
    for i, b in enumerate(branches):
        if not isinstance(b, dict):
            continue
        label = str(b.get("label") or b.get("plain") or b.get("title") or "").strip()[:48]
        if not label:
            continue
        detail = str(b.get("detail") or "").strip()[:240]
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
        children_raw = [c for c in (b.get("children") or []) if isinstance(c, dict)][:2]
        children: list[dict] = []
        for j, th in enumerate(children_raw):
            tlabel = str(th.get("label") or th.get("plain") or "").strip()[:48]
            if not tlabel:
                continue
            tid = _slug(str(th.get("id") or ""), f"{bid}_then_{j}")
            edge = _coerce_edge(th.get("edge"))
            tcond = _filter_conditions(th.get("conditions"))
            outs: list[dict] = []
            for k, oc in enumerate([c for c in (th.get("children") or []) if isinstance(c, dict)][:1]):
                olabel = str(oc.get("label") or oc.get("plain") or "").strip()[:48]
                if not olabel:
                    continue
                oid = _slug(str(oc.get("id") or ""), f"{tid}_out_{k}")
                osec = _filter_sectors(oc.get("sector_keys")) or sector_keys[:3]
                outs.append({
                    "id": oid, "kind": "outcome", "edge": "then", "label": olabel,
                    "detail": str(oc.get("detail") or "").strip()[:240],
                    "assumptions": _as_str_list(oc.get("assumptions"), limit=3),
                    "sector_keys": osec,
                    "evidence_query": str(oc.get("evidence_query") or eq)[:120],
                    "children": [],
                })
            if not outs:
                outs = [{
                    "id": f"{tid}_out", "kind": "outcome", "edge": "then",
                    "label": "관련 업종을 눈여겨볼 만함", "detail": "",
                    "assumptions": [], "sector_keys": sector_keys[:3],
                    "evidence_query": eq, "children": [],
                }]
            children.append({
                "id": tid, "kind": "then", "edge": edge, "label": tlabel,
                "detail": str(th.get("detail") or "").strip()[:240],
                "assumptions": _as_str_list(th.get("assumptions"), limit=3),
                "conditions": tcond, "children": outs,
            })
        if not children:
            # then 없이도 이슈는 살림
            children = [{
                "id": f"{bid}_then_0", "kind": "then", "edge": "then",
                "label": "관련 신호가 이어지면", "detail": "",
                "assumptions": [], "conditions": [],
                "children": [{
                    "id": f"{bid}_out", "kind": "outcome", "edge": "then",
                    "label": "관련 업종을 눈여겨볼 만함", "detail": "",
                    "assumptions": [], "sector_keys": sector_keys[:3],
                    "evidence_query": eq, "children": [],
                }],
            }]
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
    # 프롬프트 짧게 — 잘린 JSON·타임아웃 방지
    sector_list = ", ".join(bundle["sector_keys"][:14])
    kw_line = ", ".join(f"{w}" for w, _ in bundle["keywords"][:14])
    heads = "\n".join(f"- {h}" for h in bundle["headlines"][:14])
    prev = ", ".join(bundle["prev_labels"]) or "(없음)"
    points = "\n".join(f"- {p}" for p in bundle["macro_points"][:4]) or "(없음)"
    system = (
        "시황 이슈 에디터. 투자권유·확률% 금지. "
        "뉴스 기반 핫이슈 1~3개(배타 아님). 고정 템플릿 금지. "
        "label=쉬운 한국어 한 줄. detail에만 전문용어. "
        "유효한 JSON 객체만(코드펜스 금지)."
    )
    user = (
        f"요약: {bundle['macro_summary'] or '(없음)'}\n"
        f"points:\n{points}\n"
        f"키워드: {kw_line or '(없음)'}\n"
        f"헤드라인:\n{heads or '- (없음)'}\n"
        f"직전 라벨: {prev}\n"
        f"sector_keys(영문만): {sector_list}\n"
        f"metric: NASDAQCOM,VIXCLS,CPIAUCSL,FEDFUNDS,macro_bias,regime\n"
        "op: chg>,chg<,>=,<=,==,in\n"
        "affinity: risk_on|consumer|risk_off 문자열\n"
        "edge: then|and|but\n"
        "assumptions는 문자열 배열. 각 이슈 then 1개+outcome 1개.\n"
        '{"branches":[{"label":"…","detail":"…","affinity":"risk_on","assumptions":["…"],'
        '"sector_keys":["semiconductor"],"evidence_query":"…",'
        '"children":[{"label":"…","edge":"then","detail":"…","assumptions":[],'
        '"conditions":[{"metric":"VIXCLS","op":"<","threshold":20,"label":"…"}],'
        '"children":[{"label":"…","detail":"…","sector_keys":["semiconductor"],'
        '"evidence_query":"…"}]}]}]}'
    )
    model = llm_mod.DIGEST_QUALITY_MODEL
    text = llm_mod.complete(
        system + "\n반드시 JSON 객체만 출력.",
        user, max_tokens=2200, model=model,
    )
    if not text:
        return None, model, "LLM 응답이 비었습니다. 잠시 후 다시 시도하세요."
    raw = _parse_llm_json(text)
    if raw is None:
        log.warning("hypothesis LLM JSON 파싱 실패 head=%r", text[:160])
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
    if kind == "outcome" and not conditions:
        status = "n/a"
    children = [
        _build_child_node(
            c, parent_id=node_id, indicators=indicators, macro_bias=macro_bias,
            regime_name=regime_name, evidence_cache=evidence_cache,
        )
        for c in (tmpl.get("children") or [])
    ]
    edge = tmpl.get("edge") or "then"
    return {
        "id": node_id,
        "parent_id": parent_id,
        "kind": kind,
        "edge": edge,
        "edge_ko": _EDGE_KO.get(edge, edge),
        "label": tmpl["label"],
        "detail": tmpl.get("detail") or "",
        "support_pct": None,
        "assumptions": list(tmpl.get("assumptions") or []),
        "conditions": conditions,
        "status": status,
        "status_ko": _STATUS_KO.get(status, status),
        "current": current,
        "sector_keys": sector_keys,
        "sectors": _sector_nodes(sector_keys),
        "evidence": evidence,
        "evidence_n": len(evidence),
        "children": children,
    }


def _inherit_outcome_status(node: dict) -> None:
    for ch in node.get("children") or []:
        if ch.get("kind") == "outcome" and ch.get("status") == "n/a":
            ch["status"] = node.get("status") or "watching"
        _inherit_outcome_status(ch)


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
        for ch in children:
            _inherit_outcome_status(ch)

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
            "status": "n/a",
            "status_ko": "",
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
        "label": "지금 볼 이슈",
        "detail": "뉴스·KB 기준으로 뽑아 둔 거시 핫이슈와 이어질 수 있는 흐름",
        "support_pct": 100,
        "assumptions": [],
        "conditions": [],
        "status": "n/a",
        "status_ko": "",
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
            "reason": fail or "가설 생성에 실패했습니다.",
            "model": model,
            "kept_previous": bool(prev.get("ready")),
        }
    data = build(templates=templates, source="llm", model=model)
    db.kv_set(_KV_KEY, data)
    return data


def get(*, build_if_missing: bool = False) -> dict:
    """캐시만. 자동 생성·LLM 호출 없음."""
    cached = db.kv_get(_KV_KEY)
    if isinstance(cached, dict) and cached.get("ready") and cached.get("tree"):
        kids = (cached.get("tree") or {}).get("children") or []
        if kids and kids[0].get("kind") == "if":
            return cached
    # 구 v2 캐시가 있으면 읽기만 허용(재생성 안 함)
    legacy = db.kv_get("hypo:v2:latest")
    if isinstance(legacy, dict) and legacy.get("ready") and legacy.get("tree"):
        kids = (legacy.get("tree") or {}).get("children") or []
        if kids and kids[0].get("kind") == "if":
            return {**legacy, "source": legacy.get("source") or "legacy_v2"}
    # build_if_missing는 무시 — 자동 생성·LLM 금지(비용). 수동 refresh만 생성.
    return {
        "ready": False,
        "reason": "시황 가설이 아직 없습니다. 관리자 새로고침으로 뉴스·KB 기반 가설을 생성하세요.",
    }
