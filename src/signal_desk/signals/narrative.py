"""시그널 해설 — 규칙 기반 자연어 문장 생성(LLM 호출 없음, 즉시·무료).

apt-signal의 "종합 해설"(지수들을 묶어 사회·통계적 의미를 1~2문장으로 해석)과 같은 접근.
`reasons`는 이미 "[기술]"/"[기본]" 태그가 붙어 있으므로 그대로 파싱해 문장으로 엮는다.

v2: 종목별 KB·개요를 LLM에 넣어 쉬운 해설을 만들고 캐시. 실패/미설정이면 v1 폴백.
BUY/SELL만 고품질 모델 호출 — HOLD는 규칙 문장만(비용·노이즈 절감).
"""

from __future__ import annotations

_KIND_WORD = {"STRONG_BUY": "강력매수", "BUY": "매수", "HOLD": "관망",
              "SELL": "매도", "STRONG_SELL": "강력매도"}


def _group_by_tag(reasons: list[str]) -> dict[str, list[str]]:
    """"[태그] 내용" 형식의 reason들을 태그별로 묶는다 — 새 팩터(저평가/낙폭과대 등)가 추가돼도
    이 함수는 손댈 필요 없이 자동으로 포함된다."""
    groups: dict[str, list[str]] = {}
    for r in reasons:
        if r.startswith("[") and "]" in r:
            tag, _, rest = r[1:].partition("]")
            groups.setdefault(tag, []).append(rest.strip())
    return groups


def explain(result) -> str:
    """result: engine.SignalResult (덕타이핑 — ticker/name/kind/score/confidence/reasons/has_fundamental).

    기술/기본은 항상 먼저 다루고(데이터 유무에 따른 문구가 있어서), 그 외 태그(저평가/고평가/
    낙폭과대/단기과열 등 — 종합 시그널에 팩터가 추가될 때마다 자동으로 반영됨)는 있는 만큼 덧붙인다.
    """
    groups = _group_by_tag(result.reasons)
    tech = groups.pop("기술", [])
    fund = groups.pop("기본", [])

    facts = []
    if tech:
        facts.append("가격 흐름: " + tech[0])
    else:
        facts.append("차트에는 뚜렷한 신호가 없습니다")
    if result.has_fundamental and fund:
        facts.append("재무: " + fund[0])
    for tag, items in groups.items():
        if items:
            facts.append(f"{tag}: {items[0]}")

    body = " · ".join(facts[:3])
    if not result.has_fundamental:
        body += " · 재무 자료는 아직 없습니다"
    kind_word = _KIND_WORD[result.kind]
    conf_word = "높은" if result.confidence >= 0.6 else "보통" if result.confidence >= 0.3 else "낮은"

    return (
        f"현재 판정은 {kind_word}입니다(점수 {result.score:+.2f}). "
        f"주요 근거: {body}. "
        f"점수로 계산한 신호 강도는 {conf_word} 편({result.confidence:.2f})이며 적중 확률은 아닙니다."
    )


def explain_llm(name: str, ticker: str, kind: str, score: float, reasons: list[str],
                kb_summary: str = "", *, about: str = "",
                model: str | None = None) -> str | None:
    """v2 해설 — 시그널 근거·회사 개요·KB만 근거로 LLM이 쉬운 해설을 생성한다.
    근거 밖 내용은 지어내지 않도록 강제하고, 투자 권유·수익 보장 표현을 금지한다(규제).
    LLM 미설정/실패 시 None(호출측이 규칙기반 v1으로 폴백). 캐시는 호출측(api)에서 담당."""
    from signal_desk import llm
    from signal_desk.copy_style import PLAIN_KOREAN
    if not llm.available():
        return None
    reason_lines = "\n".join(f"- {r}" for r in (reasons or [])) or "- (근거 없음)"
    about_block = f"\n[회사 한줄 개요]\n{about.strip()}\n" if about and about.strip() else ""
    kb_block = f"\n[최근 이슈 요약]\n{kb_summary.strip()}\n" if kb_summary and kb_summary.strip() else ""
    kind_word = _KIND_WORD.get(kind, kind)
    system = (
        "너는 처음 보는 종목을 설명하는 투자 정보 편집자다. " + PLAIN_KOREAN + "\n"
        "첫 문장에서 현재 판정과 가장 중요한 근거를 말한다. "
        "이어서 필요한 경우에만 회사가 하는 일과 다른 근거 한 가지를 덧붙인다. "
        "전문용어는 꼭 필요할 때만 짧게 풀어 쓴다. "
        "전체 2~3문장, 문장당 65자 안팎으로 쓴다. 수익이나 매매 행동을 단정하지 않는다."
    )
    user = (f"종목: {name}({ticker})\n시그널: {kind_word} ({kind}, 종합점수 {score:+.2f})\n"
            f"{about_block}[시그널 근거]\n{reason_lines}\n{kb_block}\n"
            "쉬운 한국어 해설:")
    use_model = model or llm.SIGNAL_EXPLAIN_MODEL
    out = llm.complete(system, user, max_tokens=320, model=use_model, purpose="narrative")
    return out.strip() if out else None
