"""Offline net-alpha order/cash mechanics, not a certified strategy backtest.

The caller owns source/action validation, rights and portfolio valuation. This
kernel consumes only precommitted quantities and declared auction assumptions;
it cannot certify absent actions or turn synthetic fills into performance proof.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import math

import numpy as np

from signal_desk import market_clock
from signal_desk.signals import research_net_alpha as core


def path_grid(spec):
    paths = [(scenario, arm, phase) for scenario in spec["scenarios"]
             for arm in spec["arms"] for phase in range(spec["phase_count"])]
    if len(paths) != spec["expected_paths"] or len(set(paths)) != len(paths):
        raise ValueError("frozen path grid differs")
    return paths


def portfolio_plan(snapshot, model, bias, previous, caps, spec, *, arm, held_history=None):
    """Frozen six-arm targets; no outcomes or future eligibility mask are read.

    Snapshot provenance is admitted by the v2 input builder, not by this helper.
    Extra held histories must already use the same as-of action units as U.
    """
    if arm not in spec["arms"] or snapshot["state"] != "ready" or not math.isfinite(bias) or bias < 0:
        raise ValueError("plan needs admitted as-of inputs and frozen calibration")
    names, f = snapshot["tickers"], snapshot["features"]
    full = sorted(set(names) | set(previous))
    if set(caps) != set(names) or any(not math.isfinite(caps[t]) or caps[t] <= 0 for t in names):
        raise ValueError("plan market caps differ from input universe")
    daily = core._finite(f["daily_returns"], (spec["covariance_sessions"], len(names)))
    budget, _ = core.risk(daily, f["market_sigma20"], spec)
    if arm == "cash":
        return {"weights": {}, "budget": budget, "priority": {}, "force_roundtrip": False}
    if arm == "cap":
        weights = {t: budget/spec["max_positions"] for t in sorted(names, key=lambda t: (-caps[t], t))[:spec["new_candidate_count"]]}
        return {"weights": weights, "budget": budget, "priority": dict(caps), "force_roundtrip": False}
    history = {t: daily[:, i] for i,t in enumerate(names)}
    for t in set(previous)-set(names):
        if t not in (held_history or {}):
            raise ValueError("held risk/unit history missing; defer optimization")
        history[t] = core._finite(held_history[t], (spec["covariance_sessions"],))
    budget, covariance = core.risk(np.column_stack([history[t] for t in full]), f["market_sigma20"], spec)
    if arm == "fixed_budget":
        budget = 1.0
    alpha_array, market = core.predict(f["stock"], f["market"], model, spec)
    alpha = dict(zip(names, map(float, alpha_array)))
    if arm == "shuffled_alpha":
        targets = sorted(names, key=lambda t: hashlib.sha256(f"{spec['seed']}|{snapshot['date']}|{t}".encode()).hexdigest())
        alpha = dict(zip(targets, (alpha[t] for t in sorted(names))))
    expected = {t: market+alpha[t]-bias for t in names}
    choice = core.choose(names, alpha, expected, previous, covariance, budget, spec)
    weights, priority = choice["weights"], alpha
    return {"weights": weights, "budget": budget, "priority": dict(priority),
        "force_roundtrip": arm == "forced_roundtrip"}


class ExecutionBook:
    def __init__(self, spec, scenario):
        if scenario not in spec["scenarios"]:
            raise ValueError("execution scenario outside frozen contract")
        self.spec = deepcopy(spec)
        self.scenario = scenario
        self.side_cost = int(scenario.split("bp", 1)[0])/20_000
        self.settled_cash = float(spec["initial_cash_krw"])
        self.settlements = []
        self.holdings = {}
        self.pending = []
        self.fills = []
        self.events = []
        self.fees = 0.0
        self.last_open = None
        self.last_decision = None

    @property
    def buying_power(self):
        """Declared settlement-credit convention, not withdrawable cash."""
        return self.settled_cash+sum(r["amount"] for r in self.settlements)

    def _day(self, day):
        if not market_clock.is_session("kr", day) or not self.spec["evaluation_start"] <= day <= self.spec["liquidation_through"]:
            raise ValueError("execution outside frozen market calendar")

    def submit(self, day, target, closes, volumes, nav, *, priority, input_id, budget, force_roundtrip=False):
        """Replace residuals only on a new valid scheduled decision, at close."""
        self._day(day)
        if (not input_id or type(budget) not in (int, float) or not math.isfinite(budget) or not 0 < budget <= 1
                or self.last_decision is not None and day <= self.last_decision
                or self.last_open is not None and day < self.last_open):
            raise ValueError("duplicate or out-of-order decision")
        names = set(target) | set(self.holdings)
        if (not set(target) <= set(priority) or not all(math.isfinite(priority[t]) for t in target)
                or any(t not in closes or t not in volumes for t in names)):
            raise ValueError("incomplete decision inputs")
        if (len(self.holdings) > self.spec["max_positions"] or len(target) > self.spec["max_positions"]
                or sum(target.values()) > 1+1e-12):
            raise ValueError("portfolio/action state needs caller review")
        # Build and validate everything before changing the old residual queue.
        orders = core.orders(target, self.holdings, closes, volumes, nav, self.spec)
        if force_roundtrip:
            sells = core.orders({}, self.holdings, closes, volumes, nav, self.spec)
            buys = core.orders(target, {}, closes, volumes, nav, self.spec)
            orders = sells+buys
        if day > self.spec["evaluation_end"] and any(o["side"] == "buy" for o in orders):
            raise ValueError("no new buys in frozen liquidation tail")
        delay = 2 if self.scenario.endswith("delay1") else 1
        due = market_clock.next_sessions("kr", day, delay)[-1]
        new = [{**o, "remaining": o["shares"], "attempts": 0, "decision_on": day,
            "due_on": due, "input_id": input_id, "budget": budget,
            "priority": priority.get(o["ticker"], 0)} for o in orders]
        for old in self.pending:
            self.events.append({"state": "replaced_by_valid_decision", "on": day,
                "ticker": old["ticker"], "remaining": old["remaining"], "input_id": old["input_id"]})
        self.pending = new
        self.last_decision = day
        return deepcopy(new)

    def open_session(self, day, opens):
        """Open observations never determine a larger quantity or volume limit.

        ``opens[ticker]`` requires price, an explicit open-tradable flag and a
        source ID. Daily full-session volume is deliberately not consumed here.
        """
        self._day(day)
        if self.last_open is not None and not market_clock.consecutive_sessions("kr", self.last_open, day):
            raise ValueError("missing or repeated execution session")
        if self.last_open is None and self.last_decision is not None and market_clock.next_sessions("kr", self.last_decision, 1) != [day]:
            raise ValueError("missing first execution session")
        if self.last_decision is not None and day <= self.last_decision:
            raise ValueError("open must follow close decision")
        self.last_open = day
        due_cash = [r for r in self.settlements if r["on"] <= day]
        self.settled_cash += sum(r["amount"] for r in due_cash)
        self.settlements = [r for r in self.settlements if r["on"] > day]
        queue = sorted(self.pending, key=lambda o: (o["side"] != "sell", -o["priority"], o["ticker"]))
        pending, filled = [], []
        for order in queue:
            if day < order["due_on"]:
                pending.append(order)
                continue
            order["attempts"] += 1
            ticker, side = order["ticker"], order["side"]
            observation = opens.get(ticker, {})
            price = observation.get("price")
            valid = (type(price) in (int, float) and math.isfinite(price) and price > 0
                and observation.get("open_tradable") is True and bool(observation.get("source_id")))
            quantity, reason = 0, "open_unavailable_or_unverified"
            if valid:
                quantity, reason = order["remaining"], "filled"
                if self.scenario.endswith("none"):
                    quantity, reason = 0, "assumed_no_fill"
                elif side == "buy" and self.scenario.endswith("half") and not self.scenario.endswith("sell_half"):
                    quantity //= 2
                elif side == "sell" and self.scenario.endswith("sell_half"):
                    quantity //= 2
                if not quantity and reason == "filled":
                    reason = "assumed_half_fill_rounded_to_zero"
                if side == "buy" and quantity:
                    if price > order["limit"]:
                        quantity, reason = 0, "buy_limit_not_met"
                    else:
                        quantity = min(quantity, max(0, math.floor(self.buying_power/(price*(1+self.side_cost)))))
                        marks = [opens.get(t, {}) for t in self.holdings]
                        if (ticker not in self.holdings and len(self.holdings) >= self.spec["max_positions"]):
                            quantity, reason = 0, "position_slots_still_occupied"
                        elif any(type(m.get("price")) not in (int, float) or not math.isfinite(m["price"])
                                or m["price"] <= 0 or m.get("open_tradable") is not True or not m.get("source_id") for m in marks):
                            quantity, reason = 0, "held_open_mark_unavailable"
                        else:
                            invested = sum(q*opens[t]["price"] for t,q in self.holdings.items())
                            nav = self.buying_power+invested
                            b = order["budget"]
                            # Paying the buy fee also lowers NAV; do not spend
                            # the full-session volume or assume old sells filled.
                            capacity = max(0, math.floor((b*nav-invested)/(price*(1+b*self.side_cost))))
                            quantity = min(quantity, capacity)
                            if not quantity:
                                reason = "cash_or_exposure_limit"
                elif side == "sell":
                    quantity = min(quantity, self.holdings.get(ticker, 0))
            if quantity:
                amount, fee = quantity*price, quantity*price*self.side_cost
                cash_delta = amount-fee if side == "sell" else -amount-fee
                self.settlements.append({"on": market_clock.next_sessions("kr", day, 2)[-1],
                    "amount": cash_delta, "input_id": order["input_id"]})
                self.holdings[ticker] = self.holdings.get(ticker, 0)+(quantity if side == "buy" else -quantity)
                if not self.holdings[ticker]:
                    del self.holdings[ticker]
                order["remaining"] -= quantity
                self.fees += fee
                fill = {"on": day, "ticker": ticker, "side": side, "shares": quantity,
                    "price": price, "fee": fee, "source_id": observation["source_id"],
                    "decision_on": order["decision_on"], "input_id": order["input_id"]}
                filled.append(fill); self.fills.append(fill)
            if order["remaining"]:
                # Initial attempt plus five regular sessions; halts consume time.
                retry = side == "sell" and order["attempts"] <= self.spec["sell_residual_attempts"] and not self.scenario.endswith("none")
                if retry:
                    order["due_on"] = market_clock.next_sessions("kr", day, 1)[0]
                    pending.append(order)
                self.events.append({"state": "sell_retry" if retry else "cancelled_residual",
                    "reason": reason if not quantity else "partial_fill", "on": day,
                    "ticker": ticker, "remaining": order["remaining"], "attempts": order["attempts"],
                    "input_id": order["input_id"]})
        self.pending = pending
        if self.buying_power < -1e-6 or any(type(q) is not int or q <= 0 for q in self.holdings.values()):
            raise ValueError("execution cash or quantity conservation failed")
        return deepcopy(filled)

    def submit_exits(self, day, tickers, closes, volumes, *, input_id):
        """Independent known exits/terminal liquidation; no guessed NAV or buys."""
        self._day(day)
        if (not input_id or self.last_decision is not None and day <= self.last_decision
                or self.last_open is not None and day < self.last_open):
            raise ValueError("duplicate or out-of-order exit decision")
        orders = []
        for ticker in sorted(set(tickers) & set(self.holdings)):
            price, volume = closes.get(ticker), volumes.get(ticker)
            if (type(price) not in (int, float) or not math.isfinite(price) or price <= 0
                    or type(volume) not in (int, float) or not math.isfinite(volume) or volume < 0):
                continue
            quantity = min(self.holdings[ticker], math.floor(volume*self.spec["prior_volume_participation"]))
            if quantity > 0:
                delay = 2 if self.scenario.endswith("delay1") else 1
                orders.append({"ticker": ticker, "side": "sell", "shares": quantity,
                    "remaining": quantity, "attempts": 0, "limit": None, "basis_close": price,
                    "decision_on": day, "due_on": market_clock.next_sessions("kr", day, delay)[-1],
                    "input_id": input_id, "budget": 1, "priority": 0})
        if orders:
            names = {o["ticker"] for o in orders}
            keep = []
            for old in self.pending:
                if old["ticker"] in names:
                    self.events.append({"state": "replaced_by_valid_exit", "on": day,
                        "ticker": old["ticker"], "remaining": old["remaining"], "input_id": old["input_id"]})
                else:
                    keep.append(old)
            self.pending = keep+orders
            self.last_decision = day
        return deepcopy(orders)

    def price_nav(self, closes):
        """Price-only diagnostic. Rights/action completeness must be checked outside."""
        if any(type(closes.get(t)) not in (int, float) or not math.isfinite(closes[t]) or closes[t] <= 0
               for t in self.holdings):
            return None
        return self.buying_power+sum(q*closes[t] for t,q in self.holdings.items())

    def state(self):
        return deepcopy({"settled_cash": self.settled_cash, "buying_power": self.buying_power,
            "settlements": self.settlements, "holdings": self.holdings, "pending": self.pending,
            "fees": self.fees, "fills": self.fills, "events": self.events,
            "return_basis": "execution_kernel_only_not_certified_total_return",
            "strict_pit_eligible": False, "live_order_eligible": False})
