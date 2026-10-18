"""Synthetic boundaries; explicitly import the staged sibling, never repo runner."""
import importlib.util
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from contextlib import ExitStack, contextmanager
from copy import deepcopy
import json
import tempfile
from unittest.mock import patch
from zipfile import ZipFile
import unittest

STAGED = Path(__file__).resolve().parents[1] / "scripts/measure/kr_ridge_hold_renewal_diagnostic.py"
spec = importlib.util.spec_from_file_location("staged_hold_renewal", STAGED)
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)
REPO = Path(os.environ.get("SIGNAL_DESK_DEPENDENCY_REPO", Path(__file__).resolve().parents[1])).resolve()
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


class SupplementBoundaries(unittest.TestCase):
    @contextmanager
    def packet(self):
        """Synthetic bodies; replace trust pins only inside this test context."""
        helper = m.source_supplement_module()
        listing = ("현대자동차 변경상장(주식소각) KR7005380001 (단축코드:A005380) "
                   "기명식 보통주 211,531,506주 → 209,416,191주 - 변경주식수 : -2,115,315주 "
                   "기명식 1우선주 24,113,119주 → 23,871,988주 - 변경주식수 : -241,131주 "
                   "발행일(소각일) : 2024년 03월 27일 변경상장일 : 2024년 04월 16일").encode()
        decision = ("<table><tr><td>주식 소각 결정 1. 소각할 주식의 종류와 수</td></tr>"
                    "<tr><td>보통주식 (주)</td><td>2,115,315</td></tr>"
                    "<tr><td>종류주식 (주)</td><td>626,381</td></tr>"
                    "<tr><td>2. 발행주식 총수 보통주식 (주)</td><td>211,531,506</td></tr>"
                    "<tr><td>기취득 자기주식; 자본금의 감소는 없음</td></tr>"
                    "<tr><td>소각 예정일</td><td>2024-03-27</td></tr></table>").encode()
        bodies = dict(zip(helper.SOURCES, (listing, decision)))
        sources = {n: (len(b), helper.sha(b)) for n, b in bodies.items()}
        proposal = dict(helper.FACT, source_url="https://kind.krx.co.kr/external/listing",
            decision_source_url="https://kind.krx.co.kr/external/decision",
            source_sha256=sources["13-listing-original.bin"][1],
            decision_source_sha256=sources["14-decision-original.bin"][1])
        manifest = {"files": [{"path": n, "bytes": v[0], "sha256": v[1]} for n, v in sources.items()],
                    "sources": [{"path": n, "sha256": v[1],
                        "url": proposal["decision_source_url" if n.startswith("14-") else "source_url"],
                        "retrieved_at": "2026-10-18T08:08:30.031898+00:00" if n.startswith("14-") else proposal["source_observed_at"]}
                        for n, v in sources.items()]}
        files = dict(bodies, **{"manifest.json": json.dumps(manifest).encode(),
                              "adjudicated-fact-proposal.json": json.dumps(proposal).encode()})
        dates = m.sessions_between(CLOCK, "2024-03-04", "2024-05-31")
        panel = SimpleNamespace(sessions=dates, index={d: i for i, d in enumerate(dates)},
                                bars={(d, "005380"): (100., 100.) for d in dates})
        rows = {d: {"005380": {"LIST_SHRS": "211531506" if d < "2024-04-16" else "209416191"}}
                for d in dates}
        from signal_desk.signals.research_action_coverage import ReviewedShareCountChange
        with tempfile.TemporaryDirectory() as temp, ExitStack() as stack:
            for name, value in (("SOURCES", sources), ("MANIFEST_SHA", helper.sha(files["manifest.json"])),
                                ("PROPOSAL_SHA", helper.sha(files["adjudicated-fact-proposal.json"]))):
                stack.enter_context(patch.object(helper, name, value))
            archive = Path(temp) / "supplement.zip"
            def write(content=files):
                with ZipFile(archive, "w") as output:
                    for name, body in content.items():
                        output.writestr(name, body)
            write()
            yield helper, archive, files, write, rows, panel, ReviewedShareCountChange

    def test_16_sibling_and_synthetic_event_shared_consumers_no_adjustment(self):
        with self.packet() as (h, archive, files, write, rows, panel, cls):
            self.assertEqual(Path(h.__file__).resolve(), STAGED.with_name("kr_ridge_hold_source_supplement.py"))
            before = deepcopy((rows, panel.bars))
            with self.assertRaises(m.SourceStop):
                m.unit_check("2024-04-16", "005380", rows, panel, {})
            reviewed = h.merge_archive(archive, {}, rows, panel, cls)
            self.assertEqual(list(reviewed), [("2024-04-16", "005380")])
            self.assertEqual(reviewed[("2024-04-16", "005380")].effective_on, "2024-03-27")
            m.unit_check("2024-04-16", "005380", rows, panel, reviewed)
            self.assertIsNone(m.feature_history_issue("2024-04-30", {"005380": []}, rows, panel, reviewed, m.Budget()))
            self.assertEqual(m.trend_at("2024-04-30", "005380", panel, rows, reviewed)["ma20"], 100.)
            self.assertEqual(before, (rows, panel.bars))

    def test_17_missing_tampered_original_and_fabricated_JSON_rejected(self):
        with self.packet() as (h, archive, files, write, rows, panel, cls):
            for name in files:
                with self.subTest(missing=name):
                    write({n: b for n, b in files.items() if n != name})
                    with self.assertRaises(ValueError):
                        h.merge_archive(archive, {}, rows, panel, cls)
                with self.subTest(tampered=name):
                    write({**files, name: files[name]+b" "})
                    with self.assertRaises(ValueError):
                        h.merge_archive(archive, {}, rows, panel, cls)
            archive.write_bytes(b"not a ZIP")
            with self.assertRaisesRegex(ValueError, "supplement_invalid_zip"):
                h.merge_archive(archive, {}, rows, panel, cls)
            # Unmodified fictional originals cannot pass the production pins.
            write()
            production = m.source_supplement_module()
            with self.assertRaises(ValueError):
                production.merge_archive(archive, {}, rows, panel, cls)

    def test_18_exact_mapping_and_duplicate_fail_closed(self):
        with self.packet() as (h, archive, files, write, rows, panel, cls):
            reviewed = h.merge_archive(archive, {}, rows, panel, cls)
            with self.assertRaises(ValueError):
                h.merge_archive(archive, reviewed, rows, panel, cls)
            for value in ("209416190", "209416191.0", "209416191.1", True):
                bad = deepcopy(rows)
                bad["2024-04-16"]["005380"]["LIST_SHRS"] = value
                with self.subTest(value=value), self.assertRaises(ValueError):
                    h.merge_archive(archive, {}, bad, panel, cls)
            for field, value in (("ticker", "005385"), ("security_id", "KR7005381009"),
                                 ("share_class", "preferred"), ("effective_on", "2024-04-16"),
                                 ("previous_session", "2024-04-12"), ("listed_shares_after", 209416190),
                                 ("holder_quantity_multiplier", .99), ("price_unit_multiplier", .99),
                                 ("source_publication_at_displayed_KST", "2024-03-21T16:27:00+09:00")):
                proposal = json.loads(files["adjudicated-fact-proposal.json"])
                proposal[field] = value
                body = json.dumps(proposal).encode()
                write({**files, "adjudicated-fact-proposal.json": body})
                with patch.object(h, "PROPOSAL_SHA", h.sha(body)):
                    with self.subTest(field=field), self.assertRaises(ValueError):
                        h.merge_archive(archive, {}, rows, panel, cls)

    def test_19_body_semantics_and_extra_member_rejected(self):
        with self.packet() as (h, archive, files, write, rows, panel, cls):
            listing, decision = (files[n] for n in h.SOURCES)
            for old, new in ((b"KR7005380001", b"KR7005381009"), (b"209,416,191", b"209,416,190"),
                             (b"03", b"04")):
                with self.subTest(old=old), self.assertRaises(ValueError):
                    h.validate_bodies(listing.replace(old, new), decision)
            with self.assertRaises(ValueError):
                h.validate_bodies(listing, decision.replace(b"2,115,315", b"2,115,314"))
            write({**files, "another-event.json": b"{}"})
            with self.assertRaises(ValueError):
                h.merge_archive(archive, {}, rows, panel, cls)

    def test_20_precommit_code_and_archive_binding(self):
        import subprocess
        h = m.source_supplement_module()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            archive = root / "supplement.zip"
            archive.write_bytes(b"preflight fixture only")
            spec = {"schema": "kr-r3-hyundai-source-supplement-v1", "contract_commit": m.CONTRACT_COMMIT,
                    "contract_sha256": m.CONTRACT_SHA, "input_sha256": {"fixture": "hash"},
                    "archive_sha256": h.sha(archive.read_bytes()), "manifest_sha256": h.MANIFEST_SHA,
                    "proposal_sha256": h.PROPOSAL_SHA, "maximum_seconds": 600, "new_collection": 0,
                    "code_sha256": {n: h.sha((STAGED.parents[2] / n).read_bytes()) for n in h.CODE_FILES}}
            path = root / "spec.json"
            path.write_text(json.dumps(spec))
            subprocess.run(["git", "add", "spec.json"], cwd=root, check=True)
            subprocess.run(["git", "-c", "user.name=Boundary Test", "-c", "user.email=test@example.invalid",
                            "-c", "commit.gpgsign=false", "commit", "-qm", "synthetic precommit"], cwd=root, check=True)
            commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
            def check():
                return h.checked_spec(root, path, archive, commit, m.CONTRACT_COMMIT, m.CONTRACT_SHA,
                                      {"fixture": "hash"}, m.Budget())
            self.assertEqual(check()[1]["spec_commit"], commit)
            archive.write_bytes(b"tampered")
            with self.assertRaises(ValueError):
                check()
            path.write_text(json.dumps({**spec, "new_collection": 1}))
            with self.assertRaises(ValueError):
                check()


if __name__ == "__main__":
    unittest.main()
