"""Immutable observation contract: preservation is not proof of strict PIT availability."""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from signal_desk.signals import observation_archive as archive


def _frame(score=1.0):
    return pd.DataFrame([{"ticker": "005930", "date": "2026-10-05", "market": "kr",
                          "observed_at": "2026-10-05T07:00:00+00:00", "score": score,
                          "bar_asof": "2026-10-05", "data_coverage": 0.8}])


def test_same_observation_is_idempotent_and_changed_one_does_not_overwrite(tmp_path):
    root = tmp_path / "observations"
    first = archive.publish(_frame(), market="kr", session="2026-10-05", root=root,
                            captured_at="2026-10-05T07:00:00+00:00")
    repeated = archive.publish(_frame(), market="kr", session="2026-10-05", root=root,
                               captured_at="2026-10-05T07:00:00+00:00")
    changed = archive.publish(_frame(2.0), market="kr", session="2026-10-05", root=root,
                              captured_at="2026-10-05T07:00:00+00:00")
    assert first == repeated
    assert changed["snapshot_id"] != first["snapshot_id"]
    assert len(list(root.rglob("*.parquet"))) == 2
    assert archive.verify(root / "kr/2026-10-05" / f"{first['snapshot_id']}.json") == first
    assert first["strict_pit_eligible"] is False


def test_retrospective_observation_never_becomes_forward_pit(tmp_path):
    manifest = archive.publish(_frame(), market="kr", session="2026-10-05", root=tmp_path,
                               captured_at="2026-10-07T07:00:00+00:00")
    assert manifest["timing"] == "retrospective_or_delayed"
    assert manifest["source_available_at_verified"] is False


def test_corrupt_artifact_is_rejected_not_repaired(tmp_path):
    manifest = archive.publish(_frame(), market="kr", session="2026-10-05", root=tmp_path,
                               captured_at="2026-10-05T07:00:00+00:00")
    path = tmp_path / "kr/2026-10-05" / f"{manifest['snapshot_id']}.parquet"
    path.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="checksum"):
        archive.verify(path.with_suffix(".json"))


def test_mixed_or_duplicate_population_is_rejected(tmp_path):
    mixed = pd.concat([_frame(), _frame()])
    with pytest.raises(ValueError, match="mixed or duplicate"):
        archive.publish(mixed, market="kr", session="2026-10-05", root=tmp_path,
                        captured_at="2026-10-05T07:00:00+00:00")


def test_dataset_freeze_is_deterministic_and_refuses_unverified_training(tmp_path):
    root = tmp_path / "signal_observations"
    manifest = archive.publish(_frame(), market="kr", session="2026-10-05", root=root,
                               captured_at="2026-10-05T07:00:00+00:00")
    path = root / "kr/2026-10-05" / f"{manifest['snapshot_id']}.json"
    output = tmp_path / "signal_datasets"
    first = archive.freeze_dataset([path], output_root=output)
    assert first == archive.freeze_dataset([path], output_root=output)
    assert first["observed_rows"] == 1
    assert first["strict_pit_rows"] == 0
    assert first["exclusion_reasons"] == {"source_time_unverified": 1}
    assert first["label_spec"] is None and first["model_training_allowed"] is False
    with pytest.raises(ValueError, match="multiple versions"):
        archive.freeze_dataset([path, path], output_root=output)


def test_readiness_counts_sessions_not_independent_rows(tmp_path):
    root = tmp_path / "signal_observations"
    archive.publish(_frame(), market="kr", session="2026-10-05", root=root,
                    captured_at="2026-10-05T07:00:00+00:00")
    archive.publish(_frame(2), market="kr", session="2026-10-05", root=root,
                    captured_at="2026-10-05T08:00:00+00:00")
    report = archive.readiness(root)
    assert report["markets"]["kr"]["observations"] == 2
    assert report["markets"]["kr"]["distinct_sessions"] == 1
    assert report["markets"]["kr"]["strict_pit_eligible"] == 0
    assert report["matured_label_spec"] is None and report["decision"] == "defer"


def test_store_snapshot_preserves_computation_time_and_applied_policy(tmp_path, monkeypatch):
    from signal_desk import store
    from signal_desk.signals.engine import SignalResult
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(store, "SIGNAL_HISTORY_FILE", tmp_path / "signal_history.parquet")
    signal = SignalResult(ticker="005930", name="삼성전자", score=1.0, kind="HOLD",
                          confidence=0.5, technical_score=0.0, fundamental_score=0.0,
                          has_fundamental=False)
    signal.computed_at = datetime.now(timezone.utc).isoformat()
    signal.signal_policy_id = "policy-test"
    store.snapshot_signals([signal], date="2026-10-05")
    manifests = list((tmp_path / "signal_observations").rglob("*.json"))
    assert len(manifests) == 1
    manifest, frame = archive.read_verified(manifests[0])
    assert manifest["signal_policy_ids"] == ["policy-test"]
    assert manifest["missing_computed_at"] == 0
    assert frame.iloc[0]["computed_at"] == signal.computed_at
