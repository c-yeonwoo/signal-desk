"""Bounded, resumable official inputs for the frozen separated-alpha research.

Each request is reserved in a journal before network I/O; retries count too.
Original responses stay local, and acquisition never opens a performance label.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import math
from pathlib import Path
import time
import urllib.error
import urllib.request
from zipfile import ZipFile

from signal_desk import config, market_clock
from scripts.measure.acquire_historical_sources import write_archive

ROOT = Path(__file__).resolve().parents[2]
PLAN = ROOT / "docs/kr-net-alpha-plan-2026-10-17.json"
PLAN_SHA = "cd3d5d631ef46bf33c551ac5cb8a1e9e116539fd04b9418b3276d36c12dd1d0b"
PROSE_SHA = "4bcf4c5662c5161cba5aa31d6295a30b2535ec5dc749e134e684d3735ad235d4"
PLAN_COMMIT = "8c6d837a91f856ade03b75bdaec672abd51e051d"
PROBE = ROOT / "data/research/kr-net-alpha-probe-2026-10-18-v1.zip"
PROBE_SHA = "e3c4eb606ebc8645a6f399f1f28dc0d196cc10673058c292c90d3c62f292a9b7"
DEST = ROOT / "data/research/kr-net-alpha-inputs-2026-10-18-v1"
REUSED = {
    "krx-daily-2023-q1-v1.zip": "e9bfadf172c80a91f1eca853037350db007f28d9b850f1b00d9774646ab1a8d0",
    "krx-daily-2023-q2-v1.zip": "a6f83ef305e8c5d7f277444af69af3030224633cd0c70da58096906909eb30e7",
    "krx-daily-2023-q3-v1.zip": "bc708b3ce35d2c28542f1b8c6423e3c771b5ea81b534e85c23dc506ae357788d",
    "krx-daily-2023-q4-v1.zip": "d0fdfdef45ccf488833f2b63a97d5503a4297f53a4918b3c5ab641adc852d5b1",
    "krx-daily-2024-q1-v1.zip": "f579892c94e216cde45eeaef9135d65ce9964fc878464cdf19af489dca473de3",
}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        # AUTH_KEY must not be forwarded to a redirect target.
        return None


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def plan():
    import subprocess
    for path, sha in ((PLAN, PLAN_SHA), (ROOT / "docs/KR-NET-ALPHA-PLAN-2026-10-17.md", PROSE_SHA)):
        committed = subprocess.check_output(["git", "show", f"{PLAN_COMMIT}:{path.relative_to(ROOT)}"], cwd=ROOT)
        if digest(path.read_bytes()) != sha or digest(committed) != sha:
            raise ValueError("frozen separated-alpha contract differs")
    return json.loads(PLAN.read_text())


def read_bundle(path):
    with ZipFile(path) as z:
        m = json.loads(z.read("manifest.json"))
        files = {}
        for name, entry in m["entries"].items():
            raw = z.read(name)
            if digest(raw) != entry["sha256"] or len(raw) != entry["bytes"]:
                raise ValueError("original response digest differs")
            files[name] = raw
    return m, files


def validate_rows(endpoint, day, raw):
    body = json.loads(raw)
    if not isinstance(body, dict) or "respCode" in body:
        raise ValueError("KRX rejected response")
    rows = body.get("OutBlock_1")
    if not isinstance(rows, list) or len(rows) < 200 or any(not isinstance(r, dict) for r in rows):
        raise ValueError("empty or incomplete official market response")
    code_key = "ISU_CD" if endpoint == "stk_bydd_trd" else "ISU_SRT_CD"
    codes = [r.get(code_key) for r in rows]
    if any(not isinstance(c, str) or len(c) != 6 for c in codes) or len(set(codes)) != len(codes):
        raise ValueError("duplicate or invalid official security identity")
    if endpoint == "stk_bydd_trd":
        if any(r.get("BAS_DD") != day.replace("-", "") for r in rows):
            raise ValueError("official price date differs")
    elif endpoint == "stk_isu_base_info":
        if any(not r.get("ISU_CD") or not r.get("KIND_STKCERT_TP_NM") for r in rows):
            raise ValueError("official ISIN/share class missing")
    else:
        raise ValueError("endpoint outside frozen acquisition scope")
    return rows


class Collector:
    def __init__(self, destination=DEST):
        self.spec = plan()
        self.destination = Path(destination)
        self.destination.mkdir(parents=True, exist_ok=True)
        self._lock = (self.destination / ".collector.lock").open("a")
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lock.close()
            raise ValueError("another collector already owns this input journal") from None
        self.journal_path = self.destination / "requests.json"
        self.journal = json.loads(self.journal_path.read_text()) if self.journal_path.exists() else []
        self.last_request = 0.0
        self._import_probe()

    def _save_journal(self):
        temporary = self.journal_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.journal, indent=2, ensure_ascii=False))
        temporary.replace(self.journal_path)

    def _import_probe(self):
        if digest(PROBE.read_bytes()) != PROBE_SHA:
            raise ValueError("fixed initial probe differs")
        m, files = read_bundle(PROBE)
        for name, entry in m["entries"].items():
            day = dt.datetime.strptime(entry["requested_date"], "%Y%m%d").date().isoformat()
            if not any(e.get("probe_member") == name for e in self.journal):
                self.journal.append({"probe_member": name, "endpoint": entry["endpoint"],
                    "session": day, "state": "probe_imported", "bytes": entry["bytes"],
                    "sha256": entry["sha256"], "observed_at": entry["observed_at"]})
                self._save_journal()
            target = self.destination / entry["endpoint"] / f"{day}.zip"
            if not target.exists():
                validate_rows(entry["endpoint"], day, files[name])
                self._archive(target, entry["endpoint"], day, files[name], entry["observed_at"])

    def _archive(self, target, endpoint, day, raw, observed):
        rows = validate_rows(endpoint, day, raw)
        name = f"raw/{day}.json"
        m = {"schema": "historical-source-v1", "source": "KRX official Open API",
             "endpoint": f"sto/{endpoint}", "market": "kr", "start": day, "end": day,
             "expected_sessions": [day], "request_count": 1,
             "entries": {name: {"session": day, "rows": len(rows), "bytes": len(raw),
                 "sha256": digest(raw), "retrieved_at_utc": observed}},
             "strict_pit_eligible": False, "total_return_coverage": False,
             "plan_sha256": PLAN_SHA}
        write_archive(target, {name: raw}, m)

    def get(self, endpoint, day):
        if (endpoint not in ("stk_bydd_trd", "stk_isu_base_info")
                or not self.spec["warmup_from"] <= day <= self.spec["liquidation_through"]
                or not market_clock.is_session("kr", day)):
            raise ValueError("request outside fixed source scope")
        target = self.destination / endpoint / f"{day}.zip"
        if target.exists():
            m, files = read_bundle(target)
            if m["endpoint"] != f"sto/{endpoint}" or m["start"] != day or len(files) != 1:
                raise ValueError("cached source identity differs")
            return validate_rows(endpoint, day, next(iter(files.values())))
        if len(self.journal) >= self.spec["max_price_metadata_requests"]:
            raise ValueError("frozen price/metadata request budget exhausted")
        if self.storage_bytes() >= self.spec["max_new_archive_bytes"]:
            raise ValueError("frozen source byte budget exhausted")
        key = config.krx_key()
        if not key:
            raise ValueError("KRX key unavailable")
        time.sleep(max(0, self.spec["min_request_interval_seconds"] - (time.monotonic() - self.last_request)))
        row = {"endpoint": endpoint, "session": day, "state": "reserved", "bytes": 0,
               "observed_at": dt.datetime.now(dt.timezone.utc).isoformat()}
        self.journal.append(row)
        self._save_journal()
        self.last_request = time.monotonic()
        request = urllib.request.Request(
            f"https://data-dbg.krx.co.kr/svc/apis/sto/{endpoint}?basDd={day.replace('-', '')}",
            headers={"AUTH_KEY": key})
        try:
            with urllib.request.build_opener(_NoRedirect()).open(request, timeout=30) as response:
                raw = response.read(10_000_001)
            row.update({"bytes": len(raw), "sha256": digest(raw)})
            if (len(raw) > 10_000_000 or self.storage_bytes()+len(raw)
                    > self.spec["max_new_archive_bytes"]):
                raise ValueError("source response or cumulative size limit")
            self._archive(target, endpoint, day, raw, row["observed_at"])
            row["state"] = "archived"
            return validate_rows(endpoint, day, raw)
        except KeyboardInterrupt:
            row["state"] = "interrupted"
            raise
        except Exception as exc:
            row["state"] = "failed"
            row["error"] = type(exc).__name__
            # Provider URLs and exception bodies can carry credentials.
            raise ValueError(f"KRX {endpoint} {day}: {type(exc).__name__}") from None
        finally:
            self._save_journal()

    def storage_bytes(self):
        # Include initial probe and independently archived action probes as well.
        return (sum(p.stat().st_size for p in self.destination.rglob("*") if p.is_file())
                + sum(p.stat().st_size for p in (ROOT / "data/research").glob("kr-net-alpha-*.zip")))


def collect_prices():
    collector = Collector()
    day = dt.date(2020, 1, 1)
    end = dt.date(2022, 12, 31)
    done = 0
    while day <= end:
        if market_clock.is_session("kr", day):
            collector.get("stk_bydd_trd", day.isoformat())
            done += 1
            if done % 25 == 0:
                print(json.dumps({"through": day.isoformat(), "sessions": done,
                    "requests_including_probe": len(collector.journal)}), flush=True)
        day += dt.timedelta(days=1)
    print(json.dumps({"state": "price_responses_archived_not_action_verified",
        "sessions": done, "requests": len(collector.journal)}), flush=True)


def price_inventory(destination=DEST):
    """Read original responses, without calculating a return or fitting a model."""
    spec = plan()
    days, sources = {}, []
    paths = sorted((Path(destination)/"stk_bydd_trd").glob("*.zip"))
    paths.extend(ROOT/"data/research"/name for name in REUSED)
    for path in paths:
        if path.name in REUSED and digest(path.read_bytes()) != REUSED[path.name]:
            raise ValueError("frozen reused price archive differs")
        m, files = read_bundle(path)
        if m["endpoint"] != "sto/stk_bydd_trd":
            raise ValueError("wrong input archive family")
        accepted = 0
        for name, raw in files.items():
            day = m["entries"][name]["session"]
            if not spec["warmup_from"] <= day <= spec["liquidation_through"]:
                continue
            rows = validate_rows("stk_bydd_trd", day, raw)
            if day in days:
                raise ValueError("duplicate original price session")
            # Retain only fields needed for availability and unit-review queues.
            fields = ("ISU_CD", "ISU_NM", "TDD_OPNPRC", "TDD_CLSPRC", "MKTCAP", "LIST_SHRS", "ACC_TRDVOL")
            days[day] = {r["ISU_CD"]: {k: r[k] for k in fields} for r in rows}
            accepted += 1
        sources.append({"archive": str(path.relative_to(ROOT)), "sha256": digest(path.read_bytes()),
                        "accepted_sessions": accepted})
    expected, cursor = [], dt.date.fromisoformat(spec["warmup_from"])
    while cursor.isoformat() <= spec["liquidation_through"]:
        if market_clock.is_session("kr", cursor):
            expected.append(cursor.isoformat())
        cursor += dt.timedelta(days=1)
    missing = sorted(set(expected)-set(days))
    return days, sources, missing


def number(value):
    try:
        result = float(str(value).replace(",", ""))
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def collect_identities():
    """As-of official classifications, refreshed on new/reappearing codes/quarters.

    Existing ISIN/share-class identity is carried until the next historical
    snapshot; this acquisition assumption is recorded, never strict PIT proof.
    """
    collector = Collector()
    days, sources, missing = price_inventory()
    if missing:
        raise ValueError(f"price collection incomplete: {len(missing)} sessions; first {missing[0]}")
    previous_codes, metadata, metadata_day, quarter = set(), {}, None, None
    universes = []
    for day in sorted(days):
        if not collector.spec["train_start"] <= day <= collector.spec["evaluation_end"]:
            continue
        codes = set(days[day])
        q = (day[:4], (int(day[5:7])-1)//3)
        if q != quarter or codes-previous_codes:
            rows = collector.get("stk_isu_base_info", day)
            metadata = {r["ISU_SRT_CD"]: r for r in rows}
            metadata_day, quarter = day, q
        previous_codes = codes
        if not codes <= set(metadata):
            raise ValueError(f"official identity absent on {day}")
        ordinary = []
        for ticker, row in days[day].items():
            identity = metadata[ticker]
            if identity["KIND_STKCERT_TP_NM"] != "보통주":
                continue
            cap = number(row["MKTCAP"])
            if identity["MKT_TP_NM"] != "KOSPI" or identity["LIST_DD"] > day.replace("-", ""):
                raise ValueError(f"historical identity is not available as of {day}")
            if cap is not None and cap > 0:
                ordinary.append((cap, ticker))
        selected = sorted(ordinary, key=lambda pair: (-pair[0], pair[1]))[:collector.spec["universe_count"]]
        if len(selected) != collector.spec["universe_count"]:
            raise ValueError("incomplete common-share top200")
        universes.append({"date": day, "identity_snapshot_asof": metadata_day,
            "securities": [{"ticker": t, "isin": metadata[t]["ISU_CD"],
                            "name": days[day][t]["ISU_NM"]} for _, t in selected]})
    payload = {"schema": "kr-net-alpha-universe-input-v1", "plan_sha256": PLAN_SHA,
        "price_sources": sources, "dates": universes,
        "identity_basis": "historical_snapshot_carried_until_new_code_or_next_quarter",
        "strict_pit_eligible": False, "action_units_verified": False}
    output = collector.destination/"universes.json"
    if output.exists():
        if json.loads(output.read_text()) != payload:
            raise ValueError("existing universe evidence differs; do not overwrite")
    else:
        output.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    print(json.dumps({"state": "identity_inputs_archived_not_action_verified",
        "dates": len(universes), "tickers": len({s["ticker"] for r in universes for s in r["securities"]}),
        "requests_including_probe": len(collector.journal), "sha256": digest(output.read_bytes())}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prices", "identities"))
    args = parser.parse_args()
    config.load_env()
    if args.command == "prices":
        collect_prices()
    else:
        collect_identities()
