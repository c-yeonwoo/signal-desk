"""토스 투자경고 수집 — STOCK 그룹 TPS 한도 대응.

배치 API가 없어서 종목별 단건이다. sleep 없이 200연타하면 중간부터 429가 나고,
실패를 []와 같게 취급하면 기존 캐시까지 통째로 지워진다."""

import json

from signal_desk import store
from signal_desk.ingest import toss


def test_warnings_returns_none_on_http_failure(monkeypatch):
    monkeypatch.setattr(toss, "_get", lambda *a, **k: None)
    assert toss.warnings("005930") is None
    monkeypatch.setattr(toss, "_get", lambda *a, **k: {"result": []})
    assert toss.warnings("005930") == []


def test_fetch_warnings_pauses_between_tickers(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data/cache").mkdir(parents=True)
    sleeps: list[float] = []
    monkeypatch.setattr(store.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(toss, "available", lambda: True)
    monkeypatch.setattr(toss, "warnings", lambda t: ["INVESTMENT_WARNING"] if t == "A" else [])
    n = store.fetch_warnings(["A", "B", "C"], pause=0.25)
    assert n == 1
    assert sleeps == [0.25, 0.25]                 # 첫 종목은 대기 없음


def test_fetch_warnings_keeps_cache_when_request_fails(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cache = tmp_path / "data/cache"
    cache.mkdir(parents=True)
    (cache / "warnings.json").write_text(
        json.dumps({"005930": ["VI"], "000660": ["INVESTMENT_WARNING"]}), encoding="utf-8")
    monkeypatch.setattr(toss, "available", lambda: True)
    monkeypatch.setattr(store.time, "sleep", lambda s: None)

    def fake(t):
        if t == "005930":
            return []                               # 경고 해제(성공)
        if t == "000660":
            return None                             # 429 등 실패
        return ["VI"]

    monkeypatch.setattr(toss, "warnings", fake)
    store.fetch_warnings(["005930", "000660", "035420"], pause=0)
    saved = json.loads((cache / "warnings.json").read_text(encoding="utf-8"))
    assert "005930" not in saved                    # 해제 반영
    assert saved["000660"] == ["INVESTMENT_WARNING"]  # 실패 → 기존 유지
    assert saved["035420"] == ["VI"]                # 신규


def test_warnings_status_separates_never_fetched_from_none_active(tmp_path, monkeypatch):
    """경고 0종목이 '정상'인지 '미수집'인지 구분돼야 한다 — 못 구분하면 veto가 죽은 걸 몇 주 못 본다."""
    monkeypatch.chdir(tmp_path)
    cache = tmp_path / "data/cache"
    cache.mkdir(parents=True)
    monkeypatch.setattr(toss, "available", lambda: True)
    st = store.warnings_status()
    assert st["fetched"] is False and st["blocked_reason"]      # 파일 없음 → 이유가 붙는다
    (cache / "warnings.json").write_text("{}", encoding="utf-8")
    st = store.warnings_status()
    assert st["fetched"] is True and st["active"] == 0 and st["blocked_reason"] is None


def test_daily_maintenance_refreshes_the_buy_veto(tmp_path, monkeypatch):
    """투자경고 수집이 수동 갱신에만 걸려 있으면 아무도 안 눌러 veto가 영구히 빈다."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data/cache").mkdir(parents=True)
    from signal_desk import api
    calls: list[list] = []
    monkeypatch.setattr(api, "kb", type("_", (), {"refresh": staticmethod(lambda t: None)}))
    monkeypatch.setattr(api, "_kb_targets", lambda: [])
    monkeypatch.setattr(api, "_signals", type("_", (), {"cache_clear": staticmethod(lambda: None),
                                                        "__call__": staticmethod(lambda: [])})())
    monkeypatch.setattr(api, "_regime", type("_", (), {"cache_clear": staticmethod(lambda: None)}))
    monkeypatch.setattr(api.store, "prices_need_deep_backfill", lambda: False)
    for name in ("fetch_prices", "fetch_flows", "fetch_market_flow", "fetch_short",
                 "fetch_consensus", "snapshot_signals"):
        monkeypatch.setattr(api.store, name, lambda *a, **k: [])
    monkeypatch.setattr(api.store, "load_universe", lambda: [{"ticker": "005930"}])
    monkeypatch.setattr(api.store, "fetch_warnings", lambda tks: calls.append(tks) or 0)
    monkeypatch.setattr(api.climate, "snapshot_shadow", lambda s: None)
    monkeypatch.setattr(api.db, "kv_set", lambda k, v: None)
    api._daily_maintenance([])
    assert calls == [["005930"]]                               # 자동 루프가 veto 데이터를 갱신한다


def test_retry_after_reads_header_and_clamps():
    class H(dict):
        def get(self, k, default=None):
            return super().get(k, default)

    err = type("E", (), {"headers": H({"Retry-After": "2.5"})})()
    assert toss._retry_after(err) == 2.5
    err.headers = H({"Retry-After": "999"})
    assert toss._retry_after(err) == 30.0
    err.headers = H()
    assert toss._retry_after(err, default=1.0) == 1.0
