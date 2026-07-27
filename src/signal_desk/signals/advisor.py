"""봇 LLM 자문(하이브리드) — 가드레일 안에서 '무엇을 왜 살지' 최종 선별.

원칙: 안전장치(장시간·손절/익절·트레일링·최대종목·회당한도·비중·정수주)는 코드가 절대 사수한다.
LLM은 이미 정량 가드레일을 통과한 후보 중에서만 고르고, 근거를 만든다 — 후보 밖은 못 고른다.
입력: 후보(정량 근거) + 시장맥락(국면·거시·경기사이클) + KB 정성 다이제스트 + 과거 의사결정 성패(학습).

**기권과 실패는 다르다**:
- `[]`(빈 리스트) = LLM이 "지금은 살 게 없다"고 판단한 **기권**. 봇은 이를 존중해 매수하지 않는다.
- `None` = 키 없음·API 실패·파싱 실패·후보 밖 종목만 골라 전부 탈락 = **사용 불가**. 봇이 점수순 폴백.

**하네스(2026-07-27)**:
1. Shadow kill — paired Δ가 유의하게 음수면 LLM 선별 경로 OFF(기계 판정, LLM끼리 합의 아님).
2. Challenger veto — 1차 픽에 대해 '사지 말 이유'만 묻는 2차 호출. **제거만**, 추가 선별 금지.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from signal_desk import db, llm

log = logging.getLogger("signal_desk.advisor")


@dataclass
class BuyAdvice:
    """봇이 소비하는 선별 결과.

    picks: list = 선별 · [] = 기권/차단 후 매수 없음 · None = 사용 불가(점수순 폴백).
    """
    picks: list[dict] | None
    reason: str | None = None
    primary: list[str] = field(default_factory=list)
    vetoed: list[str] = field(default_factory=list)
    killed: bool = False

    @property
    def outcome(self) -> str:
        if self.killed:
            return "killed"
        if self.picks is None:
            return "unavailable"
        return "picked" if self.picks else "abstained"


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
                lessons: list[dict], max_new: int,
                *, style: str | None = None, gate: dict | None = None,
                challenge: bool | None = None) -> list[dict] | None:
    """하위호환 래퍼 — `advise(...).picks`."""
    return advise(candidates, context, digests, lessons, max_new,
                  style=style, gate=gate, challenge=challenge).picks


def advise(candidates: list[dict], context: dict, digests: dict[str, dict],
           lessons: list[dict], max_new: int,
           *, style: str | None = None, gate: dict | None = None,
           challenge: bool | None = None) -> BuyAdvice:
    """선별 + kill switch + challenger. bot은 이 결과의 picks만 보면 된다."""
    from signal_desk.signals import advisor_shadow

    if not candidates or max_new <= 0:
        return BuyAdvice(None, reason="no_candidates")

    g = gate if gate is not None else advisor_shadow.gate(style=style)
    if not g.get("active", True):
        fb = g.get("fallback") or "abstain"
        log.info("advisor kill — %s · fallback=%s", g.get("reason"), fb)
        if fb == "score":
            return BuyAdvice(None, reason=g.get("reason") or "killed_shadow", killed=True)
        return BuyAdvice([], reason=g.get("reason") or "killed_shadow", killed=True)

    primary = _primary_select(candidates, context, digests, lessons, max_new)
    if primary.picks is None:
        return primary
    if not primary.picks:
        return primary

    do_challenge = advisor_shadow.challenger_enabled() if challenge is None else bool(challenge)
    if not do_challenge:
        return primary

    survivors, vetoed = _challenge_veto(primary.picks, candidates, context, digests)
    if vetoed:
        log.info("advisor challenger veto %s → 생존 %s", vetoed,
                 [p["ticker"] for p in survivors])
    return BuyAdvice(
        picks=survivors,
        reason="challenged" if vetoed else primary.reason,
        primary=primary.primary or [p["ticker"] for p in primary.picks],
        vetoed=vetoed,
        killed=False,
    )


def _primary_select(candidates: list[dict], context: dict, digests: dict[str, dict],
                    lessons: list[dict], max_new: int) -> BuyAdvice:
    if not llm.available():
        return BuyAdvice(None, reason="no_key")

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
        "정성 판단은 제공된 KB 요약의 사실에만 근거하고, KB에 없는 내용은 추측·언급하지 마라(KB 없으면 정성은 중립). "
        "손절·익절·비중 같은 실행 규칙은 코드가 처리하니 너는 '무엇을 왜'만 정한다. "
        "고르기 전에 각 후보의 '사지 않을 이유'를 스스로 검토하고, 그 반론을 넘어서는 후보만 남겨라. "
        "확신이 없으면 개수를 채우지 마라 — 후보 전부가 반론을 넘지 못하면 빈 배열을 반환하는 것이 정답이다. "
        "억지로 채운 한 종목이 안 사는 것보다 나쁘다.")
    gate_note = "이미 매수 기준(임계값)에 반영됨 — 재차 감점 말 것" if context.get("gate_applied") else "매수 기준 조정 없음"
    macro_note = context.get("macro_note")
    macro_line = f"\n[시황 코멘터리 · 참고용] {macro_note}\n" if macro_note else ""
    macro_detail = context.get("macro_detail")
    fred_line = f"\n[거시 지표 · 참고용] {macro_detail}\n" if macro_detail else ""
    user = (
        f"[시장 맥락 · 참고용, {gate_note}] 국면={context.get('regime')} · 거시={context.get('macro_bias')} · "
        f"경기사이클={context.get('cycle_phase')}\n" + fred_line + macro_line + "\n"
        f"[매수 후보(가드레일 통과)]\n" + "\n".join(cand_lines) + "\n\n"
        f"[과거 의사결정 성패(학습, 경향 참고용)]\n" + "\n".join(lesson_lines) + "\n\n"
        f"이 중 지금 매수할 종목을 최대 {max_new}개 골라라. 개수를 채울 의무는 없다 — "
        "반론을 넘는 후보가 없으면 picks를 빈 배열로 두어라(기권). "
        'JSON으로만: {"picks": [{"ticker": "코드", "rationale": "한국어 한 줄 근거"}]}')

    try:
        out = llm.complete_json(system, user, max_tokens=700)
    except Exception as e:
        log.info("LLM 자문 호출 실패(%s) — 결정론적 폴백", type(e).__name__)
        return BuyAdvice(None, reason="api_fail")
    if out is None:
        log.info("LLM 자문 응답 없음 — 결정론적 폴백")
        return BuyAdvice(None, reason="api_fail")
    if not isinstance(out.get("picks"), list):
        log.info("LLM 자문 파싱 실패 — 결정론적 폴백")
        return BuyAdvice(None, reason="parse_fail")
    raw = out["picks"]
    picks = []
    seen = set()
    for p in raw:
        t = p.get("ticker")
        if t in valid and t not in seen:
            picks.append({"ticker": t, "rationale": str(p.get("rationale", ""))[:200]})
            seen.add(t)
        if len(picks) >= max_new:
            break
    if raw and not picks:
        log.info("LLM 자문이 후보 밖 종목만 선택 — 결정론적 폴백")
        return BuyAdvice(None, reason="out_of_pool")
    if not picks:
        log.info("LLM 자문 기권 — 이번 회차 매수 없음(폴백 매수하지 않음)")
    return BuyAdvice(picks=picks, primary=[p["ticker"] for p in picks])


def _challenge_veto(picks: list[dict], candidates: list[dict],
                    context: dict, digests: dict[str, dict]) -> tuple[list[dict], list[str]]:
    """2차 호출 — veto만. 새 종목 추가 금지. 실패 시 1차 픽 유지(차단으로 매수를 늘리지 않음)."""
    if not picks or not llm.available():
        return picks, []
    by = {c["ticker"]: c for c in candidates}
    lines = []
    for p in picks:
        c = by.get(p["ticker"]) or {}
        dg = digests.get(p["ticker"]) or {}
        senti = f", 정성 {dg['sentiment']:+.2f}" if dg else ""
        lines.append(
            f'- {p["ticker"]} {c.get("name") or ""}: 점수 {c.get("score", 0):+.2f}, '
            f'1차근거 [{p.get("rationale", "")[:80]}]{senti}')
    system = (
        "너는 매수 선별의 **반론자(challenger)** 다. 이미 1차가 고른 종목만 심사한다. "
        "새 종목을 추천·추가하지 마라. '지금 사면 안 되는' 종목만 veto 배열에 넣어라. "
        "애매하면 veto하지 마라(과도한 거부는 기회비용). "
        "KB에 없는 악재를 지어내지 마라. "
        'JSON만: {"veto": [{"ticker": "코드", "why": "한국어 한 줄"}]}')
    user = (
        f"[시장] 국면={context.get('regime')} · 거시={context.get('macro_bias')} · "
        f"사이클={context.get('cycle_phase')}\n\n"
        f"[1차 선별 — 이 안에서만 veto]\n" + "\n".join(lines) + "\n\n"
        "사지 말아야 할 종목만 veto에 넣고, 나머지는 넣지 마라.")
    try:
        out = llm.complete_json(system, user, max_tokens=400)
    except Exception as e:
        log.info("challenger 호출 실패(%s) — 1차 픽 유지", type(e).__name__)
        return picks, []
    if not out or not isinstance(out.get("veto"), list):
        return picks, []
    pick_set = {p["ticker"] for p in picks}
    vetoed = []
    for v in out["veto"]:
        t = v.get("ticker")
        if t in pick_set and t not in vetoed:
            vetoed.append(t)
    if not vetoed:
        return picks, []
    survivors = [p for p in picks if p["ticker"] not in set(vetoed)]
    return survivors, vetoed
