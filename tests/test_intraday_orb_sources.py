"""Source-only collection fixtures. No provider calls or simulated profitability."""

import base64
import hashlib
import json
import threading
import time

import pytest

from scripts.measure import intraday_orb_sources as collector


def _plan(*dates):
    return {"schema": "intraday-orb-source-plan-v1", "market": "us",
            "symbols": list(collector.SYMBOLS), "sessions": list(dates or ("2024-06-03",))}


def _envelope(item, *, partial=False, status="observed"):
    quote = item["kind"] == "quotes"
    result = {"schema": f"alpaca-historical-{'quote' if quote else 'bar'}-window-v1",
              "symbol": item["symbol"], "request": item["request"], "status": status,
              "live_order_eligible": False, "strict_pit_eligible": False}
    if status not in {"observed", "empty"}:
        return {**result, "http_status": 403}
    rows = ([{"t": item["start"], "bp": 100, "ap": 100.01, "bs": 1, "as": 1, "c": ["R"]}]
            if quote else [{"t": item["start"], "o": 100, "h": 101, "l": 99, "c": 100, "v": 100}])
    if status == "empty":
        rows = []
    raw = collector._json({"symbol": item["symbol"], item["kind"]: rows,
                           "next_page_token": "more" if partial else None})
    result.update(raw_b64=base64.b64encode(raw).decode(), raw_sha256=hashlib.sha256(raw).hexdigest(),
                  raw_bytes=len(raw), has_next_page=partial)
    result["quote_count" if quote else "bar_count"] = len(rows)
    if not quote:
        result["bars"] = [{"t": row["t"], "open": row["o"], "high": row["h"], "low": row["l"],
                           "close": row["c"], "volume": row["v"]} for row in rows]
        result["response_complete"] = not partial
    return result


def _fake(monkeypatch, plan, *, quote=False, partial=False, status="observed"):
    items = collector.requests_for_plan(plan, quote=quote)
    by_key = {(row["symbol"], row["start"], row["end"]): row for row in items}
    calls, sleeps = [], []
    monkeypatch.setattr(collector.config, "load_env", lambda: None)
    monkeypatch.setattr(collector.source, "availability", lambda: {"configured": True})
    monkeypatch.setattr(collector.time, "monotonic", lambda: 10.0)
    monkeypatch.setattr(collector.time, "sleep", sleeps.append)

    def read(symbol, start, end):
        calls.append((symbol, start, end))
        return _envelope(by_key[(symbol, start, end)], partial=partial, status=status)

    monkeypatch.setattr(collector.source, "quote_window" if quote else "bar_window", read)
    return calls, sleeps


def test_calendar_windows_follow_dst_early_close_and_fixed_panel():
    items = collector.requests_for_plan(_plan("2024-03-08", "2024-03-11", "2024-11-29"))
    assert len(items) == 24
    assert items[0]["start"] == "2024-03-08T14:30:00+00:00"
    assert items[8]["start"] == "2024-03-11T13:30:00+00:00"
    assert items[16]["end"] == "2024-11-29T18:00:00+00:00"
    assert items[16]["request"]["end"] == "2024-11-29T17:59:59+00:00"
    assert items[0]["path"] == "bars/AAPL/2024-03-08.json"
    for plan in (_plan("2024-07-04"), _plan("2024-06-03", "2024-06-03"),
                 _plan("2024-06-04", "2024-06-03"), _plan("2025-01-02"),
                 {**_plan(), "symbols": ["SPY"]}, {**_plan(), "market": "kr"}):
        with pytest.raises(ValueError):
            collector.requests_for_plan(plan)


def test_budget_new_files_rate_limit_and_reuse_without_overwrite(tmp_path, monkeypatch):
    plan = _plan()
    calls, sleeps = _fake(monkeypatch, plan)
    first = collector.collect(plan, tmp_path, 2)
    assert first["status"] == "blocked" and first["stop_reason"] == "request_budget_exhausted"
    assert first["attempted"] == first["request_successes"] == len(calls) == 2 and sleeps == [0.5]
    assert len(first["unmet"]) == 6 and first["request_failures"] == 0
    original = (tmp_path / "bars/AAPL/2024-06-03.json").read_bytes()
    second = collector.collect(plan, tmp_path, 8)
    assert second["status"] == "collected" and second["attempted"] == 6 and second["reused"] == 2
    assert (tmp_path / "bars/AAPL/2024-06-03.json").read_bytes() == original
    assert second["source_bytes"] > second["new_source_bytes"]
    assert second["manifest_path"] != first["manifest_path"]
    assert not second["live_order_eligible"] and not second["strict_pit_eligible"]


@pytest.mark.parametrize("status,partial,reason", [
    ("provider_rejected", False, "source_provider_rejected"),
    ("transport_failed", False, "source_transport_failed"),
    ("observed", True, "partial_source"),
])
def test_failed_or_partial_source_is_preserved_and_never_automatically_retried(tmp_path, monkeypatch, status, partial, reason):
    calls, _ = _fake(monkeypatch, _plan(), partial=partial, status=status)
    first = collector.collect(_plan(), tmp_path, 8)
    source_path = tmp_path / "bars/AAPL/2024-06-03.json"
    original = source_path.read_bytes()
    assert first["attempted"] == 1 and first["stop_reason"] == reason and len(first["unmet"]) == 8
    second = collector.collect(_plan(), tmp_path, 8)
    assert second["attempted"] == 0 and second["reused"] == 1 and second["stop_reason"] == reason
    assert len(calls) == 1 and source_path.read_bytes() == original


def test_missing_credentials_reports_block_without_network_or_key_values(tmp_path, monkeypatch):
    calls, _ = _fake(monkeypatch, _plan())
    monkeypatch.setattr(collector.source, "availability", lambda: {"configured": False})
    result = collector.collect(_plan(), tmp_path, 8)
    assert result["stop_reason"] == "credentials_missing" and not calls
    assert result["attempted"] == 0 and len(result["unmet"]) == 8
    assert list(tmp_path.glob("manifest-*.json"))


def test_corrupt_existing_hash_stops_without_network_or_overwrite(tmp_path, monkeypatch):
    plan = _plan()
    calls, _ = _fake(monkeypatch, plan)
    item = collector.requests_for_plan(plan)[0]
    broken = {**_envelope(item), "raw_sha256": "0" * 64}
    path = tmp_path / item["path"]
    collector._save_new(path, broken)
    original = path.read_bytes()
    result = collector.collect(plan, tmp_path, 8)
    assert result["stop_reason"] == "cached_source_invalid" and not calls
    assert path.read_bytes() == original


def test_collector_exception_is_sanitized_and_stops(tmp_path, monkeypatch):
    _fake(monkeypatch, _plan())
    def fail(*args):
        raise RuntimeError("private-api-key-echo")
    monkeypatch.setattr(collector.source, "bar_window", fail)
    result = collector.collect(_plan(), tmp_path, 8)
    assert result["stop_reason"] == "source_reader_exception" and result["request_failures"] == 1
    assert "private-api-key-echo" not in (tmp_path / "bars/AAPL/2024-06-03.json").read_text()


def test_quote_plan_reuses_existing_bounds_and_same_serial_budget(tmp_path, monkeypatch):
    plan = {"schema": "intraday-orb-quote-plan-v1", "market": "us", "windows": [
        {"symbol": "AAPL", "start": "2024-06-03T13:30:00Z", "end": "2024-06-03T13:30:05Z"},
        {"symbol": "MSFT", "start": "2024-06-03T13:31:00Z", "end": "2024-06-03T13:31:05Z"}]}
    calls, sleeps = _fake(monkeypatch, plan, quote=True)
    result = collector.collect(plan, tmp_path, 2, quote=True)
    assert result["status"] == "collected" and len(calls) == 2 and sleeps == [0.5]
    assert (tmp_path / "quotes/AAPL/20240603T133000Z_20240603T133005Z.json").exists()
    for changes in ({"symbol": "SPY"}, {"start": "2024-06-03T13:29:59Z"},
                    {"end": "2024-06-03T13:35:01Z"},
                    {"start": "2025-01-02T14:30:00Z", "end": "2025-01-02T14:30:05Z"}):
        invalid = {**plan, "windows": [{**plan["windows"][0], **changes}]}
        with pytest.raises(ValueError):
            collector.requests_for_plan(invalid, quote=True)


def test_required_budget_lock_and_production_refusal(tmp_path, monkeypatch):
    _fake(monkeypatch, _plan())
    for budget in (0, 2401, True):
        with pytest.raises(ValueError):
            collector.collect(_plan(), tmp_path, budget)
    lock = tmp_path / ".collector.lock"
    lock.write_text("another-owner")
    with pytest.raises(FileExistsError):
        collector.collect(_plan(), tmp_path, 8)
    assert lock.read_text() == "another-owner"
    monkeypatch.setenv("APP_ENV", "prod")
    with pytest.raises(ValueError, match="local research"):
        collector.collect(_plan(), tmp_path, 8)


def test_cli_requires_explicit_budget(tmp_path):
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(_plan()))
    with pytest.raises(SystemExit):
        collector.main(["--plan", str(plan_path), "--outdir", str(tmp_path)])


@pytest.mark.parametrize("budget,fail,expected", [(3, False, 3), (8, True, 4)])
def test_parallel_budget_rate_and_failure_drain(tmp_path, monkeypatch, budget, fail, expected):
    real_sleep = time.sleep
    plan = _plan()
    _fake(monkeypatch, plan)
    by_symbol = {item["symbol"]: item for item in collector.requests_for_plan(plan)}
    clock = [100.0]
    monkeypatch.setattr(collector.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(collector.time, "sleep", lambda duration: clock.__setitem__(0, clock[0] + duration))
    barrier = threading.Barrier(expected)
    released = threading.Event()
    starts, active, maximum = [], [0], [0]
    guard = threading.Lock()

    def read(symbol, start, end):
        with guard:
            starts.append(clock[0])
            active[0] += 1
            maximum[0] = max(maximum[0], active[0])
        barrier.wait(timeout=5)
        if fail:
            if symbol == "AAPL":
                released.set()
            else:
                assert released.wait(timeout=5)
                real_sleep(0.02)  # The first rejection is observed before other responses complete.
        with guard:
            active[0] -= 1
        return _envelope(by_symbol[symbol], status="provider_rejected" if fail and symbol == "AAPL" else "observed")

    monkeypatch.setattr(collector.source, "bar_window", read)
    result = collector.collect(plan, tmp_path, budget, workers=4)
    assert result["attempted"] == expected <= budget and maximum[0] <= 4
    assert all(later - earlier >= 0.5 for earlier, later in zip(starts, starts[1:]))
    assert len(list(tmp_path.glob("bars/*/*.json"))) == expected
    assert len(result["sources"]) == expected
    assert result["stop_reason"] == ("source_provider_rejected" if fail else "request_budget_exhausted")
    assert result["request_successes"] == expected - int(fail) and result["request_failures"] == int(fail)


def test_interrupt_stops_new_launches_but_preserves_started_responses_and_manifest(tmp_path, monkeypatch):
    plan = _plan()
    _fake(monkeypatch, plan)
    by_symbol = {item["symbol"]: item for item in collector.requests_for_plan(plan)}
    all_started, released, guard = threading.Event(), threading.Event(), threading.Lock()
    calls = []

    def read(symbol, start, end):
        with guard:
            calls.append(symbol)
            if len(calls) == 4:
                all_started.set()
        assert released.wait(timeout=5)
        return _envelope(by_symbol[symbol])

    def interrupt(*args, **kwargs):
        assert all_started.wait(timeout=5)
        released.set()
        raise KeyboardInterrupt

    monkeypatch.setattr(collector.source, "bar_window", read)
    monkeypatch.setattr(collector, "wait", interrupt)
    result = collector.collect(plan, tmp_path, 8, workers=4)
    assert result["status"] == "blocked" and result["stop_reason"] == "interrupted"
    assert len(calls) == result["attempted"] == result["request_successes"] == 4
    assert len(list(tmp_path.glob("bars/*/*.json"))) == 4 and len(result["unmet"]) == 4
    assert list(tmp_path.glob("manifest-*.json")) and not (tmp_path / ".collector.lock").exists()


def test_atomic_publication_never_replaces_existing_or_leaves_partial_source(tmp_path, monkeypatch):
    target = tmp_path / "source.json"
    collector._save_new(target, {"original": True})
    original = target.read_bytes()
    with pytest.raises(FileExistsError):
        collector._save_new(target, {"replacement": True})
    assert target.read_bytes() == original
    assert list(tmp_path.iterdir()) == [target]

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(collector.os, "link", interrupt)
    interrupted = tmp_path / "interrupted.json"
    with pytest.raises(KeyboardInterrupt):
        collector._save_new(interrupted, {"complete": True})
    assert not interrupted.exists() and list(tmp_path.iterdir()) == [target]
