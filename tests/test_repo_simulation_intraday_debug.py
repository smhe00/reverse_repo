from __future__ import annotations

import sys
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from repo_simulation_intraday_debug_summary import (  # noqa: E402
    _journal_result,
    _stress_result,
    _stress_order_evidence,
)
from prepare_repo_simulation_morning_recovery import (  # noqa: E402
    _wait_for_post_trigger_book,
)
from repo_execution_core import GC001, QuoteValidationError  # noqa: E402


class SimulationIntradayDebugTests(unittest.TestCase):
    def test_fault_injection_waits_for_first_post_trigger_l1_snapshot(self):
        now = datetime.now().astimezone()
        book = object()
        with patch(
            "prepare_repo_simulation_morning_recovery.read_quote_books",
            side_effect=[
                QuoteValidationError("quote predates the execution trigger"),
                {GC001: book},
            ],
        ) as reader, patch(
            "prepare_repo_simulation_morning_recovery.time.sleep"
        ) as sleeper:
            result = _wait_for_post_trigger_book(
                xtdata=object(),
                target_at=now,
                deadline_at=now + timedelta(seconds=10),
            )
        self.assertIs(result[GC001], book)
        self.assertEqual(reader.call_count, 2)
        sleeper.assert_called_once_with(0.2)

    def test_recovery_summary_requires_restart_recovery_and_reconcile(self):
        payload = {
            "machine": {
                "state": "done_filled",
                "facts": {"unresolved_order": False},
            },
            "data": {"filled_principal_yuan": 1000},
            "history": [
                {"event": "restart"},
                {
                    "event": "recovery_terminal",
                    "details": {
                        "order": {
                            "order_id": 123,
                            "symbol": "204001.SH",
                            "strategy_name": "repo_morning_v2",
                            "remark": "repo_debug_m1_20260803_0001",
                            "status": 56,
                        }
                    },
                },
                {
                    "event": "reconciled_full",
                    "details": {
                        "order": {
                            "order_id": 123,
                            "remark": "repo_debug_m1_20260803_0001",
                            "status": 56,
                            "traded_volume": 10,
                        }
                    },
                },
            ],
        }
        result = _journal_result(payload, afternoon=False)
        self.assertTrue(result["passed"])
        self.assertEqual(result["orders"][0]["order_id"], 123)
        self.assertEqual(
            result["orders"][0]["strategy_name"], "repo_morning_v2"
        )
        self.assertEqual(result["orders"][0]["traded_volume"], 10)
        self.assertNotIn("account_id", result["orders"][0])
        payload["history"] = [{"event": "reconciled_full"}]
        self.assertFalse(_journal_result(payload, afternoon=False)["passed"])

    def test_afternoon_summary_requires_fill_and_no_unresolved_order(self):
        payload = {
            "machine": {
                "state": "complete_at_hard_stop",
                "facts": {"unresolved_order": False},
            },
            "data": {"accounted_filled_principal_yuan": 1000},
            "history": [],
        }
        self.assertTrue(_journal_result(payload, afternoon=True)["passed"])
        payload["machine"]["facts"]["unresolved_order"] = True
        self.assertFalse(_journal_result(payload, afternoon=True)["passed"])

    def test_stress_summary_joins_submit_and_terminal_order_evidence(self):
        records = [
            {
                "at": "2026-08-03T13:05:01+08:00",
                "kind": "order_submitted",
                "order_id": 321,
                "symbol": "511880.SH",
                "side": "BUY",
                "volume": 100,
                "price": 100.0,
                "remark": "st260803130500abc_001b01",
            },
            {
                "at": "2026-08-03T13:05:02+08:00",
                "kind": "order_terminal",
                "order_id": 321,
                "symbol": "511880.SH",
                "side": "BUY",
                "status": 56,
                "requested_volume": 100,
                "traded_volume": 100,
            },
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "samples.jsonl"
            path.write_text(
                "".join(json.dumps(item) + "\n" for item in records),
                encoding="utf-8",
            )
            evidence = _stress_order_evidence(path)
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]["order_id"], 321)
        self.assertEqual(evidence[0]["status"], 56)
        self.assertEqual(evidence[0]["traded_volume"], 100)
        self.assertIn("submitted_at", evidence[0])
        self.assertIn("terminal_at", evidence[0])

    def test_stress_summary_accepts_only_proven_callback_parser_false_negative(self):
        raw = {
            "passed": False,
            "failures": [
                "primary money-ETF produced no unique tick timestamp"
            ],
            "metrics": {
                "tick_counts": {"511880.SH": 100},
                "tick_missing_timestamp_counts": {"511880.SH": 100},
            },
        }
        result = _stress_result(raw, {"passed": True})
        self.assertTrue(result["passed"])
        self.assertFalse(result["raw_passed"])
        self.assertTrue(result["accepted_known_parser_false_negative"])

        raw["failures"].append("broker query error ratio exceeds 0.1%")
        self.assertFalse(_stress_result(raw, {"passed": True})["passed"])
        raw["failures"] = [
            "primary money-ETF produced no unique tick timestamp"
        ]
        self.assertFalse(_stress_result(raw, {"passed": False})["passed"])

    def test_orchestrator_is_simulation_only_and_caps_recovery_principal(self):
        root = Path(__file__).resolve().parents[1]
        source = (
            root / "scripts" / "run_repo_simulation_intraday_debug.ps1"
        ).read_text(encoding="utf-8")
        self.assertIn('Get-ReverseRepoQmtPath -Environment "simulation"', source)
        self.assertIn('--environment simulation', source)
        self.assertIn('--maximum-principal-yuan 1000', source)
        self.assertIn('--partial-session', source)
        self.assertIn('--stress-samples $stressSamples', source)
        self.assertIn('$qmtPath -ieq $liveQmtPath', source)
        self.assertNotIn('--environment live', source)


if __name__ == "__main__":
    unittest.main()
