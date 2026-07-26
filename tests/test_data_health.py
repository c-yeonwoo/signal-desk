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


def test_kb_refresh_stall_is_visible_per_target(tmp_path, monkeypatch):
    """다이제스트 신선도를 전체 max()로 재면 정지를 못 잡는다 — 거시·US가 매일 갱신되면
    국내 종목이 몇 주 멈춰도 '방금 갱신'으로 보인다(실제로 7일 놓쳤다). 대상별로 센다."""
    monkeypatch.chdir(tmp_path)
    import time

    from signal_desk import db, kb
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    old = int(time.time() - 20 * 86400)
    db.kb_digest_set("005930", "삼성전자", 0.1, "요약", [], 3, newest_ts=old)
    c = db.conn()
    c.execute("UPDATE kb_digest SET updated=? WHERE ticker='005930'", (old,))
    c.commit()
    c.close()
    db.kb_digest_set("_MARKET", "거시", 0.0, "오늘 시황", [], 5, newest_ts=int(time.time()))  # 방금 갱신

    st = kb.refresh_status([{"ticker": "005930", "name": "삼성전자"}])
    assert st["targets"] == 1 and st["fresh"] == 0        # 거시가 최신이어도 대상은 밀려 있다
    assert "삼성전자" in st["stale_names"]
    assert st["blocked_reason"]                          # 이유 없는 0은 화면에서 정상과 구분되지 않는다

    st2 = kb.refresh_status([])
    assert st2["blocked_reason"] and "대상" in st2["blocked_reason"]


def test_snapshot_signals_accumulates_pit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data/cache").mkdir(parents=True)
    from signal_desk.signals.engine import SignalResult

    def _sig(t, score, kind, flow=None, q=None):
        return SignalResult(ticker=t, name=t, score=score, kind=kind, confidence=0.5,
                            technical_score=0.1, fundamental_score=0.0, has_fundamental=False,
                            reasons=[], flow_intensity=flow, quality_points=q)
    n = store.snapshot_signals([_sig("005930", 1.8, "BUY", flow=0.2, q=4)], date="2026-07-08")
    assert n == 1
    store.snapshot_signals([_sig("005930", 2.0, "BUY"), _sig("000660", 0.5, "HOLD")], date="2026-07-09")
    df = store.load_signal_history()
    assert len(df) == 3 and set(df["date"]) == {"2026-07-08", "2026-07-09"}
    # 같은 날 재실행 → 그 날짜만 갱신(중복 없음)
    store.snapshot_signals([_sig("005930", 2.5, "BUY")], date="2026-07-09")
    df = store.load_signal_history()
    d9 = df[df["date"] == "2026-07-09"]
    assert len(d9) == 1 and float(d9.iloc[0]["score"]) == 2.5   # 덮어씀
    assert len(df[df["date"] == "2026-07-08"]) == 1              # 다른 날은 유지

    # signal_history_for: 종목별 {date: {kind, score}} — 차트 실측 우선용
    hist = store.signal_history_for("005930")
    assert set(hist) == {"2026-07-08", "2026-07-09"}
    assert hist["2026-07-09"] == {"kind": "BUY", "score": 2.5}
    assert store.signal_history_for("999999") == {}             # 이력 없는 종목
