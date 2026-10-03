"""Official evidence refresh stays bounded, repeat-safe, and outside scoring."""

import datetime as dt
import json

import pytest

from signal_desk.ingest import financial_evidence as evidence
from signal_desk.ingest import financial_refresh as refresh


NOW = dt.datetime(2026, 10, 3, 18, tzinfo=dt.timezone(dt.timedelta(hours=9)))
CORPS = {f"{i:06d}": f"{i:08d}" for i in range(1, 8)}


@pytest.mark.parametrize("month,expected", [
    (2, ("2025", "11014")), (4, ("2025", "11011")),
    (6, ("2026", "11013")), (9, ("2026", "11012")),
    (12, ("2026", "11014")),
])
def test_conservative_disclosure_window(month, expected):
    assert refresh.report_window(NOW.replace(month=month).date()) == expected


def test_plan_deduplicates_issuers_and_caps_daily_requests(tmp_path):
    codes = {**CORPS, "999999": CORPS["000001"]}
    targets = refresh.plan(tmp_path / "e.db", [*codes, "BAD", "12345"], codes,
                           now=NOW, last_attempt=lambda _: None)
    assert len(targets) == refresh.MAX_REQUESTS_PER_DAY
    assert len({target.issuer for target in targets}) == refresh.MAX_ISSUERS_PER_DAY
    assert all(target.report == "11012" and target.basis == "CFS" for target in targets)
    assert set(target.year for target in targets) == {"2026", "2025"}


def test_failures_cool_down_and_do_not_block_other_targets(tmp_path):
    kv = {}
    seen = []

    def collect(_path, target, **_kwargs):
        seen.append(target)
        if target.year == "2026":
            raise RuntimeError("upstream failure")
        return {"status": "no_data", "response_bytes": 12}

    opts = dict(now=NOW, dart_key="test-key", attempt_get=kv.get,
                attempt_set=kv.__setitem__, collector=collect)
    first = refresh.run(tmp_path / "e.db", ["000001"], CORPS, **opts)
    assert first == {"status": "partial_failure", "requested": 2, "ok": 0,
                     "no_data": 1, "failed": 1, "response_bytes": 12, "at": NOW.isoformat()}
    assert refresh.run(tmp_path / "e.db", ["000001"], CORPS, **opts)["requested"] == 0
    assert len(seen) == 2
    assert refresh.run(tmp_path / "e.db", ["000001"], CORPS,
                       **{**opts, "now": NOW + dt.timedelta(days=3)})["requested"] == 2


def test_daily_budget_persists_across_runs_and_new_favorites(tmp_path):
    state = {}
    seen = []

    def collect(_path, target, **_kwargs):
        seen.append(target)
        return {"status": "no_data", "response_bytes": 3}

    opts = dict(now=NOW, dart_key="test-key", attempt_get=state.get,
                attempt_set=state.__setitem__, collector=collect)
    first = refresh.run(tmp_path / "e.db", list(CORPS)[:4], CORPS, **opts)
    assert first["requested"] == 8
    assert state[refresh.budget_key(NOW.date())] == 8
    second = refresh.run(tmp_path / "e.db", list(CORPS)[4:], CORPS, **opts)
    assert second["status"] == "budget_exhausted"
    assert second["requested"] == 0 and len(seen) == 8


def test_corrupt_daily_budget_fails_closed(tmp_path):
    key = refresh.budget_key(NOW.date())
    assert refresh.run(tmp_path / "e.db", ["000001"], CORPS, now=NOW,
                       dart_key="test-key", attempt_get=lambda k: "broken" if k == key else None,
                       attempt_set=lambda *_: None)["status"] == "budget_exhausted"


def test_archived_no_data_and_prior_success_have_separate_ttls(tmp_path, monkeypatch):
    path = tmp_path / "e.db"
    day = NOW - dt.timedelta(days=8)
    monkeypatch.setattr(evidence, "_now", lambda: day.isoformat())
    current = evidence.Target("dart", CORPS["000001"], "2026", "11012")
    prior = evidence.Target("dart", CORPS["000001"], "2025", "11012")
    evidence.archive(path, current, json.dumps({"status": "013"}).encode(), observed_at=day.isoformat())
    evidence.archive(path, prior, json.dumps({"status": "013"}).encode(), observed_at=day.isoformat())
    due = refresh.plan(path, ["000001"], CORPS, now=NOW, last_attempt=lambda _: None)
    assert due == [current]  # 7-day no-data retry; prior report waits 180 days


def test_missing_credentials_never_plans_or_requests(tmp_path):
    def bomb(_):
        raise AssertionError("must not read state")

    assert refresh.run(tmp_path / "e.db", ["000001"], CORPS, now=NOW, dart_key="",
                       attempt_get=bomb, attempt_set=lambda *_: None)["status"] == "missing_credentials"


def test_daily_api_refresh_reports_missing_key_and_empty_favorites(monkeypatch):
    from signal_desk import api

    state = {}
    monkeypatch.setattr(api.db, "kv_set", state.__setitem__)
    monkeypatch.setattr(api.config, "dart_key", lambda: None)
    monkeypatch.setattr(api.db, "uids_with_ticker_favorites", lambda: (_ for _ in ()).throw(
        AssertionError("missing key must not inspect users")))
    api._refresh_financial_evidence_daily()
    assert state["financial_evidence_refresh_last"]["status"] == "missing_credentials"

    monkeypatch.setattr(api.config, "dart_key", lambda: "test-key")
    monkeypatch.setattr(api.db, "uids_with_ticker_favorites", lambda: [])
    monkeypatch.setattr(api, "_corp_codes", lambda: (_ for _ in ()).throw(
        AssertionError("no favorites must not fetch corp codes")))
    api._refresh_financial_evidence_daily()
    assert state["financial_evidence_refresh_last"]["status"] == "no_favorites"


def test_daily_api_refresh_uses_only_ticker_favorites(monkeypatch, tmp_path):
    from signal_desk import api
    from signal_desk.ingest import financial_refresh
    from signal_desk.signals import financial_change

    state = {}
    seen = {}
    monkeypatch.setattr(api.config, "dart_key", lambda: "test-key")
    monkeypatch.setattr(api.db, "uids_with_ticker_favorites", lambda: [1, 2])
    monkeypatch.setattr(api.db, "fav_list", lambda uid: [
        {"kind": "ticker", "key": "000001"}, {"kind": "sector", "key": "000002"}])
    monkeypatch.setattr(api.db, "kv_get", state.get)
    monkeypatch.setattr(api.db, "kv_set", state.__setitem__)
    monkeypatch.setattr(api, "_corp_codes", lambda: CORPS)
    monkeypatch.setattr(api, "_kst_now", lambda: NOW)
    monkeypatch.setattr(financial_change, "DEFAULT_ARCHIVE", tmp_path / "e.db")
    monkeypatch.setattr(financial_refresh, "run", lambda path, favorites, codes, **kwargs:
                        (seen.update(path=path, favorites=favorites, codes=codes, **kwargs),
                         {"status": "ok", "requested": 0})[1])
    api._refresh_financial_evidence_daily()
    assert seen["favorites"] == ["000001"]
    assert seen["path"] == tmp_path / "e.db"
    assert state["financial_evidence_refresh_last"] == {"status": "ok", "requested": 0}
