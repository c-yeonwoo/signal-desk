"""/api/refresh scope 분할 — 요청당 타임아웃 회피용. 각 scope가 해당 러너만 돌리는지 검증."""

from signal_desk import api


def test_scope_runs_only_that_runner(monkeypatch):
    called = []
    for name in ("kr", "macro", "flows", "us"):
        monkeypatch.setitem(api._REFRESH_RUNNERS, name,
                            lambda data, n=name: (called.append(n) or {f"{n}_ran": True}))
    monkeypatch.setattr(api, "_clear_signal_caches", lambda: None)

    out = api.refresh({"scope": "flows"})
    assert called == ["flows"] and out["ok"] and out["scope"] == "flows" and out["flows_ran"]


def test_scope_all_runs_every_runner(monkeypatch):
    called = []
    for name in ("kr", "macro", "flows", "us"):
        monkeypatch.setitem(api._REFRESH_RUNNERS, name,
                            lambda data, n=name: (called.append(n) or {}))
    monkeypatch.setattr(api, "_clear_signal_caches", lambda: None)

    out = api.refresh({})  # scope 미지정 → all
    assert called == ["kr", "macro", "flows", "us"] and out["ok"]


def test_scope_all_reports_partial_failure(monkeypatch):
    monkeypatch.setitem(api._REFRESH_RUNNERS, "kr",
                        lambda data: (_ for _ in ()).throw(ValueError("boom")))
    monkeypatch.setitem(api._REFRESH_RUNNERS, "macro", lambda data: {"macro_size": 3})
    monkeypatch.setitem(api._REFRESH_RUNNERS, "flows", lambda data: {"flows_size": 0})
    monkeypatch.setitem(api._REFRESH_RUNNERS, "us", lambda data: {})
    monkeypatch.setattr(api, "_clear_signal_caches", lambda: None)

    out = api.refresh({})  # kr는 죽지만 나머지는 진행
    assert out["ok"] is False and "boom" in out["errors"]["kr"] and out["macro_size"] == 3


def test_single_scope_error_surfaced(monkeypatch):
    monkeypatch.setitem(api._REFRESH_RUNNERS, "kr",
                        lambda data: (_ for _ in ()).throw(RuntimeError("krx down")))
    monkeypatch.setattr(api, "_clear_signal_caches", lambda: None)
    out = api.refresh({"scope": "kr"})
    assert out["ok"] is False and "krx down" in out["error"] and out["scope"] == "kr"


def test_unknown_scope_rejected(monkeypatch):
    monkeypatch.setattr(api, "_clear_signal_caches", lambda: None)
    out = api.refresh({"scope": "bogus"})
    assert out["ok"] is False and "bogus" in out["reason"]


def _stub_kr(monkeypatch, profiles, calls):
    monkeypatch.setattr(api.store, "fetch_universe", lambda: [{"ticker": "005930", "name": "삼성전자"}])
    monkeypatch.setattr(api.store, "fetch_prices", lambda u: None)
    monkeypatch.setattr(api, "_dart_stale", lambda: False)          # DART 최신 → 재무 블록 스킵
    monkeypatch.setattr(api.store, "update_valuation", lambda: None)
    monkeypatch.setattr(api.store, "load_fundamentals", lambda: {"005930": {}})
    monkeypatch.setattr(api.store, "load_company_profiles", lambda: profiles)
    monkeypatch.setattr(api.store, "fetch_company_profiles", lambda u: calls.append("cp"))


def test_refresh_kr_backfills_company_profiles_when_empty(monkeypatch):
    calls = []
    _stub_kr(monkeypatch, {}, calls)                                # 기업개황 비어 있음
    api._refresh_kr({})
    assert calls == ["cp"]     # date-gate로 재무 블록 스킵돼도 기업개황은 백필


def test_refresh_kr_skips_company_backfill_when_present(monkeypatch):
    calls = []
    _stub_kr(monkeypatch, {"005930": {"ceo": "x"}}, calls)          # 이미 수집됨
    api._refresh_kr({})
    assert calls == []         # 있으면 재백필 안 함(정적 데이터)
