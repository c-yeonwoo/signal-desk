"""애널 컨센서스 수집 레이어 — 파싱 + 일별 PIT 스냅샷(append/dedup). 시그널·목표가엔 아직 미반영."""

import importlib

from signal_desk.ingest import naver


def test_fnum():
    assert naver._fnum("513,958") == 513958.0
    assert naver._fnum("-1,508") == -1508.0
    assert naver._fnum("") is None
    assert naver._fnum("-") is None
    assert naver._fnum(None) is None


def test_consensus_parse(monkeypatch):
    integ = {"consensusInfo": {"priceTargetMean": "513,958", "recommMean": "4.04",
                               "createDate": "2026-07-09"}}
    annual = {"financeInfo": {
        "trTitleList": [{"key": "202412", "isConsensus": "N"},
                        {"key": "202612", "isConsensus": "Y"},
                        {"key": "202712", "isConsensus": "Y"}],
        "rowList": [{"title": "EPS", "columns": {"202612": {"value": "46,664"},
                                                 "202712": {"value": "52,000"}}}]}}
    monkeypatch.setattr(naver, "_get_json",
                        lambda code, path: integ if path == "integration" else annual)
    c = naver.consensus("005930")
    assert c["price_target_mean"] == 513958.0 and c["recomm_mean"] == 4.04
    assert c["source_date"] == "2026-07-09"
    assert c["forwards"] == [{"year": "202612", "eps": 46664.0}, {"year": "202712", "eps": 52000.0}]


def test_consensus_none_when_no_coverage(monkeypatch):
    monkeypatch.setattr(naver, "_get_json", lambda code, path: {})  # 빈 응답 → 커버리지 없음
    assert naver.consensus("999999") is None


def test_consensus_row_flat():
    from signal_desk import store
    c = {"price_target_mean": 100.0, "recomm_mean": 4.0, "source_date": "2026-07-09",
         "forwards": [{"year": "202712", "eps": 20.0}, {"year": "202612", "eps": 10.0}]}
    row = store._consensus_row("005930", "2026-07-11", c)
    # 가까운 연도(202612)가 fwd1
    assert row["fwd1_year"] == "202612" and row["fwd1_eps"] == 10.0
    assert row["fwd2_year"] == "202712" and row["fwd2_eps"] == 20.0


def test_fetch_consensus_append_and_dedup(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from signal_desk import store
    importlib.reload(store)
    (tmp_path / "data" / "cache").mkdir(parents=True)
    monkeypatch.setattr(store, "load_universe", lambda: [{"ticker": "005930"}, {"ticker": "000660"}])
    from signal_desk.ingest import naver as nv
    monkeypatch.setattr(nv, "consensus", lambda code: {
        "price_target_mean": 100.0, "recomm_mean": 4.0, "source_date": "2026-07-09",
        "forwards": [{"year": "202612", "eps": 10.0}]})
    assert store.fetch_consensus(date="2026-07-11") == 2
    assert store.fetch_consensus(date="2026-07-11") == 2   # 같은 날 재실행 → 덮어쓰기
    assert store.fetch_consensus(date="2026-07-12") == 2   # 다음 날 → append
    hist = store.load_consensus_history()
    assert len(hist) == 4 and sorted(hist["date"].unique()) == ["2026-07-11", "2026-07-12"]
    latest = store.load_consensus_latest()
    assert set(latest) == {"005930", "000660"}
    assert latest["005930"]["price_target_mean"] == 100.0


def test_consensus_readiness_gives_a_date_not_just_a_count(tmp_path, monkeypatch):
    """축적만 하는 데이터엔 '언제 판정 가능한가'가 붙어야 한다 — 조건 없는 축적은 영원히 안 본다."""
    monkeypatch.chdir(tmp_path)
    import pandas as pd

    from signal_desk import store
    importlib.reload(store)
    (tmp_path / "data" / "cache").mkdir(parents=True)

    r = store.consensus_readiness()
    assert r["ready"] is False and r["blocked_reason"] and r["eta_trading_days"] > 0

    # 스냅샷 3일 + 이후 종가 충분 → Δ 계산 가능한 날짜는 2개(첫 날은 Δ 불가)
    dates = [f"2026-01-{d:02d}" for d in range(1, 29)]
    pd.DataFrame([{"date": d, "ticker": "005930", "open": 1.0, "close": 1.0, "volume": 1}
                  for d in dates]).to_parquet(store.PRICES_FILE, index=False)
    pd.DataFrame([{"date": d, "ticker": "005930", "price_target_mean": 1.0}
                  for d in dates[:3]]).to_parquet(store.CONSENSUS_HISTORY_FILE, index=False)
    r = store.consensus_readiness(horizon=5, need=2)
    assert r["days"] == 3 and r["testable_dates"] == 2 and r["ready"] is True
    # 표본이 모자라면 판정 날짜(추정)를 낸다
    r = store.consensus_readiness(horizon=5, need=40)
    assert r["ready"] is False and r["eta_date"] and r["eta_trading_days"] == 37 + 5


def test_fetch_consensus_circuit_breaker(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from signal_desk import store
    importlib.reload(store)
    (tmp_path / "data" / "cache").mkdir(parents=True)
    from signal_desk.ingest import naver as nv
    monkeypatch.setattr(nv, "consensus", lambda code: None)   # 소스 통째로 막힘
    uni = [{"ticker": f"{i:06d}"} for i in range(30)]
    assert store.fetch_consensus(uni, date="2026-07-11") == 0   # 조기 중단, 파일 미생성
    assert store.load_consensus_history().empty
