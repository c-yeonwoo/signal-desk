"""거장 전략 스크린 + 동종업계 비교 — 규칙 필터와 percentile 로직."""

import pandas as pd

from signal_desk.reference import guru_screens


def test_buffett_requires_all_criteria():
    good = {"roe": 20, "debt_ratio": 40, "per": 15}          # 3조건 모두 충족
    weak = {"roe": 20, "debt_ratio": 40, "per": 40}          # PER 과다 → 탈락
    assert guru_screens.matches(guru_screens.SCREEN_BY_KEY["buffett"], good)
    assert guru_screens.matches(guru_screens.SCREEN_BY_KEY["buffett"], weak) is None


def test_missing_data_fails_conservatively():
    assert guru_screens.matches(guru_screens.SCREEN_BY_KEY["graham"], {"per": 10}) is None  # PBR·부채 결측
    assert guru_screens.matches(guru_screens.SCREEN_BY_KEY["buffett"], {}) is None


def test_lynch_peg_rule():
    # 성장 30%인데 PER 30 → PER ≤ 30*1.5=45 통과, ROE 15 통과, 성장 통과
    assert guru_screens.matches(guru_screens.SCREEN_BY_KEY["lynch"], {"revenue_growth": 30, "roe": 15, "per": 30})
    # 성장 12%인데 PER 40 → 40 > 18 → 탈락
    assert guru_screens.matches(guru_screens.SCREEN_BY_KEY["lynch"], {"revenue_growth": 12, "roe": 15, "per": 40}) is None


def test_run_returns_all_screens():
    fundamentals = {
        "A": {"roe": 25, "debt_ratio": 30, "per": 12, "pbr": 1.2, "revenue_growth": 20},  # 세 스크린 다 걸림직
        "B": {"roe": 3, "debt_ratio": 200, "per": 80, "pbr": 9, "revenue_growth": -5},    # 아무데도 안 걸림
    }
    out = guru_screens.run(fundamentals)
    assert set(out) == {"buffett", "graham", "lynch"}
    assert "A" in out["buffett"] and "B" not in out["buffett"]


def _seed(tmp_path):
    (tmp_path / "data/cache").mkdir(parents=True)
    from signal_desk import store
    # 반도체 섹터 실제 매핑 종목(005930 삼성전자, 000660 하이닉스) 사용
    store._write_json(store.FUNDAMENTALS_FILE, {
        "005930": {"per": 12, "pbr": 1.3, "roe": 18, "revenue_growth": 15, "debt_ratio": 35, "mktcap": 5e14},
        "000660": {"per": 40, "pbr": 5.0, "roe": 8, "revenue_growth": 3, "debt_ratio": 90, "mktcap": 3e14},
    })
    pd.DataFrame([{"ticker": "005930", "name": "삼성전자"}, {"ticker": "000660", "name": "SK하이닉스"}]) \
        .to_json(store.UNIVERSE_FILE, orient="records", force_ascii=False)


def test_guru_screens_endpoint(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _seed(tmp_path)
    from signal_desk import api
    out = api.guru_screens_get(market="kospi")
    assert out["ready"]
    buf = next(s for s in out["screens"] if s["key"] == "buffett")
    assert any(i["ticker"] == "005930" for i in buf["items"])  # 삼성전자 통과
    # tickers = 전체 매칭(스크리너 프리셋 필터용) — count와 길이 일치, 삼성전자 포함
    assert "005930" in buf["tickers"] and len(buf["tickers"]) == buf["count"]
    # US는 미지원이지만 스크린 메타는 준다
    us = api.guru_screens_get(market="us")
    assert us["ready"] is False and us["screens"]


def test_peers_endpoint_percentile(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _seed(tmp_path)
    from signal_desk import api
    out = api.signal_peers_get("005930", market="kospi")
    assert out["ready"] and out["sector"] == "반도체"
    per = next(m for m in out["metrics"] if m["key"] == "per")
    # 005930 PER 12 < 000660 PER 40, PER은 낮을수록 좋음 → 100% 앞섬
    assert per["better_pct"] == 100.0
    assert api.signal_peers_get("005930", market="us")["ready"] is False
