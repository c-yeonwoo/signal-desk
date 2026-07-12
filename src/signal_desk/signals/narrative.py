"""시그널 해설 — 규칙 기반 자연어 문장 생성(LLM 호출 없음, 즉시·무료).

apt-signal의 "종합 해설"(지수들을 묶어 사회·통계적 의미를 1~2문장으로 해석)과 같은 접근.
`reasons`는 이미 "[기술]"/"[기본]" 태그가 붙어 있으므로 그대로 파싱해 문장으로 엮는다.

이건 v1(폴백용)이다 — BACKLOG.md #17 계획: 종목별로 뉴스·증권사 리포트·개인분석 등을 크롤링해
KB(brightdesk의 kb_facts/source_registry 패턴)를 만들고, 그 KB를 LLM에 넣어 시그널 근거를
답변·해설로 생성한 뒤 캐시해서 쓰는 v2로 교체 예정. v2가 실패/미설정이면 이 v1로 폴백.
"""

from __future__ import annotations

_KIND_WORD = {"STRONG_BUY": "강력 매수", "BUY": "매수", "HOLD": "관망",
              "SELL": "매도", "STRONG_SELL": "강력 매도"}


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

    clauses = []
    if tech:
        clauses.append("기술적으로는 " + ", ".join(tech) + " 상황")
    else:
        clauses.append("기술 지표상 뚜렷한 신호는 없는 상황")

    if result.has_fundamental and fund:
        clauses.append("기본적으로는 " + ", ".join(fund) + "인 점")
    elif result.has_fundamental:
        clauses.append("기본적분석은 중립 수준")
    else:
        clauses.append("재무데이터는 아직 없어 기술 지표 위주로 판단")

    for tag, items in groups.items():
        clauses.append(f"{tag} 측면에서는 " + ", ".join(items))

    body = ", ".join(clauses) + "이 반영됐습니다."
    kind_word = _KIND_WORD[result.kind]
    conf_word = "높은" if result.confidence >= 0.6 else "보통" if result.confidence >= 0.3 else "낮은"

    return (
        f"{body} 종합 점수 {result.score:+.2f}로 {kind_word} 시그널이며, "
        f"신뢰도는 {conf_word} 편입니다({result.confidence:.2f})."
    )


def explain_llm(name: str, ticker: str, kind: str, score: float, reasons: list[str],
                kb_summary: str = "") -> str | None:
    """v2 해설 — 시그널 근거(reasons)와 KB 요약만 근거로 LLM이 2~3문장 해설을 생성한다.
    근거 밖 내용은 지어내지 않도록 강제하고, 투자 권유·수익 보장 표현을 금지한다(규제).
    LLM 미설정/실패 시 None(호출측이 규칙기반 v1으로 폴백). 캐시는 호출측(api)에서 담당."""
    from signal_desk import llm
    if not llm.available():
        return None
    reason_lines = "\n".join(f"- {r}" for r in (reasons or [])) or "- (근거 없음)"
    kb_block = f"\n[KB 정성 요약]\n{kb_summary}\n" if kb_summary else ""
    system = (
        "너는 주식 초보에게 시그널을 쉽게 풀어 설명하는 가이드다. 전문 애널리스트 말투(지표 나열·"
        "영어 약어 남발)를 쓰지 말고, 처음 보는 사람도 바로 이해되게 일상어로 설명한다.\n"
        "형식(반드시 지켜라):\n"
        "1) 첫 문장은 '쉽게 말하면, …'으로 시작해 지금 상태를 한 문장으로 요약(왜 이 판정인지).\n"
        "2) 그다음 가장 중요한 근거 2~3가지만 쉬운 말로 짚는다. 근거를 전부 나열하지 마라. 전문용어"
        "(RSI·MACD·PER·PBR 등)는 꼭 필요할 때만 쓰되 반드시 괄호로 짧게 풀이한다. 예: 'RSI(단기 과열·과매도 지표)'.\n"
        "3) 숫자 나열이 아니라 '그래서 어떤 의미인지'를 말한다. 전체 3~4문장, 간결하게(장황하게 늘이지 마라).\n"
        "제약: 아래 '시그널 근거'와 'KB 요약'의 사실만 쓰고 없는 수치·전망은 지어내지 마라. "
        "투자 권유·매수 종용·수익 보장 표현 금지 — 관찰된 근거를 중립적으로 설명만 한다.")
    user = (f"종목: {name}({ticker})\n시그널: {kind} (종합점수 {score:+.2f})\n"
            f"[시그널 근거]\n{reason_lines}\n{kb_block}\n쉬운 한국어 해설(핵심만, 3~4문장):")
    out = llm.complete(system, user, max_tokens=600, model=llm.NARRATIVE_MODEL)
    return out.strip() if out else None
