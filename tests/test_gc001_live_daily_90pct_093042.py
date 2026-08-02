from __future__ import annotations

import argparse
import sys
import unittest
from datetime import date, datetime, timedelta
from datetime import time as clock_time
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from gc001_live_daily_90pct_093042 import (
    CASH_USAGE_RATIO,
    MAXIMUM_ORDER_ATTEMPTS,
    REMARK_PREFIX,
    STRATEGY_NAME,
    BrokerUpdateSignal,
    MorningController,
    _bid1_limit_plan,
    _finish_order_lifecycle,
    _parse_cash_usage_ratio,
    _parse_morning_execution_time,
    _reconcile_terminal,
    _recover_morning_order,
    _recover_unknown_submission,
    _remaining_order_principal,
    _should_reprice,
    _wait_for_retry_slot,
)
from repo_execution_core import (
    AtomicJournal,
    OrderView,
    QuoteBook,
    floor_principal,
    first_execution_deadline,
    principal_to_qmt_volume,
)
from repo_execution_state_machine import (
    InvalidTransition,
    MorningEvent,
    MorningState,
    advance_morning,
    initial_morning_snapshot,
    snapshot_to_payload,
)
from repo_failure_alert import FailureAlert

TRADE_DATE = date(2026, 7, 31)
REMARK_PREFIX_TODAY = f"{REMARK_PREFIX}_20260731_"
REMARK = f"{REMARK_PREFIX_TODAY}0001"


def _order(
    *,
    status: int,
    volume: int = 10,
    traded: int = 0,
    price: float = 1.5,
    order_id: int = 123,
    remark: str = REMARK,
) -> SimpleNamespace:
    return SimpleNamespace(
        order_id=order_id,
        stock_code="204001.SH",
        order_type=24,
        order_status=status,
        order_volume=volume,
        traded_volume=traded,
        traded_price=price,
        price=price,
        status_msg="",
        strategy_name=STRATEGY_NAME,
        order_remark=remark,
    )


def _book(*, bid1: float, bid1_volume: int) -> QuoteBook:
    return QuoteBook(
        symbol="204001.SH",
        quote_time_epoch_ms=1,
        quote_time="2026-07-31T09:30:42+08:00",
        quote_age_seconds=0.1,
        bid_prices=(bid1,),
        bid_volumes=(bid1_volume,),
        ask_prices=(bid1 + 0.005,),
        ask_volumes=(100_000,),
    )


class _Trader:
    def __init__(
        self,
        orders: list[object],
        *,
        all_orders: list[object] | None = None,
    ) -> None:
        self.orders = list(orders)
        self.all_orders = list(
            all_orders if all_orders is not None else orders[-1:]
        )
        self.cancel_calls: list[int] = []

    def query_stock_order(self, account: object, order_id: int) -> object:
        del account, order_id
        if len(self.orders) > 1:
            return self.orders.pop(0)
        return self.orders[0]

    def query_stock_orders(
        self,
        account: object,
        cancelable_only: bool,
    ) -> list[object]:
        del account, cancelable_only
        return list(self.all_orders)

    def cancel_order_stock(self, account: object, order_id: int) -> int:
        del account
        self.cancel_calls.append(order_id)
        return 0


class _RecordingNotifier:
    def __init__(self) -> None:
        self.alerts: list[FailureAlert] = []

    def send(self, alert: FailureAlert) -> None:
        self.alerts.append(alert)


class _AmbiguousOrderListTrader:
    def query_stock_orders(
        self,
        account: object,
        cancelable_only: bool,
    ) -> None:
        del account, cancelable_only


def _active_controller(
    directory: str,
    *,
    target_principal: int,
    attempt_counter: int = 1,
) -> MorningController:
    journal = AtomicJournal(
        Path(directory) / "morning.json",
        strategy="test",
        trade_date=TRADE_DATE,
    )
    snapshot = initial_morning_snapshot()
    journal.load_or_initialize(
        machine_payload=snapshot_to_payload(snapshot),
        initial_data={
            "environment": "simulation",
            "target_principal_yuan": target_principal,
            "initial_verified_cash_yuan": 200_000.0,
            "accounted_filled_principal_yuan": 0,
            "attempt_counter": attempt_counter,
            "current_intent": {"remark": REMARK},
        },
    )
    controller = MorningController(journal, snapshot)
    for event in (
        MorningEvent.BEGIN,
        MorningEvent.PREFLIGHT_OK,
        MorningEvent.RECOVERY_CLEAR,
        MorningEvent.TRIGGER,
        MorningEvent.SNAPSHOT_OK,
        MorningEvent.INTENT_PERSISTED,
        MorningEvent.SUBMIT_ACCEPTED,
    ):
        controller.apply(event)
    return controller


class MorningStateMachineExecutionTests(unittest.TestCase):
    def test_configurable_morning_time_accepts_only_execution_window(self):
        for value in (
            "09:30:00",
            "11:28:00",
            "13:00:00",
            "15:28:00",
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    _parse_morning_execution_time(value).isoformat(),
                    value,
                )
        for value in (
            "09:29:59",
            "11:28:01",
            "12:00:00",
            "15:28:01",
            "9:30",
            "bad",
        ):
            with self.subTest(value=value), self.assertRaises(
                argparse.ArgumentTypeError
            ):
                _parse_morning_execution_time(value)

    def test_configurable_cash_ratio_is_finite_and_bounded(self):
        self.assertEqual(_parse_cash_usage_ratio(0), 0.0)
        self.assertEqual(_parse_cash_usage_ratio("0.75"), 0.75)
        self.assertEqual(_parse_cash_usage_ratio(1), 1.0)
        for value in (-0.1, 1.01, "nan", "inf", "bad"):
            with self.subTest(value=value), self.assertRaises(
                argparse.ArgumentTypeError
            ):
                _parse_cash_usage_ratio(value)

    def test_sizing_preserves_ninety_percent_contract(self):
        self.assertEqual(CASH_USAGE_RATIO, 0.90)
        self.assertEqual(floor_principal(2_001_880.80, 0.90), 1_801_000)
        self.assertEqual(principal_to_qmt_volume(1_801_000), 18_010)

    def test_first_window_is_five_minutes_or_current_session_end(self):
        timezone = datetime.now().astimezone().tzinfo
        self.assertEqual(
            first_execution_deadline(
                TRADE_DATE,
                clock_time(9, 30, 42),
                timezone=timezone,
            ).time(),
            clock_time(9, 35, 42),
        )
        self.assertEqual(
            first_execution_deadline(
                TRADE_DATE,
                clock_time(11, 28),
                timezone=timezone,
            ).time(),
            clock_time(11, 30),
        )
        self.assertEqual(
            first_execution_deadline(
                TRADE_DATE,
                clock_time(15, 28),
                timezone=timezone,
            ).time(),
            clock_time(15, 30),
        )
        self.assertGreaterEqual(MAXIMUM_ORDER_ATTEMPTS, 52)

    def test_retry_slot_requires_five_seconds_and_time_before_hard_stop(self):
        with TemporaryDirectory() as directory:
            controller = _active_controller(
                directory,
                target_principal=100_000,
            )
            now = datetime.now().astimezone()
            controller.journal.update_data(
                last_submission_attempt_at=now.isoformat()
            )
            self.assertFalse(
                _wait_for_retry_slot(
                    controller=controller,
                    execution_deadline=(
                        now + timedelta(seconds=5)
                    ),
                )
            )

    def test_bid1_limit_submits_full_remaining_even_if_visible_depth_is_small(self):
        plan = _bid1_limit_plan(
            _book(bid1=1.5, bid1_volume=10),
            requested_volume=1_000,
        )
        self.assertEqual(plan.order_volume, 1_000)
        self.assertEqual(plan.principal_yuan, 100_000)
        self.assertEqual(plan.immediately_executable_volume, 10)
        self.assertFalse(plan.covers_requested_volume_immediately)

    def test_same_price_keeps_time_priority_and_changed_price_reprices(self):
        self.assertFalse(
            _should_reprice(current_rate=1.500, fresh_bid1_rate=1.500)
        )
        self.assertTrue(
            _should_reprice(current_rate=1.500, fresh_bid1_rate=1.505)
        )

    def test_stale_cash_cannot_relend_already_filled_principal(self):
        principal, effective_cash = _remaining_order_principal(
            initial_cash_yuan=2_000_000,
            target_principal_yuan=1_800_000,
            cumulative_filled_principal_yuan=1_000_000,
            reported_cash_yuan=2_000_000,
        )
        self.assertEqual(effective_cash, 1_000_000)
        self.assertEqual(principal, 800_000)

        manually_reduced, effective_cash = _remaining_order_principal(
            initial_cash_yuan=2_000_000,
            target_principal_yuan=1_800_000,
            cumulative_filled_principal_yuan=1_000_000,
            reported_cash_yuan=500_000,
        )
        self.assertEqual(effective_cash, 500_000)
        self.assertEqual(manually_reduced, 500_000)

    def test_matching_callback_wakes_poll_but_foreign_callback_does_not(self):
        signal = BrokerUpdateSignal(remark_prefix=REMARK_PREFIX_TODAY)
        signal.on_order(
            SimpleNamespace(
                strategy_name="foreign",
                order_remark=REMARK,
            )
        )
        self.assertFalse(signal.wait(0.0))
        signal.on_trade(
            SimpleNamespace(
                strategy_name=STRATEGY_NAME,
                order_remark=REMARK,
            )
        )
        self.assertTrue(signal.wait(0.0))

    def test_full_fill_reaches_success_only_after_complete_order_reconcile(self):
        with TemporaryDirectory() as directory:
            controller = _active_controller(
                directory,
                target_principal=1_000,
            )
            terminal_raw = _order(status=56, traded=10)
            trader = _Trader(
                [terminal_raw],
                all_orders=[terminal_raw],
            )
            terminal = _finish_order_lifecycle(
                trader=trader,
                account=object(),
                controller=controller,
                order=OrderView.from_qmt(_order(status=50)),
                xtdata=object(),
                update_signal=BrokerUpdateSignal(
                    remark_prefix=REMARK_PREFIX_TODAY
                ),
                execution_deadline=(
                    datetime.now().astimezone() + timedelta(seconds=10)
                ),
            )
            self.assertIsNotNone(terminal)
            self.assertEqual(controller.snapshot.state, MorningState.RECONCILE)
            should_retry = _reconcile_terminal(
                trader=trader,
                account=object(),
                controller=controller,
                order=terminal,
                remark_prefix=REMARK_PREFIX_TODAY,
                execution_deadline=(
                    datetime.now().astimezone() + timedelta(seconds=10)
                ),
                sell_order_type=24,
            )
            self.assertFalse(should_retry)
            self.assertEqual(controller.snapshot.state, MorningState.DONE_FILLED)
            self.assertFalse(controller.snapshot.facts.unresolved_order)

    def test_changed_bid1_cancels_then_waits_for_terminal_confirmation(self):
        with TemporaryDirectory() as directory:
            controller = _active_controller(
                directory,
                target_principal=100_000,
            )
            active = _order(status=50, volume=1_000, price=1.500)
            canceled = _order(
                status=53,
                volume=1_000,
                traded=400,
                price=1.500,
            )
            trader = _Trader(
                [active, canceled],
                all_orders=[canceled],
            )
            with patch(
                "gc001_live_daily_90pct_093042.ORDER_REPRICE_CHECK_SECONDS",
                0.0,
            ), patch(
                "gc001_live_daily_90pct_093042.read_quote_books",
                return_value={"204001.SH": _book(bid1=1.505, bid1_volume=10)},
            ):
                terminal = _finish_order_lifecycle(
                    trader=trader,
                    account=object(),
                    controller=controller,
                    order=OrderView.from_qmt(active),
                    xtdata=object(),
                    update_signal=BrokerUpdateSignal(
                        remark_prefix=REMARK_PREFIX_TODAY
                    ),
                    execution_deadline=(
                        datetime.now().astimezone() + timedelta(seconds=5)
                    ),
                )
            self.assertIsNotNone(terminal)
            self.assertEqual(trader.cancel_calls, [123])
            self.assertEqual(controller.snapshot.state, MorningState.RECONCILE)
            self.assertTrue(controller.snapshot.facts.terminal_order_confirmed)

    def test_partial_terminal_reconciles_total_then_returns_to_snapshot(self):
        with TemporaryDirectory() as directory:
            controller = _active_controller(
                directory,
                target_principal=100_000,
            )
            terminal = OrderView.from_qmt(
                _order(status=53, volume=1_000, traded=400)
            )
            controller.apply(MorningEvent.ORDER_TERMINAL)
            trader = _Trader([], all_orders=[_order(
                status=53,
                volume=1_000,
                traded=400,
            )])
            should_retry = _reconcile_terminal(
                trader=trader,
                account=object(),
                controller=controller,
                order=terminal,
                remark_prefix=REMARK_PREFIX_TODAY,
                execution_deadline=(
                    datetime.now().astimezone() + timedelta(seconds=10)
                ),
                sell_order_type=24,
            )
            self.assertTrue(should_retry)
            self.assertEqual(controller.snapshot.state, MorningState.SNAPSHOT)
            self.assertEqual(
                controller.journal.payload["data"][
                    "accounted_filled_principal_yuan"
                ],
                40_000,
            )
            self.assertFalse(controller.snapshot.facts.unresolved_order)
            self.assertTrue(controller.snapshot.facts.submitted_once)

    def test_partial_fill_at_deadline_finishes_without_another_order(self):
        with TemporaryDirectory() as directory:
            controller = _active_controller(
                directory,
                target_principal=100_000,
            )
            terminal = OrderView.from_qmt(
                _order(status=53, volume=1_000, traded=400)
            )
            controller.apply(MorningEvent.ORDER_TERMINAL)
            trader = _Trader([], all_orders=[_order(
                status=53,
                volume=1_000,
                traded=400,
            )])
            should_retry = _reconcile_terminal(
                trader=trader,
                account=object(),
                controller=controller,
                order=terminal,
                remark_prefix=REMARK_PREFIX_TODAY,
                execution_deadline=(
                    datetime.now().astimezone() - timedelta(seconds=1)
                ),
                sell_order_type=24,
            )
            self.assertFalse(should_retry)
            self.assertEqual(controller.snapshot.state, MorningState.DONE_PARTIAL)

    def test_broker_rejection_does_not_create_a_fast_retry_loop(self):
        with TemporaryDirectory() as directory:
            controller = _active_controller(
                directory,
                target_principal=100_000,
            )
            rejected_raw = _order(status=57, volume=1_000, traded=0)
            rejected = OrderView.from_qmt(rejected_raw)
            controller.apply(MorningEvent.ORDER_TERMINAL)
            trader = _Trader([], all_orders=[rejected_raw])
            should_retry = _reconcile_terminal(
                trader=trader,
                account=object(),
                controller=controller,
                order=rejected,
                remark_prefix=REMARK_PREFIX_TODAY,
                execution_deadline=(
                    datetime.now().astimezone() + timedelta(minutes=1)
                ),
                sell_order_type=24,
            )
            self.assertFalse(should_retry)
            self.assertEqual(controller.snapshot.state, MorningState.HALTED)

    def test_restart_from_active_order_forces_recovery(self):
        snapshot = initial_morning_snapshot()
        for event in (
            MorningEvent.BEGIN,
            MorningEvent.PREFLIGHT_OK,
            MorningEvent.RECOVERY_CLEAR,
            MorningEvent.TRIGGER,
            MorningEvent.SNAPSHOT_OK,
            MorningEvent.INTENT_PERSISTED,
            MorningEvent.SUBMIT_ACCEPTED,
            MorningEvent.RESTART,
        ):
            snapshot = advance_morning(snapshot, event)
        self.assertEqual(snapshot.state, MorningState.RECOVERY)
        self.assertTrue(snapshot.facts.unresolved_order)
        with self.assertRaises(InvalidTransition):
            advance_morning(snapshot, MorningEvent.TRIGGER)

    def test_recovery_halts_if_broker_fill_history_moves_backwards(self):
        with TemporaryDirectory() as directory:
            journal = AtomicJournal(
                Path(directory) / "morning.json",
                strategy="test",
                trade_date=TRADE_DATE,
            )
            snapshot = initial_morning_snapshot()
            journal.load_or_initialize(
                machine_payload=snapshot_to_payload(snapshot),
                initial_data={
                    "environment": "simulation",
                    "target_principal_yuan": 100_000,
                    "initial_verified_cash_yuan": 200_000.0,
                    "accounted_filled_principal_yuan": 40_000,
                },
            )
            controller = MorningController(journal, snapshot)
            controller.apply(MorningEvent.BEGIN)
            controller.apply(MorningEvent.PREFLIGHT_OK)
            recovered = _recover_morning_order(
                trader=_Trader([], all_orders=[]),
                account=object(),
                controller=controller,
                remark_prefix=REMARK_PREFIX_TODAY,
                sell_order_type=24,
            )
            self.assertIsNone(recovered)
            self.assertEqual(controller.snapshot.state, MorningState.HALTED)

    def test_unknown_submission_and_ambiguous_order_list_halts_without_retry(self):
        with TemporaryDirectory() as directory:
            journal = AtomicJournal(
                Path(directory) / "morning.json",
                strategy="test",
                trade_date=TRADE_DATE,
            )
            snapshot = initial_morning_snapshot()
            journal.load_or_initialize(
                machine_payload=snapshot_to_payload(snapshot),
                initial_data={
                    "environment": "simulation",
                    "current_intent": {"remark": REMARK},
                },
            )
            controller = MorningController(journal, snapshot)
            for event in (
                MorningEvent.BEGIN,
                MorningEvent.PREFLIGHT_OK,
                MorningEvent.RECOVERY_CLEAR,
                MorningEvent.TRIGGER,
                MorningEvent.SNAPSHOT_OK,
                MorningEvent.INTENT_PERSISTED,
                MorningEvent.SUBMIT_EXCEPTION,
            ):
                controller.apply(event)
            recovered = _recover_unknown_submission(
                trader=_AmbiguousOrderListTrader(),
                account=object(),
                controller=controller,
                remark=REMARK,
                sell_order_type=24,
            )
            self.assertIsNone(recovered)
            self.assertEqual(controller.snapshot.state, MorningState.HALTED)
            self.assertTrue(controller.snapshot.facts.unresolved_order)

    def test_safe_halt_sends_one_failure_email_after_state_persistence(self):
        with TemporaryDirectory() as directory:
            journal = AtomicJournal(
                Path(directory) / "morning.json",
                strategy="test",
                trade_date=TRADE_DATE,
            )
            snapshot = initial_morning_snapshot()
            journal.load_or_initialize(
                machine_payload=snapshot_to_payload(snapshot),
                initial_data={"environment": "simulation"},
            )
            notifier = _RecordingNotifier()
            controller = MorningController(journal, snapshot, notifier)
            controller.apply(MorningEvent.BEGIN)
            result = controller.halt(
                event=MorningEvent.FAULT,
                reason="preflight cannot recover",
            )
            self.assertEqual(result, 1)
            self.assertEqual(controller.snapshot.state, MorningState.HALTED)
            self.assertEqual(len(notifier.alerts), 1)
            self.assertEqual(
                journal.payload["data"]["failure_alert"]["status"],
                "sent",
            )

    def test_halt_from_intent_escalates_submission_unknown_to_terminal(self):
        with TemporaryDirectory() as directory:
            journal = AtomicJournal(
                Path(directory) / "morning.json",
                strategy="test",
                trade_date=TRADE_DATE,
            )
            snapshot = initial_morning_snapshot()
            journal.load_or_initialize(
                machine_payload=snapshot_to_payload(snapshot),
                initial_data={"environment": "simulation"},
            )
            controller = MorningController(journal, snapshot)
            for event in (
                MorningEvent.BEGIN,
                MorningEvent.PREFLIGHT_OK,
                MorningEvent.RECOVERY_CLEAR,
                MorningEvent.TRIGGER,
                MorningEvent.SNAPSHOT_OK,
                MorningEvent.INTENT_PERSISTED,
            ):
                controller.apply(event)
            result = controller.halt(
                event=MorningEvent.SUBMIT_EXCEPTION,
                reason="unexpected failure after durable intent",
            )
            self.assertEqual(result, 1)
            self.assertEqual(controller.snapshot.state, MorningState.HALTED)
            self.assertTrue(controller.snapshot.facts.unresolved_order)


if __name__ == "__main__":
    unittest.main()
