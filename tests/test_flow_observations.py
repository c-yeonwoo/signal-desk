import datetime

import pytest

from signal_desk import store
from signal_desk.ingest import naver


def test_daily_flow_versions_are_deduped_and_replayed_by_observation_time(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    instants = iter(datetime.datetime(2026, 9, 24, hour, tzinfo=datetime.timezone.utc)
                    for hour in (0, 1, 2))
    monkeypatch.setattr(store, "_utc_now", lambda: next(instants))
    revision = {"foreign_net": 10}
    monkeypatch.setattr(naver, "investor_flow_series", lambda ticker, days=20: [
        {"date": "2026-09-23", "foreign_net": revision["foreign_net"],
         "inst_net": 5, "volume": 100}])
    universe = [{"ticker": "005930"}]
    assert store.fetch_flows(universe)["005930"]["intensity"] == 0.15
    revision["foreign_net"] = 20
    assert store.fetch_flows(universe)["005930"]["intensity"] == 0.25
    assert store.fetch_flows(universe)["005930"]["intensity"] == 0.25

    archive = store._pd_read_parquet(store.FLOW_OBSERVATIONS_FILE)
    assert len(archive) == 2
    early = store.load_flow_observations_as_of(
        datetime.datetime(2026, 9, 24, 0, 30, tzinfo=datetime.timezone.utc))
    late = store.load_flow_observations_as_of(
        datetime.datetime(2026, 9, 24, 1, 30, tzinfo=datetime.timezone.utc))
    assert early.iloc[0]["foreign_net"] == 10
    assert late.iloc[0]["foreign_net"] == 20
    assert early.iloc[0]["available_at_verified"] == False  # noqa: E712
    assert late.iloc[0]["source_published_at"] is None
    with pytest.raises(ValueError, match="timezone-aware"):
        store.load_flow_observations_as_of(datetime.datetime(2026, 9, 24))


def test_corrupt_flow_archive_fails_closed_without_replacing_live_aggregate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(naver, "investor_flow_series", lambda ticker, days=20: [
        {"date": "2026-09-23", "foreign_net": 10, "inst_net": 5, "volume": 100}])
    store.fetch_flows([{"ticker": "005930"}])
    before = store.FLOWS_FILE.read_bytes()
    store.FLOW_OBSERVATIONS_FILE.write_bytes(b"not parquet")
    with pytest.raises(Exception):
        store.fetch_flows([{"ticker": "005930"}])
    assert store.FLOW_OBSERVATIONS_FILE.read_bytes() == b"not parquet"
    assert store.FLOWS_FILE.read_bytes() == before
