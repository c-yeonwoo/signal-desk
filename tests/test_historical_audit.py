"""Historical signal audits must preserve timing, missingness, and all denominators."""

import pandas as pd
import pytest
import hashlib
import json
from io import BytesIO
from zipfile import ZipFile

from signal_desk import market_clock
from signal_desk.signals.historical_audit import audit_snapshots, select_recorded_inputs


def _bars(ticker: str, dates: list[str], *, first_open: float = 100.0,
          close: float = 100.0) -> list[dict]:
    return [{"date": day, "ticker": ticker, "open": first_open, "close": close}
            for day in dates]


def test_audits_all_rows_and_marks_transitions_without_future_prices():
    signal_day = "2026-07-20"
    days = market_clock.next_sessions("kr", signal_day, 5)
    signals = pd.DataFrame([
        {"date": signal_day, "ticker": "AAA", "score": 1.0, "kind": "HOLD", "momentum": 0.2},
        {"date": signal_day, "ticker": "BBB", "score": 1.8, "kind": "BUY"},
        {"date": days[0], "ticker": "AAA", "score": 1.7, "kind": "BUY", "momentum": 0.4},
    ])
    price_days = [signal_day, *days, *market_clock.next_sessions("kr", days[-1], 1)]
    bars = _bars("AAA", price_days) + _bars("BBB", price_days)
    # The signal-day close is irrelevant: entry is the *next* scheduled open.
    bars[0]["close"] = 999.0
    for bar in bars:
        if bar["ticker"] == "AAA" and bar["date"] == days[0]:
            bar["open"] = 120.0
            bar["close"] = 90.0
    result = audit_snapshots(signals, pd.DataFrame(bars), market="kr", major_tickers=("AAA",))
    assert result["signal_rows"] == 3
    rows = {(row["date"], row["ticker"]): row for row in result["rows"]}
    first = rows[(signal_day, "AAA")]
    assert first["outcomes"]["1"]["entry_open"] == 120.0
    assert first["outcomes"]["1"]["net_pct"] == -25.25
    assert first["outcomes"]["5"]["state"] == "matured"
    assert first["outcomes"]["20"]["state"] == "not_matured"
    change = rows[(days[0], "AAA")]
    assert change["kind_change"] and change["score_change"]
    assert change["factor_changes"]["momentum"] == 0.2
    assert result["summary"]["kind_changed"]["5"]["observations"] == 1
    assert result["summary"]["all"]["5"]["observations"] == 3


def test_missing_middle_session_is_not_interpolated():
    day = "2026-07-20"
    future = market_clock.next_sessions("kr", day, 5)
    signals = pd.DataFrame([{"date": day, "ticker": "AAA", "score": 2.0, "kind": "BUY"}])
    prices = pd.DataFrame(_bars("AAA", [future[0], future[1], future[3], future[4]]))
    out = audit_snapshots(signals, prices, market="kr")["rows"][0]["outcomes"]["5"]
    assert out == {"state": "price_gap", "entry_date": future[0],
                   "exit_date": future[4], "first_missing_date": future[2]}


def test_duplicate_signal_or_bar_is_rejected_instead_of_picking_a_version():
    day = "2026-07-20"
    signals = pd.DataFrame([{"date": day, "ticker": "AAA", "score": 1.0, "kind": "HOLD"}])
    bars = pd.DataFrame(_bars("AAA", market_clock.next_sessions("kr", day, 1)))
    with pytest.raises(ValueError, match="duplicate signal"):
        audit_snapshots(pd.concat([signals, signals], ignore_index=True), bars, market="kr")
    with pytest.raises(ValueError, match="duplicate price"):
        audit_snapshots(signals, pd.concat([bars, bars], ignore_index=True), market="kr")


def test_gap_between_recorded_signals_is_visible():
    day = "2026-07-20"
    future = market_clock.next_sessions("kr", day, 4)
    signals = pd.DataFrame([
        {"date": day, "ticker": "AAA", "score": 1.0, "kind": "HOLD"},
        {"date": future[2], "ticker": "AAA", "score": 1.9, "kind": "BUY"},
    ])
    bars = pd.DataFrame(_bars("AAA", future))
    result = audit_snapshots(signals, bars, market="kr")
    assert result["snapshot_gap_rows"] == 1
    assert result["rows"][1]["kind_change"]
    assert result["rows"][1]["snapshot_gap"]


def test_export_selects_last_recorded_sessions_and_matching_prices_only():
    signals = pd.DataFrame([
        {"date": "2026-07-10", "ticker": "AAA", "score": 1.0, "kind": "HOLD", "private": "drop"},
        {"date": "2026-07-13", "ticker": "AAA", "score": 1.1, "kind": "HOLD", "private": "drop"},
        {"date": "2026-07-14", "ticker": "BBB", "score": 2.0, "kind": "BUY", "private": "drop"},
    ])
    prices = pd.DataFrame(_bars("AAA", ["2026-07-10", "2026-07-13", "2026-07-14", "2026-07-15"])
                          + _bars("BBB", ["2026-07-13", "2026-07-14", "2026-07-15"]))
    selected, bars = select_recorded_inputs(signals, prices, market="kr", sessions=2)
    assert selected["date"].tolist() == ["2026-07-13", "2026-07-14"]
    assert "private" not in selected
    assert len(bars) == 6
    assert not (bars["date"] == "2026-07-10").any()
    with pytest.raises(ValueError, match="invalid market"):
        select_recorded_inputs(signals, prices, market="kr", sessions=62)


def test_admin_export_is_protected_and_has_checkable_inputs(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from signal_desk import api, store

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ADMIN_EMAILS", "historical-admin@example.com")
    monkeypatch.setattr(api, "_rl_hits", {})  # 이 테스트의 가입 제한 횟수를 다른 API 테스트와 격리
    signal_file, price_file = tmp_path / "signals.parquet", tmp_path / "prices.parquet"
    pd.DataFrame([{"date": "2026-07-10", "ticker": "267250", "score": 1.0, "kind": "HOLD"},
                  {"date": "2026-07-13", "ticker": "267250", "score": 1.7, "kind": "BUY"}]).to_parquet(signal_file)
    pd.DataFrame(_bars("267250", ["2026-07-13", "2026-07-14"])).to_parquet(price_file)
    monkeypatch.setattr(store, "SIGNAL_HISTORY_FILE", signal_file)
    monkeypatch.setattr(store, "PRICES_FILE", price_file)
    url = "/api/admin/research/historical-inputs?market=kr"
    guest = TestClient(api.app)
    assert guest.get(url).status_code == 401
    guest.post("/api/auth/signup", json={"email": "historical-reader@example.com", "pw": "abcdef12"})
    assert guest.get(url).status_code == 403
    admin = TestClient(api.app)
    admin.post("/api/auth/signup", json={"email": "historical-admin@example.com", "pw": "abcdef12"})
    response = admin.get(url)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    with ZipFile(BytesIO(response.content)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        signal_bytes = archive.read("signals.parquet")
        price_bytes = archive.read("prices.parquet")
    assert manifest["signal_rows"] == manifest["price_rows"] == 2
    assert hashlib.sha256(signal_bytes).hexdigest() == manifest["signals_sha256"]
    assert hashlib.sha256(price_bytes).hexdigest() == manifest["prices_sha256"]
    case_url = "/api/admin/research/historical-cases?market=kr"
    assert guest.get(case_url).status_code == 403
    case_response = admin.get(case_url + "&sessions=1")
    assert case_response.status_code == 200
    assert "summary" not in case_response.json()  # 등록 기간의 조기 성적 합산을 반환하지 않는다
    assert case_response.json()["case_rows"][0]["ticker"] == "267250"
    assert case_response.json()["case_rows"][0]["kind_change"] is True
    assert case_response.json()["recorded_signal_rows"] == 1
    assert case_response.json()["case_rows"][0]["outcomes"]["5"]["state"] == "not_matured"
