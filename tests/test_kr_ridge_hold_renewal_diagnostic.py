"""Synthetic boundaries; explicitly import the staged sibling, never repo runner."""
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

STAGED = Path(__file__).resolve().parents[1] / "scripts/measure/kr_ridge_hold_renewal_diagnostic.py"
spec = importlib.util.spec_from_file_location("staged_hold_renewal", STAGED)
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)
REPO = Path(__file__).resolve().parents[1]
D = m.dependencies(REPO)
CLOCK = D["clock"]
SESSIONS = m.sessions_between(CLOCK, "2024-08-01", "2024-12-30")


def fixture():
    bars = {(d, "000010"): (100., 100.) for d in SESSIONS}
    by_day = {d: {"000010": {"LIST_SHRS": "1000", "ACC_TRDVOL": "1000000"}} for d in SESSIONS}
    panel = SimpleNamespace(sessions=SESSIONS, index={d: i for i, d in enumerate(SESSIONS)}, bars=bars)
    return panel, by_day


def books(cost=75, scenario=None):
    period = {"decisionStart": SESSIONS[0], "decisionEnd": SESSIONS[-1], "tailThrough": SESSIONS[-1]}
    cfg = m.execution_spec(period, cost)
    if scenario:
        cfg["scenarios"] = [scenario]
    return {a: D["execution"].ExecutionBook(cfg, scenario or f"{cost}bp_full") for a in m.ARMS}


def decision(day, picks=(("000010", 1),)):
    return D["old"].CommonDecision(day, m.INITIAL, picks, (("000010", 100.),), (("000010", 1000000.),))


def fill(side, quantity, day, fee=None, price=100.):
    return {"ticker": "000010", "side": side, "shares": quantity, "on": day,
            "price": price, "fee": quantity*price*.00375 if fee is None else fee}


class Boundaries(unittest.TestCase):
    def test_01_staged_import_and_trend_equality(self):
        self.assertEqual(Path(m.__file__).resolve(), STAGED)
        panel, rows = fixture()
        trend = m.trend_at(SESSIONS[25], "000010", panel, rows, {})
        self.assertTrue(trend["passes"])
        self.assertEqual(len(trend["history"]), 20)
        state = m.CandidateState({"000010"})
        self.assertEqual(state.targets({"000010"}, {"000010": 10}, {"000010": trend}, 25), {"000010": 1/6})

    def test_02_never_filled_buy_remains_first_entry(self):
        state, bs = m.CandidateState(), books()
        orders = m.submit_three(bs, decision(SESSIONS[1]), state, {}, 1)
        self.assertTrue(orders["orders"]["candidate"])
        day = SESSIONS[2]
        before = dict(bs["candidate"].holdings)
        fills = bs["candidate"].open_session(day, {"000010": {"price": 102., "open_tradable": True, "source_id": "synthetic"}})
        state.fills(fills, before, 2)
        self.assertFalse(fills)
        self.assertFalse(state.ever_held)
        self.assertEqual(state.targets({"000010"}, {}, {}, 6), {"000010": 1/6})

    def test_03_f4_f5_actual_flat_and_partial_trim(self):
        state = m.CandidateState({"000010"})
        state.fills([fill("sell", 4, SESSIONS[5])], {"000010": 10}, 5)
        self.assertFalse(state.last_flat_session)
        state.fills([fill("sell", 6, SESSIONS[6])], {"000010": 6}, 6)
        trend = {"000010": {"passes": True}}
        self.assertEqual(state.targets({"000010"}, {}, trend, 10), {})
        self.assertEqual(state.targets({"000010"}, {}, trend, 11), {"000010": 1/6})

    def test_04_partial_exit_sticky_until_fill_zero(self):
        state = m.CandidateState({"000010"})
        self.assertEqual(state.targets(set(), {"000010": 10}, {}, 4), {})
        state.fills([fill("sell", 4, SESSIONS[5])], {"000010": 10}, 5)
        self.assertIn("000010", state.exit_pending)
        self.assertEqual(state.targets({"000010"}, {"000010": 6}, {"000010": {"passes": True}}, 9), {})
        state.fills([fill("sell", 6, SESSIONS[10])], {"000010": 6}, 10)
        self.assertNotIn("000010", state.exit_pending)

    def test_05_future_mutation_cannot_change_close_intent(self):
        panel, rows = fixture()
        day = SESSIONS[25]
        a = m.trend_at(day, "000010", panel, rows, {})
        bs = books()
        first = m.submit_three(bs, decision(day), m.CandidateState(), {}, 25)
        for future in SESSIONS[26:]:
            panel.bars[future, "000010"] = (1., 10000.)
            rows[future]["000010"]["ACC_TRDVOL"] = "0"
        self.assertEqual(a, m.trend_at(day, "000010", panel, rows, {}))
        second = m.submit_three(books(), decision(day), m.CandidateState(), {}, 25)
        self.assertEqual(first, second)

    def test_06_last_order_first_next_open_terminal_single_submission(self):
        bs = books()
        day = SESSIONS[20]
        state = m.CandidateState()
        m.submit_three(bs, decision(day), state, {}, 20)
        for book in bs.values():
            fills = book.open_session(SESSIONS[21], {"000010": {"price": 100., "open_tradable": True, "source_id": "synthetic"}})
            self.assertEqual(len(fills), 1)
        for i in range(22, 26):
            for book in bs.values():
                book.open_session(SESSIONS[i], {"000010": {"price": 100., "open_tradable": True, "source_id": "synthetic"}})
        orders = m.submit_three(bs, decision(SESSIONS[25], ()), state, {}, 25, terminal=True)
        self.assertTrue(all(o["side"] == "sell" for arm in orders["orders"].values() for o in arm))
        for i in range(26, 29):
            for book in bs.values():
                book.open_session(SESSIONS[i], {"000010": {"price": 100., "open_tradable": True, "source_id": "synthetic"}})
        self.assertTrue(all(not b.holdings and not b.pending and not b.settlements for b in bs.values()))
        self.assertTrue(all(b.last_decision == SESSIONS[25] for b in bs.values()))

    def test_07_fifo_partial_cost_allocation_and_quantity(self):
        fifo = m.FIFO()
        fifo.fill(fill("buy", 10, SESSIONS[1], fee=10), 1)
        fifo.fill(fill("buy", 5, SESSIONS[2], fee=5, price=110), 2)
        fifo.fill(fill("sell", 4, SESSIONS[3], fee=4, price=120), 3)
        stat = fifo.metrics({"000010": 120}, 3, {"000010": 11})
        self.assertEqual(stat["completedFIFOBuyLots"], 0)
        fifo.fill(fill("sell", 8, SESSIONS[4], fee=8, price=120), 4)
        stat = fifo.metrics({"000010": 120}, 4, {"000010": 3})
        self.assertEqual(stat["completedFIFOBuyLots"], 1)
        self.assertAlmostEqual(stat["fifo_price_pnl"], 250-15-12)

    def test_08_gap_T2_fees_and_three_arm_deltas(self):
        panel, rows = fixture()
        period = {"id": "synthetic", "decisionStart": SESSIONS[25], "decisionEnd": SESSIONS[25], "tailThrough": SESSIONS[36]}
        # Known opening gap then intraday move, no rewrite of prior intent.
        panel.bars[SESSIONS[26], "000010"] = (100.5, 101.)
        result = m.replay(period, 75, 0, {SESSIONS[25]: [("000010", .02)]}, {}, rows, panel, {}, D, m.Budget())
        self.assertIsNone(result["stop"])
        self.assertTrue(all(v["complete"] for v in result["metrics"].values()))
        prefix = result["confirmed_execution_prefix"]["candidate"]
        self.assertEqual(len(prefix["fills"]), 2)
        row = next(r for r in result["trace"] if r["session"] == SESSIONS[26])["arms"]["candidate"]
        self.assertEqual(row["settled_cash"], m.INITIAL)
        self.assertTrue(row["settlements"])
        self.assertAlmostEqual(row["priceNAV"]-m.INITIAL, row["gap"]+row["intraday"]-row["fees"])
        self.assertAlmostEqual(result["deltas"]["candidate-forced"]["afterCostPricePnL"], 0)

    def test_09_historical_MA_unit_issue_joint_null(self):
        panel, rows = fixture()
        rows[SESSIONS[20]]["000010"]["LIST_SHRS"] = "2000"
        with self.assertRaises(m.SourceStop):
            m.trend_at(SESSIONS[25], "000010", panel, rows, {})
        period = {"id": "synthetic", "decisionStart": SESSIONS[25], "decisionEnd": SESSIONS[30], "tailThrough": SESSIONS[42]}
        selected = {SESSIONS[i]: [("000010", .02)] for i in range(25, 31)}
        result = m.replay(period, 75, 0, selected, {}, rows, panel, {}, D, m.Budget())
        self.assertTrue(result["stop"])
        self.assertTrue(all(v["afterCostPricePnL"] is None for v in result["metrics"].values()))
        self.assertTrue(all(v is None for v in result["deltas"].values()))

    def test_10_valid_open_missing_close_preserves_fills(self):
        panel, rows = fixture()
        day = SESSIONS[26]
        panel.bars[day, "000010"] = (100., None)
        rows[day]["000010"]["LIST_SHRS"] = "2000"
        self.assertEqual(m.open_marks(day, {"000010"}, panel)["000010"]["price"], 100)
        period = {"id": "synthetic", "decisionStart": SESSIONS[25], "decisionEnd": SESSIONS[25], "tailThrough": SESSIONS[36]}
        result = m.replay(period, 75, 0, {SESSIONS[25]: [("000010", .02)]}, {}, rows, panel, {}, D, m.Budget())
        self.assertEqual(result["stop"]["stage"], "close")
        self.assertTrue(all(len(v["fills"]) == 1 for v in result["confirmed_execution_prefix"].values()))
        self.assertTrue(all(v["fees"] is None for v in result["metrics"].values()))

    def test_11_incomplete_tail_null_and_timeout_grid(self):
        book = books()["candidate"]
        book.holdings = {"000010": 10}
        book.settled_cash -= 1000
        fifo = m.FIFO()
        fifo.fill(fill("buy", 10, SESSIONS[1], fee=0), 1)
        metrics = m.final_metrics(book, fifo, {"000010": 100}, 2, [m.INITIAL])
        self.assertIsNone(metrics["afterCostPricePnL"])
        self.assertEqual(metrics["reason"], "incomplete_tail")
        with self.assertRaises(TimeoutError):
            m.Budget(seconds=0).check()

    def test_12_gate_failure_survives_other_incomplete_paths(self):
        metric = {"complete": True, "completedFIFOBuyLots": 12, "distinctBuyFillDates": 6,
            "distinctTickers": 6, "afterCostPricePnL": -1, "fees": 10,
            "priceMDDPct": -1, "absoluteTickerPnLShare": .4, "positiveEntryMonths": 2}
        paths = [{"period": "historical_price_2025q1", "cost": 75, "phase": 0,
                  "metrics": {a: dict(metric) for a in m.ARMS}, "stop": None},
                 {"period": "historical_price_2025q1", "cost": 120, "phase": 1, "metrics": {}, "stop": {"reason": "source"}}]
        verdict = m.gates(paths)
        self.assertTrue(verdict["observedGateFailures"])
        self.assertTrue(verdict["sourceOrTimeoutIncomplete"])
        self.assertFalse(verdict["limitedDevelopmentPriceSupport"])
        self.assertFalse(verdict["livePromotion"])

    def test_13_feature_meta_gaps_keep_mask_but_observed_changes_stop_today(self):
        panel, rows = fixture()
        day = SESSIONS[30]
        rows[SESSIONS[3]]["000010"].pop("LIST_SHRS")
        self.assertIsNone(m.feature_history_issue(day, {"000010": []}, rows, panel, {}, m.Budget()))
        rows[SESSIONS[10]]["000010"]["LIST_SHRS"] = "2000"
        issue = m.feature_history_issue(day, {"000010": []}, rows, panel, {}, m.Budget())
        self.assertEqual(issue["day"], day)
        self.assertEqual(issue["evidence_session"], SESSIONS[10])
        stop = m.SourceStop(**issue).detail
        self.assertEqual(stop["session"], day)

    def test_14_MA20_window_boundary_and_discovery_clock(self):
        panel, rows = fixture()
        day = SESSIONS[30]
        rows[SESSIONS[10]]["000010"]["LIST_SHRS"] = "2000"
        self.assertTrue(m.trend_at(day, "000010", panel, rows, {})["passes"])
        rows[SESSIONS[20]]["000010"]["LIST_SHRS"] = "2000"
        with self.assertRaises(m.SourceStop) as caught:
            m.trend_at(day, "000010", panel, rows, {})
        self.assertEqual(caught.exception.detail["session"], day)
        self.assertEqual(caught.exception.detail["evidence_session"], SESSIONS[20])

    def test_15_irreversible_prefix_MDD_failure_survives_source_stop(self):
        book, fifo = books()["candidate"], m.FIFO()
        metric = m.final_metrics(book, fifo, {}, 3, [m.INITIAL, m.INITIAL*.89], "source_stop")
        self.assertIsNone(metric["afterCostPricePnL"])
        verdict = m.gates([{"period": "historical_price_2025q1", "cost": 75, "phase": 0,
                            "metrics": {"candidate": metric}, "stop": {"reason": "source_stop"}}])
        self.assertIn("75bp/phase0/observed_prefix_price_MDD", verdict["observedGateFailures"])
        self.assertNotIn("75bp/phase0/candidate_price_PnL", verdict["observedGateFailures"])
        self.assertTrue(verdict["sourceOrTimeoutIncomplete"])


if __name__ == "__main__":
    unittest.main()
