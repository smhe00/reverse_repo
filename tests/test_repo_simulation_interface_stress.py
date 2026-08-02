from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, timezone
from datetime import time as clock_time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from repo_simulation_interface_stress import (  # noqa: E402
    PRIMARY_SYMBOL,
    StressMetrics,
    _distribution,
    _evaluate,
    _expected_cycles,
    _split_volume,
    build_windows,
)


class SimulationInterfaceStressTests(unittest.TestCase):
    def test_windows_are_isolated_from_functional_tests(self):
        windows = build_windows(
            date(2026, 8, 3),
            morning_start=clock_time(9, 42),
            morning_end=clock_time(11, 30),
            afternoon_start=clock_time(13, 0),
            afternoon_end=clock_time(15, 5),
            tzinfo=timezone.utc,
        )
        self.assertEqual(windows[0].start.hour, 9)
        self.assertEqual(windows[1].end.time(), clock_time(15, 5))
        self.assertEqual(_expected_cycles(windows, 5.0), 69_900)

        invalid = (
            (clock_time(9, 40), clock_time(11, 30), clock_time(13), clock_time(15, 5)),
            (clock_time(9, 42), clock_time(11, 31), clock_time(13), clock_time(15, 5)),
            (clock_time(9, 42), clock_time(11, 30), clock_time(12, 59), clock_time(15, 5)),
            (clock_time(9, 42), clock_time(11, 30), clock_time(13), clock_time(15, 6)),
        )
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValueError):
                build_windows(
                    date(2026, 8, 3),
                    morning_start=values[0],
                    morning_end=values[1],
                    afternoon_start=values[2],
                    afternoon_end=values[3],
                    tzinfo=timezone.utc,
                )

    def test_child_orders_preserve_volume_and_lot_size(self):
        children = _split_volume(12_300, 5)
        self.assertEqual(sum(children), 12_300)
        self.assertEqual(len(children), 5)
        self.assertTrue(all(volume % 100 == 0 for volume in children))
        self.assertEqual(_split_volume(99), [])

    def test_distribution_reports_tail_latency(self):
        result = _distribution([0.01, 0.02, 0.03, 0.20])
        self.assertEqual(result["count"], 4)
        self.assertEqual(result["max"], 0.20)
        self.assertGreater(float(result["p95"]), 0.03)

    def test_pass_gate_requires_5hz_callbacks_and_three_asset_classes(self):
        passing = {
            "cycle_coverage_ratio": 0.995,
            "slow_cycle_ratio_over_200ms": 0.005,
            "query_error_ratio": 0.0001,
            "maximum_consecutive_query_failures": 1,
            "disconnect_callbacks": 0,
            "tick_counts": {PRIMARY_SYMBOL: 100},
            "tick_timestamp_regressions": {PRIMARY_SYMBOL: 0},
            "completed_round_trips": {
                "money_etf": 1,
                "bond_etf": 1,
                "gold_etf": 1,
            },
            "order_callbacks": 6,
            "trade_callbacks": 6,
            "position_residuals": {PRIMARY_SYMBOL: 0},
            "unresolved_stress_order_count": 0,
        }
        passed, failures = _evaluate(passing)
        self.assertTrue(passed)
        self.assertEqual(failures, [])

        failed = {**passing, "cycle_coverage_ratio": 0.90}
        passed, failures = _evaluate(failed)
        self.assertFalse(passed)
        self.assertTrue(any("coverage" in item for item in failures))

    def test_metrics_counts_consecutive_failures(self):
        metrics = StressMetrics()
        metrics.record_cycle(0.01, 0.0, False)
        metrics.record_cycle(0.01, 0.0, False)
        metrics.record_cycle(0.01, 0.0, True)
        summary = metrics.summary(expected_cycles=3)
        self.assertEqual(summary["maximum_consecutive_query_failures"], 2)


if __name__ == "__main__":
    unittest.main()
