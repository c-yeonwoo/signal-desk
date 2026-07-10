"""안내 에이전트(챗봇) — tool-use 루프·가드레일·그레이스풀 폴백. LLM은 목으로 대체."""

from signal_desk import chat, llm


def test_system_prompt_has_guardrails():
    s = chat.SYSTEM
    assert "추천" in s and "금지" in s            # 매수/매도 추천 금지 명시
    assert "지어내지" in s                          # 환각 금지
    assert "다시 계산하지 않는다" in s              # 재분석 금지
    assert chat.PERSONA_NAME in s


def test_no_llm_key_graceful(monkeypatch):
    monkeypatch.setattr(llm, "available", lambda: False)
    out = chat.answer("삼성전자 어때?", dispatch=lambda n, i: "{}")
    assert out["ok"] is False and "ANTHROPIC" in out["reply"]


def test_dispatch_required(monkeypatch):
    monkeypatch.setattr(llm, "available", lambda: True)
    out = chat.answer("안녕", dispatch=None)
    assert out["ok"] is False


def test_tool_loop_reads_then_answers(monkeypatch):
    monkeypatch.setattr(llm, "available", lambda: True)
    turns = {"n": 0}

    def fake(system, messages, tools, **kw):
        turns["n"] += 1
        if turns["n"] == 1:  # 1턴: 도구 호출
            return {"content": [{"type": "tool_use", "id": "t1", "name": "find_signal",
                                 "input": {"query": "삼성전자"}}], "stop_reason": "tool_use"}
        # 2턴: 도구 결과 받은 뒤 최종 답변
        assert any(m["role"] == "user" and isinstance(m["content"], list)
                   and m["content"][0].get("type") == "tool_result" for m in messages)
        return {"content": [{"type": "text", "text": "삼성전자는 현재 '매수' 시그널이에요(점수 +1.9)."}],
                "stop_reason": "end_turn"}
    monkeypatch.setattr(llm, "messages_with_tools", fake)

    seen = {}

    def dispatch(name, inp):
        seen["name"], seen["inp"] = name, inp
        return '{"종목":"삼성전자","시그널":"매수","종합점수":1.9}'

    out = chat.answer("삼성전자 어때?", dispatch=dispatch)
    assert out["ok"] and "삼성전자" in out["reply"]
    assert seen["name"] == "find_signal" and seen["inp"]["query"] == "삼성전자"
    assert "find_signal" in out["tools"] and turns["n"] == 2


def test_llm_failure_graceful(monkeypatch):
    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "messages_with_tools", lambda *a, **k: None)  # API 실패
    out = chat.answer("뭐 좋아?", dispatch=lambda n, i: "{}")
    assert out["ok"] is False and "어려" in out["reply"]


def test_tool_loop_caps_turns(monkeypatch):
    monkeypatch.setattr(llm, "available", lambda: True)
    # 항상 도구만 호출 → 상한(_MAX_TURNS)에서 안전 종료
    monkeypatch.setattr(llm, "messages_with_tools", lambda *a, **k:
                        {"content": [{"type": "tool_use", "id": "x", "name": "market_context", "input": {}}],
                         "stop_reason": "tool_use"})
    out = chat.answer("시장 어때?", dispatch=lambda n, i: "{}")
    assert out["ok"] and len(out["tools"]) == chat._MAX_TURNS


def test_chat_meta_endpoint(monkeypatch):
    from signal_desk import api
    out = api.chat_meta_get()
    assert "available" in out and out["name"] == chat.PERSONA_NAME


def test_answer_stream_tools_then_streams_text(monkeypatch):
    monkeypatch.setattr(llm, "available", lambda: True)
    turns = {"n": 0}

    def fake_stream(system, messages, tools, **kw):
        turns["n"] += 1
        if turns["n"] == 1:   # 1턴: 도구 호출(텍스트 없음)
            yield ("result", {"content": [{"type": "tool_use", "id": "t1", "name": "find_signal",
                                           "input": {"query": "삼성전자"}}], "stop_reason": "tool_use"})
        else:                 # 2턴: 최종 답변을 토큰 단위로 스트리밍
            for tok in ["삼성전자", "는 ", "'매수'", " 시그널이에요."]:
                yield ("text", tok)
            yield ("result", {"content": [{"type": "text", "text": "삼성전자는 '매수' 시그널이에요."}],
                              "stop_reason": "end_turn"})
    monkeypatch.setattr(llm, "stream_call", fake_stream)
    seen = {}
    deltas = []
    for kind, payload in chat.answer_stream("삼성전자 어때?", dispatch=lambda n, i: (seen.update(name=n) or '{"시그널":"매수"}')):
        if kind == "text":
            deltas.append(payload)
    assert "".join(deltas) == "삼성전자는 '매수' 시그널이에요."
    assert seen["name"] == "find_signal" and turns["n"] == 2


def test_answer_stream_no_key(monkeypatch):
    monkeypatch.setattr(llm, "available", lambda: False)
    out = [p for k, p in chat.answer_stream("hi", dispatch=lambda n, i: "{}") if k == "text"]
    assert out and "ANTHROPIC" in out[0]


def test_chat_stream_endpoint_sse(tmp_path, monkeypatch):
    import importlib
    from fastapi.testclient import TestClient
    monkeypatch.chdir(tmp_path)
    from signal_desk import db as dbm
    importlib.reload(dbm)
    from signal_desk import api as apim
    importlib.reload(apim)
    monkeypatch.setattr(apim.chat, "answer_stream",
                        lambda *a, **k: iter([("text", "안녕"), ("text", "하세요")]))
    client = TestClient(apim.app)
    client.post("/api/auth/signup", json={"email": "u@x.com", "pw": "abcdef"})
    r = client.post("/api/chat/stream", json={"message": "안녕", "history": []})
    assert r.status_code == 200 and "text/event-stream" in r.headers["content-type"]
    body = r.text
    assert '"delta": "안녕"' in body and '"delta": "하세요"' in body and "[DONE]" in body
