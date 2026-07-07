"""분기 1회 DART + 매일 시총만 재계산(update_valuation) — DART 재호출 없이 PER/PBR·시총 갱신."""

import json

from signal_desk import store


def test_update_valuation_recomputes_from_cached_financials(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data/cache").mkdir(parents=True)
    # 캐시된 DART 재무(연간, 불변) — net_income/equity 보존
    store._write_json(store.FUNDAMENTALS_FILE, {
        "005930": {"net_income": 20_000, "equity": 100_000, "per": 5.0, "pbr": 1.0, "mktcap": 100_000},
    })
    # 오늘 시총만 바뀜(주가 상승) → PER/PBR·시총 재계산돼야
    monkeypatch.setattr(store.krx_open_api, "market_caps", lambda: {"005930": 200_000})
    n = store.update_valuation()
    assert n == 1
    f = json.loads(store.FUNDAMENTALS_FILE.read_text())["005930"]
    assert f["mktcap"] == 200_000
    assert f["per"] == 10.0        # 200000/20000
    assert f["pbr"] == 2.0         # 200000/100000
    assert f["net_income"] == 20_000  # DART 재무는 그대로(재호출 안 함)


def test_update_valuation_graceful_without_data(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data/cache").mkdir(parents=True)
    assert store.update_valuation() == 0                      # 캐시 재무 없음 → 0
    store._write_json(store.FUNDAMENTALS_FILE, {"005930": {"net_income": 1, "equity": 1}})
    monkeypatch.setattr(store.krx_open_api, "market_caps", lambda: {})  # 시총 조회 실패
    assert store.update_valuation() == 0                      # 기존값 유지, 스킵
