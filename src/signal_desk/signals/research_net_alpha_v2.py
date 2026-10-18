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

import numpy as np

from signal_desk import market_clock
from signal_desk.signals import research_net_alpha as core
from signal_desk.signals.research_action_coverage import CoverageCertificate
from signal_desk.signals import research_action_clocks as clocks
from signal_desk.signals.research_action_clocks import KST, timestamp, session_time, available_at

UNIT_KINDS = frozenset({"split", "stock_dividend", "rights", "capital_reduction",
    "spin_off", "merger_exchange", "symbol_change", "share_count_change", "halt", "delisting"})
LABEL_KINDS = UNIT_KINDS | {"cash_dividend"}


@lru_cache(maxsize=2048)
def label_sessions(day):
    return tuple(market_clock.next_sessions("kr", day, 6))


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

    def __init__(self, documents=None, certificates=(), facts=(), assertions=()):
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
            if fact["kind"] != "halt":
                raise ValueError("legacy effective_at is supported for halt only; explicit action components required")
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
        self.actions = clocks.ActionBook(self.documents, assertions)
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
                and timestamp(f["effective_at"]).astimezone(KST).date().isoformat() <= cert.end
                and (not f.get("end_at") or timestamp(f["end_at"]).astimezone(KST).date().isoformat() >= cert.start))
            if review.get("fact_ids") != actual:
                raise ValueError("reconciliation fact inventory differs")
            assertions = sorted(a["assertion_id"] for a in self.actions.dependencies(
                cert.security_id, cert.start, cert.end, "account"))
            if review.get("assertion_ids", []) != assertions:
                raise ValueError("reconciliation action assertion inventory differs")
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
        if any(f["kind"] == "halt" and timestamp(f["effective_at"]) <= at
            and (not f.get("end_at") or at < timestamp(f["end_at"]))
            for f in self.versions(security, known_by)):
            return True
        for event in self.actions.events(security, known_by, "input"):
            if event["kind"] == "halt":
                if event["conflicts"]:
                    return None
                start, end = clocks.value(event, "halt_start", confirmed=False), clocks.value(event, "halt_end", confirmed=False)
                if start and clocks.boundary(start) <= at and (
                        end is None or at < clocks.boundary(end)):
                    return True
        return False

    def action_events(self, security, start, end, known_by, purpose):
        roots = {a["event_id"] for a in self.actions.dependencies(security, start, end, purpose, known_by)}
        return [e for e in self.actions.events(security, known_by, purpose) if e["event_id"] in roots]


def normalized_history(days, closes, volumes, events):
    """Return same-unit inputs; the exchange reference price is never a fill."""
    c, v = list(closes), list(volumes)
    units = []
    for event in events:
        if event["conflicts"]:
            return None, None, [], "conflicting_action_fields"
        if event["kind"] == "share_count_change" and clocks.value(event, "holder_quantity_effect") != "unchanged":
            return None, None, [], "holder_quantity_change_unverified"
        if event["kind"] in ("halt", "delisting", "share_count_change"):
            continue
        if event["kind"] != "split":
            return None, None, [], "complex_history_units_unsupported"
        date = clocks.value(event, "price_basis_from_session")
        ratio = number(clocks.value(event, "share_ratio"))
        if date is None or ratio is None or ratio <= 0:
            return None, None, [], "split_price_boundary_unverified"
        if date > days[-1]:
            continue
        if not market_clock.is_session("kr", date):
            return None, None, [], "split_price_boundary_not_session"
        c = [x/ratio if d < date else x for d, x in zip(days, c)]
        v = [x*ratio if d < date else x for d, x in zip(days[-len(v):], v)]
        units.append({"event_id": event["event_id"], "price_basis_from_session": date,
            "share_ratio": ratio, "assertion_ids": sorted(event["assertion_ids"]),
            "raw_before_unit_id": f"{event['security_id']}:{event['event_id']}:old",
            "raw_from_unit_id": f"{event['security_id']}:{event['event_id']}:new"})
    return c, v, units, None


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
    rows, accepted, provisional, inputs, gaps, unit_audit = [], [], [], {}, [], {}
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
        elif evidence.halted(isin, cutoff, cutoff) is None or evidence.halted(isin, next_open, cutoff) is None:
            state, reasons = "input_gap", ["conflicting_halt_boundary"]
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
                    relevant = evidence.action_events(isin, history_days[0], date, cutoff, "input")
                    c, v, units, reason = normalized_history(history_days, c, v, relevant)
                    if reason:
                        state, reasons = "input_gap", [reason]
                    else:
                        unexplained = []
                        for previous_day, current_day, before, after in zip(
                                history_days, history_days[1:], history, history[1:]):
                            old, new = number(before.get("LIST_SHRS")), number(after.get("LIST_SHRS"))
                            if old is None or new is None or old <= 0 or new <= 0:
                                unexplained.append(current_day)
                            elif old != new and not any(
                                    clocks.value(f, "listed_share_observed_on") == current_day
                                    and number(clocks.value(f, "listed_shares_before")) == old
                                    and number(clocks.value(f, "listed_shares_after")) == new
                                    and ((f["kind"] == "split" and number(clocks.value(f, "share_ratio")) == new/old)
                                         or clocks.value(f, "holder_quantity_effect") == "unchanged")
                                    for f in relevant):
                                unexplained.append(current_day)
                        # A completed schedule alone does not prove a raw unit change.
                        for unit in units:
                            d = unit["price_basis_from_session"]
                            if history_days[0] < d <= date:
                                i = history_days.index(d)
                                old = number(history[i-1].get("LIST_SHRS"))
                                new = number(history[i].get("LIST_SHRS"))
                                if old is None or new is None or old <= 0 or new/old != unit["share_ratio"]:
                                    unexplained.append(d)
                        if unexplained:
                            state, reasons = "input_gap", ["listed_share_changes_not_reconciled"]
                        if state == "eligible":
                            accepted.append(ticker)
                            inputs[ticker] = (c, v, cap)
                            unit_audit[ticker] = units
        row = {"ticker": ticker, "security_id": isin, "state": state, "reasons": reasons}
        rows.append(row)
        if state == "input_gap":
            gaps.append(row)
    state = ("input_gap" if gaps else "ready" if len(accepted) >= spec["min_feature_eligible"]
             else "no_new_decision_insufficient_universe")
    result = {"date": date, "decision_cutoff": cutoff.isoformat(), "state": state,
        "tickers": accepted, "provisional_tickers": provisional, "rows": rows, "gaps": gaps,
        "features": None, "price_unit_transitions": unit_audit}
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
    if evidence.halted(security, start_at, cutoff) is None:
        return finish("source_missing", "entry_halt_boundary_conflicting")
    if evidence.halted(security, start_at, cutoff) or volume == 0:
        return finish("not_enterable", "entry_not_tradable")
    if op <= 0:
        return finish("open_timing_unknown", "entry_open_not_verified")
    if identities.get(entry, {}).get(ticker) != security:
        return finish("source_missing", "entry_security_identity_unverified")
    cert = evidence.coverage(security, entry, exit_, LABEL_KINDS)
    if cert is None:
        return finish("source_missing", "economic_scope_not_certified")
    dependencies = evidence.actions.dependencies(security, entry, exit_, "outcome")
    visible = {e["event_id"]: e for e in evidence.action_events(security, entry, exit_, cutoff, "outcome")}
    if any(a["event_id"] not in visible for a in dependencies):
        return finish("source_missing", "entitlement_facts_after_model_freeze")
    events = []
    for event in visible.values():
        if event["conflicts"]:
            return finish("unpriced_right", "conflicting_action_fields")
        if event["kind"] == "share_count_change":
            if clocks.value(event, "holder_quantity_effect") != "unchanged":
                return finish("unpriced_right", "holder_quantity_change_unverified")
            continue
        if event["kind"] in ("halt", "delisting"):
            continue
        at = clocks.event_boundary(event)
        if at is None:
            return finish("source_missing", "entitlement_boundary_unverified")
        if start_at < at <= end_at:
            events.append((at, event))
    events.sort(key=lambda item: (item[0], item[1]["event_id"]))
    effective_counts = Counter(at for at, _ in events)
    if any(n > 1 for n in effective_counts.values()):
        return finish("unpriced_right", "simultaneous_entitlement_order_unverified")
    quantities, cash, share_receivables = {security: 1.0}, 0.0, []
    ledger = clocks.RightsLedger(f"{date}:{security}")
    proof = {cert.reconciliation_sha256}
    assertions_used = set()
    for _, event in events:
        assertions_used.update(event["assertion_ids"])
        proof.update(f["source_sha256"] for f in event["fields"].values())
        fact = {k: clocks.value(event, k) for k in event["fields"]}
        kind, held = event["kind"], quantities.get(security, 0)
        if kind == "split":
            ratio = number(fact.get("share_ratio"))
            if ratio is None or ratio <= 0:
                return finish("unpriced_right", "split_ratio_not_final")
            if not fact.get("price_basis_from_session") or not fact.get("tradable_from_session"):
                return finish("source_missing", "split_price_or_tradability_boundary_unverified")
            if fact["price_basis_from_session"] > exit_ or fact["tradable_from_session"] > exit_:
                return finish("censored_exit", "split_shares_not_tradable_at_exit")
            quantities[security] = held * ratio
        elif kind == "stock_dividend":
            ratio = number(fact.get("additional_share_ratio"))
            if ratio is None or ratio < 0:
                return finish("unpriced_right", "stock_dividend_ratio_not_final")
            right = event["event_id"] + ":stock_dividend"
            ledger.accrue(event["event_id"], right, "share_receivable", held * ratio)
            delivered = fact.get("delivery_on")
            tradable = fact.get("tradable_from_session")
            delivery_boundary = fact.get("delivery_boundary")
            delivery_before_exit = (clocks.boundary(delivery_boundary) <= end_at if delivery_boundary else
                                    bool(delivered and delivered < exit_))
            if delivery_before_exit and tradable and tradable <= exit_:
                quantities[security] += ledger.deliver(event["event_id"], right, "share_receivable")
            else:
                mark = number(fact.get("receivable_value_per_right"))
                if mark is None or mark < 0 or fact.get("currency") != "KRW":
                    return finish("unpriced_right", "undelivered_stock_dividend_unvalued")
                share_receivables.append({"event_id": event["event_id"], "quantity": held*ratio, "mark": mark})
                cash += held*ratio*mark
        elif kind == "cash_dividend":
            amount = number(fact.get("cash_per_share"))
            if (amount is None or amount < 0 or fact.get("amount_status") != "final"
                    or fact.get("currency") != "KRW"):
                return finish("unpriced_right", "dividend_amount_not_final")
            cash += ledger.accrue(event["event_id"], event["event_id"]+":cash_dividend", "cash_receivable", held*amount)
        elif kind in ("merger_exchange", "spin_off"):
            # Successor-on-successor events require a separate audited chain.
            if (fact.get("terms_status") != "final" or not fact.get("components")
                    or fact.get("conversion_basis") != "all_assets_per_old_share"):
                return finish("unpriced_right", "conversion_terms_not_final")
            delivery = fact.get("delivery_boundary")
            tradable = fact.get("tradable_from_session")
            if not delivery or not tradable:
                return finish("unpriced_right", "successor_delivery_or_tradability_unverified")
            if clocks.boundary(delivery) > end_at or tradable > exit_:
                return finish("censored_exit", "successor_not_tradable_at_exit")
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
        if evidence.halted(isin, end_at, cutoff) is None:
            return finish("source_missing", "exit_halt_boundary_conflicting")
        if evidence.halted(isin, end_at, cutoff) or number(end_row.get("ACC_TRDVOL")) == 0:
            return finish("censored_exit", "exit_not_tradable")
        if mark is None or mark <= 0:
            return finish("unpriced_right", "exit_mark_missing")
        if isin != security:
            successor_cert = evidence.coverage(isin, entry, exit_, LABEL_KINDS)
            if successor_cert is None:
                return finish("source_missing", "successor_scope_not_certified")
            proof.add(successor_cert.reconciliation_sha256)
            if evidence.actions.dependencies(isin, entry, exit_, "outcome"):
                return finish("unpriced_right", "successor_event_chain_unsupported")
        total += quantity * mark
        result["components"].append({"security_id": isin, "quantity": quantity, "mark": mark})
    if not math.isfinite(total / op):
        return finish("source_missing", "economic_value_not_finite")
    return {**result, "state": "verified_economic_total_return", "return": total / op - 1,
        "source_evidence_ids": sorted(proof), "cash_and_receivables": cash,
        "entry_open": op, "exit_wealth": total, "assertion_ids": sorted(assertions_used),
        "share_receivables": share_receivables, "valuation_basis": "ex_post_outcome"}


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
