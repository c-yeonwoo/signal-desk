import datetime as dt
import json
import sqlite3

from signal_desk.ingest import sia_market as sia


RELEASE = {
    "id": 31603,
    "date_gmt": "2026-10-05T12:00:17",
    "slug": "year-to-date-global-semiconductor-sales-top-1-trillion-through-august",
    "link": "https://www.semiconductors.org/year-to-date-global-semiconductor-sales-top-1-trillion-through-august/",
    "title": {"rendered": "Year-to-Date Global Semiconductor Sales Top $1 Trillion Through August"},
    "categories": [3],
    "content": {"rendered": "<p>Global semiconductor sales were $159.7 billion during the month of August 2026, "
        "an increase of 8% compared to the July 2026 total of $147.9 billion and 144.3% more than "
        "the August 2025 total of $65.4 billion. Monthly sales represent a three-month moving average.</p>"},
}
NOW = dt.datetime(2026, 10, 8, 3, tzinfo=dt.timezone.utc)


def test_parse_extracts_only_public_release_facts_and_gmt_timestamp():
    facts = sia.parse(json.dumps([RELEASE]).encode(), observed_at=NOW)
    assert len(facts) == 1
    fact = facts[0]
    assert fact["period"] == "2026-08"
    assert fact["sales_usd_billions"] == 159.7
    assert fact["mom_pct"] == 8
    assert fact["prior_month_sales_usd_billions"] == 147.9
    assert fact["yoy_pct"] == 144.3
    assert fact["year_ago_sales_usd_billions"] == 65.4
    assert fact["published_at"] == "2026-10-05T12:00:17+00:00"
    assert "Monthly sales represent" not in json.dumps(fact)


def test_parse_fails_closed_on_bad_comparison_period_or_external_host():
    bad_month = json.loads(json.dumps(RELEASE))
    bad_month["content"]["rendered"] = bad_month["content"]["rendered"].replace("July 2026", "June 2026")
    external = json.loads(json.dumps(RELEASE))
    external["link"] = "https://example.org/year-to-date-global-semiconductor-sales-top-1-trillion-through-august/"
    future = json.loads(json.dumps(RELEASE))
    future["date_gmt"] = "2026-10-09T12:00:00"
    assert sia.parse(json.dumps([bad_month, external, future]).encode(), observed_at=NOW) == []


def test_archive_is_compact_append_only_and_readable_as_of(tmp_path):
    facts = sia.parse(json.dumps([RELEASE]).encode(), observed_at=NOW)
    path = tmp_path / "sia.db"
    first = sia.archive(path, facts, observed_at=NOW)
    again = sia.archive(path, facts, observed_at=NOW + dt.timedelta(days=3))
    assert first["observations_added"] == 1
    assert again["observations_added"] == 0
    assert sia.describe(path, as_of=NOW)["sales_usd_billions"] == 159.7
    assert sia.latest(path, as_of=NOW - dt.timedelta(days=1)) is None
    with sqlite3.connect(path) as conn:
        material = conn.execute("SELECT envelope FROM sia_market_observations").fetchone()[0]
    assert b"Monthly sales represent" not in material
    assert b'"strict_pit_eligible":false' in material


def test_archive_keeps_only_newest_release_when_feed_contains_history(tmp_path):
    older = json.loads(json.dumps(RELEASE))
    newer = json.loads(json.dumps(RELEASE))
    newer["id"] = 31699
    newer["date_gmt"] = "2026-11-05T12:00:17"
    newer["slug"] = "year-to-date-global-semiconductor-sales-through-september"
    newer["link"] = "https://www.semiconductors.org/year-to-date-global-semiconductor-sales-through-september/"
    newer["title"] = {"rendered": "Global Semiconductor Sales Through September"}
    newer["content"] = {"rendered": "<p>Global semiconductor sales were $168.0 billion during the month of September 2026, "
        "an increase of 5.2% compared to the August 2026 total of $159.7 billion and 130% more than "
        "the September 2025 total of $73.0 billion. Monthly sales are a three-month moving average.</p>"}
    later = dt.datetime(2026, 11, 6, tzinfo=dt.timezone.utc)
    facts = sia.parse(json.dumps([older, newer]).encode(), observed_at=later)
    path = tmp_path / "sia.db"
    result = sia.archive(path, facts, observed_at=later)
    assert result["observations_added"] == 1
    assert sia.describe(path, as_of=later)["period"] == "2026-09"
    assert result["ids"] == [sia.latest(path, as_of=later)["id"]]


def test_refresh_uses_three_day_cooldown_and_atomic_monthly_budget(tmp_path):
    state, calls, budget = {}, [], []
    def reserve(key):
        budget.append(key)
        return True
    def collector(path, *, now):
        calls.append((path, now))
        return {"status": "ok", "requested": 1, "ok": 1, "failed": 0, "response_bytes": 20}
    result = sia.refresh(tmp_path / "sia.db", now=NOW, state_get=state.get,
                         state_set=state.__setitem__, reserve=reserve, collector=collector)
    assert result["status"] == "ok" and len(calls) == 1
    next_day = sia.refresh(tmp_path / "sia.db", now=NOW + dt.timedelta(days=1), state_get=state.get,
                           state_set=state.__setitem__, reserve=reserve, collector=collector)
    assert next_day["status"] == "not_due" and len(calls) == 1 and len(budget) == 1
    assert budget == ["sia_market_requests:2026-10"]


def test_describe_never_marks_public_release_as_trading_or_pit_eligible(tmp_path):
    facts = sia.parse(json.dumps([RELEASE]).encode(), observed_at=NOW)
    sia.archive(tmp_path / "sia.db", facts, observed_at=NOW)
    result = sia.describe(tmp_path / "sia.db", as_of=NOW)
    assert result["mode"] == "read_only_research"
    assert result["live_eligible"] is False
    assert result["strict_pit_eligible"] is False


def test_api_endpoint_reads_saved_release_without_collecting(monkeypatch, tmp_path):
    from signal_desk import api
    monkeypatch.setattr(sia, "DEFAULT_ARCHIVE", tmp_path / "absent.db")
    result = api.industry_pulse_sia_get()
    assert result["status"] == "not_recorded"
    assert result["live_eligible"] is False


def test_corrupt_non_object_archive_fails_closed_instead_of_raising(tmp_path):
    path = tmp_path / "corrupt.db"
    conn = sia._connect(path)
    try:
        with conn:
            conn.execute("INSERT INTO sia_market_observations VALUES (?,?,?)",
                         ("bad-id", NOW.isoformat(), b"[]"))
    finally:
        conn.close()
    result = sia.describe(path, as_of=NOW)
    assert result["status"] == "archive_error"
