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


def test_pilot_refuses_registered_dates_before_scoring():
    with pytest.raises(ValueError, match="registered period"):
        pilot.run_pilot({}, start="2026-08-05", end="2026-08-05")
