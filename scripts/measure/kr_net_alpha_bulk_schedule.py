"""Bounded all-security KIS/KSD dividend source probe, not a scope certificate."""

import argparse
import datetime as dt
import fcntl
import json
from pathlib import Path
import re
import time

from signal_desk import config
from scripts.measure.acquire_historical_sources import _fetch_kis_schedule_page, write_archive, KIS_SCHEDULES
from scripts.measure.kr_net_alpha_sources import DEST, digest
from scripts.measure.kr_net_alpha_v2 import contract, source_storage_bytes


def collect(output, start="2021-03-01", end="2021-03-31", max_pages=5):
    spec = contract()
    if (output.exists() or not spec["warmup_from"] <= start <= end <= spec["train_labels_through"]
            or (dt.date.fromisoformat(end)-dt.date.fromisoformat(start)).days > 31
            or not 1 <= max_pages <= 5):
        raise ValueError("bulk probe outside fixed training source/month/page scope")
    creds = config.kis_credentials()
    if not creds:
        raise ValueError("KIS source credentials unavailable")
    lock = (DEST/".collector.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    journal_path = DEST/"v2-action-requests.json"
    journal = json.loads(journal_path.read_text()) if journal_path.exists() else []
    attempts_before = len(journal)
    files, entries, hashes, all_rows, state = {}, {}, set(), [], "partial"
    previous = 0.0
    endpoint, tr_id = KIS_SCHEDULES["dividend"]
    params = {"CTS": "", "GB1": "0", "F_DT": start.replace("-", ""),
              "T_DT": end.replace("-", ""), "SHT_CD": "", "HIGH_GB": ""}
    for page in range(1, max_pages+1):
        if spec["amendment"]["source_scope"]["previous_action_requests"]+len(journal) >= spec["max_action_source_requests"]:
            raise ValueError("cumulative action request cap reached")
        if source_storage_bytes() >= spec["max_new_archive_bytes"]:
            raise ValueError("cumulative source byte cap reached")
        time.sleep(max(0, 1-(time.monotonic()-previous)))
        entry = {"source": "KIS KSD bulk dividend", "start": start, "end": end,
                 "page": page, "state": "reserved", "observed_at": dt.datetime.now(dt.timezone.utc).isoformat()}
        journal.append(entry)
        journal_path.write_text(json.dumps(journal, indent=2))
        previous = time.monotonic()
        try:
            raw, body, continuation = _fetch_kis_schedule_page(endpoint, tr_id, params, creds, "N" if page > 1 else "")
            if source_storage_bytes()+len(raw) > spec["max_new_archive_bytes"]:
                raise ValueError("source response exceeds remaining byte budget")
            target = DEST/"v2-bulk-responses"/f"{len(journal):04d}.json"
            target.parent.mkdir(exist_ok=True)
            target.write_bytes(raw)
            entry.update(state="response_saved", response_path=str(target.relative_to(DEST)),
                         sha256=digest(raw), bytes=len(raw))
            name = f"raw/page-{page:03d}.json"; files[name] = raw
            entries[name] = {"sha256": digest(raw), "bytes": len(raw), "continuation": continuation,
                             "observed_at": entry["observed_at"]}
            if str(body.get("rt_cd")) != "0":
                state = "source_rejected"; break
            rows = body.get("output1") or []
            if isinstance(rows, dict): rows = [rows]
            if not isinstance(rows, list) or any(not isinstance(r, dict)
                    or not re.fullmatch(r"[0-9A-Z]{6}", r.get("sht_cd", "")) for r in rows):
                state = "malformed_rows"; break
            if digest(raw) in hashes:
                state = "repeated_page"; break
            hashes.add(digest(raw)); all_rows.extend(rows)
            entries[name]["rows"] = len(rows)
            if continuation in ("", "D", "E", "F"):
                state = "source_response_ended_not_effective_scope_certified"; break
            if continuation != "M":
                state = "unknown_continuation"; break
        except Exception as exc:
            entry.update(state="failed", error=type(exc).__name__)
            state = "source_error"; break
        finally:
            journal_path.write_text(json.dumps(journal, indent=2))
    manifest = {"schema": "net-alpha-bulk-dividend-probe-v1", "source": "KIS KSD schedule",
        "endpoint": endpoint, "query": params, "start": start, "end": end, "environment": creds["env"],
        "state": state, "request_count": len(journal)-attempts_before, "entries": entries,
        "rows": len(all_rows), "securities": len({r["sht_cd"] for r in all_rows}),
        "query_date_basis": "record_or_schedule_unverified_not_ex_date",
        "total_return_coverage": False, "strict_pit_eligible": False,
        "cumulative_action_attempts": spec["amendment"]["source_scope"]["previous_action_requests"]+len(journal)}
    write_archive(output, files, manifest)
    print(json.dumps({"archive_sha256": digest(output.read_bytes()), **manifest}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--start", default="2021-03-01")
    parser.add_argument("--end", default="2021-03-31")
    args = parser.parse_args()
    config.load_env()
    collect(args.output, args.start, args.end)
