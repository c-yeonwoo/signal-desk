"""시세 데이터 신뢰도 진단 — 캐시 종가 vs 토스 실시간가 비율로 스케일/합성 판정."""

import pandas as pd

from signal_desk import store


def _seed_prices(tmp_path, closes: dict):
    (tmp_path / "data/cache").mkdir(parents=True)
    rows = [{"date": "2026-07-06", "ticker": t, "open": c, "close": c, "volume": 1}
            for t, c in closes.items()]
    pd.DataFrame(rows).to_parquet(store.PRICES_FILE, index=False)


def test_detects_scaled_data(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _seed_prices(tmp_path, {"005930": 285000.0, "000660": 1800000.0})  # 스케일된(3.6x·10x) 종가
    from signal_desk.ingest import toss
    monkeypatch.setattr(toss, "available", lambda: True)
    monkeypatch.setattr(toss, "prices", lambda syms: {"005930": 79000.0, "000660": 180000.0})  # 실제가
    out = store.price_sanity(["005930", "000660"])
    assert out["ok"] and out["scaled_suspect"] is True         # 비율 3.6·10 → 스케일 의심
    assert any(r["ratio"] and r["ratio"] > 3 for r in out["rows"])


def test_real_data_passes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _seed_prices(tmp_path, {"005930": 79050.0, "000660": 179500.0})  # 실데이터(장중 소폭 차이)
    from signal_desk.ingest import toss
    monkeypatch.setattr(toss, "available", lambda: True)
    monkeypatch.setattr(toss, "prices", lambda syms: {"005930": 79000.0, "000660": 180000.0})
    out = store.price_sanity(["005930", "000660"])
    assert out["ok"] and out["scaled_suspect"] is False        # 비율≈1 → 실데이터


def test_graceful_without_toss(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _seed_prices(tmp_path, {"005930": 79000.0})
    from signal_desk.ingest import toss
    monkeypatch.setattr(toss, "available", lambda: False)
    out = store.price_sanity(["005930"])
    assert out["ok"] is False and out["toss"] is False and out["rows"][0]["cached"] == 79000.0


def test_data_freshness_reports_sources(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data/cache").mkdir(parents=True)
    store._write_json(store.FLOWS_FILE, {"005930": {"intensity": 0.1}})
    fr = store.data_freshness()
    by = {f["key"]: f for f in fr}
    assert by["flows"]["updated"] and by["flows"]["rows"] == 1 and by["flows"]["stale"] is False
    assert by["prices"]["updated"] is None and by["prices"]["stale"] is True  # 없으면 미수집·stale
    assert {"prices", "fundamentals", "flows", "macro", "company"} <= set(by)


def test_data_health_includes_freshness(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from signal_desk import api, db
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    out = api.data_health_get()
    assert isinstance(out.get("freshness"), list) and out["freshness"]
