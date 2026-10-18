"""As-of rights bridge for the frozen research execution book. Never live orders.

Event presence is not scope completeness. Unsupported/fractional/late conversions
remain explicit gaps; no prices, completed payments or delivery dates are made up.
"""

from copy import deepcopy
import datetime as dt
from decimal import Decimal

from signal_desk import market_clock
from signal_desk.signals import research_action_clocks as clocks
from signal_desk.signals.research_net_alpha_v2 import LABEL_KINDS, number


def date_end(day):
    if market_clock.is_session("kr", day):
        return clocks.session_time(day)+dt.timedelta(microseconds=1)
    return dt.datetime.combine(dt.date.fromisoformat(day), dt.time.max, clocks.KST)


def whole_shares(quantity, ratio):
    value = Decimal(str(quantity))*Decimal(str(ratio))
    if not value.is_finite() or value < 0 or value != value.to_integral_value():
        return None
    return int(value)


class AccountLedger:
    def __init__(self, book, evidence, identities, portfolio_id):
        self.book, self.evidence, self.identities = book, evidence, identities
        self.rights = clocks.RightsLedger(portfolio_id)
        self.claims, self.conversions, self.tracked = {}, {}, {}
        self.history, self.gaps, self.journal = [], [], []
        self.last_at = None
        self.units = {}
        self.position_identity = {}
        self.blocked_units = set()

    def _gap(self, day, security, event, reason):
        row = {"on": day, "security_id": security, "event_id": event, "reason": reason}
        if row not in self.gaps:
            self.gaps.append(row)
        if security and reason in {
                "held_security_identity_changed_without_conversion", "holder_quantity_effect_unverified",
                "conflicting_or_simultaneous_action", "entitlement_boundary_unverified",
                "unsupported_account_entitlement", "split_terms_or_fraction_unverified",
                "late_conversion_unreconciled", "applied_conversion_revision_unreconciled",
                "delivered_stock_revision_needs_reconciliation"}:
            self.blocked_units.add(security)
            ticker = self.tracked.get(security)
            if ticker:
                self.cancel_orders(ticker, day, "cancelled_for_unresolved_units")

    def cancel_orders(self, ticker, day, reason, *, buys_only=False):
        keep = []
        for order in self.book.pending:
            if (ticker is None or order["ticker"] == ticker) and (not buys_only or order["side"] == "buy"):
                self.book.events.append({"state": reason, "on": day, "ticker": order["ticker"],
                    "remaining": order["remaining"], "input_id": order["input_id"]})
            else:
                keep.append(order)
        self.book.pending = keep

    def _owners_at(self, security, at):
        # A before-open boundary precedes all open fills. For later disclosures,
        # use the recorded holdings then, never today's remaining position.
        rows = [r for r in self.history if r["at"] <= at]
        if not rows:
            return 0
        row = rows[-1]
        return sum(q for t,q in row["holdings"].items() if row["identities"].get(t) == security)

    def record(self, day, at):
        actual = self.identities.get(day, {})
        self.position_identity = {t:s for t,s in self.position_identity.items() if t in self.book.holdings}
        for ticker in self.book.holdings:
            security = actual.get(ticker)
            prior = self.position_identity.get(ticker)
            if prior and prior != security:
                self._gap(day, prior, None, "held_security_identity_changed_without_conversion")
                continue
            if security:
                self.position_identity[ticker] = security
                self.tracked[security] = ticker
            else:
                self._gap(day, None, None, "held_security_identity_missing")
        self.history.append({"at": at, "holdings": dict(self.book.holdings), "identities": dict(self.position_identity)})

    def _claim(self, event, quantity, role, security, ticker):
        key = (event["event_id"], security, role)
        claim = self.claims.setdefault(key, {"event_id": event["event_id"], "security_id": security,
            "ticker": ticker, "role": role, "owners": quantity, "amount": None, "paid": 0.0,
            "delivered": False, "event_boundary": clocks.event_boundary(event).isoformat()})
        claim["owners"] = quantity
        return claim

    def _cash(self, event, claim, at, day):
        amount = number(clocks.value(event, "cash_per_share"))
        final = (amount is not None and amount >= 0 and clocks.value(event, "amount_status") == "final"
                 and clocks.value(event, "currency") == "KRW")
        if not final:
            claim["amount"] = None
            return
        total = claim["owners"]*amount
        delta = self.rights.accrue(event["event_id"], claim["security_id"], "cash_receivable", total)
        claim["amount"] = total
        paid_on = clocks.value(event, "payment_confirmed_on")
        if claim["delivered"]:
            # A later confirmed change is a delta now, not a second full payment.
            self.book.settled_cash += delta
            claim["paid"] += delta
        elif paid_on and date_end(paid_on) <= at:
            delivered = self.rights.deliver(event["event_id"], claim["security_id"], "cash_receivable")
            self.book.settled_cash += delivered
            claim.update(delivered=True, paid=delivered)
            self.journal.append({"on": day, "role": "cash_transfer", "amount": delivered,
                                 "event_id": event["event_id"], "wealth_delta": 0})

    def _stock(self, event, claim, at, day):
        ratio = number(clocks.value(event, "additional_share_ratio"))
        if ratio is None or ratio < 0:
            self._gap(day, claim["security_id"], event["event_id"], "stock_dividend_ratio_unverified")
            return
        quantity = whole_shares(claim["owners"], ratio)
        if quantity is None:
            self._gap(day, claim["security_id"], event["event_id"], "fractional_share_terms_unverified")
            return
        old = claim.get("quantity")
        if claim["delivered"] and old != quantity:
            self._gap(day, claim["security_id"], event["event_id"], "delivered_stock_revision_needs_reconciliation")
            return
        claim["quantity"] = quantity
        self.rights.accrue(event["event_id"], claim["security_id"], "share_receivable", quantity)
        raw_boundary = clocks.value(event, "delivery_boundary")
        delivered_on = clocks.value(event, "delivery_on")
        delivery = clocks.boundary(raw_boundary) if raw_boundary else date_end(delivered_on) if delivered_on else None
        tradable = clocks.value(event, "tradable_from_session")
        if not claim["delivered"] and delivery and delivery <= at and tradable and tradable <= day:
            ticker = claim["ticker"]
            if self.identities.get(day, {}).get(ticker) != claim["security_id"]:
                self._gap(day, claim["security_id"], event["event_id"], "delivery_security_identity_missing")
                return
            self.cancel_orders(ticker, day, "cancelled_for_share_delivery")
            self.book.holdings[ticker] = self.book.holdings.get(ticker, 0)+int(self.rights.deliver(
                event["event_id"], claim["security_id"], "share_receivable"))
            if not self.book.holdings[ticker]:
                del self.book.holdings[ticker]
            claim["delivered"] = True
            self.journal.append({"on": day, "event_id": event["event_id"], "role": "share_transfer", "wealth_delta": 0})

    def advance(self, day, phase):
        if phase not in ("before_open", "close", "after_close"):
            raise ValueError("invalid account processing phase")
        at = clocks.session_time(day, "open" if phase == "before_open" else "close")
        if phase == "after_close":
            at += dt.timedelta(microseconds=1)
        if self.last_at is not None and at < self.last_at:
            raise ValueError("account clock moved backwards")
        # Capture pre-order owners just before the declared entitlement phase.
        stamp = at-dt.timedelta(microseconds=2) if phase == "before_open" else at
        self.record(day, stamp)
        for security, ticker in list(self.tracked.items()):
            start = self.history[0]["at"].astimezone(clocks.KST).date().isoformat()
            roots = {a["event_id"] for a in self.evidence.actions.dependencies(security, start, day, "account", at)}
            events = [e for e in self.evidence.actions.events(security, at, "account") if e["event_id"] in roots]
            relevant = []
            for event in events:
                if event["kind"] == "share_count_change":
                    if clocks.value(event, "holder_quantity_effect") != "unchanged":
                        self._gap(day, security, event["event_id"], "holder_quantity_effect_unverified")
                    continue
                if event["kind"] in ("halt", "delisting", "symbol_change"):
                    continue
                boundary = clocks.event_boundary(event)
                if boundary is None:
                    if self.book.holdings.get(ticker) or any(c["security_id"] == security for c in self.claims.values()):
                        self._gap(day, security, event["event_id"], "entitlement_boundary_unverified")
                    continue
                if boundary <= at:
                    relevant.append((boundary, event))
            for boundary, event in relevant:
                event_id = event["event_id"]
                owners = self._owners_at(security, boundary)
                if not owners and (event_id, security, "cash") not in self.claims and (event_id, security, "stock") not in self.claims:
                    continue
                if event["conflicts"] or sum(b == boundary for b,e in relevant) > 1:
                    self._gap(day, security, event_id, "conflicting_or_simultaneous_action")
                    continue
                if any(c["event_id"] == event_id and c["security_id"] == security
                       and c["event_boundary"] != boundary.isoformat() for c in self.claims.values()):
                    self._gap(day, security, event_id, "accrued_entitlement_boundary_revision_unreconciled")
                    continue
                if event["kind"] == "cash_dividend":
                    self._cash(event, self._claim(event, owners, "cash", security, ticker), at, day)
                elif event["kind"] == "stock_dividend":
                    self._stock(event, self._claim(event, owners, "stock", security, ticker), at, day)
                elif event["kind"] == "split":
                    key = f"{security}:{event_id}"
                    if key in self.conversions:
                        if (clocks.value(event, "share_ratio") != self.conversions[key]["ratio"]
                                or boundary.isoformat() != self.conversions[key]["boundary"]):
                            self._gap(day, security, event_id, "applied_conversion_revision_unreconciled")
                        continue
                    ratio = number(clocks.value(event, "share_ratio"))
                    quantity = whole_shares(owners, ratio) if ratio is not None and ratio > 0 else None
                    price_day, tradable = clocks.value(event, "price_basis_from_session"), clocks.value(event, "tradable_from_session")
                    if quantity is None or not price_day or not tradable:
                        self._gap(day, security, event_id, "split_terms_or_fraction_unverified")
                        continue
                    if self.last_at is not None and boundary <= self.last_at:
                        self._gap(day, security, event_id, "late_conversion_unreconciled")
                        continue
                    self.cancel_orders(ticker, day, "cancelled_for_unit_change")
                    self.book.holdings[ticker] = quantity
                    self.conversions[key] = {"security_id": security, "event_id": event_id,
                        "ratio": ratio, "quantity": quantity, "boundary": boundary.isoformat()}
                    self.units[security] = {"price_from": price_day, "tradable_from": tradable}
                    self.journal.append({"on": day, "event_id": event_id, "role": "split_conversion", "quantity": quantity})
                else:
                    self._gap(day, security, event_id, "unsupported_account_entitlement")
        self.last_at = at

    def gate_opens(self, day, opens, *, at=None):
        result = deepcopy(opens)
        at = at or clocks.session_time(day, "open")
        for ticker in set(self.book.holdings) | {o["ticker"] for o in self.book.pending}:
            security = self.identities.get(day, {}).get(ticker)
            unit = self.units.get(security, {})
            safe = (security is not None and self.evidence.coverage(security, day, day, LABEL_KINDS) is not None
                and security not in self.blocked_units
                and (ticker not in self.book.holdings or self.position_identity.get(ticker) == security)
                and self.evidence.halted(security, at, at) is False
                and day >= unit.get("price_from", day) and day >= unit.get("tradable_from", day))
            if not safe and ticker in result:
                result[ticker]["open_tradable"] = False
        return result

    def frame(self, day, prices, at):
        """Immutable historical cash/position/claim snapshot, before later facts."""
        value = self.book.price_nav(prices)
        reasons = []
        for ticker in self.book.holdings:
            security = self.identities.get(day, {}).get(ticker)
            unit = self.units.get(security, {})
            if (security is None or self.position_identity.get(ticker) != security
                    or self.evidence.coverage(security, day, day, LABEL_KINDS) is None
                    or self.evidence.halted(security, at, at) is not False
                    or day < unit.get("price_from", day) or day < unit.get("tradable_from", day)):
                reasons.append("held_unit_identity_halt_or_scope_unverified")
        for claim in self.claims.values():
            if not claim["delivered"] and self.evidence.coverage(claim["security_id"], day, day, LABEL_KINDS) is None:
                reasons.append("right_scope_unverified")
        if self.gaps:
            reasons.append("account_action_gap")
        return deepcopy({"on": day, "at": at.isoformat(), "base_value": value,
            "claims": list(self.claims.values()), "reasons": reasons,
            "tracked_securities": list(self.tracked), "conversions": self.conversions,
            "history_from": self.history[0]["at"].isoformat() if self.history else at.isoformat()})

    def mark(self, frame, *, final_at=None):
        """Final facts revalue recorded rights, never historical orders/cash."""
        value, reasons = frame["base_value"], list(frame["reasons"])
        at = clocks.timestamp(frame["at"])
        if final_at is not None:
            # A later disclosure may expose a previously unrecorded economic
            # event. Do not silently report a price-only historical NAV then.
            for security in frame["tracked_securities"]:
                recorded = {c["event_id"] for c in frame["claims"] if c["security_id"] == security} | {
                    c["event_id"] for c in frame["conversions"].values() if c["security_id"] == security}
                for event in self.evidence.actions.events(security, final_at, "outcome"):
                    boundary = clocks.event_boundary(event)
                    if (event["kind"] not in ("halt", "delisting", "symbol_change", "share_count_change")
                            and boundary and clocks.timestamp(frame["history_from"]) <= boundary <= at
                            and self._owners_at(security, boundary) and event["event_id"] not in recorded):
                        reasons.append("unrecorded_final_entitlement_requires_reconciliation")
        for claim in frame["claims"]:
            if claim["role"] == "cash":
                amount = claim["amount"]
                if final_at is not None:
                    amount = None
                    events = {e["event_id"]: e for e in self.evidence.actions.events(claim["security_id"], final_at, "outcome")}
                    event = events.get(claim["event_id"])
                    per_share = number(clocks.value(event, "cash_per_share")) if event else None
                    if event and clocks.event_boundary(event) and clocks.event_boundary(event).isoformat() != claim["event_boundary"]:
                        reasons.append("final_entitlement_boundary_differs")
                    if (event and not event["conflicts"] and per_share is not None and per_share >= 0
                            and clocks.value(event, "amount_status") == "final" and clocks.value(event, "currency") == "KRW"):
                        amount = claim["owners"]*per_share
                if amount is None:
                    reasons.append("cash_right_unpriced")
                elif value is not None:
                    value += amount-claim["paid"]
            elif not claim["delivered"]:
                events = {e["event_id"]: e for e in self.evidence.actions.events(claim["security_id"], final_at or at, "outcome" if final_at else "account")}
                event = events.get(claim["event_id"])
                mark = number(clocks.value(event, "receivable_value_per_right")) if event else None
                quantity = claim.get("quantity")
                if final_at is not None:
                    ratio = number(clocks.value(event, "additional_share_ratio")) if event else None
                    quantity = whole_shares(claim["owners"], ratio) if ratio is not None and ratio >= 0 else None
                if (event and clocks.event_boundary(event)
                        and clocks.event_boundary(event).isoformat() != claim["event_boundary"]):
                    reasons.append("final_entitlement_boundary_differs")
                if (mark is None or mark < 0 or not event or event["conflicts"]
                        or clocks.value(event, "currency") != "KRW" or quantity is None):
                    reasons.append("stock_right_unpriced")
                elif value is not None:
                    value += quantity*mark
        return {"value": value if value is not None and not reasons else None,
            "reasons": sorted(set(reasons)), "basis": "ex_post_outcome" if final_at else "asof_decision",
            "live_order_eligible": False}

    def valuation(self, day, prices, at, *, final_at=None):
        return self.mark(self.frame(day, prices, at), final_at=final_at)
