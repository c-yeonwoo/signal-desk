"""Archive integrity and optional fields; synthetic data only."""
import io
import json
from zipfile import ZipFile

import pandas as pd
import pytest

from scripts.measure import kr_selection_source_audit as audit


def archive(tmp_path, *, corrupt=False):
    signals = pd.DataFrame([{"date": "2024-10-02", "ticker": "A"},
                            {"date": "2024-10-03", "ticker": "A"}])
    prices = pd.DataFrame([{"date": "2024-10-02", "ticker": "A", "close": 100}])
    sb, pb = io.BytesIO(), io.BytesIO()
    signals.to_parquet(sb, index=False)
    prices.to_parquet(pb, index=False)
    manifest = {"schema": "historical-inputs-v1", "market": "kr",
        "signals_sha256": audit.f.sha(sb.getvalue()), "prices_sha256": audit.f.sha(pb.getvalue())}
    if corrupt:
        manifest["signals_sha256"] = "bad"
    path = tmp_path / "input.zip"
    with ZipFile(path, "w") as z:
        z.writestr("manifest.json", json.dumps(manifest))
        z.writestr("signals.parquet", sb.getvalue())
        z.writestr("prices.parquet", pb.getvalue())
    return path


def test_absent_columns_and_holidays_are_not_invented(tmp_path):
    result = audit.recorded_inventory(archive(tmp_path))
    assert result["regular_session_rows"] == 1 and result["non_session_rows"] == 1
    assert result["observed_at_present"] == result["bar_asof_present"] == 0
    assert "flow" in result["missing_columns"]
    assert result["factor_output_present"]["flow"] == 0
    assert result["protected_performance_aggregates"] is False


def test_mutated_archive_member_refuses_inventory(tmp_path):
    with pytest.raises(ValueError, match="member_hash_differs"):
        audit.recorded_inventory(archive(tmp_path, corrupt=True))
