"""Offline v2 as-of inputs and fixed-horizon economic labels.

No provider, broker, environment or production-store access. Reviewed scope is
an input; positive prices and unchanged share counts cannot manufacture it.
"""

from __future__ import annotations

from collections import Counter
import datetime as dt
import hashlib
from html import unescape
import json
import math
import re
from functools import lru_cache
from zoneinfo import ZoneInfo

import numpy as np

from signal_desk import market_clock
from signal_desk.signals import research_net_alpha as core
from signal_desk.signals.research_action_coverage import CoverageCertificate

KST = ZoneInfo("Asia/Seoul")
UNIT_KINDS = frozenset({"split", "stock_dividend", "rights", "capital_reduction",
    "spin_off", "merger_exchange", "symbol_change", "share_count_change", "halt", "delisting"})
LABEL_KINDS = UNIT_KINDS | {"cash_dividend"}


def timestamp(value):
    stamp = dt.datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(stamp, dt.datetime) or stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("aware evidence timestamp required")
    return stamp


@lru_cache(maxsize=4096)
def session_time(day, column="close"):
    if not market_clock.is_session("kr", day) or column not in ("open", "close"):
        raise ValueError("unverified research session")
    # regular_window subtracts the bot auction buffer. Research uses actual close.
    return market_clock._calendar("kr").schedule.loc[day, column].to_pydatetime()


@lru_cache(maxsize=2048)
def label_sessions(day):
    return tuple(market_clock.next_sessions("kr", day, 6))


def available_at(fact):
    if fact.get("published_at"):
        result = timestamp(fact["published_at"])
        if result.astimezone(KST).date().isoformat() != fact["published_on"]:
            raise ValueError("publication date/time differs")
        if fact.get("publication_precision") == "minute":
            if result.second or result.microsecond:
                raise ValueError("minute publication must have zero seconds")
            result += dt.timedelta(seconds=59, microseconds=999999)
        return result
    published = dt.date.fromisoformat(fact["published_on"])
    for offset in range(1, 32):
        day = (published + dt.timedelta(days=offset)).isoformat()
        if market_clock.is_session("kr", day):
            return session_time(day, "open")
    raise ValueError("publication availability outside calendar")


def number(value):
    try:
        if isinstance(value, bool):
            return None
        result = float(str(value).replace(",", ""))
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


class Evidence:
    """Reviewed facts and scopes, bound to actual hashed original/review bytes.

    Hash integrity does not independently verify an analyst's interpretation.
    Scope reviews must be supplied by source audit, never inferred by this class.
    """

    def __init__(self, documents=None, certificates=(), facts=()):
        self.documents = documents or {}
        self.certificates = tuple(certificates)
        self.facts = tuple(facts)
        for sha, raw in self.documents.items():
            if hashlib.sha256(raw).hexdigest() != sha:
                raise ValueError("evidence original hash differs")
        ids = [f["fact_id"] for f in self.facts]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate fact version")
        for fact in self.facts:
            if fact["kind"] not in LABEL_KINDS|{"cash_exchange", "unsupported"}:
                raise ValueError("unknown reviewed event kind")
            raw = self.documents.get(fact["source_sha256"])
            if raw is None or not fact.get("source_span"):
                raise ValueError("fact original/span missing")
            text = unescape(re.sub(r"<[^>]*>", " ", raw.decode("utf-8")))
            if re.sub(r"\s+", "", fact["source_span"]) not in re.sub(r"\s+", "", text):
                raise ValueError("reviewed fact span absent from original")
            if fact.get("publication_source_sha256"):
                publication = self.documents.get(fact["publication_source_sha256"])
                if publication is None:
                    raise ValueError("publication original missing")
                published = timestamp(fact["published_at"]).astimezone(KST).strftime("%Y-%m-%d %H:%M")
                if published not in unescape(re.sub(r"<[^>]*>", " ", publication.decode("utf-8"))):
                    raise ValueError("publication time absent from index original")
            if not re.fullmatch(r"KR[0-9A-Z]{10}", fact["security_id"]):
                raise ValueError("fact needs official security identity")
            available_at(fact)
            timestamp(fact["effective_at"])
            if fact["kind"] == "halt" and fact.get("end_at"):
                if timestamp(fact["end_at"]) <= timestamp(fact["effective_at"]):
                    raise ValueError("halt ends before it starts")
        for cert in self.certificates:
            self._check_certificate(cert)

    def _review(self, sha, schema):
        if sha not in self.documents:
            raise ValueError("review proof bytes missing")
        review = json.loads(self.documents[sha])
        if review.get("schema") != schema:
            raise ValueError("review proof schema differs")
        return review

    def _check_certificate(self, cert):
        if not isinstance(cert, CoverageCertificate) or cert.market != "kr":
            raise ValueError("invalid reviewed certificate")
        if not re.fullmatch(r"KR[0-9A-Z]{10}", cert.security_id):
            raise ValueError("certificate needs official security identity")
        for window in cert.windows:
            if any(sha not in self.documents for sha in window.page_hashes):
                raise ValueError("scope original bytes missing")
        if cert.reconciliation_sha256:
            review = self._review(cert.reconciliation_sha256, "net-alpha-reconciliation-v1")
            expected = {"security_id": cert.security_id, "start": cert.start, "end": cert.end,
                "required_kinds": sorted(cert.required_kinds),
                "page_hashes": sorted({s for w in cert.windows for s in w.page_hashes}),
                "unresolved_event_ids": list(cert.unresolved_event_ids)}
            if any(review.get(k) != v for k, v in expected.items()):
                raise ValueError("reconciliation scope differs")
            actual = sorted(f["fact_id"] for f in self.facts if f["security_id"] == cert.security_id
                and cert.start <= timestamp(f["effective_at"]).astimezone(KST).date().isoformat() <= cert.end)
            if review.get("fact_ids") != actual:
                raise ValueError("reconciliation fact inventory differs")
        for scope in cert.official_scopes:
            for sha, schema in ((scope.source_scope_sha256, "net-alpha-official-scope-v1"),
                    (scope.corrections_review_sha256, "net-alpha-corrections-v1"),
                    (scope.event_reconciliation_sha256, "net-alpha-kind-reconciliation-v1")):
                review = self._review(sha, schema)
                expected = {"security_id": cert.security_id, "kind": scope.kind,
                    "start": scope.window.start, "end": scope.window.end,
                    "page_hashes": list(scope.window.page_hashes)}
                if any(review.get(k) != v for k, v in expected.items()):
                    raise ValueError("official kind review differs")

    def coverage(self, security, start, end, kinds):
        matches = [c for c in self.certificates if c.security_id == security
            and c.start <= start and c.end >= end and kinds <= c.required_kinds]
        return next((c for c in matches if c.state == "complete_for_scope"), None)

    def versions(self, security, cutoff):
        latest = {}
        for fact in self.facts:
            if fact["security_id"] != security or available_at(fact) > cutoff:
                continue
            key = fact["event_id"]
            if key in latest and available_at(fact) == available_at(latest[key]):
                raise ValueError("ambiguous same-time fact revisions")
            if key not in latest or available_at(fact) > available_at(latest[key]):
                latest[key] = fact
        return sorted((f for f in latest.values() if f.get("status") != "cancelled"),
            key=lambda f: (timestamp(f["effective_at"]), f["fact_id"]))

    def halted(self, security, at, known_by):
        return any(f["kind"] == "halt" and timestamp(f["effective_at"]) <= at
            and (not f.get("end_at") or at < timestamp(f["end_at"]))
            for f in self.versions(security, known_by))


def universe(date, roster, history_days, prices, evidence, spec):
    """Future labels are deliberately not an argument. Roster is already top200."""
    cutoff = session_time(date)
    if (not history_days or history_days[-1] != date or len(history_days) > spec["required_closes"]
            or history_days != sorted(set(history_days))
            or history_days != [s.date().isoformat() for s in
                market_clock._calendar("kr").sessions_in_range(history_days[0], date)]):
        raise ValueError("history must be consecutive market sessions through decision date")
    next_open = session_time(market_clock.next_sessions("kr", date, 1)[0], "open")
    if len(roster) != spec["universe_count"] or len({r["ticker"] for r in roster}) != len(roster):
        raise ValueError("incomplete official ranked roster")
    rows, accepted, provisional, inputs, gaps = [], [], [], {}, []
    for security in roster:
        ticker, isin = security["ticker"], security["isin"]
        current = prices.get(date, {}).get(ticker, {})
        op, close, cap, vol = (number(current.get(k)) for k in
            ("TDD_OPNPRC", "TDD_CLSPRC", "MKTCAP", "ACC_TRDVOL"))
        state, reasons = "eligible", []
        if any(v is None or v < 0 for v in (op, close, cap, vol)) or close == 0 or cap == 0:
            state, reasons = "input_gap", ["missing_or_invalid_current_row"]
        elif op == 0 or vol == 0:
            state, reasons = "excluded", ["no_trade_observed_t"]
        elif evidence.halted(isin, cutoff, cutoff) or evidence.halted(isin, next_open, cutoff):
            state, reasons = "excluded", ["known_halt"]
        else:
            history = [prices.get(d, {}).get(ticker, {}) for d in history_days]
            c = [number(r.get("TDD_CLSPRC")) for r in history]
            v = [number(r.get("ACC_TRDVOL")) for r in history[-21:]]
            listed = security.get("listed_on")
            if len(history_days) < spec["required_closes"] or (listed and listed > history_days[0]):
                state, reasons = "excluded", ["insufficient_history"]
            elif (any(x is None or x <= 0 for x in c) or len(v) != 21
                    or any(x is None or x < 0 for x in v) or sum(v[:-1]) <= 0):
                state, reasons = "input_gap", ["unverified_price_or_volume_history"]
            else:
                provisional.append(ticker)
                cert = evidence.coverage(isin, history_days[0], date, UNIT_KINDS)
                if cert is None:
                    state, reasons = "input_gap", ["unit_identity_and_halt_scope_not_certified"]
                else:
                    unit_facts = evidence.versions(isin, cutoff)
                    # Complex reorganizations cannot be treated as a simple split.
                    relevant = [f for f in unit_facts if history_days[0] <=
                        timestamp(f["effective_at"]).astimezone(KST).date().isoformat() <= date]
                    complex_facts = [f for f in relevant if f["kind"] in (
                        "rights", "capital_reduction", "spin_off", "merger_exchange", "symbol_change",
                        "cash_exchange", "unsupported")]
                    if complex_facts:
                        state, reasons = "input_gap", ["complex_history_units_unsupported"]
                    else:
                        unexplained = []
                        for previous_day, current_day, before, after in zip(
                                history_days, history_days[1:], history, history[1:]):
                            old, new = number(before.get("LIST_SHRS")), number(after.get("LIST_SHRS"))
                            if old is None or new is None or old <= 0 or new <= 0:
                                unexplained.append(current_day)
                            elif old != new and not any(
                                    f.get("observed_share_change_on") == current_day
                                    and number(f.get("listed_shares_before")) == old
                                    and number(f.get("listed_shares_after")) == new
                                    and (f["kind"] in ("split", "stock_dividend")
                                         or f.get("holder_quantity_effect") == "unchanged")
                                    for f in relevant):
                                unexplained.append(current_day)
                        if unexplained:
                            state, reasons = "input_gap", ["listed_share_changes_not_reconciled"]
                        for f in relevant:
                            if f["kind"] in ("split", "stock_dividend"):
                                ratio = number(f.get("share_ratio"))
                                if ratio is None or ratio <= 0:
                                    raise ValueError("reviewed split ratio missing")
                                effective = timestamp(f["effective_at"]).astimezone(KST).date().isoformat()
                                c = [x / ratio if d < effective else x for d, x in zip(history_days, c)]
                                v = [x * ratio if d < effective else x for d, x in zip(history_days[-21:], v)]
                        if state == "eligible":
                            accepted.append(ticker)
                            inputs[ticker] = (c, v, cap)
        row = {"ticker": ticker, "security_id": isin, "state": state, "reasons": reasons}
        rows.append(row)
        if state == "input_gap":
            gaps.append(row)
    state = ("input_gap" if gaps else "ready" if len(accepted) >= spec["min_feature_eligible"]
             else "no_new_decision_insufficient_universe")
    result = {"date": date, "decision_cutoff": cutoff.isoformat(), "state": state,
        "tickers": accepted, "provisional_tickers": provisional, "rows": rows, "gaps": gaps,
        "features": None}
    if state == "ready":
        data = core.features(np.asarray([inputs[t][0] for t in accepted]).T,
            np.asarray([inputs[t][1] for t in accepted]).T, [inputs[t][2] for t in accepted])
        result["features"] = {k: v.tolist() if isinstance(v, np.ndarray) else v for k, v in data.items()}
    return result


def label(date, ticker, security, sessions, prices, identities, evidence, fact_cutoff):
    """Build a per-original-share label; never accept a caller-supplied return."""
    entry, exit_ = sessions[0], sessions[-1]
    if tuple(sessions) != label_sessions(date):
        raise ValueError("label must end at exactly t+6")
    start_at, end_at = session_time(entry, "open"), session_time(exit_, "open")
    cutoff = timestamp(fact_cutoff)
    result = {"decision_date": date, "ticker": ticker, "security_id": security,
        "entry_session": entry, "exit_session": exit_, "label_fact_cutoff": cutoff.isoformat(),
        "state": "source_missing", "return": None, "source_evidence_ids": [], "components": []}

    def finish(state, reason):
        return {**result, "state": state, "reason": reason}

    if cutoff < end_at:
        return finish("source_missing", "outcome_after_fact_cutoff")
    row = prices.get(entry, {}).get(ticker, {})
    op, volume = number(row.get("TDD_OPNPRC")), number(row.get("ACC_TRDVOL"))
    if op is None or volume is None or op < 0 or volume < 0:
        return finish("source_missing", "entry_original_missing")
    if evidence.halted(security, start_at, cutoff) or volume == 0:
        return finish("not_enterable", "entry_not_tradable")
    if op <= 0:
        return finish("open_timing_unknown", "entry_open_not_verified")
    if identities.get(entry, {}).get(ticker) != security:
        return finish("source_missing", "entry_security_identity_unverified")
    cert = evidence.coverage(security, entry, exit_, LABEL_KINDS)
    if cert is None:
        return finish("source_missing", "economic_scope_not_certified")
    # A future-only final fact is unavailable, not evidence of no entitlement.
    if any(f["security_id"] == security and start_at < timestamp(f["effective_at"]) <= end_at
           and available_at(f) > cutoff and not any(old["event_id"] == f["event_id"]
               for old in evidence.versions(security, cutoff)) for f in evidence.facts):
        return finish("source_missing", "entitlement_facts_after_model_freeze")
    events = [f for f in evidence.versions(security, cutoff)
              if start_at < timestamp(f["effective_at"]) <= end_at]
    effective_counts = Counter(timestamp(f["effective_at"]) for f in events
                              if f["kind"] not in ("halt", "share_count_change"))
    if any(n > 1 for n in effective_counts.values()):
        return finish("unpriced_right", "simultaneous_entitlement_order_unverified")
    quantities, cash = {security: 1.0}, 0.0
    proof = {cert.reconciliation_sha256}
    for fact in events:
        proof.add(fact["source_sha256"])
        kind, held = fact["kind"], quantities.get(security, 0)
        if kind in ("split", "stock_dividend"):
            ratio = number(fact.get("share_ratio"))
            if ratio is None or ratio <= 0:
                raise ValueError("invalid sourced share ratio")
            quantities[security] = held * ratio
        elif kind == "cash_dividend":
            amount = number(fact.get("cash_per_share"))
            if (amount is None or amount < 0 or fact.get("amount_status") != "final"
                    or fact.get("currency") != "KRW"):
                return finish("unpriced_right", "dividend_amount_not_final")
            cash += held * amount
        elif kind in ("merger_exchange", "spin_off"):
            # Successor-on-successor events require a separate audited chain.
            if (fact.get("terms_status") != "final" or not fact.get("components")
                    or fact.get("conversion_basis") != "all_assets_per_old_share"):
                return finish("unpriced_right", "conversion_terms_not_final")
            for part in fact["components"]:
                ratio = number(part.get("ratio"))
                if ratio is None or ratio < 0:
                    raise ValueError("invalid successor ratio")
                isin = part["security_id"]
                quantities[isin] = quantities.get(isin, 0) + held * ratio
            quantities[security] -= held
            amount = number(fact.get("cash_per_share", 0))
            if amount is None or amount < 0 or (amount and fact.get("currency") != "KRW"):
                raise ValueError("invalid conversion cash")
            cash += held * amount
        elif kind == "cash_exchange":
            amount = number(fact.get("cash_per_share"))
            if (amount is None or amount <= 0 or fact.get("amount_status") != "final"
                    or fact.get("currency") != "KRW"):
                return finish("unpriced_right", "cash_exchange_amount_not_final")
            cash += held * amount
            quantities[security] = 0
        elif kind == "share_count_change" and fact.get("holder_quantity_effect") != "unchanged":
            return finish("unpriced_right", "holder_quantity_change_unverified")
        elif kind not in ("halt", "share_count_change", "delisting"):
            return finish("unpriced_right", "unsupported_entitlement")
    total = cash
    for isin, quantity in sorted(quantities.items()):
        if quantity == 0:
            continue
        mapped = [t for t, i in identities.get(exit_, {}).items() if i == isin]
        if len(mapped) != 1:
            return finish("unpriced_right", "exit_security_identity_missing")
        end_row = prices.get(exit_, {}).get(mapped[0], {})
        mark = number(end_row.get("TDD_OPNPRC"))
        if evidence.halted(isin, end_at, cutoff) or number(end_row.get("ACC_TRDVOL")) == 0:
            return finish("censored_exit", "exit_not_tradable")
        if mark is None or mark <= 0:
            return finish("unpriced_right", "exit_mark_missing")
        if isin != security:
            successor_cert = evidence.coverage(isin, entry, exit_, LABEL_KINDS)
            if successor_cert is None:
                return finish("source_missing", "successor_scope_not_certified")
            proof.add(successor_cert.reconciliation_sha256)
            if any(start_at < timestamp(f["effective_at"]) <= end_at
                   for f in evidence.versions(isin, cutoff)):
                return finish("unpriced_right", "successor_event_chain_unsupported")
        total += quantity * mark
        result["components"].append({"security_id": isin, "quantity": quantity, "mark": mark})
    if not math.isfinite(total / op):
        return finish("source_missing", "economic_value_not_finite")
    return {**result, "state": "verified_economic_total_return", "return": total / op - 1,
        "source_evidence_ids": sorted(proof), "cash_and_receivables": cash,
        "entry_open": op, "exit_wealth": total}


def sample(snapshot, labels):
    """A label failure never mutates the input snapshot or drops just one name."""
    gaps = [t for t in snapshot["tickers"] if t not in labels or
            labels[t]["state"] != "verified_economic_total_return"]
    if snapshot["state"] != "ready" or gaps:
        return None
    names, f = snapshot["tickers"], snapshot["features"]
    identities = {r["ticker"]: r["security_id"] for r in snapshot.get("rows", [])}
    if any(labels[t].get("ticker") != t or labels[t].get("security_id") != identities.get(t)
           or number(labels[t].get("return")) is None or not labels[t].get("source_evidence_ids") for t in names):
        raise ValueError("sample outcome identity/evidence differs")
    exits = {labels[t]["exit_session"] for t in names}
    if len(exits) != 1 or any(labels[t]["decision_date"] != snapshot["date"] for t in names):
        raise ValueError("sample label identity/date mismatch")
    return {"date": snapshot["date"], "exit_date": exits.pop(), "tickers": names,
        "features": f["stock"], "market_features": f["market"],
        "returns": [labels[t]["return"] for t in names],
        "label_basis": "verified_economic_total_return",
        "source_evidence_ids": sorted({s for t in names for s in labels[t]["source_evidence_ids"]})}


def readiness(rows, spec):
    phases = {}
    for name, start, end, minimum in (
        ("train", spec["train_start"], spec["train_end"], spec["min_complete_train_dates"]),
        ("calibration", spec["calibration_start"], spec["calibration_end"], spec["min_complete_calibration_dates"]),
        ("evaluation", spec["evaluation_start"], spec["evaluation_end"], spec["min_evaluation_sessions"])):
        selected = [r for r in rows if start <= r["date"] <= end]
        complete = sum(r.get("sample") is not None for r in selected)
        phases[name] = {"decision_dates": len(selected), "input_ready_dates": sum(
            r["snapshot"]["state"] == "ready" for r in selected), "complete_label_dates": complete,
            "required_dates": minimum, "label_states": dict(Counter(
                v["state"] for r in selected for v in r["labels"].values()))}
    ready = all(phases[p]["complete_label_dates"] >= phases[p]["required_dates"]
                for p in ("train", "calibration"))
    return {"state": "ready_for_fit" if ready else "blocked_data", "phases": phases}
