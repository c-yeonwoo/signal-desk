"""Offline, source-bound corporate-action fields with explicit revision edges."""

from __future__ import annotations

from collections import defaultdict
import datetime as dt
from functools import lru_cache
import hashlib
from html import unescape
import json
import math
import re
from zoneinfo import ZoneInfo

from signal_desk import market_clock

KST = ZoneInfo("Asia/Seoul")
DATE_ROLES = frozenset({"legal_effective_on", "price_basis_from_session", "record_on",
    "tradable_from_session", "listed_share_observed_on", "payment_scheduled_on",
    "payment_confirmed_on", "quote_rule_from_session", "delivery_on"})
BOUNDARIES = frozenset({"entitlement_boundary", "conversion_boundary", "halt_start", "halt_end", "delivery_boundary"})
KINDS = frozenset({"split", "stock_dividend", "cash_dividend", "rights", "capital_reduction",
    "spin_off", "merger_exchange", "symbol_change", "share_count_change", "halt", "delisting",
    "cash_exchange", "unsupported"})
COMPONENTS = frozenset({"schedule", "price_unit", "entitlement", "delivery", "payment",
    "quote_rule", "share_count", "halt", "identity"})


def timestamp(value):
    stamp = dt.datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(stamp, dt.datetime) or stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("aware evidence timestamp required")
    return stamp


@lru_cache(maxsize=4096)
def session_time(day, column="close"):
    if not market_clock.is_session("kr", day) or column not in ("open", "close"):
        raise ValueError("unverified research session")
    return market_clock._calendar("kr").schedule.loc[day, column].to_pydatetime()


def available_at(fact):
    if fact.get("published_at"):
        result = timestamp(fact["published_at"])
        if result.astimezone(KST).date().isoformat() != fact["published_on"]:
            raise ValueError("publication date/time differs")
        if fact.get("publication_precision") == "minute":
            if result.second or result.microsecond:
                raise ValueError("minute publication must have zero seconds")
            result += dt.timedelta(seconds=59, microseconds=999999)
    else:
        published = dt.date.fromisoformat(fact["published_on"])
        result = None
        for offset in range(1, 32):
            day = (published + dt.timedelta(days=offset)).isoformat()
            if market_clock.is_session("kr", day):
                result = session_time(day, "open")
                break
        if result is None:
            raise ValueError("publication availability outside calendar")
    if fact.get("availability_mode") == "observed":
        result = max(result, timestamp(fact["observed_at"]))
    return result


def boundary(value):
    """Date phases are simulation conventions, never fabricated legal timestamps."""
    if isinstance(value, str):
        return timestamp(value)
    if not isinstance(value, dict) or set(value) != {"on", "phase"}:
        raise ValueError("explicit boundary date and phase required")
    phase = value["phase"]
    if dt.date.fromisoformat(value["on"]).isoformat() != value["on"]:
        raise ValueError("canonical boundary date required")
    if phase not in ("before_open", "after_close"):
        raise ValueError("ambiguous daily boundary")
    return session_time(value["on"], "open" if phase == "before_open" else "close") + (
        dt.timedelta(microseconds=-1) if phase == "before_open" else dt.timedelta(microseconds=1))


def text_of(raw):
    return re.sub(r"\s+", "", unescape(re.sub(r"<[^>]*>", " ", raw.decode("utf-8"))))


def prove(documents, proof):
    raw = documents.get(proof.get("source_sha256"))
    span = proof.get("source_span")
    if raw is None or not isinstance(span, str) or not span or re.sub(r"\s+", "", span) not in text_of(raw):
        raise ValueError("component source/span missing or differs")


class ActionBook:
    """Immutable assertions; resolution preserves each field's own provenance."""

    def __init__(self, documents, assertions=()):
        if any(hashlib.sha256(raw).hexdigest() != sha for sha, raw in documents.items()):
            raise ValueError("component original hash differs")
        self.assertions = tuple(assertions)
        self.by_id = {a["assertion_id"]: a for a in self.assertions}
        if len(self.by_id) != len(self.assertions):
            raise ValueError("duplicate action assertion")
        groups = defaultdict(list)
        for a in self.assertions:
            if (not re.fullmatch(r"KR[0-9A-Z]{10}", a["security_id"])
                    or a["share_class"] != "ordinary" or a["action_kind"] not in KINDS
                    or a["component_kind"] not in COMPONENTS or not a["changes"]
                    or a.get("status") not in ("announced", "confirmed", "disputed", "cancelled")
                    or a.get("availability_mode") not in ("observed", "reconstructed_public")):
                raise ValueError("invalid action component identity or status")
            timestamp(a["observed_at"])
            precision = a.get("publication_precision")
            if precision not in ("date", "minute", "second") or bool(a.get("published_at")) != (precision != "date"):
                raise ValueError("explicit component publication precision required")
            available_at(a)
            prove(documents, a["publication_proof"])
            token = (timestamp(a["published_at"]).astimezone(KST).strftime("%Y-%m-%d %H:%M" +
                     (":%S" if precision == "second" else "")) if a.get("published_at") else a["published_on"])
            if a["publication_proof"].get("date_format") == "english_month" and precision == "date":
                token = dt.date.fromisoformat(a["published_on"]).strftime("%B %d, %Y")
            if a["publication_proof"].get("date_format") == "english_abbrev_dot" and precision == "date":
                token = dt.date.fromisoformat(a["published_on"]).strftime("%b %d. %Y")
            if re.sub(r"\s+", "", token) not in re.sub(r"\s+", "", a["publication_proof"]["source_span"]):
                raise ValueError("component publication absent from proof")
            for name, f in a["changes"].items():
                prove(documents, f)
                if not isinstance(f.get("precision"), str) or not f["precision"]:
                    raise ValueError("field precision missing")
                if f.get("status", a["status"]) not in ("announced", "confirmed", "disputed", "cancelled"):
                    raise ValueError("invalid action field status")
                if name in DATE_ROLES:
                    if dt.date.fromisoformat(f["value"]).isoformat() != f["value"]:
                        raise ValueError("canonical action date required")
                    if f["precision"] != "date":
                        raise ValueError("date role must preserve date precision")
                elif name in BOUNDARIES:
                    boundary(f["value"])
                    if f["precision"] != ("session_phase" if isinstance(f["value"], dict) else "second"):
                        raise ValueError("boundary precision differs")
                    if isinstance(f["value"], str) and f["value"] not in f["source_span"]:
                        digits = re.sub(r"\D", "", f["source_span"])
                        if timestamp(f["value"]).astimezone(KST).strftime("%Y%m%d%H%M%S") not in digits:
                            raise ValueError("exact boundary clock absent from source span")
            groups[(a["security_id"], a["event_id"], a["component_id"])].append(a)
        self.groups = dict(groups)
        self.ancestors = {}

        def ancestors(aid, visiting):
            if aid in visiting:
                raise ValueError("cyclic action revision")
            if aid in self.ancestors:
                return self.ancestors[aid]
            a = self.by_id[aid]
            result = set()
            for previous in a["supersedes_assertion_ids"]:
                old = self.by_id.get(previous)
                if old is None or any(old[k] != a[k] for k in (
                        "security_id", "event_id", "component_id", "component_kind", "action_kind", "share_class")):
                    raise ValueError("revision target missing or component differs")
                if available_at({**old, "availability_mode": "reconstructed_public"}) > available_at(
                        {**a, "availability_mode": "reconstructed_public"}):
                    raise ValueError("revision predates its source")
                result |= {previous} | ancestors(previous, visiting | {aid})
            self.ancestors[aid] = frozenset(result)
            return self.ancestors[aid]
        for aid in self.by_id:
            ancestors(aid, set())
        self._cache = {}

    def resolve_components(self, security, known_by, purpose):
        if purpose not in ("input", "outcome", "account"):
            raise ValueError("unknown action purpose")
        known_by = timestamp(known_by)
        key = (security, known_by, purpose)
        if key in self._cache:
            return self._cache[key]
        result = []
        for (isin, event, component), rows in self.groups.items():
            if isin != security:
                continue
            visible = [a for a in rows if available_at(a) <= known_by]
            if not visible:
                continue
            fields, conflicts = {}, []
            for name in sorted({k for a in visible for k in a["changes"]}):
                candidates = [a for a in visible if name in a["changes"]]
                tips = [a for a in candidates if not any(a["assertion_id"] in self.ancestors[b["assertion_id"]]
                    for b in candidates)]
                def signature(a):
                    return json.dumps({"value": a["changes"][name]["value"],
                        "precision": a["changes"][name]["precision"],
                        "status": a["changes"][name].get("status", a["status"])}, sort_keys=True)
                values = {signature(a) for a in tips}
                if len(values) != 1:
                    conflicts.append(name)
                    continue
                origins = {a["assertion_id"]: a for a in tips}
                examined = set()

                def unchanged_origin(a):
                    for aid in a["supersedes_assertion_ids"]:
                        if aid in examined:
                            continue
                        examined.add(aid)
                        old = self.by_id[aid]
                        if available_at(old) > known_by:
                            continue
                        if name in old["changes"]:
                            if signature(old) not in values:
                                continue
                            origins[aid] = old
                        unchanged_origin(old)
                for a in tips:
                    unchanged_origin(a)
                chosen = min(origins.values(), key=lambda a: (available_at(a), a["assertion_id"]))
                f = chosen["changes"][name]
                fields[name] = {**f, "status": f.get("status", chosen["status"]),
                    "known_at": available_at(chosen).isoformat(),
                    "assertion_ids": sorted(origins)}
            first = visible[0]
            result.append({"security_id": isin, "event_id": event, "component_id": component,
                "component_kind": first["component_kind"], "kind": first["action_kind"],
                "fields": fields, "conflicts": conflicts})
        resolved = tuple(sorted(result, key=lambda r: (r["event_id"], r["component_id"])))
        self._cache[key] = resolved
        return resolved

    def events(self, security, known_by, purpose):
        events = {}
        for c in self.resolve_components(security, known_by, purpose):
            if c["component_kind"] == "quote_rule":
                continue
            if purpose == "input" and c["kind"] == "cash_dividend":
                continue
            event = events.setdefault(c["event_id"], {"event_id": c["event_id"], "security_id": security,
                "kind": c["kind"], "fields": {}, "conflicts": [], "assertion_ids": set()})
            if event["kind"] != c["kind"]:
                event["conflicts"].append("action_kind")
            event["conflicts"].extend(c["conflicts"])
            for name, f in c["fields"].items():
                old = event["fields"].get(name)
                if old and (old["value"], old["status"], old["precision"]) != (f["value"], f["status"], f["precision"]):
                    event["conflicts"].append(name)
                else:
                    if old:
                        original = min((old, f), key=lambda item: item["known_at"])
                        event["fields"][name] = {**original,
                            "assertion_ids": sorted(set(old["assertion_ids"]) | set(f["assertion_ids"]))}
                    else:
                        event["fields"][name] = f
                event["assertion_ids"].update(f["assertion_ids"])
        return tuple(events[k] for k in sorted(events))

    def dependencies(self, security, start, end, purpose, known_by=None):
        """Include every relevant revision, even if a correction moves a date out."""
        roots = set()
        for a in self.assertions:
            if a["security_id"] != security or (known_by is not None and available_at(a) > known_by):
                continue
            if a["component_kind"] == "quote_rule" or (purpose == "input" and a["action_kind"] == "cash_dividend"):
                continue
            for name, f in a["changes"].items():
                value = f["value"]
                day = (value["on"] if isinstance(value, dict) else str(value)[:10])
                if purpose != "account" and name in ("payment_scheduled_on", "payment_confirmed_on", "delivery_on", "delivery_boundary"):
                    continue
                if name in DATE_ROLES | BOUNDARIES and start <= day <= end:
                    roots.add(a["event_id"])
            if a["action_kind"] == "halt" and "halt_start" in a["changes"]:
                first = a["changes"]["halt_start"]["value"]
                first_day = first["on"] if isinstance(first, dict) else first[:10]
                last = a["changes"].get("halt_end", {}).get("value")
                last_day = last["on"] if isinstance(last, dict) else last[:10] if last else None
                if first_day <= end and (last_day is None or last_day >= start):
                    roots.add(a["event_id"])
        return tuple(a for a in self.assertions if a["security_id"] == security
            and a["event_id"] in roots and (known_by is None or available_at(a) <= known_by))


def value(event, name, *, confirmed=True):
    field = event["fields"].get(name)
    if field is None or field["status"] in ("disputed", "cancelled") or (confirmed and field["status"] != "confirmed"):
        return None
    return field["value"]


def event_boundary(event):
    name = "conversion_boundary" if event["kind"] in ("split", "merger_exchange", "spin_off", "cash_exchange") else "entitlement_boundary"
    raw = value(event, name)
    return boundary(raw) if raw is not None else None


class RightsLedger:
    """Per-right postings and asset transfers; no brokerage or account imports."""

    def __init__(self, portfolio):
        self.portfolio = portfolio
        self.postings = {}
        self.delivered = set()
        self.journal = []

    def accrue(self, event, entitlement, role, amount):
        if isinstance(amount, bool) or not math.isfinite(amount) or amount < 0:
            raise ValueError("invalid rights amount")
        key = (self.portfolio, event, entitlement, role)
        previous = self.postings.get(key, 0.0)
        self.postings[key] = amount
        if amount != previous:
            self.journal.append({"key": key, "role": "accrual_delta", "amount": amount-previous})
        return amount - previous

    def deliver(self, event, entitlement, role):
        key = (self.portfolio, event, entitlement, role)
        if key not in self.postings:
            raise ValueError("delivery without accrued right")
        if key in self.delivered:
            return 0.0
        self.delivered.add(key)
        amount = self.postings[key]
        self.journal.append({"key": key, "role": "delivery_transfer", "amount": amount, "wealth_delta": 0})
        return amount
