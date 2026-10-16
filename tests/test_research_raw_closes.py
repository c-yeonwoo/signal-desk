"""Retrospective raw price capture stays bounded and disconnected from trading."""

import datetime as dt
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from signal_desk.signals import research_raw_closes as raw


def _row(day: str, close: str = "100") -> dict:
    return {"date": day, "close": close, "source_row": {"xymd": day.replace("-", ""), "clos": close}}


def test_capture_preserves_missing_sessions_without_filling_and_checks_hash(tmp_path):
    out = raw.capture(market="kr", ticker="005930", start="2026-07-13", end="2026-07-15",
                      root=tmp_path, observed_at=dt.datetime(2026, 10, 16, tzinfo=dt.timezone.utc),
                      fetch=lambda *_: [_row("2026-07-13"), _row("2026-07-15", "98")])
    saved = raw.verify(Path(out["path"]))
    assert saved["missing_sessions"] == ["2026-07-14"]
    assert saved["coverage"] == "missing_sessions"
    assert saved["price_basis"] == "requested_unadjusted_unverified"
    assert saved["strict_pit_eligible"] is False
    assert saved["live_eligible"] is False
    assert [row["close"] for row in saved["rows"]] == [100.0, 98.0]
    assert raw.capture(market="kr", ticker="005930", start="2026-07-13", end="2026-07-15",
                       root=tmp_path, observed_at=dt.datetime(2026, 10, 16, tzinfo=dt.timezone.utc),
                       fetch=lambda *_: [_row("2026-07-13"), _row("2026-07-15", "98")]) == out
    Path(out["path"]).write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="checksum"):
        raw.verify(Path(out["path"]))


@pytest.mark.parametrize("kwargs", [
    {"end": "2026-08-05"},
    {"start": "2026-05-01", "end": "2026-07-13"},
    {"ticker": "../../../secret"},
    {"market": "us", "ticker": "AAPL"},
])
def test_rejects_protected_or_unbounded_or_ambiguous_scope(tmp_path, kwargs):
    args = {"market": "kr", "ticker": "005930", "start": "2026-07-13",
            "end": "2026-07-15", "root": tmp_path, "fetch": lambda *_: [_row("2026-07-13")]}
    with pytest.raises(ValueError):
        raw.capture(**(args | kwargs))
    assert list(tmp_path.iterdir()) == []


def test_duplicate_and_invalid_close_fail_closed_before_archive(tmp_path):
    for rows in ([_row("2026-07-13"), _row("2026-07-13", "101")],
                 [_row("2026-07-13", "NaN")], [_row("2026-07-13", "0")]):
        with pytest.raises(ValueError):
            raw.capture(market="kr", ticker="005930", start="2026-07-13", end="2026-07-15",
                        root=tmp_path, fetch=lambda *_, rows=rows: rows)
    assert list(tmp_path.iterdir()) == []


def test_kr_adapter_explicitly_requests_unadjusted(monkeypatch, capsys):
    from pykrx import stock
    capsys.readouterr()  # Ignore any import-time output before invoking the adapter.

    captured = {}

    def fake(start, end, ticker, *, adjusted):
        print("provider login identifier must not enter audit logs")
        captured.update(start=start, end=end, ticker=ticker, adjusted=adjusted)
        return pd.DataFrame({"시가": [99], "종가": [100], "거래량": [123]},
                            index=pd.to_datetime(["2026-07-13"]))

    monkeypatch.setattr(stock, "get_market_ohlcv_by_date", fake)
    rows = raw._fetch_kr("005930", "2026-07-13", "2026-07-15", None)
    assert captured == {"start": "20260713", "end": "20260715", "ticker": "005930", "adjusted": False}
    assert rows[0]["source_row"]["종가"] == 100
    assert capsys.readouterr().out == ""


def test_us_adapter_requests_modp_zero_while_normal_signal_path_keeps_one(monkeypatch):
    from signal_desk.broker import kis
    from signal_desk.ingest import us

    queries = []

    def fake_request(path, tr_id, creds, params):
        queries.append(params)
        return {"rt_cd": "0", "output2": [{"xymd": "20260713", "clos": "100"}]}

    monkeypatch.setattr(kis, "_request", fake_request)
    monkeypatch.setattr(raw.config, "kis_credentials", lambda: {"env": "demo"})
    rows = raw._fetch_us("AAPL", "2026-07-13", "2026-07-15", "NAS")
    assert rows[0]["source_row"]["clos"] == "100"
    assert queries[-1]["MODP"] == "0" and queries[-1]["BYMD"] == "20260715"
    us._fetch_page("AAPL", "NAS", {"env": "demo"}, "20260715")
    assert queries[-1]["MODP"] == "1"


def test_us_capture_is_research_only_even_with_complete_date_panel(tmp_path):
    out = raw.capture(market="us", ticker="AAPL", exchange="NAS",
                      start="2026-07-13", end="2026-07-13", root=tmp_path,
                      fetch=lambda *_: [_row("2026-07-13"), _row("2026-07-10", "95")])
    saved = raw.verify(Path(out["path"]))
    assert saved["coverage"] == "date_panel_only"
    assert saved["request"]["MODP"] == "0"
    assert saved["price_basis"] != "unadjusted_verified"
    assert len(saved["rows"]) == 1
    assert len(saved["source_response_rows"]) == 2  # Preserve the entire returned provider page.


def test_fill_range_audit_flags_outside_price_and_does_not_certify_basis(tmp_path):
    out = raw.capture(market="us", ticker="AAPL", exchange="NAS", start="2026-07-13",
                      end="2026-07-13", root=tmp_path,
                      fetch=lambda *_: [{**_row("2026-07-13"),
                                         "source_row": {"xymd": "20260713", "clos": "100",
                                                        "low": "95", "high": "105"}}])
    ts = dt.datetime(2026, 7, 13, 10, tzinfo=ZoneInfo("America/New_York")).timestamp()
    trades = [{"id": 1, "ticker": "AAPL", "ts": ts, "price": 100},
              {"id": 2, "ticker": "AAPL", "ts": ts, "price": 110},
              {"id": 3, "ticker": "MSFT", "ts": ts, "price": 200}]
    result = raw.audit_fill_range(trades, [Path(out["path"])], market="us")
    assert (result["within_daily_range"], result["outside_daily_range"], result["missing_bar"]) == (1, 1, 1)
    assert result["state"] == "needs_review"
    assert result["examples"][0]["trade_id"] == 2
    assert result["price_basis_verified"] is False
    assert result["verified_total_return"] is None


def test_outside_fill_matches_older_close_only_as_retrospective_clue(tmp_path):
    rows = [{"date": "2026-07-13", "close": "100",
             "source_row": {"xymd": "20260713", "clos": "100", "low": "95", "high": "105"}},
            {"date": "2026-07-02", "close": "90",
             "source_row": {"xymd": "20260702", "clos": "90", "low": "88", "high": "93"}}]
    out = raw.capture(market="us", ticker="AAPL", exchange="NAS", start="2026-07-13",
                      end="2026-07-13", root=tmp_path, fetch=lambda *_: rows)
    ts = dt.datetime(2026, 7, 13, 10, tzinfo=ZoneInfo("America/New_York")).timestamp()
    future = dt.datetime(2026, 8, 5, 10, tzinfo=ZoneInfo("America/New_York")).timestamp()
    result = raw.audit_fill_range([
        {"id": 1, "ticker": "AAPL", "ts": ts, "price": 90},
        {"id": 2, "ticker": "AAPL", "ts": ts, "price": 110},
        {"id": 3, "ticker": "AAPL", "ts": future, "price": 90},
    ], [Path(out["path"])], market="us")
    assert result["outside_daily_range"] == 2
    assert (result["outside_prior_close_match"], result["outside_prior_close_unmatched"],
            result["outside_prior_close_unknown"]) == (1, 1, 0)
    assert result["examples"][0]["matched_prior_close_session"] == "2026-07-02"
    assert result["excluded_protected_trades"] == 1
    assert result["price_basis_verified"] is False and result["verified_total_return"] is None


def test_conflicting_older_closes_cannot_be_used_to_label_a_fill(tmp_path):
    at = dt.datetime(2026, 10, 16, tzinfo=dt.timezone.utc)
    current = {"date": "2026-07-13", "close": "100",
               "source_row": {"xymd": "20260713", "clos": "100", "low": "95", "high": "105"}}
    paths = []
    for close in ("90", "91"):
        older = {"date": "2026-07-02", "close": close,
                 "source_row": {"xymd": "20260702", "clos": close}}
        out = raw.capture(market="us", ticker="AAPL", exchange="NAS", start="2026-07-13",
                          end="2026-07-13", root=tmp_path, observed_at=at,
                          fetch=lambda *_, older=older: [current, older])
        paths.append(Path(out["path"]))
    ts = dt.datetime(2026, 7, 13, 10, tzinfo=ZoneInfo("America/New_York")).timestamp()
    result = raw.audit_fill_range([{"id": 1, "ticker": "AAPL", "ts": ts, "price": 90}],
                                  paths, market="us")
    assert result["outside_daily_range"] == result["outside_prior_close_unknown"] == 1
    assert result["outside_prior_close_match"] == 0


def test_module_cli_loads_local_env_before_provider_call(monkeypatch, tmp_path, capsys):
    calls = []
    monkeypatch.setattr(raw.config, "load_env", lambda: calls.append("env"))
    monkeypatch.setattr(raw, "capture", lambda **kwargs: (
        calls.append("capture") or {"path": str(tmp_path / "observation.json")}))
    monkeypatch.setattr(raw, "verify", lambda path: calls.append("verify"))
    monkeypatch.setattr(sys, "argv", ["research_raw_closes", "--market", "kr",
                                  "--ticker", "005930", "--start", "2026-07-13",
                                  "--end", "2026-07-15", "--root", str(tmp_path)])
    raw.main()
    assert calls == ["env", "capture", "verify"]
    assert "observation.json" in capsys.readouterr().out


def test_conflicting_raw_observations_are_not_silently_chosen(tmp_path):
    at = dt.datetime(2026, 10, 16, tzinfo=dt.timezone.utc)
    source = lambda close: [{**_row("2026-07-13", close),
                             "source_row": {"low": "95", "high": "105", "clos": close}}]
    one = raw.capture(market="us", ticker="AAPL", exchange="NAS", start="2026-07-13",
                      end="2026-07-13", root=tmp_path, observed_at=at,
                      fetch=lambda *_: source("100"))
    two = raw.capture(market="us", ticker="AAPL", exchange="NAS", start="2026-07-13",
                      end="2026-07-13", root=tmp_path, observed_at=at,
                      fetch=lambda *_: source("101"))
    ts = dt.datetime(2026, 7, 13, 10, tzinfo=ZoneInfo("America/New_York")).timestamp()
    result = raw.audit_fill_range([{"id": 1, "ticker": "AAPL", "ts": ts, "price": 100}],
                                  [Path(one["path"]), Path(two["path"])], market="us")
    assert result["conflicting_bar"] == 1
    assert result["within_daily_range"] == 0
