"""깊이 백필이 못 채우는 종목을 유예한다 — KIS HTTP 500 로그 폭주의 원인.

프로덕션(2026-08-16)에서 같은 줄이 30분마다 수십 번 찍혔다::

    KIS API HTTP 오류(/uapi/overseas-price/v1/quotations/dailyprice): HTTP Error 500

한도가 아니었다. 모멘텀이 안 붙은 8종목이 **FISV·BNY·MRSH·FDXF·HONA·Q·ECHO·FITB** —
개명(Fiserv→FI)·폐지된 **없는 심볼**이고, KIS는 모르는 심볼에 500을 낸다.

유예되지 않은 이유가 결함이다: `fetch_us_prices` 의 실패 기록은 `if not bars` 뿐인데
토스가 짧은 봉이라도 주면 `bars` 가 비지 않아 **성공으로 처리**된다. 그래서 종목은 영원히
"얕음"으로 남고 매 틱 다시 뽑혀 5페이지씩 재요청된다.

핵심 규약 — 깊이 유예는 수집 유예와 **다른 파일**이다. 섞으면 200봉으로 꼬리를 잘 따라가던
종목의 일일 갱신까지 멈춘다(깊이 결함 ≠ 수집 결함).
"""

from __future__ import annotations

import datetime
import json

import pytest

from signal_desk import store


@pytest.fixture()
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "CACHE_DIR", tmp_path)
    for name in ("US_PRICES_FILE", "US_EXCHANGES_FILE", "US_SYMBOLS_FILE",
                 "US_PRICE_SKIP_FILE", "US_DEEP_SKIP_FILE"):
        monkeypatch.setattr(store, name, tmp_path / getattr(store, name).name)
    return tmp_path


def _bars(n: int, *, start: str = "2025-01-02") -> list[dict]:
    d0 = datetime.date.fromisoformat(start)
    return [{"date": (d0 + datetime.timedelta(days=i)).isoformat(),
             "open": 10.0, "close": 10.0, "volume": 100} for i in range(n)]


def _wire(monkeypatch, *, toss_bars: int, kis_bars: int | None):
    """토스는 성공(짧게), KIS는 실패 — 프로덕션의 그 상태 그대로."""
    from signal_desk.ingest import toss, us
    monkeypatch.setattr(toss, "available", lambda: True)
    monkeypatch.setattr(toss, "daily_ohlcv", lambda sym, count=200: _bars(min(toss_bars, count)))
    monkeypatch.setattr(us, "detect_exchange", lambda sym: "NAS")
    calls: list[str] = []

    def _ohlcv(sym, days=100, excd=None):
        calls.append(sym)
        return _bars(kis_bars) if kis_bars else []

    monkeypatch.setattr(us, "us_ohlcv", _ohlcv)
    return calls


def test_deep_failure_is_recorded_even_though_toss_succeeded(cache, monkeypatch):
    """**이게 그 버그다.** 토스가 200봉을 주면 실패로 안 세어 영원히 재시도됐다."""
    _wire(monkeypatch, toss_bars=200, kis_bars=None)
    store.fetch_us_prices(["FISV"], days=store.US_DEEP_TARGET_BARS)
    rec = json.loads(store.US_DEEP_SKIP_FILE.read_text()).get("FISV")
    assert rec and rec["fails"] == 1, "깊이 미달을 안 세면 30분마다 KIS 500을 다시 받는다"
    assert rec["bars"] == 200, "몇 봉에서 막혔는지 없으면 '못 받음'과 '짧게 받음'을 못 가른다"


def test_deferral_stops_the_retries(cache, monkeypatch):
    """연속 실패가 문턱을 넘으면 유예된다 — 로그 폭주가 멈추는 지점."""
    _wire(monkeypatch, toss_bars=200, kis_bars=None)
    for _ in range(store.US_SKIP_AFTER_FAILS):
        assert not store.us_deep_deferred("FISV"), "문턱 전에 유예하면 일시 장애로 종목을 잃는다"
        store.fetch_us_prices(["FISV"], days=store.US_DEEP_TARGET_BARS)
    assert store.us_deep_deferred("FISV")
    assert store.us_deep_deferred_tickers() == ["FISV"], "이름으로 안 드러나면 조용한 0이다"


def test_deep_deferral_does_not_stop_the_daily_refresh(cache, monkeypatch):
    """**깊이 유예와 수집 유예는 다른 파일이다.** 섞으면 잘 돌던 꼬리까지 멈춘다."""
    _wire(monkeypatch, toss_bars=200, kis_bars=None)
    for _ in range(store.US_SKIP_AFTER_FAILS):
        store.fetch_us_prices(["FISV"], days=store.US_DEEP_TARGET_BARS)
    assert store.us_deep_deferred("FISV")
    assert not store.us_price_deferred("FISV"), "일일 갱신까지 막으면 200봉 꼬리가 멈춘다"

    calls = _wire(monkeypatch, toss_bars=200, kis_bars=None)
    assert store.fetch_us_prices(["FISV"], days=60) == 1, "일일(얕은) 갱신은 계속 돌아야 한다"
    assert calls == [], "얕은 요청은 KIS로 올라가지 않는다 — 500의 출처가 여기였다"


def test_success_clears_the_deferral(cache, monkeypatch):
    """심볼이 고쳐지거나 KIS가 회복되면 유예는 사라져야 한다 — 래치는 영구 손실이다."""
    _wire(monkeypatch, toss_bars=200, kis_bars=None)
    for _ in range(store.US_SKIP_AFTER_FAILS):
        store.fetch_us_prices(["FISV"], days=store.US_DEEP_TARGET_BARS)
    assert store.us_deep_deferred("FISV")

    _wire(monkeypatch, toss_bars=200, kis_bars=300)
    store.fetch_us_prices(["FISV"], days=store.US_DEEP_TARGET_BARS)
    assert not store.us_deep_deferred("FISV")
    assert store.us_deep_skips().get("FISV") is None


def test_shallow_only_counts_when_a_deep_request_was_made(cache, monkeypatch):
    """얕게 **요청한** 것을 깊이 실패로 세면 안 된다 — 일일 갱신마다 유예가 쌓인다."""
    _wire(monkeypatch, toss_bars=200, kis_bars=None)
    store.fetch_us_prices(["AAA"], days=60)
    assert store.us_deep_skips() == {}, "60봉만 요청해 놓고 252봉 미달이라 하면 안 된다"


def test_short_history_is_not_reported_as_a_symbol_failure(cache, monkeypatch):
    """**신규 상장은 고장이 아니다.** 실측 8종목이 전부 분할·개명 직후 종목이었다 —
    Qnity(DuPont 분할)·FedEx Freight·Honeywell Aerospace. 252거래일이 원리적으로 없다.
    이걸 심볼 실패로 보고하면 없는 고장을 조사하게 된다."""
    _wire(monkeypatch, toss_bars=40, kis_bars=60)       # 소스는 응답했다 — 이력이 짧을 뿐
    store.fetch_us_prices(["Q"], days=store.US_DEEP_TARGET_BARS)
    assert store.us_deep_skips()["Q"]["reason"] == "short_history"

    _wire(monkeypatch, toss_bars=200, kis_bars=None)    # 소스가 아예 못 줬다 — 심볼 문제
    store.fetch_us_prices(["FISV"], days=store.US_DEEP_TARGET_BARS)
    assert store.us_deep_skips()["FISV"]["reason"] == "symbol_failed"


def test_backfill_batch_skips_deep_deferred_tickers(cache, monkeypatch):
    """배치 선택기가 유예를 안 보면 유예는 있으나 마나다(=고장 안 고쳐짐)."""
    from signal_desk import api
    monkeypatch.setattr(store, "load_us_universe", lambda: [{"ticker": "FISV"}, {"ticker": "OKAY"}])
    monkeypatch.setattr(store, "load_us_price_series", lambda: {"FISV": [], "OKAY": []})
    monkeypatch.setattr(store, "us_prices_shallow_tickers", lambda u, **k: ["FISV", "OKAY"])
    monkeypatch.setattr(store, "us_deep_skips",
                        lambda: {"FISV": {"fails": 9, "last": datetime.date.today().isoformat()}})
    asked: list[list[str]] = []
    monkeypatch.setattr(store, "fetch_us_prices",
                        lambda ts, days=100: (asked.append(list(ts)), len(ts))[1])
    api._backfill_us_prices_batch(batch=10)
    assert asked == [["OKAY"]], f"유예 종목을 또 요청했다: {asked}"
