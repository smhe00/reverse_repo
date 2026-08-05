from __future__ import annotations

import argparse
import sys
import unittest
from datetime import date, datetime, timezone
from datetime import time as clock_time
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from gc001_r001_live_afternoon_sweep import (  # noqa: E402
    CONNECT_TIME,
    MAXIMUM_QUOTE_AGE_SECONDS,
    EXECUTION_TIME,
    HARD_STOP,
    BrokerUpdateSignal,
    STRATEGY_NAME,
    AfternoonController,
    _durable_afternoon_remaining,
    _market_break_resume_at,
    _parse_first_execution_time,
    _parse_afternoon_execution_time,
    _parse_cash_usage_ratio,
    _parse_clock_time,
    _reconcile_filled_cash,
    _recover_afternoon,
    _remaining_ratio_budget,
)
from repo_execution_core import (  # noqa: E402
    AtomicJournal,
    ExecutionSafetyError,
)
from repo_execution_state_machine import (  # noqa: E402
    AfternoonEvent,
    AfternoonState,
    initial_afternoon_snapshot,
    snapshot_to_payload,
)
from repo_failure_alert import FailureAlert  # noqa: E402


def _order(
    *,
    remark: str,
    status: int,
    volume: int = 10,
    traded: int = 0,
) -> SimpleNamespace:
    return SimpleNamespace(
        order_id=321,
        stock_code="204001.SH",
        order_type=24,
        order_status=status,
        order_volume=volume,
        traded_volume=traded,
        traded_price=1.4,
        price=1.4,
        status_msg="",
        strategy_name=STRATEGY_NAME,
        order_remark=remark,
    )


class _Trader:
    def __init__(self, orders: list[object]) -> None:
        self.orders = list(orders)

    def query_stock_orders(
        self,
        account: object,
        cancelable_only: bool,
    ) -> list[object]:
        del account, cancelable_only
        return self.orders


class _FailingNotifier:
    def send(self, alert: FailureAlert) -> None:
        del alert
        raise OSError("SMTP unavailable")


class AfternoonRecoveryTests(unittest.TestCase):
    def test_strategy_name_fits_observed_qmt_field_limit(self):
        self.assertLessEqual(len(STRATEGY_NAME), 23)

    def test_second_cash_ratio_accepts_closed_unit_interval(self):
        self.assertEqual(_parse_cash_usage_ratio(0), 0.0)
        self.assertEqual(_parse_cash_usage_ratio("0.45"), 0.45)
        self.assertEqual(_parse_cash_usage_ratio(1), 1.0)
        for value in (-0.01, 1.01, "nan", "inf", "bad"):
            with self.subTest(value=value), self.assertRaises(
                argparse.ArgumentTypeError
            ):
                _parse_cash_usage_ratio(value)

    def test_second_ratio_freezes_one_target_across_retries(self):
        target, remaining, updates = _remaining_ratio_budget(
            data={"accounted_filled_principal_yuan": 0},
            effective_cash=1_000_000,
            cash_usage_ratio=0.40,
            maximum_principal_yuan=0,
        )
        self.assertEqual(target, 399_000)
        self.assertEqual(remaining, 399_000)
        self.assertEqual(updates["target_principal_yuan"], 399_000)

        target, remaining, updates = _remaining_ratio_budget(
            data={
                "accounted_filled_principal_yuan": 150_000,
                "initial_available_cash_yuan": 1_000_000,
                "target_principal_yuan": 399_000,
            },
            effective_cash=850_000,
            cash_usage_ratio=0.40,
            maximum_principal_yuan=0,
        )
        self.assertEqual(target, 399_000)
        self.assertEqual(remaining, 249_000)
        self.assertEqual(updates, {})

    def test_second_ratio_rejects_tampered_durable_target(self):
        with self.assertRaises(ExecutionSafetyError):
            _remaining_ratio_budget(
                data={
                    "accounted_filled_principal_yuan": 0,
                    "initial_available_cash_yuan": 1_000_000,
                    "target_principal_yuan": 500_000,
                },
                effective_cash=1_000_000,
                cash_usage_ratio=0.40,
                maximum_principal_yuan=0,
            )

    def test_durable_afternoon_remaining_tracks_completion(self):
        self.assertEqual(
            _durable_afternoon_remaining(
                {
                    "target_principal_yuan": 400_000,
                    "accounted_filled_principal_yuan": 0,
                }
            ),
            400_000,
        )
        self.assertEqual(
            _durable_afternoon_remaining(
                {
                    "target_principal_yuan": 400_000,
                    "accounted_filled_principal_yuan": 400_000,
                }
            ),
            0,
        )
        self.assertEqual(
            _durable_afternoon_remaining(
                {
                    "target_principal_yuan": 400_000,
                    "accounted_filled_principal_yuan": 399_000,
                }
            ),
            1_000,
        )
        self.assertEqual(
            _durable_afternoon_remaining(
                {
                    "accounted_filled_principal_yuan": 399_000,
                }
            ),
            None,
        )
        self.assertEqual(
            _durable_afternoon_remaining(
                {
                    "target_principal_yuan": "bad",
                    "accounted_filled_principal_yuan": 0,
                }
            ),
            None,
        )

    def test_configurable_afternoon_time_accepts_only_safe_window(self):
        for value in (
            "09:30:00",
            "11:29:59",
            "13:00:00",
            "15:29:59",
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    _parse_afternoon_execution_time(value).isoformat(),
                    value,
                )
        for value in (
            "09:29:59",
            "11:30:00",
            "12:00:00",
            "15:30:00",
            "15:10",
            "bad",
        ):
            with self.subTest(value=value), self.assertRaises(
                argparse.ArgumentTypeError
            ):
                _parse_afternoon_execution_time(value)

    def test_first_time_parser_enforces_two_minute_session_margin(self):
        for value in ("11:28:00", "15:28:00"):
            self.assertEqual(
                _parse_first_execution_time(value).isoformat(),
                value,
            )
        for value in ("11:28:01", "12:00:00", "15:28:01"):
            with self.subTest(value=value), self.assertRaises(
                argparse.ArgumentTypeError
            ):
                _parse_first_execution_time(value)

    def test_internal_connect_time_requires_full_clock_format(self):
        self.assertEqual(_parse_clock_time("15:09:00"), clock_time(15, 9))
        for value in ("15:09", "bad"):
            with self.subTest(value=value), self.assertRaises(
                argparse.ArgumentTypeError
            ):
                _parse_clock_time(value)

    def test_second_executor_waits_through_midday_break(self):
        for value in (
            datetime(2026, 7, 31, 11, 30, tzinfo=timezone.utc),
            datetime(2026, 7, 31, 12, 59, 59, tzinfo=timezone.utc),
        ):
            with self.subTest(value=value):
                resume = _market_break_resume_at(value, value.date())
                self.assertIsNotNone(resume)
                self.assertEqual(resume.time(), clock_time(13, 0))
        for value in (
            datetime(2026, 7, 31, 11, 29, 59, tzinfo=timezone.utc),
            datetime(2026, 7, 31, 13, 0, tzinfo=timezone.utc),
        ):
            with self.subTest(value=value):
                self.assertIsNone(
                    _market_break_resume_at(value, value.date())
                )

    def test_execution_starts_after_stock_market_close_with_retry_budget(self):
        self.assertEqual(EXECUTION_TIME.isoformat(), "15:10:00")
        self.assertEqual(CONNECT_TIME.isoformat(), "15:09:00")
        self.assertEqual(HARD_STOP.isoformat(), "15:30:00")
        self.assertGreater(HARD_STOP, EXECUTION_TIME)
        self.assertEqual(MAXIMUM_QUOTE_AGE_SECONDS, 4.5)

    def test_matching_broker_callback_wakes_afternoon_executor(self):
        prefix = "repo_afternoon_v2_20260731_"
        signal = BrokerUpdateSignal(
            strategy_name=STRATEGY_NAME,
            remark_prefix=prefix,
        )
        signal.on_order(
            SimpleNamespace(
                strategy_name="foreign",
                order_remark=f"{prefix}0001",
            )
        )
        self.assertFalse(signal.wait(0.0))
        signal.on_trade(
            SimpleNamespace(
                strategy_name=STRATEGY_NAME,
                order_remark=f"{prefix}0001",
            )
        )
        self.assertTrue(signal.wait(0.0))

    def _controller(
        self,
        directory: str,
    ) -> tuple[AfternoonController, AtomicJournal]:
        journal = AtomicJournal(
            Path(directory) / "afternoon.json",
            strategy="test",
            trade_date=date(2026, 7, 31),
        )
        snapshot = initial_afternoon_snapshot()
        journal.load_or_initialize(
            machine_payload=snapshot_to_payload(snapshot),
            initial_data={
                "accounted_filled_principal_yuan": 0,
                "cash_cap_yuan": None,
                "current_intent": None,
            },
        )
        controller = AfternoonController(journal, snapshot)
        controller.apply(AfternoonEvent.BEGIN)
        controller.apply(AfternoonEvent.PREFLIGHT_OK)
        return controller, journal

    def test_status_51_is_recovered_as_cancel_pending(self):
        with TemporaryDirectory() as directory:
            controller, journal = self._controller(directory)
            remark = "repo_afternoon_v2_20260731_0001"
            journal.update_data(
                current_intent={
                    "remark": remark,
                    "available_cash_yuan": 10_000,
                }
            )
            recovered = _recover_afternoon(
                trader=_Trader(
                    [_order(remark=remark, status=51)]
                ),
                account=object(),
                controller=controller,
                remark_prefix="repo_afternoon_v2_20260731_",
                sell_order_type=24,
            )
            self.assertIsNotNone(recovered)
            self.assertEqual(
                controller.snapshot.state,
                AfternoonState.CANCEL_PENDING,
            )
            self.assertTrue(
                controller.snapshot.facts.unresolved_order
            )

    def test_restart_reconciles_terminal_fill_into_cash_cap(self):
        with TemporaryDirectory() as directory:
            controller, journal = self._controller(directory)
            remark = "repo_afternoon_v2_20260731_0001"
            journal.update_data(
                current_intent={
                    "remark": remark,
                    "available_cash_yuan": 10_000,
                }
            )
            trader = _Trader(
                [
                    _order(
                        remark=remark,
                        status=53,
                        volume=100,
                        traded=40,
                    )
                ]
            )
            recovered = _recover_afternoon(
                trader=trader,
                account=object(),
                controller=controller,
                remark_prefix="repo_afternoon_v2_20260731_",
                sell_order_type=24,
            )
            self.assertIsNotNone(recovered)
            self.assertEqual(
                controller.snapshot.state,
                AfternoonState.RECONCILE,
            )
            _reconcile_filled_cash(
                trader=trader,
                account=object(),
                controller=controller,
                remark_prefix="repo_afternoon_v2_20260731_",
            )
            self.assertEqual(
                controller.snapshot.state,
                AfternoonState.SCAN,
            )
            data = journal.payload["data"]
            self.assertEqual(
                data["accounted_filled_principal_yuan"],
                4_000,
            )
            self.assertEqual(data["cash_cap_yuan"], 6_000)
            self.assertIsNone(data["current_intent"])

    def test_foreign_active_repo_order_fails_closed(self):
        with TemporaryDirectory() as directory:
            controller, _ = self._controller(directory)
            recovered = _recover_afternoon(
                trader=_Trader(
                    [
                        _order(
                            remark="manual",
                            status=50,
                        )
                    ]
                ),
                account=object(),
                controller=controller,
                remark_prefix="repo_afternoon_v2_20260731_",
                sell_order_type=24,
            )
            self.assertIsNone(recovered)
            self.assertEqual(
                controller.snapshot.state,
                AfternoonState.HALTED,
            )

    def test_owned_remark_with_wrong_direction_fails_closed(self):
        with TemporaryDirectory() as directory:
            controller, journal = self._controller(directory)
            remark = "repo_afternoon_v2_20260731_0001"
            journal.update_data(
                current_intent={
                    "remark": remark,
                    "available_cash_yuan": 10_000,
                }
            )
            wrong = _order(remark=remark, status=50)
            wrong.order_type = 23
            recovered = _recover_afternoon(
                trader=_Trader([wrong]),
                account=object(),
                controller=controller,
                remark_prefix="repo_afternoon_v2_20260731_",
                sell_order_type=24,
            )
            self.assertIsNone(recovered)
            self.assertEqual(
                controller.snapshot.state,
                AfternoonState.HALTED,
            )

    def test_email_failure_cannot_change_safe_halt_result(self):
        with TemporaryDirectory() as directory:
            journal = AtomicJournal(
                Path(directory) / "afternoon.json",
                strategy="test",
                trade_date=date(2026, 7, 31),
            )
            snapshot = initial_afternoon_snapshot()
            journal.load_or_initialize(
                machine_payload=snapshot_to_payload(snapshot),
                initial_data={"environment": "simulation"},
            )
            controller = AfternoonController(
                journal,
                snapshot,
                _FailingNotifier(),
            )
            controller.apply(AfternoonEvent.BEGIN)
            result = controller.halt(
                event=AfternoonEvent.FAULT,
                reason="preflight cannot recover",
            )
            self.assertEqual(result, 1)
            self.assertEqual(
                controller.snapshot.state,
                AfternoonState.HALTED,
            )
            self.assertEqual(
                journal.payload["data"]["failure_alert"]["status"],
                "failed",
            )


if __name__ == "__main__":
    unittest.main()
