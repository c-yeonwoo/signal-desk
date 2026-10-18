"""Synthetic-only boundaries; never load repository archives or run a strategy."""
import contextlib
import importlib.util
import io
from pathlib import Path
from types import SimpleNamespace
import unittest

path = Path(__file__).resolve().parents[1] / "scripts/measure/kr_ridge_source_inventory.py"
spec = importlib.util.spec_from_file_location("source_inventory_test_target", path)
inventory = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inventory)


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.sessions = [f"s{i:03d}" for i in range(260)]
        self.panel = SimpleNamespace(sessions=self.sessions,
            index={s: i for i, s in enumerate(self.sessions)})
        self.days = {s: {t: {"LIST_SHRS": "100"} for t in ("A", "B")} for s in self.sessions}

    def consumer(self, ticker="A", day="s252", layer="features", window=253):
        return {"period": "p", "decision_session": day, "ticker": ticker,
                "layer": layer, "window": window}

    def change(self, ticker, index, after):
        for s in self.sessions[index:]:
            self.days[s][ticker]["LIST_SHRS"] = str(after)

    def fact(self, ticker="A", index=1, before=100, after=200):
        return {"ticker": ticker, "listing_session": self.sessions[index],
            "previous_session": self.sessions[index-1], "listed_shares_before": before,
            "listed_shares_after": after, "holder_quantity_effect": "unchanged",
            "source_url": "https://example.test/original", "source_sha256": "a"*64}

    def run_inventory(self, consumers=None, reviewed=None):
        return inventory.aggregate(self.panel, self.days, reviewed or {},
            consumers or [self.consumer()])

    def test_first_pair_included_future_excluded(self):
        self.change("A", 1, 200)
        self.change("A", 253, 300)
        result = self.run_inventory()
        self.assertEqual([(e["previous_session"], e["session"]) for e in result["entries"]],
                         [("s000", "s001")])

    def test_nonpositive_metadata_is_separate(self):
        for value in ("0", "-1", "nan", None):
            with self.subTest(value=value):
                self.days["s010"]["A"]["LIST_SHRS"] = value
                result = self.run_inventory()
                self.assertEqual(len(result["entries"]), 0)
                self.assertEqual(len(result["missing_metadata_pairs"]), 2)
                self.assertFalse(any(e["verified_event_key"] for e in result["missing_metadata_pairs"]))

    def test_known_exact_values_and_unchanged_failclosed(self):
        self.change("A", 1, 200)
        key = ("s001", "A")
        result = self.run_inventory(reviewed={key: self.fact()})
        self.assertTrue(result["entries"][0]["verified_event_key"])
        for field, value in (("listed_shares_after", 201), ("holder_quantity_effect", "split"),
                             ("previous_session", "s002"), ("source_url", "")):
            with self.subTest(field=field):
                bad = self.fact() | {field: value}
                with self.assertRaises(ValueError):
                    self.run_inventory(reviewed={key: bad})

    def test_ticker_and_date_are_independent_keys(self):
        self.change("A", 1, 200)
        self.change("B", 1, 200)
        self.change("A", 2, 300)
        result = self.run_inventory([self.consumer(), self.consumer("B")])
        self.assertEqual(len(result["entries"]), 3)
        self.assertEqual(result["feature_summary"]["unreviewed_tickers"], 2)

    def test_duplicate_consumer_layers_and_path_references(self):
        self.change("A", 240, 200)
        feature = self.consumer()
        trend = self.consumer(layer="potential_trend", window=20)
        other = feature | {"path_reference": "p/75/0/forced"}
        result = self.run_inventory([feature, feature, trend, other])
        entry = result["entries"][0]
        self.assertEqual(entry["layers"], ["features", "potential_trend"])
        self.assertEqual(len(entry["consumed_by"]), 3)
        self.assertEqual(result["feature_summary"]["price_consumer_ticker_dates"], 1)
        self.assertEqual(result["feature_summary"]["unique_observed_change_keys"], 1)

    def test_253_and_20_history_boundaries(self):
        self.assertEqual(inventory.history(self.panel, "s252", 253), self.sessions[:253])
        self.assertEqual(inventory.history(self.panel, "s252", 20), self.sessions[233:253])
        with self.assertRaises(ValueError):
            inventory.history(self.panel, "s251", 253)
        self.change("A", 233, 200)  # before first MA20 close, no internal MA20 pair
        result = self.run_inventory([self.consumer(), self.consumer(layer="potential_trend", window=20)])
        self.assertEqual(result["entries"][0]["layers"], ["features"])
        self.change("A", 234, 300)
        result = self.run_inventory([self.consumer(), self.consumer(layer="potential_trend", window=20)])
        self.assertEqual(result["entries"][1]["layers"], ["features", "potential_trend"])

    def test_market_basket_uses_21_closes(self):
        self.assertEqual(inventory.history(self.panel, "s252", 21), self.sessions[232:253])
        self.change("A", 232, 200)  # boundary before the first market close
        self.change("A", 233, 300)  # first internal market-return pair
        consumers = [self.consumer(), self.consumer(layer="market-basket", window=21),
                     self.consumer(layer="potential_trend", window=20)]
        entries = self.run_inventory(consumers)["entries"]
        self.assertEqual(entries[0]["layers"], ["features"])
        self.assertEqual(entries[1]["layers"], ["features", "market-basket"])

    def test_conflicting_sources_failclosed(self):
        self.change("A", 1, 200)
        first = self.fact()
        second = first | {"source_sha256": "b"*64}
        with self.assertRaisesRegex(ValueError, "contradictory_source"):
            inventory.aggregate(self.panel, self.days,
                [(("s001", "A"), first), (("s001", "A"), second)], [self.consumer()])

    def test_partial_cli_rejected_before_loading(self):
        for extra in (["--source-supplement", "s.json"],
                      ["--event-supplement", "e.json", "--event-supplement-commit", "x"],
                      ["--source-supplement", "s", "--source-supplement-archive", "a",
                       "--supplement-commit", "x", "--event-supplement", "e"]):
            with self.subTest(extra=extra), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    inventory.parser_args(["--repo", "/does/not/exist", "--output", "unused", *extra])
                self.assertEqual(error.exception.code, 2)

    def test_reported_null_is_not_reached(self):
        saved = {"paths": [{"period": "p", "cost": 75, "phase": 0, "stop": {"reason": "gap"},
            "trace": [{"session": "s001", "arms": {"forced": None, "net": None, "candidate": None}}]}]}
        result = inventory.reported_paths(saved)
        self.assertEqual(len(result), 3)
        self.assertEqual(result[0]["reached_sessions"], [])
        self.assertEqual(result[0]["not_reached_sessions"], ["s001"])
        self.assertEqual(result[0]["fills_reported"], "unknown")


if __name__ == "__main__":
    unittest.main()
