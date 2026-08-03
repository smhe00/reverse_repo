from __future__ import annotations

import sys
import threading
import unittest
from datetime import date, datetime, timezone
from datetime import time as clock_time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from repo_simulation_interface_stress import (  # noqa: E402
    CANCELABLE_ORDER_STATUSES,
    CANCEL_PENDING_ORDER_STATUSES,
    PRIMARY_SYMBOL,
    STRATEGY_NAME,
    TERMINAL_ORDER_STATUSES,
    StressMetrics,
    _distribution,
    _cancel_probe_once,
    _evaluate,
    _expected_cycles,
    _next_trade_at,
    _next_probe_at,
    _remaining_windows,
    _split_volume,
    _wait_fresh_quote,
    _working_set_bytes,
    build_windows,
)


class SimulationInterfaceStressTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "Windows process API")
    def test_windows_working_set_probe_returns_positive_bytes(self):
        value = _working_set_bytes()
        self.assertIsInstance(value, int)
        self.assertGreater(value, 0)

    def test_trade_probe_waits_for_next_fresh_l1_snapshot(self):
        with patch(
            "repo_simulation_interface_stress._fresh_quote",
            side_effect=[
                RuntimeError("quote is stale"),
                (1.5, 1.505, 123),
            ],
        ) as reader, patch(
            "repo_simulation_interface_stress.time.sleep"
        ) as sleeper:
            quote = _wait_fresh_quote(object(), PRIMARY_SYMBOL)
        self.assertEqual(quote, (1.5, 1.505, 123))
        self.assertEqual(reader.call_count, 2)
        sleeper.assert_called_once_with(0.2)

    def test_order_status_sets_do_not_treat_active_or_cancel_pending_as_terminal(self):
        self.assertEqual(TERMINAL_ORDER_STATUSES, {53, 54, 56, 57})
        self.assertEqual(CANCELABLE_ORDER_STATUSES, {48, 49, 50, 55})
        self.assertEqual(CANCEL_PENDING_ORDER_STATUSES, {51, 52})
        self.assertFalse(
            TERMINAL_ORDER_STATUSES
            & (CANCELABLE_ORDER_STATUSES | CANCEL_PENDING_ORDER_STATUSES)
        )

    def test_cancel_probe_submits_queries_cancels_and_confirms_terminal(self):
        class Trader:
            def __init__(self):
                self.canceled = False
                self.submission = None

            def order_stock(self, *args):
                self.submission = args
                return 99

            def query_stock_orders(self, account, cancelable_only):
                del account, cancelable_only
                return [
                    SimpleNamespace(
                        order_id=99,
                        order_status=54 if self.canceled else 50,
                        traded_volume=0,
                    )
                ]

            def query_stock_positions(self, account):
                del account
                return []

            def cancel_order_stock(self, account, order_id):
                del account
                self.asserted_order_id = order_id
                self.canceled = True
                return 0

        class Quotes:
            @staticmethod
            def get_full_tick(symbols):
                return {
                    symbols[0]: {
                        "bidPrice": [1.5],
                        "askPrice": [1.503],
                        "time": int(datetime.now().timestamp() * 1000),
                    }
                }

        class Writer:
            def __init__(self):
                self.records = []

            def write(self, kind, **payload):
                self.records.append((kind, payload))

        trader = Trader()
        writer = Writer()
        status = _cancel_probe_once(
            trader=trader,
            account=object(),
            xtconstant=SimpleNamespace(STOCK_BUY=23, STOCK_SELL=24, FIX_PRICE=11),
            xtdata=Quotes(),
            remark_prefix="st08031300abcd_",
            stop_event=threading.Event(),
            writer=writer,
        )
        self.assertEqual(status, 54)
        self.assertTrue(trader.canceled)
        self.assertEqual(trader.asserted_order_id, 99)
        self.assertEqual(trader.submission[2], 23)
        self.assertEqual(trader.submission[3], 100)
        self.assertEqual(trader.submission[5], 1.425)
        self.assertEqual(trader.submission[7], "st08031300abcd_cancel")
        self.assertEqual(
            [kind for kind, _ in writer.records],
            ["order_submitted", "cancel_requested", "order_terminal"],
        )

    def test_strategy_name_fits_observed_qmt_field_limit(self):
        self.assertLessEqual(len(STRATEGY_NAME), 23)

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

        afternoon_now = datetime(
            2026, 8, 3, 13, 7, tzinfo=timezone.utc
        )
        remaining = _remaining_windows(windows, afternoon_now)
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0].start, afternoon_now)
        self.assertEqual(
            _next_trade_at(windows, afternoon_now, 20),
            datetime(2026, 8, 3, 13, 23, tzinfo=timezone.utc),
        )
        self.assertEqual(
            _next_probe_at(
                windows,
                datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc),
            ),
            datetime(2026, 8, 3, 13, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(
            _next_probe_at(windows, afternoon_now),
            afternoon_now,
        )

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

    def test_pass_gate_requires_quote_observation_and_three_asset_classes(self):
        passing = {
            "cycle_coverage_ratio": 0.995,
            "slow_cycle_ratio_over_200ms": 0.005,
            "query_error_ratio": 0.0001,
            "maximum_consecutive_query_failures": 1,
            "disconnect_callbacks": 0,
            "tick_counts": {PRIMARY_SYMBOL: 100},
            "tick_unique_counts": {PRIMARY_SYMBOL: 10},
            "tick_timestamp_regressions": {PRIMARY_SYMBOL: 0},
            "quote_poll_counts": {PRIMARY_SYMBOL: 1_000},
            "quote_poll_unique_counts": {PRIMARY_SYMBOL: 10},
            "quote_poll_timestamp_regressions": {PRIMARY_SYMBOL: 0},
            "completed_round_trips": {
                "money_etf": 1,
                "bond_etf": 1,
                "gold_etf": 1,
            },
            "order_callbacks": 6,
            "trade_callbacks": 6,
            "position_residuals": {PRIMARY_SYMBOL: 0},
            "unresolved_stress_order_count": 0,
            "cancel_probe_completed": True,
        }
        passed, failures = _evaluate(passing)
        self.assertTrue(passed)
        self.assertEqual(failures, [])

        failed = {**passing, "cycle_coverage_ratio": 0.90}
        passed, failures = _evaluate(failed)
        self.assertFalse(passed)
        self.assertTrue(any("coverage" in item for item in failures))

        failed = {**passing, "cancel_probe_completed": False}
        passed, failures = _evaluate(failed)
        self.assertFalse(passed)
        self.assertTrue(any("cancellation probe" in item for item in failures))

    def test_metrics_counts_consecutive_failures(self):
        metrics = StressMetrics()
        metrics.record_cycle(0.01, 0.0, False)
        metrics.record_cycle(0.01, 0.0, False)
        metrics.record_cycle(0.01, 0.0, True)
        summary = metrics.summary(expected_cycles=3)
        self.assertEqual(summary["maximum_consecutive_query_failures"], 2)
        self.assertFalse(summary["cancel_probe_completed"])

    def test_metrics_separates_unique_and_duplicate_l1_snapshots(self):
        metrics = StressMetrics()
        metrics.record_tick(
            PRIMARY_SYMBOL,
            {PRIMARY_SYMBOL: {"time": 1_000}},
        )
        metrics.record_tick(
            PRIMARY_SYMBOL,
            {PRIMARY_SYMBOL: {"time": 1_000}},
        )
        metrics.record_tick(
            PRIMARY_SYMBOL,
            {PRIMARY_SYMBOL: {"time": 4_000}},
        )
        metrics.record_quote_poll(
            {PRIMARY_SYMBOL: {"time": 1_000}}
        )
        metrics.record_quote_poll(
            {PRIMARY_SYMBOL: {"time": 1_000}}
        )
        metrics.record_quote_poll(
            {PRIMARY_SYMBOL: {"time": 4_000}}
        )
        summary = metrics.summary(expected_cycles=0)
        self.assertEqual(summary["tick_counts"][PRIMARY_SYMBOL], 3)
        self.assertEqual(
            summary["tick_unique_counts"][PRIMARY_SYMBOL],
            2,
        )
        self.assertEqual(
            summary["tick_duplicate_counts"][PRIMARY_SYMBOL],
            1,
        )
        self.assertAlmostEqual(
            summary["tick_duplicate_ratio"][PRIMARY_SYMBOL],
            1 / 3,
        )
        self.assertEqual(
            summary["tick_source_interval_seconds"][PRIMARY_SYMBOL]["p50"],
            3.0,
        )
        self.assertEqual(
            summary["quote_poll_counts"][PRIMARY_SYMBOL],
            3,
        )
        self.assertEqual(
            summary["quote_poll_unique_counts"][PRIMARY_SYMBOL],
            2,
        )
        self.assertEqual(
            summary["quote_poll_duplicate_counts"][PRIMARY_SYMBOL],
            1,
        )

    def test_metrics_unwraps_real_xtdata_tick_callback_batches(self):
        metrics = StressMetrics()
        metrics.record_tick(
            PRIMARY_SYMBOL,
            {
                PRIMARY_SYMBOL: [
                    {"time": 1_000},
                    {"time": 4_000},
                ]
            },
        )
        summary = metrics.summary(expected_cycles=0)
        self.assertEqual(summary["tick_counts"][PRIMARY_SYMBOL], 2)
        self.assertEqual(
            summary["tick_unique_counts"][PRIMARY_SYMBOL],
            2,
        )
        self.assertEqual(
            summary["tick_missing_timestamp_counts"].get(
                PRIMARY_SYMBOL,
                0,
            ),
            0,
        )
        self.assertEqual(
            summary["tick_source_interval_seconds"][PRIMARY_SYMBOL][
                "p50"
            ],
            3.0,
        )


if __name__ == "__main__":
    unittest.main()
