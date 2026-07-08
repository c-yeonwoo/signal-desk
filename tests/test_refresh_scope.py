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
    monkeypatch.setattr(api, "_refresh_kr", lambda d: (called.append("kr") or {"universe_size": 1}))
    monkeypatch.setattr(api, "_refresh_macro", lambda d: (called.append("macro") or {}))
    monkeypatch.setattr(api, "_refresh_flows", lambda d: (called.append("flows") or {}))
    monkeypatch.setattr(api, "_refresh_us", lambda d: (called.append("us") or {}))
    monkeypatch.setattr(api, "_clear_signal_caches", lambda: None)

    out = api.refresh({})  # scope 미지정 → all
    assert called == ["kr", "macro", "flows", "us"] and out["ok"]


def test_unknown_scope_rejected(monkeypatch):
    monkeypatch.setattr(api, "_clear_signal_caches", lambda: None)
    out = api.refresh({"scope": "bogus"})
    assert out["ok"] is False and "bogus" in out["reason"]
