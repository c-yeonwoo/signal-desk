"""시그널 해설 — 규칙 기반 자연어 문장 생성(LLM 호출 없음, 즉시·무료).

apt-signal의 "종합 해설"(지수들을 묶어 사회·통계적 의미를 1~2문장으로 해석)과 같은 접근.
`reasons`는 이미 "[기술]"/"[기본]" 태그가 붙어 있으므로 그대로 파싱해 문장으로 엮는다.
"""

from __future__ import annotations

_KIND_WORD = {"BUY": "매수", "SELL": "매도", "HOLD": "관망"}


def _strip_tag(reason: str, tag: str) -> str:
    prefix = f"[{tag}] "
    return reason[len(prefix):] if reason.startswith(prefix) else reason


def explain(result) -> str:
    """result: engine.SignalResult (덕타이핑 — ticker/name/kind/score/confidence/reasons/has_fundamental)."""
    tech = [_strip_tag(r, "기술") for r in result.reasons if r.startswith("[기술]")]
    fund = [_strip_tag(r, "기본") for r in result.reasons if r.startswith("[기본]")]

    parts = []
    if tech:
        parts.append("기술적으로는 " + ", ".join(tech) + " 상황이고")
    else:
        parts.append("기술 지표상 뚜렷한 신호는 없고")

    if result.has_fundamental and fund:
        parts.append("기본적으로는 " + ", ".join(fund) + "인 점이 반영됐습니다.")
    elif result.has_fundamental:
        parts.append("기본적분석은 중립 수준입니다.")
    else:
        parts.append("재무데이터는 아직 없어 기술 지표만으로 판단했습니다.")

    body = " ".join(parts)
    kind_word = _KIND_WORD[result.kind]
    conf_word = "높은" if result.confidence >= 0.6 else "보통" if result.confidence >= 0.3 else "낮은"

    return (
        f"{body} 종합 점수 {result.score:+.2f}로 {kind_word} 시그널이며, "
        f"신뢰도는 {conf_word} 편입니다({result.confidence:.2f})."
    )
