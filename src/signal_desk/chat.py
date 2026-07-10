"""안내 에이전트(챗봇) — 이미 계산된 시그널·KB·포트폴리오를 '대화로 풀어주는' 역할.

핵심 설계(환각·자문 방지): 에이전트는 **재분석하지 않는다.** 모든 사실은 tool 호출로 엔진/스토어의
실제 값을 READ해서만 말하고, 값이 없으면 "없다"고 답한다. 매수/매도 지시는 금지 — 시그널이 무엇으로
났는지와 그 근거만 설명한다. 도구 실행(dispatch)은 실데이터에 접근할 수 있는 api 계층이 주입한다
(chat.py는 api를 import하지 않아 순환참조 없음).
"""

from __future__ import annotations

import json
from typing import Callable

from signal_desk import llm

# 브랜드 캐릭터(플레이스홀더 — 이름·외형은 추후 확정). 안경 쓴 똑똑한 동물 컨셉.
PERSONA_NAME = "데스키"

SYSTEM = f"""너는 '{PERSONA_NAME}', 주식 시그널 서비스 'Signal Desk'의 안내 도우미다. 안경 쓴 똑똑하고
친근한 캐릭터로, 이미 계산된 데이터를 초보도 알기 쉽게 풀어 설명한다. 한국어로, 간결하고 따뜻하게.

반드시 지키는 규칙:
1. 매수/매도를 추천하거나 지시하지 않는다. "사세요/파세요/지금이 기회" 같은 말은 절대 금지.
   대신 "시그널이 무엇으로 나왔는지"와 "그 근거(어떤 팩터·지표 때문인지)"만 설명한다.
   최종 판단과 책임은 사용자 본인에게 있음을 자연스럽게 상기시킨다.
2. 도구(tool)로 받은 데이터에 있는 사실만 말한다. 숫자·근거·종목을 절대 지어내지 않는다.
   데이터가 없거나 도구가 빈 값을 주면 "그 정보는 아직 없어요"라고 솔직히 답한다.
3. 점수·판정·팩터 값을 스스로 다시 계산하지 않는다. 도구가 준 값을 그대로 전한다.
4. 종목을 물으면 먼저 도구로 조회한다. 조회 없이 기억으로 답하지 않는다.
5. 시그널 용어는 반드시 도구가 준 그대로 쓴다: 5단계는 **강력매수·매수·관망·매도·강력매도**뿐이다.
   '주목'·'강한 주목'·'주의' 같은 없는 용어를 만들어 쓰지 말 것. '매수 시그널'은 "매수 시그널이
   떴다는 분석 결과"일 뿐 "사라"는 지시가 아님을 지킨다.

답변은 보통 2~5문장. 강조는 **굵게**, 목록은 '- '로. 과장·확신에 찬 예측 금지."""


# 도구 스키마(Anthropic tool use). 전부 READ 전용 — 실행/주문 도구는 없음(MVP는 답변만).
TOOLS = [
    {
        "name": "find_signal",
        "description": "특정 종목의 현재 시그널을 조회한다. 종목명(예: '삼성전자') 또는 6자리 코드로 찾는다. "
                       "반환: 시그널 종류·종합점수·신뢰도·팩터 강약·근거·PER/PBR/ROE·섹터·목표가 상승여력·뉴스심리.",
        "input_schema": {"type": "object", "properties": {
            "query": {"type": "string", "description": "종목명 또는 종목코드"}},
            "required": ["query"]},
    },
    {
        "name": "list_signals",
        "description": "현재 시그널 목록을 조건으로 조회한다. kind로 필터(strong_buy=강한주목, buy=주목, 생략=전체 상위). "
                       "반환: 종목명·코드·시그널·점수·섹터 목록(점수순).",
        "input_schema": {"type": "object", "properties": {
            "kind": {"type": "string", "enum": ["strong_buy", "buy", "all"]},
            "limit": {"type": "integer", "description": "최대 개수(기본 10)"}}},
    },
    {
        "name": "get_portfolio",
        "description": "자동매매 봇의 모의 포트폴리오(보유 종목·평가손익·현금)를 조회한다. 사용자 '내 포트폴리오' 질문에 사용.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_events",
        "description": "특정 종목의 최근 공시·배당·예정 일정을 조회한다(국내). 종목명 또는 코드.",
        "input_schema": {"type": "object", "properties": {
            "query": {"type": "string"}}, "required": ["query"]},
    },
    {
        "name": "market_context",
        "description": "현재 시장 국면(경기 사이클)과 거시 요약, 매수 기준 상향 여부를 조회한다.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "explain_term",
        "description": "투자·시그널 용어의 쉬운 설명을 조회한다(예: 'PER', 'RSI', '낙폭과대', '수급').",
        "input_schema": {"type": "object", "properties": {
            "term": {"type": "string"}}, "required": ["term"]},
    },
    {
        "name": "search_kb",
        "description": "뉴스·전문가 기고·영상 등 정성 지식베이스(KB)를 키워드로 검색한다. 반환: 관련 종목·요약·핵심 포인트.",
        "input_schema": {"type": "object", "properties": {
            "query": {"type": "string"}}, "required": ["query"]},
    },
]

_MAX_TURNS = 6  # tool 왕복 상한(무한루프 방지)


def answer(message: str, history: list | None = None,
           dispatch: Callable[[str, dict], str] | None = None,
           model: str = llm.NARRATIVE_MODEL) -> dict:
    """사용자 메시지에 답한다. dispatch(tool_name, input)->json문자열 로 실데이터 도구를 실행.
    반환: {"ok": bool, "reply": str, "tools": [사용한 도구명]}. LLM 키 없으면 ok=False 안내."""
    if not llm.available():
        return {"ok": False, "reply": f"{PERSONA_NAME}가 아직 준비 중이에요(관리자: ANTHROPIC_API_KEY 설정 필요).",
                "tools": []}
    if dispatch is None:
        return {"ok": False, "reply": "도구가 연결되지 않았어요.", "tools": []}

    messages = list(history or [])
    messages.append({"role": "user", "content": message})
    used: list[str] = []

    for _ in range(_MAX_TURNS):
        resp = llm.messages_with_tools(SYSTEM, messages, TOOLS, max_tokens=1200, model=model)
        if not resp:
            return {"ok": False, "reply": "지금은 답하기 어려워요. 잠시 후 다시 시도해 주세요.", "tools": used}
        content = resp.get("content") or []
        messages.append({"role": "assistant", "content": content})
        tool_uses = [c for c in content if c.get("type") == "tool_use"]
        if not tool_uses:  # 최종 답변
            text = "".join(c.get("text", "") for c in content if c.get("type") == "text").strip()
            return {"ok": True, "reply": text or "음, 답을 정리하지 못했어요. 다시 물어봐 주세요.", "tools": used}
        results = []
        for tu in tool_uses:
            used.append(tu.get("name", "?"))
            try:
                out = dispatch(tu.get("name", ""), tu.get("input") or {})
            except Exception:
                out = json.dumps({"error": "조회 실패"}, ensure_ascii=False)
            results.append({"type": "tool_result", "tool_use_id": tu.get("id"), "content": out or "{}"})
        messages.append({"role": "user", "content": results})

    return {"ok": True, "reply": "답변이 길어져 여기서 멈췄어요. 조금 더 구체적으로 물어봐 주세요.", "tools": used}
