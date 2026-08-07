"""사업 개요(무엇을 하는 회사) — 요청 경로는 무비용(캐시/섹터 폴백), 생성은 백필에서만."""

from signal_desk import company, db, llm


def test_about_request_path_no_llm_call(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    called = {"n": 0}
    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "complete_json", lambda *a, **k: (called.__setitem__("n", called["n"] + 1), {"about": "x"})[1])
    # generate=False(요청 경로) → LLM 호출 없이 None(캐시 없음 → 프론트가 섹터 소개로 폴백, 허구 문장 금지)
    out = company.about("005930", "삼성전자", "반도체", "kr")
    assert out is None
    assert called["n"] == 0
    out_us = company.about("AAPL", "Apple", None, "us")
    assert out_us is None
    assert called["n"] == 0


def test_about_generate_and_cache(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    calls = {"n": 0}

    def fake_complete(system, user, **k):
        calls["n"] += 1
        return {"about": "메모리 반도체를 만드는 회사입니다."}

    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "complete_json", fake_complete)
    out = company.about("005930", "삼성전자", "반도체", "kr", generate=True)
    assert out == "메모리 반도체를 만드는 회사입니다."
    assert calls["n"] == 1
    # 캐시됨 → 두 번째는 LLM 호출 없이 동일 반환
    out2 = company.about("005930", "삼성전자", "반도체", "kr")
    assert out2 == out and calls["n"] == 1


def test_about_llm_off_returns_none(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(llm, "available", lambda: False)
    out = company.about("000660", "SK하이닉스", "반도체", "kr", generate=True)
    assert out is None  # LLM 없으면 허구 없이 None(요청 경로가 아니어도 폴백 문장 만들지 않음)


def test_us_description_summarized(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    seen = {}

    def fake_complete(system, user, **k):
        seen["user"] = user
        return {"about": "아이폰·맥을 만드는 애플."}

    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "complete_json", fake_complete)
    out = company.about("AAPL", "Apple", "기술", "us", generate=True,
                        us_description="Apple Inc. designs and sells smartphones and computers.")
    assert out == "아이폰·맥을 만드는 애플."
    assert "Apple Inc. designs" in seen["user"]  # 영문 설명이 프롬프트에 포함


def test_backfill_only_uncached_and_capped(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "complete_json", lambda *a, **k: {"about": "설명"})
    db.kv_set("about:B", "이미 있음")  # 캐시된 종목은 건너뜀
    targets = [{"ticker": "A", "name": "a", "sector": "s", "market": "kr"},
               {"ticker": "B", "name": "b", "sector": "s", "market": "kr"},
               {"ticker": "C", "name": "c", "sector": "s", "market": "kr"}]
    r = company.backfill(targets, max_llm=1)  # 상한 1 → A만
    assert r["generated"] == 1 and r["attempted"] == 1
    assert db.kv_get("about:A") == "설명"
    assert db.kv_get("about:C") is None  # 상한으로 미처리


def test_backfill_cap_counts_calls_not_successes(tmp_path, monkeypatch):
    """**상한은 호출 수에 걸린다.** 예전엔 성공만 세서 실패 종목이 많으면 상한이 없었다.

    실측(2026-08-08): `company` 라벨로 하루 525콜 · $0.61(월 $18)이 나가는데 유니버스는
    703종목 고정이고 캐시가 있어 진작 수렴했어야 했다. 실패가 카운트되지 않아 같은 종목을
    매 루프 다시 불렀고, `got=0` 이면 로그도 안 남았다(조용한 0).
    """
    monkeypatch.chdir(tmp_path)
    calls = {"n": 0}

    def always_fail(*a, **k):
        calls["n"] += 1
        return None                                    # 생성 실패

    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "complete_json", always_fail)
    targets = [{"ticker": f"T{i}", "name": f"n{i}", "sector": "s", "market": "kr"}
               for i in range(20)]
    r = company.backfill(targets, max_llm=3)
    assert r["generated"] == 0
    assert r["attempted"] == 3, "상한을 넘겨 호출했다"
    assert calls["n"] == 3, f"LLM을 {calls['n']}회 불렀다 — 상한 3인데"
    assert r["failed"] == ["T0", "T1", "T2"], "실패 종목을 이름으로 내지 않는다"


def test_repeatedly_failing_ticker_is_deferred(tmp_path, monkeypatch):
    """성공하지 못하는 종목을 30분마다 영원히 재시도하면 안 된다 — 미국 시세에서 겪은 병이다."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "complete_json", lambda *a, **k: None)
    targets = [{"ticker": "X", "name": "x", "sector": "s", "market": "kr"}]
    for _ in range(company._FAIL_DEFER_AFTER):
        assert company.backfill(targets, max_llm=5)["attempted"] == 1
    r = company.backfill(targets, max_llm=5)
    assert r["attempted"] == 0 and r["deferred"] == 1, "연속 실패 뒤에도 계속 부른다"

    # 성공하면 유예가 풀린다 — 프롬프트·데이터가 고쳐졌는데 영구 제외되면 안 된다.
    company._clear_fail("X")
    monkeypatch.setattr(llm, "complete_json", lambda *a, **k: {"about": "설명"})
    assert company.backfill(targets, max_llm=5)["generated"] == 1


# ---------- 최근 행보 ----------
def test_recent_moves_request_path_no_llm(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    calls = {"n": 0}
    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "complete_json", lambda *a, **k: (calls.__setitem__("n", calls["n"] + 1), {"moves": ["x"]})[1])
    monkeypatch.setattr(db, "kb_digest_get", lambda t: {"n_sources": 3, "newest_ts": 111})
    # 캐시 없음 + generate=False → LLM 호출 없이 None
    assert company.recent_moves("005930", "삼성전자") is None
    assert calls["n"] == 0


def test_recent_moves_generate_cache_and_freshness(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    calls = {"n": 0}
    monkeypatch.setattr(llm, "available", lambda: True)

    def fake(system, user, **k):
        calls["n"] += 1
        return {"moves": ["신제품 출시", "대형 공급계약"]}

    monkeypatch.setattr(llm, "complete_json", fake)
    monkeypatch.setattr(db, "kb_entries_recent", lambda t, n=12, confirmed_only=False: [{"title": "삼성 신제품", "source": "news"}])
    monkeypatch.setattr(db, "kb_digest_get", lambda t: {"n_sources": 3, "newest_ts": 111})
    out = company.recent_moves("005930", "삼성전자", generate=True)
    assert out == ["신제품 출시", "대형 공급계약"] and calls["n"] == 1
    # 서명 동일 → 캐시 히트(재생성 없음)
    assert company.recent_moves("005930", "삼성전자", generate=True) == out and calls["n"] == 1
    # 새 뉴스로 서명 변경 → 재생성
    monkeypatch.setattr(db, "kb_digest_get", lambda t: {"n_sources": 5, "newest_ts": 222})
    company.recent_moves("005930", "삼성전자", generate=True)
    assert calls["n"] == 2


def test_recent_moves_no_kb_docs_returns_none(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "complete_json", lambda *a, **k: {"moves": ["지어냄"]})
    monkeypatch.setattr(db, "kb_digest_get", lambda t: None)  # KB 문서 없음 → 허구 방지
    assert company.recent_moves("AAPL", "Apple", generate=True) is None
