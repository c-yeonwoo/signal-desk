"""Small operational ledger for optional official evidence collectors.

Call/byte counts are resource use, not a claimed monetary bill. Neither this
module nor its daily summaries feed signals, research verdicts, or orders.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
from pathlib import Path

from signal_desk import db

SOURCES = ("dart", "fed_g17", "sec", "sia_market")
PREFIX = "evidence_ops:"

_DART_URL = "https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json?%"
_SEC_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK%"


def _count(value: object) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, number)


def record(source: str, *, when: dt.datetime, result: dict) -> bool:
    """Keep the first summary for a source/day; retries cannot rewrite history."""
    if source not in SOURCES or when.tzinfo is None or when.utcoffset() is None:
        raise ValueError("valid source and timezone required")
    day = when.date().isoformat()
    requested = _count(result.get("requested"))
    status = str(result.get("status") or "unknown")[:48]
    collection_status = (str(result.get("collection_status") or status)[:48]
                         if source == "fed_g17" else status)
    failed = (_count(result.get("failed")) if source in {"dart", "sec", "sia_market"}
              else int(requested > 0 and collection_status != "ok"))
    payload = {"source": source, "day": day, "status": status,
               "requested": requested, "ok": _count(result.get("ok")) if source in {"dart", "sec", "sia_market"} else int(collection_status == "ok" and requested > 0),
               "no_data": _count(result.get("no_data")) if source == "dart" else 0,
               "failed": failed, "response_bytes": _count(result.get("response_bytes")),
               "raw_changed": _count(result.get("raw_changed")) if source in {"dart", "sec", "sia_market"} else 0,
               "revised_periods": (len(result.get("revised_periods"))
                                   if source == "fed_g17" and isinstance(result.get("revised_periods"), list) else 0)}
    key = f"{PREFIX}{source}:{day}"

    def first(old):
        return (payload, True) if old is None else (None, False)

    return bool(db.kv_transform(key, first))


def report(*, now: dt.datetime, days: int = 30) -> dict:
    """Read-only rolling operational use; zero calls never means zero failure rate."""
    if now.tzinfo is None or now.utcoffset() is None or not 1 <= days <= 90:
        raise ValueError("timezone and 1..90 days required")
    cutoff = int((now - dt.timedelta(days=days)).timestamp())
    conn = db.conn()
    try:
        rows = conn.execute("SELECT v FROM kv WHERE k LIKE ? AND ts>=? ORDER BY ts DESC LIMIT 200",
                            (PREFIX + "%", cutoff)).fetchall()
    finally:
        conn.close()
    sources = {source: {"observed_days": 0, "requested": 0, "ok": 0, "no_data": 0,
                        "failed": 0, "response_bytes": 0, "raw_changed": 0,
                        "revised_periods": 0, "last_day": None, "last_status": None}
               for source in SOURCES}
    invalid_rows = 0
    for (value,) in rows:
        try:
            item = json.loads(value)
            source = item["source"]
            if source not in SOURCES or not isinstance(item.get("day"), str):
                raise ValueError("invalid event")
        except (json.JSONDecodeError, TypeError, KeyError, ValueError):
            invalid_rows += 1
            continue
        data = sources[source]
        data["observed_days"] += 1
        for key in ("requested", "ok", "no_data", "failed", "response_bytes", "raw_changed", "revised_periods"):
            data[key] += _count(item.get(key))
        if data["last_day"] is None or item["day"] > data["last_day"]:
            data["last_day"], data["last_status"] = item["day"], str(item.get("status") or "unknown")[:48]
    for data in sources.values():
        data["failed_pct"] = (round(100 * data["failed"] / data["requested"], 1)
                              if data["requested"] else None)
    return {"window_days": days, "sources": sources, "invalid_rows": invalid_rows,
            "monetary_cost_usd": None,
            "cost_note": "요청·응답량만 계측합니다. 제공자 요금과 서버/네트워크 비용은 검증되지 않았습니다."}


def _archive_inventory(path: Path, table: str, source_url: str | None = None) -> dict:
    """Read one archive without creating it; a broken DB is not an empty archive."""
    if table not in {"financial_observations", "fed_g17_observations", "sia_market_observations"}:
        raise ValueError("unsupported archive table")
    if not path.exists():
        return {"status": "not_recorded", "observations": 0, "first_id": None}
    try:
        # The allowlisted table name is the only SQL interpolation; source_url is bound.
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            conn.execute("BEGIN")  # Count and first anchor must describe one read snapshot.
            where = " WHERE source_url LIKE ?" if source_url else ""
            params = (source_url,) if source_url else ()
            count = conn.execute(f"SELECT COUNT(*) FROM {table}{where}", params).fetchone()[0]
            first = conn.execute(
                f"SELECT id, available_at FROM {table}{where} "
                "ORDER BY available_at, id LIMIT 1", params).fetchone()
        finally:
            conn.close()
    except (OSError, sqlite3.Error, ValueError):
        return {"status": "archive_error", "observations": None, "first_id": None}
    return {"status": "recorded" if count else "not_recorded", "observations": count,
            "first_id": first[0] if first else None,
            "first_available_at": first[1] if first else None}


def archive_inventory(*, financial_path: Path | None = None, g17_path: Path | None = None,
                      sia_path: Path | None = None) -> dict:
    """Stable anchors for comparing archive continuity across deployments.

    Counts and IDs are only an inventory, not a raw-byte integrity check.
    Missing and unreadable files are distinguished; neither is success.
    """
    if financial_path is None:
        from signal_desk.signals import financial_change
        financial_path = financial_change.DEFAULT_ARCHIVE
    if g17_path is None:
        from signal_desk.ingest import fed_g17
        g17_path = fed_g17.DEFAULT_ARCHIVE
    if sia_path is None:
        from signal_desk.ingest import sia_market
        sia_path = sia_market.DEFAULT_ARCHIVE
    return {
        "dart": _archive_inventory(financial_path, "financial_observations", _DART_URL),
        "sec": _archive_inventory(financial_path, "financial_observations", _SEC_URL),
        "fed_g17": _archive_inventory(g17_path, "fed_g17_observations"),
        "sia_market": _archive_inventory(sia_path, "sia_market_observations"),
    }


def storage_preflight(*, mount_path: str | None = None, expected_mount: str = "/app/data",
                      app_db_path: Path | None = None, financial_path: Path | None = None,
                      g17_path: Path | None = None, sia_path: Path | None = None) -> dict:
    """Check declared archive placement, not persistence across deployments.

    Only an unchanged raw observation anchor after a deployment can establish
    that the archive survived it. A mounted path alone cannot prove that.
    """
    from signal_desk.ingest import fed_g17
    from signal_desk.ingest import sia_market
    from signal_desk.signals import financial_change

    declared = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "") if mount_path is None else mount_path
    paths = (app_db_path or db.DB, financial_path or financial_change.DEFAULT_ARCHIVE,
             g17_path or fed_g17.DEFAULT_ARCHIVE, sia_path or sia_market.DEFAULT_ARCHIVE)
    try:
        root = Path(expected_mount).resolve()
        aligned = all(path.resolve().is_relative_to(root) for path in paths)
        directory_exists = root.is_dir()
        mount_observed = root.is_mount() if directory_exists else False
    except (OSError, RuntimeError):
        return {"status": "probe_error", "archive_paths_aligned": False,
                "persistence_proven": False}
    if declared != expected_mount:
        status = "mount_not_declared"
    elif not directory_exists:
        status = "mount_path_missing"
    elif not aligned:
        status = "archive_path_outside_mount"
    elif not mount_observed:
        status = "mount_not_observed"
    else:
        status = "mount_observed"
    return {"status": status, "archive_paths_aligned": aligned,
            "persistence_proven": False}
