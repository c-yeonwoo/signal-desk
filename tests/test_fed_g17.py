"""Fed G.17 industry evidence remains observed, bounded and research-only."""

import datetime as dt
import sqlite3

import pytest

from signal_desk.ingest import fed_g17 as g17


NOW = dt.datetime(2026, 10, 3, 12, tzinfo=dt.timezone.utc)


def raw(*, last="190.0987"):
    previous = " ".join(f"{169 + month:.4f}" for month in range(1, 13))
    current = " ".join(["176.7104", "176.1804", "173.6932", "176.6948",
                        "184.1891", "187.5187", "190.2607", last])
    return (f'"X: unrelated"\n"X" 2025 1.0000\n{g17.HEADING}\n'
            f'"G3344" 2025 {previous}\n"G3344" 2026 {current}\n'
            '"NEXT: other group"\n"NEXT" 2026 999.0000\n').encode()


def test_exact_official_series_and_us_only_scope():
    months = g17.parse(raw(), as_of=NOW.date())
    assert months[-1] == {"period": "2026-08", "value": "190.0987"}
    assert len(months) == 20


@pytest.mark.parametrize("bad", [
    lambda body: body.replace(g17.HEADING.encode(), b'"G3344: different scope"'),
    lambda body: body.replace(b'"G3344" 2026', b'"G3344" 2027'),
    lambda body: body.replace(b"190.0987", b"NaN"),
    lambda body: body.replace(b'"G3344" 2025', b'"G3344" 2026'),
])
def test_bad_source_identity_period_or_value_fails_closed(bad):
    with pytest.raises(ValueError):
        g17.parse(bad(raw()), as_of=NOW.date())


def test_archive_is_observation_not_backfilled_pit_and_keeps_revisions(tmp_path, monkeypatch):
    path = tmp_path / "industry.db"
    monkeypatch.setattr(g17, "_now", lambda: NOW.isoformat())
    old = g17.archive(path, raw(), observed_at=NOW.isoformat())
    assert g17.latest(path, as_of="2026-10-02T23:59:59Z") is None
    changed = g17.archive(path, raw(last="191.0987"),
                          observed_at=(NOW + dt.timedelta(days=1)).isoformat())
    assert old["id"] != changed["id"]
    assert g17.latest(path, as_of=NOW.isoformat())["months"][-1]["value"] == "190.0987"
    assert g17.latest(path, as_of=(NOW + dt.timedelta(days=1)).isoformat())["months"][-1]["value"] == "191.0987"
    assert g17.latest(path, as_of=(NOW + dt.timedelta(days=1)).isoformat())["strict_pit_eligible"] is False


def test_corrupt_archive_does_not_render_as_real_value(tmp_path, monkeypatch):
    path = tmp_path / "industry.db"
    monkeypatch.setattr(g17, "_now", lambda: NOW.isoformat())
    g17.archive(path, raw(), observed_at=NOW.isoformat())
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE fed_g17_observations SET raw_zlib=?", (b"bad",))
    assert g17.describe(path, as_of=NOW.isoformat())["status"] == "archive_error"


def test_read_only_card_separates_index_change_from_stock_advice(tmp_path, monkeypatch):
    path = tmp_path / "industry.db"
    monkeypatch.setattr(g17, "_now", lambda: NOW.isoformat())
    g17.archive(path, raw(), observed_at=NOW.isoformat())
    card = g17.describe(path, as_of=NOW.isoformat())
    assert card["status"] == "ready" and card["period"] == "2026-08"
    assert card["mom_pct"] == -0.1 and card["yoy_pct"] is not None
    assert card["source_published_at"] is None
    assert not card["strict_pit_eligible"] and not card["live_eligible"]
    assert "미국" in card["scope"] and "개별 종목" in card["note"]
    assert g17.describe(path, as_of="2026-10-02T00:00:00Z")["status"] == "not_recorded"
    stale = g17.describe(path, as_of="2026-12-20T00:00:00Z")
    assert stale["status"] == "stale" and stale["index"] == card["index"]


def test_weekly_retry_and_monthly_budget_survive_restart(tmp_path):
    state = {}
    calls = []

    def failure(_path):
        calls.append("request")
        return {"status": "collection_failed", "response_bytes": 10}

    for day in (3, 10, 17, 24):
        result = g17.refresh(tmp_path / "industry.db", now=NOW.replace(day=day),
                             state_get=state.get, state_set=state.__setitem__, collector=failure)
        assert result["requested"] == 1
    assert state["fed_g17_requests:2026-10"] == 4
    assert g17.refresh(tmp_path / "industry.db", now=NOW.replace(day=31),
                       state_get=state.get, state_set=state.__setitem__,
                       collector=failure)["status"] == "budget_exhausted"
    assert len(calls) == 4


def test_success_waits_seven_days_and_bad_budget_fails_closed(tmp_path):
    state = {}
    collect = lambda _path: {"status": "ok", "response_bytes": 5}
    opts = dict(state_get=state.get, state_set=state.__setitem__, collector=collect)
    assert g17.refresh(tmp_path / "industry.db", now=NOW, **opts)["status"] == "ok"
    assert g17.refresh(tmp_path / "industry.db", now=NOW + dt.timedelta(days=3), **opts)["status"] == "not_due"
    state["fed_g17_requests:2026-10"] = "broken"
    assert g17.refresh(tmp_path / "industry.db", now=NOW + dt.timedelta(days=8), **opts)["status"] == "budget_exhausted"


def test_api_read_does_not_fetch_network(tmp_path, monkeypatch):
    from signal_desk import api

    monkeypatch.setattr(g17, "DEFAULT_ARCHIVE", tmp_path / "missing.db")
    monkeypatch.setattr(g17, "collect", lambda *_: (_ for _ in ()).throw(AssertionError("network")))
    assert api.industry_pulse_get()["status"] == "not_recorded"


def test_oversized_response_fails_without_archive_or_sensitive_error(tmp_path, monkeypatch):
    class Response:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self, _limit):
            return b"x" * (g17.MAX_BYTES + 1)

    monkeypatch.setattr(g17, "_open", lambda: Response())
    path = tmp_path / "industry.db"
    result = g17.collect(path)
    assert result == {"status": "collection_failed", "failure_stage": "validate_and_archive",
                      "failure_type": "ValueError", "response_bytes": g17.MAX_BYTES + 1}
    assert not path.exists()
