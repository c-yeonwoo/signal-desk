import json

import pandas as pd
import pytest
from fastapi import HTTPException

from signal_desk.signals import observation_archive, watchlist_learning


def _snapshot(root, session, score, kind, *, reason, blocked=False):
    frame = pd.DataFrame([{
        "ticker": "005930", "market": "kr", "date": session,
        "observed_at": f"{session}T07:00:00+00:00",
        "computed_at": f"{session}T06:59:00+00:00", "signal_policy_id": "same-policy",
        "score": score, "kind": kind, "technical": score / 2, "fundamental": 0.5,
        "bar_asof": session, "data_coverage": 0.8, "low_coverage": False,
        "gate_blocked": blocked, "decision_blocked": False,
        "reasons_json": json.dumps([reason], ensure_ascii=False),
    }])
    return observation_archive.publish(frame, market="kr", session=session, root=root,
                                       captured_at=f"{session}T07:00:00+00:00")


def test_watchlist_uses_only_archived_observations_and_separates_change_from_cause(tmp_path):
    _snapshot(tmp_path, "2026-10-05", 1.0, "HOLD", reason="기록 A")
    _snapshot(tmp_path, "2026-10-06", 1.5, "BUY", reason="기록 B", blocked=True)
    report = watchlist_learning.describe(tmp_path, "kr", "005930")
    assert report["status"] == "comparison"
    assert report["change"]["score_delta"] == 0.5
    assert report["change"]["kind_changed"] is True
    assert report["change"]["new_reasons"] == ["기록 B"]
    assert report["current"]["computed_at"] == "2026-10-06T06:59:00+00:00"
    assert report["current"]["source_time_verified"] is False
    assert report["not_order_advice"] is True
    assert "원인" in report["caveat"] and "주문 허가가 아닙니다" in report["next_checks"][0]


def test_first_observation_and_missing_history_are_not_backfilled(tmp_path):
    assert watchlist_learning.describe(tmp_path, "kr", "005930")["status"] == "not_recorded"
    _snapshot(tmp_path, "2026-10-05", 1.0, "HOLD", reason="기록 A")
    report = watchlist_learning.describe(tmp_path, "kr", "005930")
    assert report["status"] == "first_observation" and report["previous"] is None


def test_corrupt_archive_is_reported_not_silently_replaced(tmp_path):
    manifest = _snapshot(tmp_path, "2026-10-05", 1.0, "HOLD", reason="기록 A")
    path = tmp_path / "kr/2026-10-05" / f"{manifest['snapshot_id']}.parquet"
    path.write_bytes(b"bad")
    report = watchlist_learning.describe(tmp_path, "kr", "005930")
    assert report["status"] == "archive_error"


def test_learning_route_is_limited_to_own_favorites(monkeypatch):
    from signal_desk import api
    monkeypatch.setattr(api, "_uid", lambda _request: 7)
    monkeypatch.setattr(api.db, "fav_list", lambda _uid: [{"kind": "ticker", "key": "AAPL"}])
    with pytest.raises(HTTPException) as exc:
        api.watchlist_learning_get(object(), market="kr", ticker="005930")
    assert exc.value.status_code == 403


def test_private_thesis_is_uid_scoped_and_never_changes_shared_observation(tmp_path, monkeypatch):
    from signal_desk import api, db, store
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    monkeypatch.setattr(store, "SIGNAL_HISTORY_FILE", tmp_path / "signal_history.parquet")
    monkeypatch.setattr(api, "_uid", lambda _request: 7)
    db.fav_add(7, "ticker", "005930", "삼성전자")
    saved = api.watchlist_thesis_put(object(), {
        "market": "kr", "ticker": "005930", "thesis": "수요 회복",
        "invalidates": "수주 감소"})
    assert saved["personal_note"]["thesis"] == "수요 회복"
    assert db.favorite_thesis_get(8, "kr", "005930")["thesis"] == ""
    report = api.watchlist_learning_get(object(), market="kr", ticker="005930")
    assert report["status"] == "not_recorded"
    assert report["personal_note"]["invalidates"] == "수주 감소"
    assert list((tmp_path / "signal_observations").rglob("*.json")) == []
    with pytest.raises(HTTPException) as exc:
        api.watchlist_thesis_put(object(), {"market": "kr", "ticker": "005930",
                                          "thesis": "x" * 1001, "invalidates": ""})
    assert exc.value.status_code == 400
    db.fav_remove(7, "ticker", "005930")
    assert db.favorite_thesis_get(7, "kr", "005930")["thesis"] == ""
