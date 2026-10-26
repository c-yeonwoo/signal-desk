import json

import pytest

from signal_desk import entry_timing_monitor as monitor


def test_packaged_report_has_null_safe_readonly_contract():
    report = monitor.snapshot()
    assert report["status"] == "available"
    assert report["research_only"] and not report["live_order_eligible"]
    assert not report["independent_holdout"] and report["eligible_events"] == 6
    assert len(report["paths"]) == 10
    for timing in ("immediate", "opening"):
        path = report["paths"][timing + ":base"]
        assert path["selected_events"] == 6 and path["closed_trades"] == 6
        assert report["verdicts"][timing] == "not_adopted_performance"
    assert {r["id"] for r in report["paths"]["immediate:base"]["rows"]} == {
        r["id"] for r in report["paths"]["opening:base"]["rows"]}
    assert "raw_b64" not in json.dumps(report)


def test_missing_and_invalid_reports_are_not_zero_returns(tmp_path, monkeypatch):
    path = tmp_path / "report.json"
    monkeypatch.setattr(monitor, "REPORT", path)
    assert monitor.snapshot()["status"] == "not_available"
    path.write_text('{"schema":"broken"}')
    assert monitor.snapshot()["status"] == "invalid_report"
    assert "paths" not in monitor.snapshot()
    path.write_text('{"bad":NaN}')
    assert monitor.snapshot()["status"] == "invalid_report"


def test_admin_auth_precedes_report_read(monkeypatch):
    from signal_desk import api
    monkeypatch.setattr(api, "_admin_or_403", lambda _: (_ for _ in ()).throw(api.HTTPException(403)))
    monkeypatch.setattr(monitor, "snapshot", lambda: pytest.fail("guest read report"))
    with pytest.raises(api.HTTPException) as error:
        api.entry_timing_get(None)
    assert error.value.status_code == 403
    monkeypatch.setattr(api, "_admin_or_403", lambda _: None)
    monkeypatch.setattr(monitor, "snapshot", lambda: {"status":"not_available", "live_order_eligible":False})
    response = api.entry_timing_get(None)
    assert response.headers["cache-control"] == "no-store"
    assert json.loads(response.body)["status"] == "not_available"
