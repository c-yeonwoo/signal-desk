from signal_desk import db
from signal_desk.signals import advisor, advisor_cache


def _scope(uid=900001, evidence="v1", policy="p1"):
    return {"uid": uid, "market": "kr", "style": "conservative", "trade_date": "2026-09-27",
            "signal_policy_id": policy, "execution_policy_id": "exec", "evidence": evidence}


def _call(scope, invoke):
    return advisor_cache.call(stage="primary", scope=scope, system="system", user="same prompt",
                              model="test-model", max_tokens=100, invoke=invoke,
                              valid=lambda out: isinstance(out, dict) and isinstance(out.get("picks"), list))


def test_exact_input_cache_hits_but_new_evidence_policy_account_and_expiry_miss(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "advisor.db")
    n = 0

    def invoke():
        nonlocal n
        n += 1
        return {"picks": [{"ticker": "AAA", "rationale": "evidence"}]}

    assert _call(_scope(), invoke)["picks"][0]["ticker"] == "AAA"
    assert _call(_scope(), invoke)["picks"][0]["ticker"] == "AAA"
    assert n == 1
    _call(_scope(evidence="new adverse event"), invoke)
    _call(_scope(policy="p2"), invoke)
    _call(_scope(uid=900002), invoke)
    assert n == 4
    key = advisor_cache.input_hash(stage="primary", scope=_scope(), system="system",
                                   user="same prompt", model="test-model", max_tokens=100)
    c = db.conn()
    c.execute("UPDATE advisor_prompt_cache SET expires=0 WHERE input_hash=?", (key,))
    c.commit(); c.close()
    _call(_scope(), invoke)
    assert n == 5
    summary = db.advisor_prompt_summary()
    assert summary["by_stage"][0]["hits"] == 1
    assert summary["by_stage"][0]["provider_calls"] == 5


def test_invalid_and_failed_responses_are_not_cached(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "invalid.db")
    n = 0

    def invalid():
        nonlocal n
        n += 1
        return {"wrong": []}

    assert _call(_scope(), invalid) == {"wrong": []}
    assert _call(_scope(), invalid) == {"wrong": []}
    assert n == 2
    assert db.advisor_prompt_summary()["by_stage"][0]["failed_or_uncached"] == 2


def test_advisor_abstention_is_cached_but_new_full_digest_invalidates(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "abstain.db")
    monkeypatch.setattr(advisor.llm, "available", lambda: True)
    calls = []

    def complete(_system, _user, **kwargs):
        calls.append(kwargs["max_tokens"])
        return {"picks": []}

    monkeypatch.setattr(advisor.llm, "complete_json", complete)
    cands = [{"ticker": "AAA", "name": "A", "score": 2.0, "confidence": 0.7, "reasons": []}]
    digests = {"AAA": {"sentiment": 0.1, "summary": "same first 50 chars " * 3 + "old"}}

    def advise():
        return advisor.advise(cands, {}, digests, [], 1, challenge=False,
                              gate={"active": True}, style="conservative", cache_scope=_scope())

    assert advise().picks == []
    assert advise().picks == []
    assert calls == [700]
    digests["AAA"]["summary"] = "same first 50 chars " * 3 + "new adverse event"
    assert advise().picks == []
    assert calls == [700, 700]


def test_cache_database_failure_keeps_original_provider_path(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "broken.db")
    monkeypatch.setattr(db, "advisor_prompt_get", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db")))
    called = []
    result = _call(_scope(), lambda: called.append(1) or {"picks": []})
    assert result == {"picks": []} and called == [1]


def test_challenger_cache_never_bypasses_current_kill_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "challenger.db")
    monkeypatch.setattr(advisor.llm, "available", lambda: True)
    calls = []

    def complete(_system, _user, **kwargs):
        calls.append(kwargs["max_tokens"])
        return ({"picks": [{"ticker": "AAA", "rationale": "선별"}]} if kwargs["max_tokens"] == 700
                else {"veto": [{"ticker": "AAA", "why": "반론"}]})

    monkeypatch.setattr(advisor.llm, "complete_json", complete)
    cands = [{"ticker": "AAA", "name": "A", "score": 2.0, "confidence": 0.7, "reasons": []}]
    args = (cands, {}, {}, [], 1)
    enabled = {"active": True}
    assert advisor.advise(*args, challenge=True, gate=enabled, cache_scope=_scope()).picks == []
    assert advisor.advise(*args, challenge=True, gate=enabled, cache_scope=_scope()).picks == []
    assert calls == [700, 400]
    stopped = advisor.advise(*args, challenge=True, gate={"active": False, "fallback": "abstain"},
                             cache_scope=_scope())
    assert stopped.killed and stopped.picks == []
    assert calls == [700, 400]


def test_malformed_ticker_neither_crashes_nor_enters_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "malformed.db")
    monkeypatch.setattr(advisor.llm, "available", lambda: True)
    calls = []
    monkeypatch.setattr(advisor.llm, "complete_json",
                        lambda *_a, **_k: calls.append(1) or {"picks": [{"ticker": ["AAA"]}]})
    cands = [{"ticker": "AAA", "name": "A", "score": 2.0, "confidence": 0.7, "reasons": []}]
    for _ in range(2):
        result = advisor.advise(cands, {}, {}, [], 1, challenge=False,
                                gate={"active": True}, cache_scope=_scope())
        assert result.picks is None
    assert len(calls) == 2


def test_cache_persists_only_consumed_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "minimal.db")
    _call(_scope(), lambda: {"picks": [{"ticker": "AAA", "rationale": "reason"}],
                             "untrusted_extra": "must not persist"})
    c = db.conn()
    payload = c.execute("SELECT payload FROM advisor_prompt_cache").fetchone()[0]
    c.close()
    assert "untrusted_extra" not in payload
