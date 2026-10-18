"""Opt-in SIG-18 one-event evidence adapter. No collection or price adjustment."""
from __future__ import annotations

import datetime as dt
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import subprocess
from zipfile import BadZipFile, ZipFile

MANIFEST_SHA = "1a17d600f8df6a5f44153438c47f35cf629fa71fa3513dc272015dbd518df455"
PROPOSAL_SHA = "b81dd03e54861a61a1786290cc19d2fef8bcb8df71be0b72a7af81950c6c61a7"
SOURCES = {
    "13-listing-original.bin": (1394, "96993779b415881980e2e364ad67d350aa267466f3cbf968a3fe98f32eb25b1a"),
    "14-decision-original.bin": (9565, "a6450cafb0c6c39bce4aa8c29a1a4e198e3ed60012d48064f7fd2ad9f9b8446b"),
}
CODE_FILES = ("scripts/measure/kr_ridge_hold_renewal_diagnostic.py",
              "scripts/measure/kr_ridge_hold_source_supplement.py",
              "tests/test_kr_ridge_hold_renewal_diagnostic.py")
FACT = {
    "ticker": "005380", "security_id": "KR7005380001", "issuer": "현대자동차(주)",
    "share_class": "ordinary_common", "kind": "treasury_cancellation",
    "effective_on": "2024-03-27", "listing_session": "2024-04-16", "previous_session": "2024-04-15",
    "listed_shares_before": 211531506, "listed_shares_after": 209416191, "cancelled_common": 2115315,
    "holder_quantity_effect": "unchanged", "holder_quantity_multiplier": 1, "price_unit_multiplier": 1,
    "coverage_state": "partial", "historical_capture_verified": False,
    "source_filing_date": "2024-04-11",
    "source_publication_at_displayed_KST": "2024-04-11T16:46:00+09:00",
    "decision_publication_at_displayed_KST": "2024-03-21T16:27:00+09:00",
    "source_observed_at": "2026-10-18T08:08:29.760807+00:00",
}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _object(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("supplement_duplicate_JSON_key:" + key)
        out[key] = value
    return out


def decode_json(raw):
    return json.loads(raw, object_pairs_hook=_object)


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def text(raw):
    # KIND originals may be UTF-8 or legacy Korean. Never discard undecodable bytes.
    for encoding in ("utf-8-sig", "cp949"):
        try:
            decoded = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError("supplement_source_encoding")
    parser = _Text()
    parser.feed(decoded)
    return re.sub(r"\s+", " ", " ".join(parser.parts)).strip()


def _one(pattern, body, expected):
    found = re.findall(pattern, body)
    if found != [expected]:
        raise ValueError("supplement_body_fact:" + pattern)


def _body_date(pattern, body, expected):
    found = re.findall(pattern, body)
    if len(found) != 1 or dt.date(*(int(v) for v in found[0])).isoformat() != expected:
        raise ValueError("supplement_body_date:" + expected)


def validate_bodies(listing, decision):
    listing, decision = text(listing), text(decision)
    # The pinned decision body omits the issuer name. Its exact official bytes
    # and URL are checked by merge_archive; the listing identifies the security.
    if "현대자동차" not in listing or "주식소각" not in listing:
        raise ValueError("supplement_issuer_or_kind")
    _one(r"(KR[0-9A-Z]{10})\s*\(단축코드\s*:\s*A005380\)", listing, "KR7005380001")
    _one(r"기명식\s*보통주\s*([\d,]+)주\s*→\s*([\d,]+)주", listing,
         ("211,531,506", "209,416,191"))
    common = re.search(r"기명식\s*보통주\s*[\d,]+주\s*→\s*[\d,]+주\s*-?\s*변경주식수\s*:\s*-([\d,]+)주", listing)
    if common is None or common[1] != "2,115,315":
        raise ValueError("supplement_common_cancellation_quantity")
    _body_date(r"발행일\(소각일\)\s*:\s*(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일", listing,
         "2024-03-27")
    _body_date(r"변경상장일\s*:\s*(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일", listing,
         "2024-04-16")
    section = re.search(r"1\.\s*소각할\s*주식의\s*종류와\s*수\s*(.*?)\s*2\.\s*발행주식\s*총수", decision)
    if section is None:
        raise ValueError("supplement_cancellation_section")
    _one(r"보통주식\s*\(주\)\s*([\d,]+)", section[1], "2,115,315")
    _one(r"소각\s*예정일\s*(\d{4}-\d{2}-\d{2})", decision, "2024-03-27")
    if "기취득 자기주식" not in decision or "자본금의 감소는 없음" not in decision:
        raise ValueError("supplement_not_treasury_cancellation")


def checked_spec(root, spec_path, archive_path, commit, contract_commit, contract_sha, hashes, budget):
    """Verify the controller's addendum precommit before processing new evidence."""
    root, spec_path = Path(root).resolve(), Path(spec_path).resolve()
    if not spec_path.is_relative_to(root) or not re.fullmatch(r"[0-9a-f]{40}", commit or ""):
        raise ValueError("supplement_requires_repo_spec_and_precommit")
    raw = spec_path.read_bytes()
    committed = subprocess.check_output(["git", "show", f"{commit}:{spec_path.relative_to(root).as_posix()}"],
        cwd=root, timeout=max(.1, budget.seconds-max(budget.elapsed().values())))
    if raw != committed:
        raise ValueError("supplement_spec_not_precommitted")
    spec = decode_json(raw)
    required = {"schema", "contract_commit", "contract_sha256", "input_sha256", "archive_sha256",
                "manifest_sha256", "proposal_sha256", "code_sha256", "maximum_seconds", "new_collection"}
    if (set(spec) != required or spec["schema"] != "kr-r3-hyundai-source-supplement-v1"
            or spec["contract_commit"] != contract_commit or spec["contract_sha256"] != contract_sha
            or spec["input_sha256"] != hashes or spec["manifest_sha256"] != MANIFEST_SHA
            or spec["proposal_sha256"] != PROPOSAL_SHA or spec["maximum_seconds"] != 600
            or type(spec["new_collection"]) is not int or spec["new_collection"] != 0
            or set(spec["code_sha256"]) != set(CODE_FILES)):
        raise ValueError("supplement_spec_scope_differs")
    for name in CODE_FILES:
        budget.check()
        # The executing files, not arbitrary repo copies, must match the addendum.
        path = Path(__file__).resolve().parents[2] / name
        if sha(path.read_bytes()) != spec["code_sha256"][name]:
            raise ValueError("supplement_code_hash_differs:" + name)
    if not re.fullmatch(r"[0-9a-f]{64}", spec["archive_sha256"]):
        raise ValueError("supplement_archive_hash_invalid")
    archive_path = Path(archive_path)
    if archive_path.stat().st_size > 100_000 or sha(archive_path.read_bytes()) != spec["archive_sha256"]:
        raise ValueError("supplement_archive_hash_differs")
    return spec, {"spec_sha256": sha(raw), "spec_commit": commit,
                  "archive_sha256": spec["archive_sha256"], "manifest_sha256": MANIFEST_SHA,
                  "proposal_sha256": PROPOSAL_SHA, "source_sha256": {n: v[1] for n, v in SOURCES.items()},
                  "historical_capture_verified": False, "coverage_state": "partial"}


def merge_archive(archive_path, reviewed, by_day, panel, fact_type):
    """No fact JSON alone is sufficient: all four pinned byte streams are required."""
    try:
        return _merge_archive(archive_path, reviewed, by_day, panel, fact_type)
    except BadZipFile as exc:
        raise ValueError("supplement_invalid_zip") from exc


def _merge_archive(archive_path, reviewed, by_day, panel, fact_type):
    with ZipFile(archive_path) as archive:
        names = archive.namelist()
        required = {*SOURCES, "manifest.json", "adjudicated-fact-proposal.json"}
        if len(names) != len(required) or set(names) != required:
            raise ValueError("supplement_archive_members_or_duplicates")
        if any(i.file_size > 20_000 or i.is_dir() for i in archive.infolist()):
            raise ValueError("supplement_member_size")
        files = {n: archive.read(n) for n in names}
    if sha(files["manifest.json"]) != MANIFEST_SHA or sha(files["adjudicated-fact-proposal.json"]) != PROPOSAL_SHA:
        raise ValueError("supplement_review_pack_hash_differs")
    manifest = decode_json(files["manifest.json"])
    proposal = decode_json(files["adjudicated-fact-proposal.json"])
    for key, expected in FACT.items():
        if type(proposal.get(key)) is not type(expected) or proposal[key] != expected:
            raise ValueError("supplement_proposal_fact:" + key)
    for name, (size, expected_sha) in SOURCES.items():
        entries = [e for e in manifest["files"] if e["path"] == name]
        sources = [e for e in manifest["sources"] if e["path"] == name]
        if (len(entries) != 1 or len(sources) != 1 or entries[0]["bytes"] != size
                or entries[0]["sha256"] != expected_sha or sources[0]["sha256"] != expected_sha
                or len(files[name]) != size or sha(files[name]) != expected_sha):
            raise ValueError("supplement_original_hash_or_bytes:" + name)
        decision = name.startswith("14-")
        if (sources[0]["url"] != proposal["decision_source_url" if decision else "source_url"]
                or proposal["decision_source_sha256" if decision else "source_sha256"] != expected_sha):
            raise ValueError("supplement_source_identity")
        observed = dt.datetime.fromisoformat(sources[0]["retrieved_at"])
        publication = dt.datetime.fromisoformat(proposal[
            "decision_publication_at_displayed_KST" if decision else "source_publication_at_displayed_KST"])
        if observed.tzinfo is None or publication > observed:
            raise ValueError("supplement_publication_observation_clock")
        if not decision and sources[0]["retrieved_at"] != proposal["source_observed_at"]:
            raise ValueError("supplement_observation_differs")
    validate_bodies(*(files[n] for n in SOURCES))
    day, ticker = FACT["listing_session"], FACT["ticker"]
    key = (day, ticker)
    # Duplicate, conflicting, or differently dated facts for this security fail closed.
    if key in reviewed or any(f.ticker == ticker or f.security_id == FACT["security_id"] for f in reviewed.values()):
        raise ValueError("supplement_duplicate_or_conflicting_fact")
    index = panel.index[day]
    if index < 1 or panel.sessions[index-1] != FACT["previous_session"]:
        raise ValueError("supplement_previous_session_differs")
    for session, field in ((FACT["previous_session"], "listed_shares_before"), (day, "listed_shares_after")):
        value = by_day[session][ticker]["LIST_SHRS"]
        # Parse integer strings directly; no float rounding or 0.99 adjustment.
        if not isinstance(value, str) or not re.fullmatch(r"\d{1,3}(?:,\d{3})*|\d+", value) or int(value.replace(",", "")) != FACT[field]:
            raise ValueError("supplement_KRX_integer_mapping_differs")
    fact = fact_type(**{k: proposal[k] for k in (
        "ticker", "security_id", "share_class", "kind", "effective_on", "listing_session", "previous_session",
        "listed_shares_before", "listed_shares_after", "source_url", "source_sha256", "source_filing_date")},
        source_observed_at=dt.datetime.fromisoformat(proposal["source_observed_at"]),
        source_span="SIG-18 pinned listing and cancellation decision; event-only quantity/price multiplier 1; retrospective, partial")
    return {**reviewed, key: fact}
