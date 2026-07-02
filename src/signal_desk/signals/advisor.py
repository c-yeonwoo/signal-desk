"""봇 LLM 자문(하이브리드) — 가드레일 안에서 '무엇을 왜 살지' 최종 선별.

원칙: 안전장치(장시간·손절/익절/트레일링·최대종목·회당한도·비중·정수주)는 코드가 절대 사수한다.
LLM은 이미 정량 가드레일을 통과한 후보 중에서만 고르고, 근거를 만든다 — 후보 밖은 못 고른다.
입력: 후보(정량 근거) + 시장맥락(국면·거시·경기사이클) + KB 정성 다이제스트 + 과거 의사결정 성패(학습).
ANTHROPIC_API_KEY 없거나 실패하면 None을 반환해 봇이 결정론적 점수순 폴백을 쓰게 한다.

학습: 단일 실패에 과적합하지 않는다 — 최근 성패는 '경향'으로만 참고하도록 프롬프트에 명시.
"""

from __future__ import annotations

import logging

from signal_desk import db, llm

log = logging.getLogger("signal_desk.advisor")


def build_lessons(limit: int = 30) -> list[dict]:
    """과거 의사결정 중 사후수익(outcome_pct)이 기록된 것만 학습 재료로 추림."""
    out = []
    for d in db.bot_decisions_recent(limit):
        if d.get("outcome_pct") is None:
            continue
        ctx = d.get("context") or {}
        out.append({
            "name": d["name"], "action": d["action"], "outcome_pct": round(d["outcome_pct"], 1),
            "regime": ctx.get("regime"), "macro": ctx.get("macro_bias"), "cycle": ctx.get("cycle_phase"),
        })
    return out


def select_buys(candidates: list[dict], context: dict, digests: dict[str, dict],
                lessons: list[dict], max_new: int) -> list[dict] | None:
    """candidates: [{ticker,name,score,confidence,reasons}] (이미 가드레일 통과분).
    반환: [{ticker, rationale}] (candidates 안에서만, 최대 max_new). LLM 없거나 실패 시 None."""
    if not llm.available() or not candidates or max_new <= 0:
        return None

    valid = {c["ticker"] for c in candidates}
    cand_lines = []
    for c in candidates:
        dg = digests.get(c["ticker"]) or {}
        senti = f", 정성심리 {dg['sentiment']:+.2f}({dg.get('summary', '')[:50]})" if dg else ""
        cand_lines.append(
            f'- {c["ticker"]} {c["name"]}: 종합점수 {c["score"]:+.2f}, 신뢰도 {c["confidence"]:.2f}, '
            f'근거 [{", ".join(c.get("reasons", [])[:3])}]{senti}')
    lesson_lines = [f'- {l["name"]} {l["action"]} @국면 {l.get("regime")}/거시 {l.get("macro")} → 사후 {l["outcome_pct"]:+.1f}%'
                    for l in lessons[:15]] or ["- (아직 학습할 과거 성패 기록 없음)"]

    system = (
        "너는 한국 주식 자동매매 봇의 최종 매수 선별 자문역이다. 목표는 리스크 관리 하의 '수익 극대화'다. "
        "반드시 아래 후보 목록 안에서만 고른다(목록 밖 종목 금지). 시장 맥락(국면/거시/경기사이클)과 "
        "정성 심리, 과거 성패 경향을 함께 고려하되, 단 한 번의 실패에 과도하게 반응하지 마라(표본이 적으면 경향만 참고). "
        "손절·익절·비중 같은 실행 규칙은 코드가 처리하니 너는 '무엇을 왜'만 정한다.")
    user = (
        f"[시장 맥락] 국면={context.get('regime')} · 거시={context.get('macro_bias')} · "
        f"경기사이클={context.get('cycle_phase')}\n\n"
        f"[매수 후보(가드레일 통과)]\n" + "\n".join(cand_lines) + "\n\n"
        f"[과거 의사결정 성패(학습, 경향 참고용)]\n" + "\n".join(lesson_lines) + "\n\n"
        f"이 중 지금 매수하기 가장 좋은 종목을 최대 {max_new}개 골라라. "
        'JSON으로만: {"picks": [{"ticker": "코드", "rationale": "한국어 한 줄 근거"}]}')

    out = llm.complete_json(system, user, max_tokens=700)
    if not out or not isinstance(out.get("picks"), list):
        log.info("LLM 자문 파싱 실패 — 결정론적 폴백")
        return None
    picks = []
    seen = set()
    for p in out["picks"]:
        t = p.get("ticker")
        if t in valid and t not in seen:  # 후보 밖·중복 방지(가드레일)
            picks.append({"ticker": t, "rationale": str(p.get("rationale", ""))[:200]})
            seen.add(t)
        if len(picks) >= max_new:
            break
    return picks or None
