"""The Douzone price disappearance is a non-trading corporate-action case."""

import json
from zipfile import ZipFile

import pytest

from scripts.measure.acquire_historical_sources import SourceError
from scripts.measure import research_action_douzone_lineage as douzone
from scripts.measure.research_action_price_lineage import analyze_price_lineage


def _bar(volume: int) -> dict:
    return {"ISU_CD": "012510", "TDD_OPNPRC": "120000" if volume else "0",
            "TDD_CLSPRC": "120000", "ACC_TRDVOL": str(volume),
            "ACC_TRDVAL": str(120000 * volume)}


def test_douzone_carry_is_not_trade_or_cash_receipt():
    stale = ["2026-06-26", "2026-06-29", "2026-06-30", "2026-07-01",
             "2026-07-02", "2026-07-03", "2026-07-06", "2026-07-07",
             "2026-07-08", "2026-07-09", "2026-07-10", "2026-07-13",
             "2026-07-14"]
    days = {"2026-06-25": [_bar(50926)], **{day: [_bar(0)] for day in stale},
            "2026-07-15": []}
    result = analyze_price_lineage(days, douzone.CASE)
    assert result["last_traded"] == {"session": "2026-06-25",
                                     "close_krw": 120000, "volume": 50926}
    assert result["zero_volume_carry"]["sessions"] == 13
    assert result["first_absent_session"] == "2026-07-15"
    assert result["successor_ticker"] is None
    assert result["total_return_verified"] is False
    days["2026-07-03"][0]["ACC_TRDVOL"] = "1"
    with pytest.raises(ValueError, match="zero-volume"):
        analyze_price_lineage(days, douzone.CASE)


def test_wrong_kind_source_identity_fails_before_event_claim(tmp_path, monkeypatch):
    path = tmp_path / "source.zip"
    with ZipFile(path, "w") as archive:
        archive.writestr("manifest.json", json.dumps({"entries": {}}))
    monkeypatch.setattr(douzone, "verify_action_archive", lambda _: {
        "source": "KRX KIND originals", "sha256": "wrong"})
    with pytest.raises(SourceError, match="frozen KIND archive differs"):
        douzone._verify_kind(path)


def test_wrong_krx_source_identity_fails_before_loading(tmp_path):
    (tmp_path / douzone.KRX_ARCHIVE).write_bytes(b"not the saved KRX archive")
    with pytest.raises(ValueError, match="frozen KRX archive differs"):
        douzone.review(tmp_path)
