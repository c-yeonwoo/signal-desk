"""Forward-only evidence archive alongside the mutable signal-history compatibility file.

An archive is an observation, not a claim that every upstream source was available at
the decision time. In particular, legacy or retrospective snapshots remain explicitly
unverified and cannot silently become strict PIT training data.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

SCHEMA = "signal-observation-v1"
DATASET_SCHEMA = "signal-dataset-manifest-v1"


def _canonical(frame: pd.DataFrame) -> bytes:
    rows = json.loads(frame.sort_values("ticker").to_json(orient="records", date_format="iso",
                                                   double_precision=15))
    return json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _exclusive_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=".manifest-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
        try:
            os.link(temp_name, path)
        except FileExistsError:
            if path.read_bytes() != content:
                raise ValueError(f"immutable archive collision: {path.name}") from None
    finally:
        Path(temp_name).unlink(missing_ok=True)


def publish(frame: pd.DataFrame, *, market: str, session: str, root: Path,
            captured_at: str) -> dict:
    """Publish a content-addressed snapshot before the mutable PIT view is replaced."""
    if market not in {"kr", "us"} or date.fromisoformat(session).isoformat() != session:
        raise ValueError("invalid market or session")
    if frame.empty or not {"ticker", "date", "market", "observed_at"} <= set(frame.columns):
        raise ValueError("incomplete signal snapshot")
    if len(frame) != frame["ticker"].nunique() or not (frame["date"] == session).all() or not (frame["market"] == market).all():
        raise ValueError("mixed or duplicate signal snapshot")
    material = _canonical(frame)
    logical_hash = hashlib.sha256(material).hexdigest()
    directory = root / market / session
    directory.mkdir(parents=True, exist_ok=True)
    artifact = directory / f"{logical_hash}.parquet"
    if not artifact.exists():
        descriptor, temp_name = tempfile.mkstemp(prefix=".snapshot-", suffix=".parquet", dir=directory)
        os.close(descriptor)
        try:
            frame.to_parquet(temp_name, index=False)
            try:
                os.link(temp_name, artifact)  # no replacement even under concurrent publishers
            except FileExistsError:
                pass
        finally:
            Path(temp_name).unlink(missing_ok=True)
    if hashlib.sha256(_canonical(pd.read_parquet(artifact))).hexdigest() != logical_hash:
        raise ValueError("immutable snapshot content mismatch")
    file_hash = hashlib.sha256(artifact.read_bytes()).hexdigest()
    # Historical re-runs are recorded, but their old session date does not prove PIT.
    observed_date = datetime.fromisoformat(captured_at).astimezone(
        ZoneInfo("Asia/Seoul" if market == "kr" else "America/New_York")).date()
    timing = "observed_session" if observed_date.isoformat() == session else "retrospective_or_delayed"
    computed = (pd.to_datetime(frame["computed_at"], utc=True, errors="coerce")
                if "computed_at" in frame else pd.Series([pd.NaT] * len(frame)))
    computed_valid = computed.dropna()
    policy_ids = (sorted(set(frame["signal_policy_id"].dropna().astype(str)))
                  if "signal_policy_id" in frame else [])
    manifest = {
        "schema": SCHEMA, "snapshot_id": logical_hash, "market": market, "session": session,
        "captured_at": captured_at, "timing": timing, "rows": len(frame),
        "tickers_sha256": hashlib.sha256("\n".join(sorted(frame["ticker"].astype(str))).encode()).hexdigest(),
        "artifact_sha256": file_hash, "source_available_at_verified": False,
        "strict_pit_eligible": False, "engine_reference": "unversioned-legacy-engine",
        "signal_policy_ids": policy_ids,
        "missing_computed_at": int(computed.isna().sum()),
        "first_computed_at": computed_valid.min().isoformat() if not computed_valid.empty else None,
        "last_computed_at": computed_valid.max().isoformat() if not computed_valid.empty else None,
        "computed_after_capture": (int((computed_valid > pd.Timestamp(captured_at)).sum())
                                   if not computed_valid.empty else 0),
        "missing_bar_asof": int(frame["bar_asof"].isna().sum()) if "bar_asof" in frame else len(frame),
        "missing_coverage": int(frame["data_coverage"].isna().sum()) if "data_coverage" in frame else len(frame),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    manifest_path = directory / f"{logical_hash}.json"
    # A repeated identical observation must retain the original immutable manifest.
    if manifest_path.exists():
        saved = json.loads(manifest_path.read_text(encoding="utf-8"))
        if saved.get("snapshot_id") != logical_hash or saved.get("artifact_sha256") != file_hash:
            raise ValueError("immutable manifest mismatch")
        return saved
    try:
        _exclusive_bytes(manifest_path, json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                                                  indent=2).encode("utf-8"))
        return manifest
    except ValueError:
        # A concurrent publisher may have won with a different created_at.
        saved = json.loads(manifest_path.read_text(encoding="utf-8"))
        if saved.get("snapshot_id") != logical_hash or saved.get("artifact_sha256") != file_hash:
            raise
        return saved


def read_verified(manifest_path: Path) -> tuple[dict, pd.DataFrame]:
    """Read manifest and rows without repair or silent deletion."""
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != SCHEMA or manifest_path.stem != manifest.get("snapshot_id"):
        raise ValueError("unsupported or misplaced observation manifest")
    artifact = manifest_path.with_suffix(".parquet")
    if hashlib.sha256(artifact.read_bytes()).hexdigest() != manifest["artifact_sha256"]:
        raise ValueError("observation artifact checksum mismatch")
    frame = pd.read_parquet(artifact)
    if hashlib.sha256(_canonical(frame)).hexdigest() != manifest["snapshot_id"] or len(frame) != manifest["rows"]:
        raise ValueError("observation rows mismatch")
    return manifest, frame


def verify(manifest_path: Path) -> dict:
    """Verify an immutable observation without returning its full panel."""
    return read_verified(manifest_path)[0]


def freeze_dataset(manifest_paths: list[Path], *, output_root: Path) -> dict:
    """Freeze explicit observations for later review; this does not create labels or train.

    One version per market/session is required. Unverified source timing stays an
    exclusion reason instead of being converted to a training-ready row count.
    """
    if not manifest_paths:
        raise ValueError("dataset requires explicit observations")
    roots = {path.parent.parent.parent.resolve() for path in manifest_paths}
    if len(roots) != 1:
        raise ValueError("observations must share one archive root")
    archive_root = roots.pop()
    observations = [(verify(path), path.resolve().relative_to(archive_root).as_posix())
                    for path in manifest_paths]
    markets = {item["market"] for item, _ in observations}
    keys = [(item["market"], item["session"]) for item, _ in observations]
    if len(markets) != 1 or len(keys) != len(set(keys)):
        raise ValueError("mixed markets or multiple versions of a session")
    observations.sort(key=lambda pair: pair[0]["session"])
    entries = [{"market": item["market"], "session": item["session"],
                "snapshot_id": item["snapshot_id"], "artifact_sha256": item["artifact_sha256"],
                "manifest_path": relative_path, "rows": item["rows"],
                "strict_pit_eligible": item["strict_pit_eligible"]}
               for item, relative_path in observations]
    material = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
    dataset_id = hashlib.sha256(material).hexdigest()
    excluded = sum(item["rows"] for item, _ in observations if not item["strict_pit_eligible"])
    dataset = {"schema": DATASET_SCHEMA, "dataset_id": dataset_id,
               "market": observations[0][0]["market"], "sessions": len(observations),
               "observed_rows": sum(item["rows"] for item, _ in observations),
               "strict_pit_rows": sum(item["rows"] for item, _ in observations if item["strict_pit_eligible"]),
               "excluded_rows": excluded,
               "exclusion_reasons": {"source_time_unverified": excluded} if excluded else {},
               "label_spec": None, "time_split": None, "model_training_allowed": False,
               "observations": entries}
    destination = output_root / dataset["market"] / f"{dataset_id}.json"
    _exclusive_bytes(destination, json.dumps(dataset, ensure_ascii=False, sort_keys=True,
                                            indent=2).encode("utf-8"))
    return dataset


def readiness(root: Path) -> dict:
    """Read-only inventory; observations are not independent labels or profit evidence."""
    markets = {}
    for market in ("kr", "us"):
        items = []
        corrupt = []
        for path in sorted((root / market).glob("*/*.json")):
            try:
                items.append(verify(path))
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                corrupt.append(str(path.relative_to(root)))
        sessions = {item["session"] for item in items}
        markets[market] = {
            "observations": len(items), "distinct_sessions": len(sessions),
            "rows": sum(item["rows"] for item in items),
            "source_time_verified": sum(bool(item["source_available_at_verified"]) for item in items),
            "strict_pit_eligible": sum(bool(item["strict_pit_eligible"]) for item in items),
            "corrupt_manifests": corrupt,
            "first_session": min(sessions) if sessions else None,
            "last_session": max(sessions) if sessions else None,
        }
    return {
        "schema": "signal-ml-readiness-v1", "mode": "research_only",
        "live_eligible": False, "model_training_started": False,
        "markets": markets,
        "matured_label_spec": None, "overlap_adjusted_periods": None,
        "collection_cost": None, "minimum_detectable_effect": None,
        "decision": "defer",
        "reason": "원천 가용 시각·성숙 라벨·기간 독립성·비용 및 효과 크기 계획이 검증되지 않았습니다.",
    }
