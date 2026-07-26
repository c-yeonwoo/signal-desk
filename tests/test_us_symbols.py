"""클래스주 심볼 표기와 반복 실패 유예.

실제 사고: S&P500 원본 `BRK.B`/`BF.B`를 내부 ID `BRK-B`/`BF-B`로 바꿔 저장한 뒤 그 표기를
그대로 외부에 물었다. 토스는 404(stock-not-found), KIS는 세 거래소 모두 조회 실패였고, 실패를
기억하지 않아 30분마다 같은 두 종목을 영원히 재시도하며 로그를 채웠다."""

import datetime
import json

from signal_desk import api, store
from signal_desk.ingest import us


def test_class_share_is_tried_as_dot_first():
    assert us.symbol_variants("BRK-B")[0] == "BRK.B"
    assert set(us.symbol_variants("BF-B")) == {"BF.B", "BF-B", "BFB", "BF/B"}


def test_plain_tickers_have_exactly_one_form():
    """대시 없는 티커는 후보가 자기 자신뿐 — 499종목의 기존 경로가 그대로여야 한다."""
    for t in ("AAPL", "MSFT", "MKC"):
        assert us.symbol_variants(t) == [t]


def _stub_providers(monkeypatch, tmp_path, *, toss_ok: set[str]):
    """토스가 toss_ok에 든 표기만 받아주고 KIS는 전부 실패하는 상황."""
    (tmp_path / "data/cache").mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(tmp_path)
    asked: list[str] = []
    from signal_desk.ingest import toss
    monkeypatch.setattr(toss, "available", lambda: True)

    def fake_daily(sym, count=200):
        asked.append(sym)
        return [{"date": "2026-07-24", "open": 1.0, "close": 1.0, "volume": 1}] \
            if sym in toss_ok else []

    monkeypatch.setattr(toss, "daily_ohlcv", fake_daily)
    monkeypatch.setattr(us, "detect_exchange", lambda sym, creds=None: None)
    return asked


def test_dot_form_is_used_and_remembered(tmp_path, monkeypatch):
    asked = _stub_providers(monkeypatch, tmp_path, toss_ok={"BRK.B"})
    assert store.fetch_us_prices(["BRK-B"]) == 1
    assert asked == ["BRK.B"]                       # 첫 후보에서 성공

    asked.clear()
    assert store.fetch_us_prices(["BRK-B"]) == 1
    assert asked == ["BRK.B"]                       # 통한 표기를 캐시해 재탐색 없음
    saved = json.loads((tmp_path / "data/cache/us_symbols.json").read_text())
    assert saved["toss"]["BRK-B"] == "BRK.B"


def test_dash_form_still_works_when_that_is_what_the_provider_wants(tmp_path, monkeypatch):
    """점 표기가 정답이라 단정하지 않는다 — 대시를 받는 제공자면 두 번째 후보에서 붙는다."""
    asked = _stub_providers(monkeypatch, tmp_path, toss_ok={"BRK-B"})
    assert store.fetch_us_prices(["BRK-B"]) == 1
    assert asked == ["BRK.B", "BRK-B"]


def test_ticker_is_stored_under_our_internal_id(tmp_path, monkeypatch):
    """외부 표기가 무엇이든 parquet·관심종목이 쓰는 ID는 대시 형태로 남아야 한다."""
    _stub_providers(monkeypatch, tmp_path, toss_ok={"BRK.B"})
    store.fetch_us_prices(["BRK-B"])
    assert "BRK-B" in store.load_us_price_series()


def test_failure_is_remembered_and_eventually_deferred(tmp_path, monkeypatch):
    _stub_providers(monkeypatch, tmp_path, toss_ok=set())        # 어떤 표기도 실패
    for expected in (1, 2, 3):
        assert store.fetch_us_prices(["BF-B"]) == 0
        rec = json.loads((tmp_path / "data/cache/us_price_skip.json").read_text())["BF-B"]
        assert rec["fails"] == expected
    assert store.us_price_deferred("BF-B") is True               # 3회부터 유예
    assert store.us_price_deferred_tickers() == ["BF-B"]


def test_success_clears_the_failure_record(tmp_path, monkeypatch):
    asked = _stub_providers(monkeypatch, tmp_path, toss_ok=set())
    store.fetch_us_prices(["BF-B"])
    assert json.loads((tmp_path / "data/cache/us_price_skip.json").read_text())
    asked.clear()
    _stub_providers(monkeypatch, tmp_path, toss_ok={"BF.B"})
    store.fetch_us_prices(["BF-B"])
    assert json.loads((tmp_path / "data/cache/us_price_skip.json").read_text()) == {}


def test_deferral_expires(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    old = (datetime.date.today() - datetime.timedelta(days=store.US_SKIP_DAYS + 1)).isoformat()
    skip = {"OLD": {"fails": 9, "last": old},
            "NEW": {"fails": 9, "last": datetime.date.today().isoformat()},
            "FEW": {"fails": 1, "last": datetime.date.today().isoformat()}}
    assert store.us_price_deferred("OLD", skip) is False      # 유예 기간 지나면 재시도
    assert store.us_price_deferred("NEW", skip) is True
    assert store.us_price_deferred("FEW", skip) is False      # 1회 실패는 유예 아님


def test_deferred_tickers_do_not_eat_the_backfill_batch(monkeypatch):
    """유예 종목이 missing 앞자리를 차지해 뒤쪽 종목 백필을 막으면 안 된다."""
    universe = [{"ticker": t} for t in ("BRK-B", "BF-B", "MKC", "MCD", "AAPL")]
    requested: list[list[str]] = []
    monkeypatch.setattr(api.store, "load_us_universe", lambda: universe)
    monkeypatch.setattr(api.store, "load_us_price_series", lambda: {"AAPL": [1.0]})
    monkeypatch.setattr(api.store, "fetch_us_prices",
                        lambda ts, days=400: (requested.append(list(ts)), len(ts))[1])
    monkeypatch.setattr(api.store, "us_price_skips", lambda: {})
    monkeypatch.setattr(api.store, "us_price_deferred",
                        lambda t, skip=None: t in ("BRK-B", "BF-B"))

    out = api._backfill_us_prices_batch(batch=2)
    assert requested == [["MKC", "MCD"]]        # 유예 2종목을 건너뛰고 실제로 받을 종목부터
    assert out["deferred"] == 2 and out["missing"] == 0
