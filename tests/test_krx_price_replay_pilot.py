import json
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from scripts.measure import acquire_historical_sources as acquisition
from scripts.measure import krx_price_replay_pilot as pilot


def test_krx_archive_reader_checks_raw_digest_and_session(tmp_path):
    row = {"BAS_DD": "20260731", "ISU_CD": "005930", "TDD_CLSPRC": "100"}
    raw = json.dumps({"OutBlock_1": [row]}).encode()
    entry = {**acquisition._entry(raw, rows=1), "session": "2026-07-31"}
    manifest = {"schema": "historical-source-v1", "endpoint": "sto/stk_bydd_trd",
                "market": "kr", "expected_sessions": ["2026-07-31"],
                "entries": {"raw/2026-07-31.json": entry}}
    archive = tmp_path / "valid.zip"
    acquisition.write_archive(archive, {"raw/2026-07-31.json": raw}, manifest)
    days, sources = pilot.load_krx_archives([archive])
    assert days["2026-07-31"] == [row] and sources[0]["sessions"] == 1

    tampered = tmp_path / "tampered.zip"
    with ZipFile(tampered, "w", compression=ZIP_DEFLATED) as output:
        output.writestr("manifest.json", json.dumps(manifest))
        output.writestr("raw/2026-07-31.json", raw + b" ")
    with pytest.raises(ValueError, match="digest mismatch"):
        pilot.load_krx_archives([tampered])

    repeated = tmp_path / "repeated.zip"
    repeated_raw = json.dumps({"OutBlock_1": [row, row]}).encode()
    repeated_manifest = dict(manifest)
    repeated_manifest["entries"] = {"raw/2026-07-31.json": {
        **acquisition._entry(repeated_raw, rows=2), "session": "2026-07-31"}}
    acquisition.write_archive(repeated, {"raw/2026-07-31.json": repeated_raw}, repeated_manifest)
    with pytest.raises(ValueError, match="duplicate or malformed"):
        pilot.load_krx_archives([repeated])


def test_pilot_refuses_registered_dates_before_scoring():
    with pytest.raises(ValueError, match="registered period"):
        pilot.run_pilot({}, start="2026-08-05", end="2026-08-05")


def test_pilot_refuses_partially_covered_window_before_scoring():
    with pytest.raises(ValueError, match="not fully covered"):
        pilot.run_pilot({"2026-06-01": []}, start="2026-05-29", end="2026-06-01")


def test_buy_episodes_separate_repeated_labels_and_share_changes():
    def row(day, kind, h5_exit="2026-06-09"):
        outcome = {"state": "matured", "exit_date": h5_exit, "net_pct": -12.0}
        return {"date": day, "ticker": "005380", "kind": kind,
                "loss_warning_path": {"state": "loss_observed",
                                      "missing_signal_sessions_before_loss": 0,
                                      "first_sell_before_loss": None},
                "outcomes": {"5": outcome, "20": {"state": "not_matured"}}}

    rows = [row("2026-06-01", "STRONG_BUY"), row("2026-06-02", "BUY"),
            row("2026-06-04", "HOLD"), row("2026-06-05", "BUY")]
    days = ["2026-06-01", "2026-06-02", "2026-06-04", "2026-06-05",
            "2026-06-08", "2026-06-09"]  # June 3 was a verified closure.
    shares = {"005380": {day: 100 for day in days}}
    shares["005380"]["2026-06-08"] = 200
    report = pilot._buy_episodes(rows, shares, days)
    assert report["count"] == 2
    assert report["distinct_tickers"] == 1
    assert report["episodes"][0]["buy_label_days"] == 2
    assert report["episodes"][0]["left_censored"] is True
    assert report["episodes"][0]["right_censored"] is False
    assert report["episodes"][1]["right_censored"] is True
    assert report["episodes"][1]["left_censored"] is False
    assert report["episodes"][0]["h5_share_status"] == "listed_shares_changed"
    assert report["episodes"][0]["h5_first_share_change"] == "2026-06-08"
    assert report["episodes"][0]["historical_8factor_or_order_eligible"] is False
    assert report["share_status_counts"][20]["not_matured_or_price_gap"] == 2
    assert report["first_entry_loss_diagnostic"]["signal_sessions_complete_before_loss"] == 2
    assert report["first_entry_loss_diagnostic"]["price_only_sell_before_loss"] == 0


def test_buy_run_gap_is_censored_not_a_continuous_position():
    rows = [{"date": day, "ticker": "005380", "kind": "BUY",
             "outcomes": {"5": {"state": "not_matured"},
                          "20": {"state": "not_matured"}}}
            for day in ("2026-06-01", "2026-06-04")]
    report = pilot._buy_episodes(rows, {}, [row["date"] for row in rows])
    assert report["count"] == 2
    assert report["episodes"][0]["right_censored"] is True
    assert report["episodes"][1]["left_censored"] is True


def test_loss_warning_summary_excludes_unobserved_signal_sessions():
    rows = []
    for ticker, missing, sell in (("005380", 0, "2026-06-04"),
                                  ("005930", 1, None)):
        rows.append({"date": "2026-06-01", "ticker": ticker, "kind": "BUY",
                     "loss_warning_path": {"state": "loss_observed",
                                           "missing_signal_sessions_before_loss": missing,
                                           "first_sell_before_loss": sell},
                     "outcomes": {"5": {"state": "not_matured"},
                                  "20": {"state": "not_matured"}}})
    report = pilot._buy_episodes(rows, {}, ["2026-06-01"])
    assert report["first_entry_loss_diagnostic"] == {
        "loss_observed": 2, "signal_sessions_complete_before_loss": 1,
        "price_only_sell_before_loss": 1, "signal_gap_before_loss": 1,
        "scope": "price_only_labels_not_holder_exit_or_notification"}
