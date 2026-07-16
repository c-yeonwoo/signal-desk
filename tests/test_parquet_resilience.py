"""손상 parquet 복원 — 잘린 파일(재시작 중 쓰기 중단)이 앱 전체를 죽이지 않는지 검증."""

import pandas as pd

from signal_desk import store


def _cache(tmp_path):
    d = tmp_path / "data/cache"
    d.mkdir(parents=True)
    return d


def test_atomic_write_roundtrip(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _cache(tmp_path)
    df = pd.DataFrame([{"ticker": "AAA", "date": "2026-07-06", "close": 100.0}])
    store._write_parquet(df, store.PRICES_FILE)
    assert store.PRICES_FILE.exists()
    # 임시파일이 남지 않아야 함(원자적 교체)
    assert not list((tmp_path / "data/cache").glob("*.tmp"))
    back = store._read_parquet(store.PRICES_FILE)
    assert back["close"].tolist() == [100.0]


def test_corrupt_parquet_returns_empty_and_removes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _cache(tmp_path)
    store.PRICES_FILE.write_bytes(b"not a parquet file, truncated garbage")  # footer magic 없음
    out = store._read_parquet(store.PRICES_FILE)
    assert out.empty
    assert not store.PRICES_FILE.exists()  # 손상 파일은 폐기 → 다음 수집이 재생성


def test_loaders_survive_corrupt_cache(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _cache(tmp_path)
    for f in (store.PRICES_FILE, store.US_PRICES_FILE):
        f.write_bytes(b"garbage")
    # 손상 캐시여도 크래시 없이 빈 결과(요청/봇 루프가 죽지 않음)
    assert store.load_price_series() == {}
    assert store.load_price_history("005930") == []
    assert store.load_us_price_series() == {}
    assert store.load_us_price_history("AAPL") == []
    assert store.load_all_dated_closes() == {}
