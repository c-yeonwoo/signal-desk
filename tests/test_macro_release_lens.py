"""경제 발표 렌즈의 발표 전 예상치 동결·실제값 시점·연구 경계를 검증한다."""

import datetime as dt

import pytest

from signal_desk import db
from signal_desk.signals import lenses, macro_release


NOW = dt.datetime(2026, 10, 13, 12, 0, tzinfo=dt.timezone.utc)
SCHEDULED = "2026-10-13T12:30:00+00:00"


def _forecast(**extra):
    return {"metric": "us_cpi_mom_sa_pct", "period": "2026-09",
            "scheduled_at": SCHEDULED, "expected_value": 0.3,
            "forecast_source_url": "https://example.com/consensus", **extra}


def _actual(**extra):
    return {"actual_value": 0.4, "source_published_at": SCHEDULED,
            "actual_source_url": "https://www.bls.gov/news.release/cpi.htm",
            "source_checked": True, **extra}


def test_forecast_must_be_frozen_before_release_and_is_immutable(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    forecast = macro_release.validate_forecast(_forecast(), observed_at=NOW)
    assert db.macro_release_forecast_add(forecast) is True
    assert db.macro_release_forecast_add({**forecast, "expected_value": 99}) is False
    saved = db.macro_release_get(forecast["id"])
    assert saved["expected_value"] == 0.3
    with pytest.raises(ValueError, match="이후"):
        macro_release.validate_forecast(_forecast(), observed_at=NOW + dt.timedelta(hours=1))


def test_actual_requires_official_source_and_ordered_timestamps(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    forecast = macro_release.validate_forecast(_forecast(), observed_at=NOW)
    db.macro_release_forecast_add(forecast)
    observed = NOW + dt.timedelta(hours=1)
    with pytest.raises(ValueError, match="공식"):
        macro_release.validate_actual(_actual(actual_source_url="https://example.com/cpi"),
                                      forecast, observed_at=observed)
    with pytest.raises(ValueError, match="대조"):
        macro_release.validate_actual(_actual(source_checked=False), forecast, observed_at=observed)
    with pytest.raises(ValueError, match="순서"):
        macro_release.validate_actual(_actual(), forecast, observed_at=NOW)
    actual = macro_release.validate_actual(_actual(), forecast, observed_at=observed)
    db.macro_release_actual_add(actual)
    saved = db.macro_release_get(forecast["id"])
    assert saved["actual_value"] == 0.4
    assert macro_release.evaluate(saved, as_of=observed)["surprise_pp"] == 0.1


def test_no_future_or_stale_macro_signal_and_no_live_promotion():
    frozen = macro_release.validate_forecast(_forecast(), observed_at=NOW)
    observed = NOW + dt.timedelta(hours=1)
    actual = macro_release.validate_actual(_actual(), frozen, observed_at=observed)
    release = {**frozen, **actual}
    row = {"ticker": "AAA", "name": "A", "kind": "BUY", "score": 1.0, "price": 100.0,
           "data_coverage": 0.9, "factor_scores": {"momentum": 1.0}}
    rows = [dict(row)]
    snap = lenses.build_snapshot(rows, market="kr", signal_policy_id="p",
                                 dates_by={"AAA": ["2026-10-13"]}, macro_releases=[release],
                                 observed_at=int((observed + dt.timedelta(hours=1)).timestamp()))
    assert rows[0]["lens_results"]["macro_release"]["verdict"] == "hold"
    assert rows[0]["lens_results"]["macro_release"]["research_only"] is True
    assert snap["order_eligible"] is False and rows[0]["kind"] == "BUY"
    stale_rows = [dict(row)]
    lenses.build_snapshot(stale_rows, market="kr", signal_policy_id="p",
                          macro_releases=[release], observed_at=int((observed + dt.timedelta(days=4)).timestamp()))
    assert stale_rows[0]["lens_results"]["macro_release"]["verdict"] == "unavailable"
    future_rows = [dict(row)]
    lenses.build_snapshot(future_rows, market="kr", signal_policy_id="p",
                          macro_releases=[release], observed_at=int(NOW.timestamp()))
    assert future_rows[0]["lens_results"]["macro_release"]["verdict"] == "unavailable"
