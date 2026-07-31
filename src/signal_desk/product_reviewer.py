"""제품 리뷰어(Lx) — 매매 경로 밖에서 구멍만 지적한다.

audit.py 가 '숫자가 틀렸을 수 있나'를 묻듯, 여기는 '제품·데이터·카피가 새는지'를 묻는다.
판정권·엔진 변경권 없음. falsifier 없는 항목은 버린다.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time

from signal_desk import db, llm

log = logging.getLogger(__name__)

MAX_FINDINGS = 5
_KV_KEY = "product_review_last"

_SYSTEM = """너는 주식 시그널 제품의 야간 리뷰어다. 매수/매도를 추천하지 마라.
임무는 **제품·데이터·UX·비용·카피**의 구멍만 찾는 것이다. 엔진 가중치·문턱을 바꾸라고 하지 마라.

규칙:
- 각 지적은 반증 가능해야 한다. falsifier 없이 쓰지 마라.
- "매일 수익을 내라", "모니터링을 강화하라" 같은 항상 참/목표 혼동 금지.
- 이미 코드가 막는 것(룩어헤드·셔플·base rate)을 다시 쓰지 마라.
- Desk Report·편중·선정 숫자를 보고, 가짜 경고(예: 미분류=편중)나 빈날 오해·이중감점·비용 낭비를 우선한다.
- 매매 경로에 자동 반영할 수 있는 지시 금지. backlog_hint 한 줄만.

JSON만:
{"findings": [{"area": "data|ux|risk|cost|copy", "title": "...",
 "claim": "...", "falsifier": "...", "severity": "high|medium|low",
 "backlog_hint": "..."}]}"""


def _fid(item: dict) -> str:
    raw = f"{item.get('area')}|{item.get('title')}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def collect_context() -> dict:
    """리뷰용 스냅샷. 조각 실패는 키별로 격리."""
    ctx: dict = {"generated": time.strftime("%Y-%m-%d %H:%M"), "layer": "Lx"}

    def _try(key, fn):
        try:
            ctx[key] = fn()
        except Exception as exc:  # noqa: BLE001
            log.warning("product_review 컨텍스트 실패(%s): %s", key, exc)
            ctx[key] = {"error": str(exc)[:200]}

    from signal_desk import signalcfg, store
    from signal_desk.signals import accuracy, advisor_shadow

    _try("engine_config", signalcfg.get_dict)
    _try("data_health", lambda: {"drift": store.signal_drift()})
    # 시그널 API가 남긴 최근 Desk Report — api 순환 import 금지
    _try("desk_report", lambda: db.kv_get("desk_report_last") or {"ready": False})
    _try("crowding_last", lambda: db.kv_get("crowding_last") or {})
    _try("advisor_shadow", lambda: _shadow_slim(advisor_shadow))
    _try("accuracy", lambda: _acc_slim(accuracy))
    return ctx


def _shadow_slim(advisor_shadow) -> dict:
    from signal_desk import store
    out = advisor_shadow.summary(store.load_all_dated_closes())
    keep = ("ready", "paired_n", "paired_delta_pct", "paired_delta_significant",
            "paired_verdict_ready", "runs")
    return {k: out.get(k) for k in keep if k in out}


def _acc_slim(accuracy) -> dict:
    from signal_desk import store
    df = store.load_signal_history()
    if df.empty:
        return {"ready": False}
    out = accuracy.realized_accuracy(df.to_dict("records"), store.load_all_dated_closes())
    keep = ("buy_precision_pct", "buy_sample", "buy_lift_pp", "coverage")
    return {k: out.get(k) for k in keep if k in out}


def generate(context: dict | None = None) -> dict:
    """지적 생성 → kv 저장. 매매 경로에 쓰지 않는다."""
    if not llm.available():
        return {"ready": False, "reason": "ANTHROPIC_API_KEY 미설정 — 리뷰 비활성", "saved": 0}
    ctx = context if context is not None else collect_context()
    user = (f"스냅샷:\n{json.dumps(ctx, ensure_ascii=False, default=str)[:12000]}\n\n"
            f"지적을 최대 {MAX_FINDINGS}개. falsifier 못 쓰면 빼라.")
    raw = llm.complete_json(_SYSTEM, user, max_tokens=2000)
    if not raw or not isinstance(raw.get("findings"), list):
        return {"ready": False, "reason": "LLM 응답 없음·파싱 실패", "saved": 0}
    items = []
    for it in raw["findings"][:MAX_FINDINGS]:
        if not isinstance(it, dict):
            continue
        if not (it.get("falsifier") or "").strip() or not (it.get("title") or "").strip():
            continue
        items.append({**it, "id": _fid(it)})
    payload = {
        "ts": int(time.time()),
        "findings": items,
        "disclaimer": ("제품 리뷰다 — 매매 지시가 아니다. 엔진·봇에 자동 반영하지 않는다. "
                       "사람이 BACKLOG로만 옮긴다."),
    }
    db.kv_set(_KV_KEY, payload)
    return {"ready": True, "saved": len(items), "dropped": len(raw["findings"]) - len(items),
            **payload}


def summary() -> dict:
    raw = db.kv_get(_KV_KEY)
    if not isinstance(raw, dict):
        return {"available": llm.available(), "findings": [], "ts": None,
                "disclaimer": "아직 리뷰 없음 — POST /api/product-review/run"}
    return {"available": llm.available(), **raw}
