from __future__ import annotations

import json
import sys
import time
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from repo_execution_core import (  # noqa: E402
    AccountBindingError,
    AtomicJournal,
    BrokerQueryAmbiguous,
    ExecutionSafetyError,
    ExecutionMutex,
    OrderClass,
    OrderView,
    QuoteBook,
    QuoteValidationError,
    account_id_fingerprint,
    build_book_plan,
    classify_order,
    journal_matches_verification,
    load_account_binding,
    normalize_repo_rate,
    qmt_path_fingerprint,
    qmt_strategy_name,
    read_cash_snapshot,
    reconcile_cash_cap,
    reverse_repo_strategy_config,
    reverse_repo_strategy_config_sha256,
    select_bound_account,
    strict_query,
)


def _view(status: int, traded: int = 0, volume: int = 10) -> OrderView:
    return OrderView(
        order_id=1,
        symbol="204001.SH",
        order_type=24,
        status=status,
        order_volume=volume,
        traded_volume=traded,
        traded_price=1.0,
        limit_price=1.0,
        status_msg="",
        strategy_name="test",
        remark="test",
    )


class CoreSafetyTests(unittest.TestCase):
    def test_qmt_strategy_name_rejects_values_the_broker_would_truncate(self):
        self.assertEqual(qmt_strategy_name("repo_morning_v2"), "repo_morning_v2")
        self.assertEqual(qmt_strategy_name("repo_afternoon_v2"), "repo_afternoon_v2")
        with self.assertRaisesRegex(ValueError, "23-character"):
            qmt_strategy_name("gc001_daily_90pct_093042_state_machine_v2")
        with self.assertRaisesRegex(ValueError, "ASCII"):
            qmt_strategy_name("逆回购策略")

    def test_strategy_config_is_canonical_and_binds_only_strategy_fields(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.json"
            path.write_text(
                json.dumps(
                    {
                        "python_path": "first-python.exe",
                        "first_execution_time": "09:31:05",
                        "second_execution_time": "15:05:30",
                        "first_cash_usage_ratio": 0.75,
                        "second_cash_usage_ratio": 0.60,
                    }
                ),
                encoding="utf-8",
            )
            expected = {
                "first_execution_time": "09:31:05",
                "second_execution_time": "15:05:30",
                "first_cash_usage_ratio": 0.75,
                "second_cash_usage_ratio": 0.60,
            }
            self.assertEqual(reverse_repo_strategy_config(path), expected)
            original_hash = reverse_repo_strategy_config_sha256(path)

            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["python_path"] = "other-python.exe"
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(
                reverse_repo_strategy_config_sha256(path),
                original_hash,
            )

            payload["first_cash_usage_ratio"] = 0.76
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertNotEqual(
                reverse_repo_strategy_config_sha256(path),
                original_hash,
            )

            payload["first_cash_usage_ratio"] = 0.75
            payload["second_cash_usage_ratio"] = 0.61
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertNotEqual(
                reverse_repo_strategy_config_sha256(path),
                original_hash,
            )

    def test_strategy_config_rejects_unsafe_or_noncanonical_values(self):
        invalid_values = (
            ("first_execution_time", "09:29:59"),
            ("first_execution_time", "11:28:01"),
            ("first_execution_time", "12:00:00"),
            ("first_execution_time", "15:28:01"),
            ("first_execution_time", "9:30:00"),
            ("second_execution_time", "11:30:00"),
            ("second_execution_time", "12:00:00"),
            ("second_execution_time", "15:30:00"),
            ("first_cash_usage_ratio", -0.01),
            ("first_cash_usage_ratio", 1.01),
            ("second_cash_usage_ratio", -0.01),
            ("second_cash_usage_ratio", 1.01),
        )
        with TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.json"
            for name, value in invalid_values:
                with self.subTest(name=name, value=value):
                    path.write_text(
                        json.dumps({name: value}),
                        encoding="utf-8",
                    )
                    with self.assertRaises(ExecutionSafetyError):
                        reverse_repo_strategy_config(path)

    def test_second_time_must_be_at_least_five_minutes_after_first(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.json"
            base = {
                "first_execution_time": "09:30:00",
                "first_cash_usage_ratio": 0.90,
            }
            for value in ("09:30:00", "09:34:59"):
                with self.subTest(value=value):
                    path.write_text(
                        json.dumps({**base, "second_execution_time": value}),
                        encoding="utf-8",
                    )
                    with self.assertRaises(ExecutionSafetyError):
                        reverse_repo_strategy_config(path)
            path.write_text(
                json.dumps({**base, "second_execution_time": "09:35:00"}),
                encoding="utf-8",
            )
            self.assertEqual(
                reverse_repo_strategy_config(path)["second_execution_time"],
                "09:35:00",
            )

    def test_legacy_strategy_keys_are_accepted_but_conflicts_fail(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.json"
            path.write_text(
                json.dumps(
                    {
                        "morning_execution_time": "09:30:42",
                        "afternoon_execution_time": "15:10:00",
                        "morning_cash_usage_ratio": 0.90,
                        "afternoon_cash_usage_ratio": 0.80,
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                reverse_repo_strategy_config(path)["first_execution_time"],
                "09:30:42",
            )
            self.assertEqual(
                reverse_repo_strategy_config(path)[
                    "second_cash_usage_ratio"
                ],
                0.80,
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["first_execution_time"] = "09:31:00"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ExecutionSafetyError, "conflicts"):
                reverse_repo_strategy_config(path)

    def test_account_binding_retries_transient_invisibility_twice(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            qmt = root / "QMT" / "userdata_mini"
            qmt.mkdir(parents=True)
            binding_path = root / "binding.json"
            binding_path.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "accounts": [
                            {
                                "label": "live",
                                "environment": "live",
                                "account_type": "SECURITY_ACCOUNT",
                                "account_id_fingerprint": (
                                    account_id_fingerprint("account")
                                ),
                                "qmt_path_fingerprint": (
                                    qmt_path_fingerprint(qmt)
                                ),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            info = SimpleNamespace(
                account_id="account",
                account_type=2,
            )
            normal_status = SimpleNamespace(
                account_id="account",
                account_type=2,
                status=0,
            )

            class Trader:
                status_calls = 0

                @staticmethod
                def query_account_infos():
                    return [info]

                def query_account_status(self):
                    self.status_calls += 1
                    if self.status_calls < 3:
                        return []
                    return [normal_status]

            trader = Trader()
            constants = SimpleNamespace(
                SECURITY_ACCOUNT=2,
                ACCOUNT_STATUS_OK=0,
            )
            types = SimpleNamespace(
                StockAccount=lambda account_id, kind: (
                    account_id,
                    kind,
                )
            )

            with patch(
                "repo_execution_core.time.sleep"
            ) as sleep:
                account, binding = select_bound_account(
                    trader,
                    constants,
                    types,
                    environment="live",
                    qmt_path=qmt,
                    binding_path=binding_path,
                )

            self.assertEqual(account, ("account", "STOCK"))
            self.assertEqual(binding.label, "live")
            self.assertEqual(trader.status_calls, 3)
            self.assertEqual(sleep.call_count, 2)
            sleep.assert_any_call(3.0)

    def test_account_binding_fails_after_two_delayed_retries(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            qmt = root / "QMT" / "userdata_mini"
            qmt.mkdir(parents=True)
            binding_path = root / "binding.json"
            binding_path.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "accounts": [
                            {
                                "label": "live",
                                "environment": "live",
                                "account_type": "SECURITY_ACCOUNT",
                                "account_id_fingerprint": (
                                    account_id_fingerprint("account")
                                ),
                                "qmt_path_fingerprint": (
                                    qmt_path_fingerprint(qmt)
                                ),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            class Trader:
                status_calls = 0

                @staticmethod
                def query_account_infos():
                    return []

                def query_account_status(self):
                    self.status_calls += 1
                    return []

            trader = Trader()
            constants = SimpleNamespace(
                SECURITY_ACCOUNT=2,
                ACCOUNT_STATUS_OK=0,
            )
            types = SimpleNamespace(StockAccount=object)

            with (
                patch(
                    "repo_execution_core.time.sleep"
                ) as sleep,
                self.assertRaisesRegex(
                    AccountBindingError,
                    "after 3 attempts",
                ),
            ):
                select_bound_account(
                    trader,
                    constants,
                    types,
                    environment="live",
                    qmt_path=qmt,
                    binding_path=binding_path,
                )

            self.assertEqual(trader.status_calls, 3)
            self.assertEqual(sleep.call_count, 2)

    def test_every_qmt_order_status_is_classified_safely(self):
        expected = {
            48: OrderClass.ACTIVE,
            49: OrderClass.ACTIVE,
            50: OrderClass.ACTIVE,
            51: OrderClass.CANCEL_PENDING,
            52: OrderClass.CANCEL_PENDING,
            53: OrderClass.CANCELED_ZERO,
            54: OrderClass.CANCELED_ZERO,
            55: OrderClass.ACTIVE,
            56: OrderClass.FILLED,
            57: OrderClass.REJECTED,
            255: OrderClass.UNKNOWN,
        }
        for status, classification in expected.items():
            with self.subTest(status=status):
                self.assertEqual(
                    classify_order(_view(status)),
                    classification,
                )
        self.assertEqual(
            classify_order(_view(53, traded=4)),
            OrderClass.TERMINAL_PARTIAL,
        )
        self.assertEqual(
            classify_order(_view(50, traded=10)),
            OrderClass.FILLED,
        )
        for status in range(-1, 256):
            self.assertIsInstance(
                classify_order(_view(status)),
                OrderClass,
            )

    def test_none_query_never_becomes_an_empty_success(self):
        calls = 0

        def query() -> None:
            nonlocal calls
            calls += 1
            return None

        with self.assertRaises(BrokerQueryAmbiguous):
            strict_query(
                query,
                name="test",
                attempts=3,
                delay_seconds=0,
            )
        self.assertEqual(calls, 3)
        self.assertEqual(
            strict_query(lambda: [], name="empty", attempts=1),
            [],
        )

    def test_cash_cap_blocks_stale_post_fill_cash(self):
        effective, cap = reconcile_cash_cap(2_000_000, 1_500_000)
        self.assertEqual(effective, 1_500_000)
        self.assertEqual(cap, 1_500_000)
        effective, cap = reconcile_cash_cap(1_499_999.8, 1_500_000)
        self.assertEqual(effective, 1_499_999.8)
        self.assertIsNone(cap)

    def test_cash_snapshot_uses_conservative_cross_check(self):
        snapshot = read_cash_snapshot(
            SimpleNamespace(
                cash=2_000_000,
                available_cash=None,
                total_asset=2_000_000,
                market_value=750_000,
                frozen_cash=50_000,
            )
        )
        self.assertEqual(
            snapshot.conservative_available_cash,
            1_200_000,
        )

    def test_binding_contains_no_plaintext_and_binds_qmt_path(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            qmt = root / "模拟QMT" / "userdata_mini"
            qmt.mkdir(parents=True)
            binding = root / "binding.json"
            binding.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "accounts": [
                            {
                                "label": "simulation",
                                "environment": "simulation",
                                "account_type": "SECURITY_ACCOUNT",
                                "account_id_fingerprint": (
                                    account_id_fingerprint("secret")
                                ),
                                "qmt_path_fingerprint": (
                                    qmt_path_fingerprint(qmt)
                                ),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            loaded = load_account_binding(
                binding,
                environment="simulation",
                qmt_path=qmt,
            )
            self.assertEqual(loaded.label, "simulation")
            other = root / "模拟QMT2" / "userdata_mini"
            other.mkdir(parents=True)
            with self.assertRaises(AccountBindingError):
                load_account_binding(
                    binding,
                    environment="simulation",
                    qmt_path=other,
                )

    def test_journal_persists_data_with_machine_transition(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.json"
            journal = AtomicJournal(
                path,
                strategy="test",
                trade_date=__import__("datetime").date(2026, 7, 31),
            )
            journal.load_or_initialize(
                machine_payload={"state": "new", "facts": {}}
            )
            journal.transition(
                event="intent",
                machine_payload={"state": "intent", "facts": {}},
                data_updates={"current_intent": {"remark": "x"}},
            )
            persisted = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(
                persisted["data"]["current_intent"]["remark"],
                "x",
            )
            self.assertEqual(persisted["machine"]["state"], "intent")

    def test_journal_is_bound_to_transition_and_source_hashes(self):
        current = {
            "transition_spec_sha256": "a",
            "execution_source_sha256": "b",
        }
        payload = {"data": {"formal_verification": dict(current)}}
        self.assertTrue(
            journal_matches_verification(payload, current)
        )
        payload["data"]["formal_verification"][
            "execution_source_sha256"
        ] = "changed"
        self.assertFalse(
            journal_matches_verification(payload, current)
        )

    def test_process_mutex_rejects_second_owner(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "execution.lock"
            with ExecutionMutex(path):
                with self.assertRaises(Exception):
                    with ExecutionMutex(path):
                        pass

    def test_process_mutex_can_wait_bounded_for_first_executor(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "execution.lock"
            with ExecutionMutex(path):
                started = time.monotonic()
                with self.assertRaises(Exception):
                    with ExecutionMutex(
                        path,
                        timeout_seconds=0.03,
                        poll_seconds=0.01,
                    ):
                        pass
                self.assertGreaterEqual(
                    time.monotonic() - started,
                    0.02,
                )

    def test_book_plan_requires_descending_valid_depth(self):
        book = QuoteBook(
            symbol="204001.SH",
            quote_time_epoch_ms=1,
            quote_time=datetime.now().astimezone().isoformat(),
            quote_age_seconds=0.1,
            bid_prices=(1.50, 1.49),
            bid_volumes=(100, 100),
            ask_prices=(),
            ask_volumes=(),
        )
        plan = build_book_plan(book, 150)
        self.assertTrue(plan.covers_requested_volume)
        self.assertEqual(plan.limit_rate_percent, 1.49)

    def test_reverse_repo_bid_rate_must_be_finite_positive_and_on_tick(self):
        for invalid in (0, -0.005, float("nan"), float("inf")):
            with self.subTest(invalid=invalid), self.assertRaises(
                QuoteValidationError
            ):
                normalize_repo_rate(invalid)
        with self.assertRaisesRegex(QuoteValidationError, "0.005-percent tick"):
            normalize_repo_rate(1.503)

        zero_bid = QuoteBook(
            symbol="204001.SH",
            quote_time_epoch_ms=1,
            quote_time=datetime.now().astimezone().isoformat(),
            quote_age_seconds=0.1,
            bid_prices=(0.0,),
            bid_volumes=(100,),
            ask_prices=(),
            ask_volumes=(),
        )
        with self.assertRaisesRegex(QuoteValidationError, "must be positive"):
            build_book_plan(zero_bid, 10)


if __name__ == "__main__":
    unittest.main()
