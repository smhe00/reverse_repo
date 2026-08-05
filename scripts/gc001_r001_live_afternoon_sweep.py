from __future__ import annotations

import argparse
import math
import random
import re
import sys
import time
from collections.abc import Mapping
from dataclasses import asdict
from datetime import date, datetime, timedelta
from datetime import time as clock_time
from pathlib import Path
from typing import Any

from repo_execution_core import (
    GC001,
    PRINCIPAL_STEP_YUAN,
    R001,
    REPO_SYMBOLS,
    AccountBinding,
    AtomicJournal,
    BookPlan,
    BrokerUpdateSignal,
    BrokerQueryAmbiguous,
    ExecutionMutex,
    ExecutionSafetyError,
    OrderClass,
    OrderView,
    QuoteValidationError,
    UnresolvedOrderError,
    assert_order_budget,
    build_book_plan,
    find_unique_order_by_remark,
    floor_principal_after_commission,
    is_first_execution_time,
    is_exchange_trading_day,
    journal_matches_verification,
    orders_with_prefix,
    principal_to_qmt_volume,
    query_all_orders_strict,
    query_asset_strict,
    query_order_strict,
    qmt_strategy_name,
    rank_book_plans,
    read_quote_books,
    reconcile_broker_fills,
    reconcile_cash_cap,
    safe_exception,
    select_bound_account,
    unresolved_repo_orders,
)
from repo_execution_state_machine import (
    AFTERNOON_TERMINAL_STATES,
    AfternoonEvent,
    AfternoonState,
    MachineSnapshot,
    advance_afternoon,
    afternoon_snapshot_from_payload,
    initial_afternoon_snapshot,
    snapshot_to_payload,
    verify_state_machines,
)
from repo_failure_alert import (
    FailureNotifier,
    load_optional_alert_notifiers,
    notify_journal_failure,
    notify_journal_success,
    send_standalone_failure,
)

EXECUTION_TIME = clock_time(15, 10, 0)
FIRST_EXECUTION_TIME = clock_time(9, 30, 42)
CONNECT_TIME = clock_time(15, 9, 0)
SZ_CONTINUOUS_END = clock_time(15, 27, 0)
HARD_STOP = clock_time(15, 30, 0)
MORNING_SESSION_END = clock_time(11, 30, 0)
AFTERNOON_SESSION_START = clock_time(13, 0, 0)
MAXIMUM_QUOTE_AGE_SECONDS = 4.5
ORDER_OBSERVE_SECONDS = 2.0
CANCEL_CONFIRM_SECONDS = 15.0
ORDER_STATUS_RECONCILE_SECONDS = 1.0
CANCEL_STATUS_RECONCILE_SECONDS = 0.5
NO_FUNDS_RECHECK_SECONDS = 1.0
NO_BOOK_RECHECK_SECONDS = 0.25
SUBMISSION_BACKOFF_SECONDS = 1.0
MAXIMUM_REJECTED_SUBMISSIONS = 5
MAXIMUM_ZERO_FILL_TERMINALS = 5
MAXIMUM_TOTAL_ATTEMPTS = 50
REMARK_PREFIX = "repo_afternoon_v2"
STRATEGY_NAME = qmt_strategy_name("repo_afternoon_v2")


def _parse_clock_time(value: object) -> clock_time:
    text = str(value)
    if re.fullmatch(r"\d{2}:\d{2}:\d{2}", text) is None:
        raise argparse.ArgumentTypeError(
            "clock time must use HH:MM:SS"
        )
    try:
        parsed = clock_time.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "clock time must use HH:MM:SS"
        ) from exc
    if parsed.tzinfo is not None:
        raise argparse.ArgumentTypeError(
            "clock time must not include a timezone"
        )
    return parsed


def _parse_second_execution_time(value: object) -> clock_time:
    parsed = _parse_clock_time(value)
    if not (
        clock_time(9, 30) <= parsed < MORNING_SESSION_END
        or AFTERNOON_SESSION_START <= parsed < HARD_STOP
    ):
        raise argparse.ArgumentTypeError(
            "second execution time must be from 09:30:00 before "
            "11:30:00 or from 13:00:00 before 15:30:00"
        )
    return parsed


_parse_afternoon_execution_time = _parse_second_execution_time


def _parse_first_execution_time(value: object) -> clock_time:
    parsed = _parse_clock_time(value)
    if not is_first_execution_time(parsed):
        raise argparse.ArgumentTypeError(
            "first execution time must be from 09:30:00 through "
            "11:28:00 or from 13:00:00 through 15:28:00"
        )
    return parsed


def _parse_cash_usage_ratio(value: object) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            "cash usage ratio must be a number"
        ) from exc
    if not math.isfinite(parsed) or not 0 <= parsed <= 1:
        raise argparse.ArgumentTypeError(
            "cash usage ratio must be from 0 through 1"
        )
    return parsed


class AfternoonController:
    def __init__(
        self,
        journal: AtomicJournal,
        snapshot: MachineSnapshot[AfternoonState],
        notifier: FailureNotifier | None = None,
    ) -> None:
        self.journal = journal
        self.snapshot = snapshot
        self.notifier = notifier

    def apply(
        self,
        event: AfternoonEvent,
        *,
        details: Mapping[str, object] | None = None,
        data_updates: Mapping[str, object] | None = None,
    ) -> None:
        self.snapshot = advance_afternoon(self.snapshot, event)
        self.journal.transition(
            event=event.value,
            machine_payload=snapshot_to_payload(self.snapshot),
            details=details,
            data_updates=data_updates,
        )

    def halt(
        self,
        *,
        event: AfternoonEvent,
        reason: str,
        error: BaseException | None = None,
    ) -> int:
        details: dict[str, object] = {"reason": reason}
        if error is not None:
            details["error"] = safe_exception(error)
        self.apply(
            event,
            details=details,
            data_updates={
                "final_reason": reason,
                "finished_at": datetime.now().astimezone().isoformat(),
                "success": False,
            },
        )
        self.notify_failure(
            event=event.value,
            reason=reason,
            error=error,
        )
        return 1

    def notify_failure(
        self,
        *,
        event: str,
        reason: str,
        error: BaseException | Mapping[str, object] | None = None,
    ) -> None:
        data = self.journal.payload.get("data") or {}
        notify_journal_failure(
            self.notifier,
            self.journal,
            environment=str(data.get("environment", "unknown")),
            state=self.snapshot.state.value,
            event=event,
            reason=reason,
            unresolved_order=self.snapshot.facts.unresolved_order,
            error=error,
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Formally model-checked second residual-cash executor. "
            "It monitors through 15:30:00 and serializes every order."
        )
    )
    parser.add_argument("--qmt-path", required=True)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--journal", required=True)
    parser.add_argument("--account-binding", required=True)
    parser.add_argument(
        "--environment",
        required=True,
        choices=("live", "simulation"),
    )
    parser.add_argument("--mutex", required=True)
    parser.add_argument(
        "--execution-time",
        type=_parse_second_execution_time,
        default=EXECUTION_TIME,
    )
    parser.add_argument(
        "--first-execution-time",
        type=_parse_first_execution_time,
        default=FIRST_EXECUTION_TIME,
    )
    parser.add_argument(
        "--cash-usage-ratio",
        type=_parse_cash_usage_ratio,
        default=1.0,
    )
    parser.add_argument(
        "--connect-time",
        type=_parse_clock_time,
        default=CONNECT_TIME,
    )
    parser.add_argument(
        "--alert-config",
        default="",
        help="Optional failure-email configuration; contains no SMTP password.",
    )
    parser.add_argument(
        "--maximum-principal-yuan",
        type=int,
        default=0,
        help="Simulation/canary cap; zero means no additional cap.",
    )
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()

    verification = verify_state_machines()
    if args.validate_only:
        print(verification)
        return 0
    if args.cash_usage_ratio == 0:
        print("Second reverse-repo execution skipped: cash usage ratio is 0.")
        return 0

    notifier: FailureNotifier | None = None
    try:
        if args.alert_config:
            notifier, alert_warnings = load_optional_alert_notifiers(
                Path(args.alert_config)
            )
            if alert_warnings:
                print(
                    "WARNING: optional notifications are disabled: "
                    + "; ".join(alert_warnings),
                    file=sys.stderr,
                )
        return _run_afternoon_command(args, verification, notifier)
    except Exception as exc:  # noqa: BLE001
        if notifier is not None:
            try:
                send_standalone_failure(
                    notifier,
                    strategy=STRATEGY_NAME,
                    trade_date=str(args.trade_date),
                    environment=str(args.environment),
                    reason="executor failed outside a recoverable state",
                    journal_path=Path(args.journal),
                    error=exc,
                )
            except Exception as alert_exc:  # noqa: BLE001
                print(
                    "Failure email could not be delivered: "
                    f"{type(alert_exc).__name__}: {alert_exc}",
                    file=sys.stderr,
                )
        print(
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1


def _run_afternoon_command(
    args: argparse.Namespace,
    verification: Mapping[str, object],
    notifier: FailureNotifier | None,
) -> int:
    trade_date = date.fromisoformat(args.trade_date)
    if trade_date != datetime.now().astimezone().date():
        raise ValueError("trade date must equal the local calendar date")
    qmt_path = Path(args.qmt_path).resolve()
    if not qmt_path.is_dir():
        raise ValueError(f"QMT userdata path does not exist: {qmt_path}")
    maximum_principal = int(args.maximum_principal_yuan)
    if maximum_principal < 0:
        raise ValueError("maximum principal cannot be negative")
    if (
        maximum_principal
        and maximum_principal % PRINCIPAL_STEP_YUAN
    ):
        raise ValueError(
            "maximum principal must be a CNY 1,000 multiple"
        )
    now = datetime.now().astimezone()
    execution_at = _at(trade_date, args.execution_time, now)
    first_execution_at = _at(
        trade_date,
        args.first_execution_time,
        now,
    )
    if execution_at - first_execution_at < timedelta(minutes=5):
        raise ValueError(
            "second execution time must be at least five minutes "
            "after first execution time"
        )
    connect_at = _at(trade_date, args.connect_time, now)
    if connect_at >= execution_at:
        raise ValueError("connect time must be before execution time")
    if (execution_at - connect_at).total_seconds() > 300:
        raise ValueError("connect lead cannot exceed five minutes")
    sz_end = _at(trade_date, SZ_CONTINUOUS_END, now)
    hard_stop = _at(trade_date, HARD_STOP, now)
    day_prefix = f"{REMARK_PREFIX}_{trade_date:%Y%m%d}_"
    journal = AtomicJournal(
        Path(args.journal),
        strategy=STRATEGY_NAME,
        trade_date=trade_date,
    )
    mutex_wait_seconds = max(
        0.0,
        (hard_stop - datetime.now().astimezone()).total_seconds(),
    )
    with ExecutionMutex(
        Path(args.mutex),
        timeout_seconds=mutex_wait_seconds,
    ):
        result = run_afternoon(
            qmt_path=qmt_path,
            account_binding=Path(args.account_binding),
            environment=args.environment,
            trade_date=trade_date,
            connect_at=connect_at,
            execution_at=execution_at,
            sz_continuous_end=sz_end,
            hard_stop=hard_stop,
            remark_prefix=day_prefix,
            maximum_principal_yuan=maximum_principal,
            cash_usage_ratio=float(args.cash_usage_ratio),
            journal=journal,
            formal_verification=verification,
            notifier=notifier,
        )
        data = journal.payload.get("data") or {}
        if result == 0 and data.get("success") is True:
            notify_journal_success(
                notifier,
                journal,
                environment=args.environment,
                state=str((journal.payload.get("machine") or {}).get("state")),
            )
        return result


def run_afternoon(
    *,
    qmt_path: Path,
    account_binding: Path,
    environment: str,
    trade_date: date,
    connect_at: datetime,
    execution_at: datetime,
    sz_continuous_end: datetime,
    hard_stop: datetime,
    remark_prefix: str,
    maximum_principal_yuan: int,
    cash_usage_ratio: float,
    journal: AtomicJournal,
    formal_verification: Mapping[str, object],
    notifier: FailureNotifier | None = None,
) -> int:
    payload, existed = journal.load_or_initialize(
        machine_payload=snapshot_to_payload(
            initial_afternoon_snapshot()
        ),
        initial_data={
            "environment": environment,
            "symbols": list(REPO_SYMBOLS),
            "side": "SELL",
            "execution_at": execution_at.isoformat(),
            "hard_stop": hard_stop.isoformat(),
            "remark_prefix": remark_prefix,
            "attempt_counter": 0,
            "rejected_submission_count": 0,
            "accounted_filled_principal_yuan": 0,
            "cash_cap_yuan": None,
            "cash_usage_ratio": cash_usage_ratio,
            "initial_available_cash_yuan": None,
            "target_principal_yuan": None,
            "current_intent": None,
            "success": False,
            "account_id_persisted": False,
            "formal_verification": dict(formal_verification),
        },
    )
    if existed:
        data = dict(payload.get("data") or {})
        expected = {
            "execution_at": execution_at.isoformat(),
            "hard_stop": hard_stop.isoformat(),
            "cash_usage_ratio": cash_usage_ratio,
        }
        for name, value in expected.items():
            if data.get(name) != value:
                raise ExecutionSafetyError(
                    f"existing second journal has different {name}"
                )
    controller = AfternoonController(
        journal,
        afternoon_snapshot_from_payload(payload["machine"]),
        notifier,
    )
    if controller.snapshot.state in AFTERNOON_TERMINAL_STATES:
        if controller.snapshot.state is AfternoonState.HALTED:
            data = journal.payload.get("data") or {}
            history = journal.payload.get("history") or []
            last_event = (
                str(history[-1].get("event", "safe_halt"))
                if history and isinstance(history[-1], Mapping)
                else "safe_halt"
            )
            controller.notify_failure(
                event=last_event,
                reason=str(
                    data.get(
                        "final_reason",
                        "existing journal is in safe-halt state",
                    )
                ),
            )
        return _terminal_exit_code(controller.snapshot.state)
    if existed and not journal_matches_verification(
        payload,
        formal_verification,
    ):
        reason = (
            "journal was created by a different state-machine "
            "implementation; manual review required"
        )
        journal.update_data(
            success=False,
            final_reason=reason,
            finished_at=datetime.now().astimezone().isoformat(),
        )
        controller.notify_failure(
            event="verification_mismatch",
            reason=reason,
        )
        return 1
    if existed and controller.snapshot.state is not AfternoonState.NEW:
        controller.apply(
            AfternoonEvent.RESTART,
            details={"reason": "existing nonterminal journal recovered"},
        )
    if controller.snapshot.state is AfternoonState.NEW:
        controller.apply(AfternoonEvent.BEGIN)

    from xtquant import xtconstant, xtdata, xttype
    from xtquant.xttrader import XtQuantTrader, XtQuantTraderCallback

    xtdata.enable_hello = False
    trader: Any = None
    subscriptions: list[int] = []
    account: Any = None
    binding: AccountBinding | None = None
    update_signal = BrokerUpdateSignal(
        strategy_name=STRATEGY_NAME,
        remark_prefix=remark_prefix,
    )

    class AfternoonPushCallback(XtQuantTraderCallback):
        def on_stock_order(self, order: object) -> None:
            update_signal.on_order(order)

        def on_stock_trade(self, trade: object) -> None:
            update_signal.on_trade(trade)

    try:
        if not is_exchange_trading_day(xtdata, trade_date):
            if controller.snapshot.state is AfternoonState.PREFLIGHT:
                controller.apply(
                    AfternoonEvent.NON_TRADING_DAY,
                    details={"reason": "not an exchange trading day"},
                    data_updates={
                        "success": True,
                        "finished_at": (
                            datetime.now().astimezone().isoformat()
                        ),
                    },
                )
                return 0
            return controller.halt(
                event=AfternoonEvent.RECOVERY_AMBIGUOUS,
                reason=(
                    "a nonterminal journal exists on a date the exchange "
                    "calendar now reports as non-trading"
                ),
            )
        if datetime.now().astimezone() > hard_stop:
            return controller.halt(
                event=AfternoonEvent.FAULT,
                reason="started after the afternoon hard stop",
            )
        _wait_until(connect_at)
        trader = XtQuantTrader(
            str(qmt_path),
            random.randint(100_000_000, 999_999_999),
            AfternoonPushCallback(),
        )
        trader.start()
        connect_result = int(trader.connect())
        if connect_result != 0:
            raise ExecutionSafetyError(
                f"QMT connection failed: {connect_result}"
            )
        account, binding = select_bound_account(
            trader,
            xtconstant,
            xttype,
            environment=environment,
            qmt_path=qmt_path,
            binding_path=account_binding,
        )
        subscribe_result = int(trader.subscribe(account))
        if subscribe_result != 0:
            raise ExecutionSafetyError(
                f"account subscription failed: {subscribe_result}"
            )
        if controller.snapshot.state is AfternoonState.PREFLIGHT:
            controller.apply(
                AfternoonEvent.PREFLIGHT_OK,
                details={
                    "account_label": binding.label,
                    "environment": binding.environment,
                },
                data_updates={
                    "account_label": binding.label,
                    "environment_verified": True,
                    "account_verified": True,
                },
            )
        else:
            journal.update_data(
                account_label=binding.label,
                environment_verified=True,
                account_verified=True,
                preflight_reverified_at=(
                    datetime.now().astimezone().isoformat()
                ),
            )

        for symbol in REPO_SYMBOLS:
            sequence = int(
                xtdata.subscribe_quote(
                    symbol,
                    period="tick",
                    count=0,
                )
                or 0
            )
            if sequence <= 0:
                raise ExecutionSafetyError(
                    f"quote subscription failed for {symbol}: {sequence}"
                )
            subscriptions.append(sequence)

        recovered = _recover_afternoon(
            trader=trader,
            account=account,
            controller=controller,
            remark_prefix=remark_prefix,
            sell_order_type=int(xtconstant.STOCK_SELL),
        )
        if controller.snapshot.state in AFTERNOON_TERMINAL_STATES:
            return _terminal_exit_code(controller.snapshot.state)
        if recovered is not None:
            if not _finish_order_lifecycle(
                trader=trader,
                account=account,
                controller=controller,
                order=recovered,
                update_signal=update_signal,
            ):
                return 1
            _reconcile_filled_cash(
                trader=trader,
                account=account,
                controller=controller,
                remark_prefix=remark_prefix,
            )
            if controller.snapshot.state in AFTERNOON_TERMINAL_STATES:
                return _terminal_exit_code(controller.snapshot.state)
            if _durable_afternoon_remaining(
                journal.payload.get("data") or {}
            ) == 0:
                return _finish_at_hard_stop(
                    trader=trader,
                    account=account,
                    controller=controller,
                    maximum_principal_yuan=maximum_principal_yuan,
                    cash_usage_ratio=cash_usage_ratio,
                )

        _wait_until(execution_at)
        if controller.snapshot.state is AfternoonState.WAIT_WINDOW:
            controller.apply(AfternoonEvent.TRIGGER)

        while datetime.now().astimezone() <= hard_stop:
            now = datetime.now().astimezone()
            resume_at = _market_break_resume_at(now, trade_date)
            if resume_at is not None:
                _wait_until(resume_at)
                continue
            if _durable_afternoon_remaining(
                journal.payload.get("data") or {}
            ) == 0:
                return _finish_at_hard_stop(
                    trader=trader,
                    account=account,
                    controller=controller,
                    maximum_principal_yuan=maximum_principal_yuan,
                    cash_usage_ratio=cash_usage_ratio,
                )
            if controller.snapshot.state is AfternoonState.BACKOFF:
                data = dict(journal.payload.get("data") or {})
                rejected = int(
                    data.get("rejected_submission_count", 0)
                )
                if rejected >= MAXIMUM_REJECTED_SUBMISSIONS:
                    return controller.halt(
                        event=AfternoonEvent.FAULT,
                        reason=(
                            "maximum confirmed submission rejections "
                            "reached"
                        ),
                    )
                time.sleep(SUBMISSION_BACKOFF_SECONDS)
                controller.apply(AfternoonEvent.RETRY_SUBMIT)
            if controller.snapshot.state is AfternoonState.WAIT_FUNDS:
                time.sleep(NO_FUNDS_RECHECK_SECONDS)
                controller.apply(AfternoonEvent.RETRY_SCAN)
            elif controller.snapshot.state is AfternoonState.WAIT_BOOK:
                time.sleep(NO_BOOK_RECHECK_SECONDS)
                controller.apply(AfternoonEvent.RETRY_SCAN)

            plan_and_cash = _scan_submission_plan(
                trader=trader,
                account=account,
                xtdata=xtdata,
                controller=controller,
                sz_continuous_end=sz_continuous_end,
                hard_stop=hard_stop,
                maximum_principal_yuan=maximum_principal_yuan,
                cash_usage_ratio=cash_usage_ratio,
            )
            if controller.snapshot.state in AFTERNOON_TERMINAL_STATES:
                return _terminal_exit_code(controller.snapshot.state)
            if plan_and_cash is None:
                continue
            plan, effective_cash = plan_and_cash
            data = dict(journal.payload.get("data") or {})
            attempt_number = int(data.get("attempt_counter", 0)) + 1
            if attempt_number > MAXIMUM_TOTAL_ATTEMPTS:
                return controller.halt(
                    event=AfternoonEvent.FAULT,
                    reason="maximum total order attempts reached",
                )
            remark = f"{remark_prefix}{attempt_number:04d}"
            intent = {
                "attempt_number": attempt_number,
                "remark": remark,
                "symbol": plan.symbol,
                "side": "SELL",
                "available_cash_yuan": effective_cash,
                "principal_yuan": plan.principal_yuan,
                "qmt_volume": plan.executable_volume,
                "limit_rate_percent": plan.limit_rate_percent,
                "expected_book_vwap_percent": (
                    plan.expected_vwap_percent
                ),
                "quote_time": plan.quote_time,
                "quote_age_seconds": plan.quote_age_seconds,
                "persisted_before_submission": True,
            }
            assert_order_budget(
                principal_yuan=plan.principal_yuan,
                verified_available_cash_yuan=effective_cash,
                maximum_ratio=1.0,
            )
            controller.apply(
                AfternoonEvent.INTENT_PERSISTED,
                details=intent,
                data_updates={
                    "attempt_counter": attempt_number,
                    "current_intent": intent,
                },
            )
            order = _submit_or_recover(
                trader=trader,
                account=account,
                xtconstant=xtconstant,
                controller=controller,
                intent=intent,
            )
            if controller.snapshot.state in AFTERNOON_TERMINAL_STATES:
                return _terminal_exit_code(controller.snapshot.state)
            if order is None:
                continue
            if not _finish_order_lifecycle(
                trader=trader,
                account=account,
                controller=controller,
                order=order,
                update_signal=update_signal,
            ):
                return 1
            _reconcile_filled_cash(
                trader=trader,
                account=account,
                controller=controller,
                remark_prefix=remark_prefix,
            )
            if controller.snapshot.state in AFTERNOON_TERMINAL_STATES:
                return _terminal_exit_code(controller.snapshot.state)

        return _finish_at_hard_stop(
            trader=trader,
            account=account,
            controller=controller,
            maximum_principal_yuan=maximum_principal_yuan,
            cash_usage_ratio=cash_usage_ratio,
        )
    except BrokerQueryAmbiguous as exc:
        if controller.snapshot.state in AFTERNOON_TERMINAL_STATES:
            return _terminal_exit_code(controller.snapshot.state)
        return controller.halt(
            event=_query_failure_event(controller.snapshot.state),
            reason="broker query remained ambiguous",
            error=exc,
        )
    except Exception as exc:
        if controller.snapshot.state in AFTERNOON_TERMINAL_STATES:
            journal.update_data(
                unhandled_error=safe_exception(exc),
                success=False,
            )
            controller.notify_failure(
                event="terminal_unhandled_error",
                reason="unhandled error after reaching a terminal state",
                error=exc,
            )
            return 1
        return controller.halt(
            event=_fault_event(controller.snapshot.state),
            reason="unhandled fail-closed execution error",
            error=exc,
        )
    finally:
        for sequence in subscriptions:
            try:
                xtdata.unsubscribe_quote(sequence)
            except Exception:
                pass
        if trader is not None:
            trader.stop()


def _recover_afternoon(
    *,
    trader: object,
    account: object,
    controller: AfternoonController,
    remark_prefix: str,
    sell_order_type: int,
) -> OrderView | None:
    orders = query_all_orders_strict(trader, account)
    own = orders_with_prefix(orders, remark_prefix=remark_prefix)
    if any(
        order.order_type != int(sell_order_type)
        or order.strategy_name != STRATEGY_NAME
        for order in own
    ):
        controller.halt(
            event=AfternoonEvent.RECOVERY_AMBIGUOUS,
            reason="owned broker order has an unexpected identity",
        )
        return None
    remarks = [order.remark for order in own]
    if len(remarks) != len(set(remarks)):
        controller.halt(
            event=AfternoonEvent.RECOVERY_AMBIGUOUS,
            reason="duplicate broker remarks exist for this strategy day",
        )
        return None
    unresolved = unresolved_repo_orders(orders)
    foreign_unresolved = [
        order for order in unresolved if order not in own
    ]
    if foreign_unresolved:
        controller.halt(
            event=AfternoonEvent.RECOVERY_AMBIGUOUS,
            reason="another unresolved reverse-repo order exists",
        )
        return None
    own_unresolved = [
        order
        for order in own
        if order.classification
        in {OrderClass.ACTIVE, OrderClass.CANCEL_PENDING}
    ]
    own_unknown = [
        order
        for order in own
        if order.classification is OrderClass.UNKNOWN
    ]
    if own_unknown or len(own_unresolved) > 1:
        controller.halt(
            event=AfternoonEvent.RECOVERY_AMBIGUOUS,
            reason="broker order recovery is ambiguous",
        )
        return None
    data = dict(controller.journal.payload.get("data") or {})
    intent = data.get("current_intent")
    intent_remark = (
        str(intent.get("remark", ""))
        if isinstance(intent, dict)
        else ""
    )
    if own_unresolved:
        order = own_unresolved[0]
        if intent_remark and order.remark != intent_remark:
            controller.halt(
                event=AfternoonEvent.RECOVERY_AMBIGUOUS,
                reason="unresolved order does not match durable intent",
            )
            return None
        if order.classification is OrderClass.ACTIVE:
            event = AfternoonEvent.RECOVERY_ACTIVE
        else:
            event = AfternoonEvent.RECOVERY_CANCEL_PENDING
        controller.apply(
            event,
            details={"order": order.safe_payload()},
            data_updates={
                "current_order_id": order.order_id,
                "current_order": order.safe_payload(),
            },
        )
        return order
    if intent_remark:
        order = find_unique_order_by_remark(own, intent_remark)
        if order is None:
            controller.halt(
                event=AfternoonEvent.RECOVERY_AMBIGUOUS,
                reason=(
                    "durable intent exists but broker order cannot be "
                    "proven absent"
                ),
            )
            return None
        if order.classification not in {
            OrderClass.FILLED,
            OrderClass.TERMINAL_PARTIAL,
            OrderClass.CANCELED_ZERO,
            OrderClass.REJECTED,
        }:
            controller.halt(
                event=AfternoonEvent.RECOVERY_AMBIGUOUS,
                reason="intent order has no recognized terminal state",
            )
            return None
        controller.apply(
            AfternoonEvent.RECOVERY_TERMINAL,
            details={"order": order.safe_payload()},
            data_updates={
                "current_order_id": order.order_id,
                "current_order": order.safe_payload(),
            },
        )
        return order

    accounted = int(
        data.get("accounted_filled_principal_yuan", 0)
    )
    broker_filled = sum(order.principal_yuan for order in own)
    if broker_filled != accounted:
        controller.halt(
            event=AfternoonEvent.RECOVERY_AMBIGUOUS,
            reason=(
                "broker fills differ from the journal without a durable "
                "intent baseline"
            ),
        )
        return None
    controller.apply(AfternoonEvent.RECOVERY_CLEAR)
    return None


def _scan_submission_plan(
    *,
    trader: object,
    account: object,
    xtdata: object,
    controller: AfternoonController,
    sz_continuous_end: datetime,
    hard_stop: datetime,
    maximum_principal_yuan: int,
    cash_usage_ratio: float,
) -> tuple[BookPlan, float] | None:
    if controller.snapshot.state is not AfternoonState.SCAN:
        raise ExecutionSafetyError(
            f"scan called from {controller.snapshot.state.value}"
        )
    now = datetime.now().astimezone()
    orders = query_all_orders_strict(trader, account)
    unresolved = unresolved_repo_orders(orders)
    if unresolved:
        raise UnresolvedOrderError(
            "an unresolved reverse-repo order appeared during scanning"
        )
    cash_snapshot = query_asset_strict(trader, account)
    data = dict(controller.journal.payload.get("data") or {})
    cash_cap = _optional_float(data.get("cash_cap_yuan"))
    effective_cash, updated_cap = reconcile_cash_cap(
        cash_snapshot.conservative_available_cash,
        cash_cap,
    )
    target, remaining_target, target_updates = _remaining_ratio_budget(
        data=data,
        effective_cash=effective_cash,
        cash_usage_ratio=cash_usage_ratio,
        maximum_principal_yuan=maximum_principal_yuan,
    )
    controller.journal.update_data(
        last_cash_snapshot=asdict(cash_snapshot),
        last_effective_cash_yuan=effective_cash,
        cash_cap_yuan=updated_cap,
        **target_updates,
    )
    principal = min(
        floor_principal_after_commission(effective_cash),
        remaining_target,
    )
    if now >= hard_stop:
        if principal < PRINCIPAL_STEP_YUAN:
            controller.apply(
                AfternoonEvent.HARD_STOP_CLEAR,
                details={"residual_cash_yuan": effective_cash},
                data_updates={
                    "success": True,
                    "finished_at": now.isoformat(),
                    "residual_cash_yuan": effective_cash,
                },
            )
        else:
            controller.halt(
                event=AfternoonEvent.HARD_STOP_RESIDUAL,
                reason=(
                    f"hard stop reached with CNY {remaining_target} "
                    "of the configured target still unlent"
                ),
            )
        return None
    if principal < PRINCIPAL_STEP_YUAN:
        controller.apply(
            AfternoonEvent.NO_FUNDS,
            details={
                "effective_cash_yuan": effective_cash,
                "remaining_target_principal_yuan": remaining_target,
                "monitoring_continues": True,
            },
        )
        return None

    symbols = [GC001]
    if now < sz_continuous_end:
        symbols.append(R001)
    try:
        books = read_quote_books(
            xtdata,
            symbols,
            now=now,
            maximum_age_seconds=MAXIMUM_QUOTE_AGE_SECONDS,
        )
        plans = _plans_from_books(
            books,
            principal_to_qmt_volume(principal),
        )
    except QuoteValidationError as exc:
        controller.apply(
            AfternoonEvent.NO_BOOK,
            details={"reason": safe_exception(exc)},
        )
        return None
    if not plans:
        controller.apply(
            AfternoonEvent.NO_BOOK,
            details={"reason": "no executable valid order book"},
        )
        return None

    # Repeat all broker and market reads immediately before durable intent.
    final_orders = query_all_orders_strict(trader, account)
    if unresolved_repo_orders(final_orders):
        raise UnresolvedOrderError(
            "an unresolved order appeared before intent persistence"
        )
    final_cash_snapshot = query_asset_strict(trader, account)
    effective_cash, updated_cap = reconcile_cash_cap(
        final_cash_snapshot.conservative_available_cash,
        updated_cap,
    )
    data = dict(controller.journal.payload.get("data") or {})
    _, remaining_target, _ = _remaining_ratio_budget(
        data=data,
        effective_cash=effective_cash,
        cash_usage_ratio=cash_usage_ratio,
        maximum_principal_yuan=maximum_principal_yuan,
    )
    final_principal = min(
        floor_principal_after_commission(effective_cash),
        remaining_target,
    )
    if final_principal < PRINCIPAL_STEP_YUAN:
        controller.journal.update_data(cash_cap_yuan=updated_cap)
        controller.apply(
            AfternoonEvent.NO_FUNDS,
            details={
                "effective_cash_yuan": effective_cash,
                "remaining_target_principal_yuan": remaining_target,
                "monitoring_continues": True,
            },
        )
        return None
    final_now = datetime.now().astimezone()
    final_symbols = [GC001]
    if final_now < sz_continuous_end:
        final_symbols.append(R001)
    try:
        final_books = read_quote_books(
            xtdata,
            final_symbols,
            now=final_now,
            maximum_age_seconds=MAXIMUM_QUOTE_AGE_SECONDS,
        )
        final_plans = _plans_from_books(
            final_books,
            principal_to_qmt_volume(final_principal),
        )
    except QuoteValidationError as exc:
        controller.apply(
            AfternoonEvent.NO_BOOK,
            details={"reason": safe_exception(exc)},
        )
        return None
    if not final_plans:
        controller.apply(
            AfternoonEvent.NO_BOOK,
            details={"reason": "final order books are not executable"},
        )
        return None
    selected = final_plans[0]
    controller.apply(
        AfternoonEvent.SCAN_READY,
        details={
            "cash": asdict(final_cash_snapshot),
            "effective_cash_yuan": effective_cash,
            "selected_plan": asdict(selected),
        },
        data_updates={
            "last_cash_snapshot_before_intent": asdict(
                final_cash_snapshot
            ),
            "last_effective_cash_yuan": effective_cash,
            "cash_cap_yuan": updated_cap,
            "target_principal_yuan": target,
            "remaining_target_principal_yuan": remaining_target,
        },
    )
    return selected, effective_cash


def _plans_from_books(
    books: Mapping[str, Any],
    requested_volume: int,
) -> list[BookPlan]:
    plans: list[BookPlan] = []
    for book in books.values():
        try:
            plans.append(build_book_plan(book, requested_volume))
        except QuoteValidationError:
            continue
    return rank_book_plans(plans)


def _submit_or_recover(
    *,
    trader: object,
    account: object,
    xtconstant: object,
    controller: AfternoonController,
    intent: Mapping[str, object],
) -> OrderView | None:
    remark = str(intent["remark"])
    try:
        order_id = int(
            trader.order_stock(
                account,
                str(intent["symbol"]),
                xtconstant.STOCK_SELL,
                int(intent["qmt_volume"]),
                xtconstant.FIX_PRICE,
                float(intent["limit_rate_percent"]),
                STRATEGY_NAME,
                remark,
            )
        )
    except Exception as exc:
        controller.apply(
            AfternoonEvent.SUBMIT_EXCEPTION,
            details={"error": safe_exception(exc)},
        )
        return _recover_unknown_submission(
            trader=trader,
            account=account,
            controller=controller,
            remark=remark,
        )
    if order_id <= 0:
        orders = query_all_orders_strict(trader, account)
        recovered = find_unique_order_by_remark(orders, remark)
        if recovered is None:
            data = dict(controller.journal.payload.get("data") or {})
            rejected = int(
                data.get("rejected_submission_count", 0)
            ) + 1
            controller.apply(
                AfternoonEvent.SUBMIT_REJECTED,
                details={"order_id": order_id},
                data_updates={
                    "rejected_submission_count": rejected,
                    "current_intent": None,
                },
            )
            return None
        order_id = recovered.order_id
    controller.apply(
        AfternoonEvent.SUBMIT_ACCEPTED,
        details={"order_id": order_id},
        data_updates={
            "current_order_id": order_id,
            "submitted_at": datetime.now().astimezone().isoformat(),
        },
    )
    return query_order_strict(trader, account, order_id)


def _recover_unknown_submission(
    *,
    trader: object,
    account: object,
    controller: AfternoonController,
    remark: str,
) -> OrderView | None:
    try:
        orders = query_all_orders_strict(trader, account, attempts=5)
    except BrokerQueryAmbiguous as exc:
        controller.halt(
            event=AfternoonEvent.RECOVERY_AMBIGUOUS,
            reason="submission outcome and broker order list are ambiguous",
            error=exc,
        )
        return None
    order = find_unique_order_by_remark(orders, remark)
    if order is None:
        controller.halt(
            event=AfternoonEvent.RECOVERED_NO_MATCH,
            reason=(
                "submission raised after durable intent; automatic retry "
                "is forbidden even though no matching order was found"
            ),
        )
        return None
    if order.classification is OrderClass.ACTIVE:
        event = AfternoonEvent.RECOVERED_ACTIVE
    elif order.classification is OrderClass.CANCEL_PENDING:
        event = AfternoonEvent.RECOVERED_CANCEL_PENDING
    elif order.classification in {
        OrderClass.FILLED,
        OrderClass.TERMINAL_PARTIAL,
        OrderClass.CANCELED_ZERO,
        OrderClass.REJECTED,
    }:
        event = AfternoonEvent.RECOVERED_TERMINAL
    else:
        controller.halt(
            event=AfternoonEvent.RECOVERY_AMBIGUOUS,
            reason=f"matching order has unknown status {order.status}",
        )
        return None
    controller.apply(
        event,
        details={"order": order.safe_payload()},
        data_updates={
            "current_order_id": order.order_id,
            "current_order": order.safe_payload(),
        },
    )
    return order


def _finish_order_lifecycle(
    *,
    trader: object,
    account: object,
    controller: AfternoonController,
    order: OrderView,
    update_signal: BrokerUpdateSignal,
) -> bool:
    if controller.snapshot.state is AfternoonState.RECONCILE:
        return True
    if controller.snapshot.state is AfternoonState.CANCEL_PENDING:
        return _wait_cancel_terminal(
            trader=trader,
            account=account,
            controller=controller,
            order_id=order.order_id,
            update_signal=update_signal,
        )
    deadline = time.monotonic() + ORDER_OBSERVE_SECONDS
    latest = order
    last_signature: tuple[int, int] | None = None
    while time.monotonic() <= deadline:
        latest = query_order_strict(
            trader,
            account,
            latest.order_id,
        )
        classification = latest.classification
        signature = (latest.status, latest.traded_volume)
        if classification in {
            OrderClass.FILLED,
            OrderClass.TERMINAL_PARTIAL,
            OrderClass.CANCELED_ZERO,
            OrderClass.REJECTED,
        }:
            controller.apply(
                AfternoonEvent.ORDER_TERMINAL,
                details={"order": latest.safe_payload()},
                data_updates={"current_order": latest.safe_payload()},
            )
            return True
        if classification is OrderClass.CANCEL_PENDING:
            controller.apply(
                AfternoonEvent.CANCEL_REQUESTED,
                details={
                    "reason": "broker reports cancellation pending",
                    "order": latest.safe_payload(),
                },
            )
            return _wait_cancel_terminal(
                trader=trader,
                account=account,
                controller=controller,
                order_id=latest.order_id,
                update_signal=update_signal,
            )
        if classification is OrderClass.UNKNOWN:
            controller.halt(
                event=AfternoonEvent.ORDER_STATUS_UNKNOWN,
                reason=f"unknown order status {latest.status}",
            )
            return False
        if signature != last_signature:
            controller.apply(
                AfternoonEvent.ORDER_STILL_ACTIVE,
                details={"order": latest.safe_payload()},
                data_updates={"current_order": latest.safe_payload()},
            )
            last_signature = signature
        update_signal.wait(ORDER_STATUS_RECONCILE_SECONDS)

    controller.apply(
        AfternoonEvent.CANCEL_REQUESTED,
        details={
            "reason": "fill observation deadline reached",
            "order": latest.safe_payload(),
        },
    )
    try:
        cancel_result = int(
            trader.cancel_order_stock(account, latest.order_id)
        )
    except Exception as exc:
        controller.halt(
            event=AfternoonEvent.CANCEL_REJECTED,
            reason="cancel request raised an exception",
            error=exc,
        )
        return False
    controller.journal.update_data(cancel_result=cancel_result)
    if cancel_result != 0:
        try:
            latest = query_order_strict(
                trader,
                account,
                latest.order_id,
            )
        except BrokerQueryAmbiguous as exc:
            controller.halt(
                event=AfternoonEvent.ORDER_QUERY_AMBIGUOUS,
                reason="cancel failed and order cannot be queried",
                error=exc,
            )
            return False
        if latest.classification in {
            OrderClass.FILLED,
            OrderClass.TERMINAL_PARTIAL,
            OrderClass.CANCELED_ZERO,
            OrderClass.REJECTED,
        }:
            controller.apply(
                AfternoonEvent.CANCEL_TERMINAL,
                details={"order": latest.safe_payload()},
            )
            return True
        controller.halt(
            event=AfternoonEvent.CANCEL_REJECTED,
            reason=f"cancel request rejected: {cancel_result}",
        )
        return False
    return _wait_cancel_terminal(
        trader=trader,
        account=account,
        controller=controller,
        order_id=latest.order_id,
        update_signal=update_signal,
    )


def _wait_cancel_terminal(
    *,
    trader: object,
    account: object,
    controller: AfternoonController,
    order_id: int,
    update_signal: BrokerUpdateSignal,
) -> bool:
    deadline = time.monotonic() + CANCEL_CONFIRM_SECONDS
    last_signature: tuple[int, int] | None = None
    while time.monotonic() <= deadline:
        order = query_order_strict(trader, account, order_id)
        classification = order.classification
        if classification in {
            OrderClass.FILLED,
            OrderClass.TERMINAL_PARTIAL,
            OrderClass.CANCELED_ZERO,
            OrderClass.REJECTED,
        }:
            controller.apply(
                AfternoonEvent.CANCEL_TERMINAL,
                details={"order": order.safe_payload()},
                data_updates={"current_order": order.safe_payload()},
            )
            return True
        if classification not in {
            OrderClass.ACTIVE,
            OrderClass.CANCEL_PENDING,
        }:
            controller.halt(
                event=AfternoonEvent.ORDER_STATUS_UNKNOWN,
                reason=f"unknown cancel status {order.status}",
            )
            return False
        signature = (order.status, order.traded_volume)
        if signature != last_signature:
            controller.apply(
                AfternoonEvent.CANCEL_STILL_PENDING,
                details={"order": order.safe_payload()},
                data_updates={"current_order": order.safe_payload()},
            )
            last_signature = signature
        update_signal.wait(CANCEL_STATUS_RECONCILE_SECONDS)
    controller.halt(
        event=AfternoonEvent.CANCEL_TIMEOUT,
        reason="cancel did not reach a terminal broker state",
    )
    return False


def _reconcile_filled_cash(
    *,
    trader: object,
    account: object,
    controller: AfternoonController,
    remark_prefix: str,
) -> None:
    if controller.snapshot.state is not AfternoonState.RECONCILE:
        raise ExecutionSafetyError(
            "cash reconciliation requires a terminal order state"
        )
    orders = query_all_orders_strict(trader, account)
    own = orders_with_prefix(orders, remark_prefix=remark_prefix)
    if any(
        order.classification
        in {
            OrderClass.ACTIVE,
            OrderClass.CANCEL_PENDING,
            OrderClass.UNKNOWN,
        }
        for order in own
    ):
        raise UnresolvedOrderError(
            "cannot reconcile cash while an owned order is unresolved"
        )
    data = dict(controller.journal.payload.get("data") or {})
    accounted = int(
        data.get("accounted_filled_principal_yuan", 0)
    )
    broker_filled = sum(order.principal_yuan for order in own)
    cash_cap = _optional_float(data.get("cash_cap_yuan"))
    intent = data.get("current_intent")
    try:
        ledger = reconcile_broker_fills(
            previously_accounted_principal_yuan=accounted,
            broker_filled_principal_yuan=broker_filled,
            cash_cap_yuan=cash_cap,
            intent_available_cash_yuan=(
                float(intent["available_cash_yuan"])
                if isinstance(intent, dict)
                else None
            ),
        )
    except ExecutionSafetyError as exc:
        controller.halt(
            event=AfternoonEvent.RECONCILE_FAILED,
            reason=str(exc),
        )
        return
    delta = ledger.newly_filled_principal_yuan
    cash_cap = ledger.cash_cap_yuan
    current_order = data.get("current_order")
    zero_fill_count = int(data.get("zero_fill_terminal_count", 0))
    if delta > 0:
        zero_fill_count = 0
    else:
        zero_fill_count += 1
    controller.apply(
        AfternoonEvent.RECONCILED,
        details={
            "broker_filled_principal_yuan": broker_filled,
            "newly_reconciled_principal_yuan": delta,
            "cash_cap_yuan": cash_cap,
        },
        data_updates={
            "accounted_filled_principal_yuan": broker_filled,
            "cash_cap_yuan": cash_cap,
            "current_intent": None,
            "last_terminal_order": current_order,
            "current_order": None,
            "current_order_id": None,
            "zero_fill_terminal_count": zero_fill_count,
        },
    )
    if zero_fill_count >= MAXIMUM_ZERO_FILL_TERMINALS:
        controller.halt(
            event=AfternoonEvent.FAULT,
            reason="maximum consecutive zero-fill terminal orders reached",
        )
    elif delta == 0:
        time.sleep(SUBMISSION_BACKOFF_SECONDS)


def _finish_at_hard_stop(
    *,
    trader: object,
    account: object,
    controller: AfternoonController,
    maximum_principal_yuan: int,
    cash_usage_ratio: float,
) -> int:
    if controller.snapshot.state in {
        AfternoonState.WAIT_FUNDS,
        AfternoonState.WAIT_BOOK,
        AfternoonState.BACKOFF,
    }:
        event_to_scan = (
            AfternoonEvent.RETRY_SUBMIT
            if controller.snapshot.state is AfternoonState.BACKOFF
            else AfternoonEvent.RETRY_SCAN
        )
        controller.apply(event_to_scan)
    if controller.snapshot.state is not AfternoonState.SCAN:
        return controller.halt(
            event=_fault_event(controller.snapshot.state),
            reason="hard stop reached outside a safe scan state",
        )
    orders = query_all_orders_strict(trader, account)
    if unresolved_repo_orders(orders):
        return controller.halt(
            event=AfternoonEvent.HARD_STOP_RESIDUAL,
            reason="hard stop reached with an unresolved repo order",
        )
    cash = query_asset_strict(
        trader,
        account,
    ).conservative_available_cash
    data = dict(controller.journal.payload.get("data") or {})
    effective, cap = reconcile_cash_cap(
        cash,
        _optional_float(data.get("cash_cap_yuan")),
    )
    _, remaining_target, _ = _remaining_ratio_budget(
        data=data,
        effective_cash=effective,
        cash_usage_ratio=cash_usage_ratio,
        maximum_principal_yuan=maximum_principal_yuan,
    )
    principal = min(
        floor_principal_after_commission(effective),
        remaining_target,
    )
    if principal >= PRINCIPAL_STEP_YUAN:
        return controller.halt(
            event=AfternoonEvent.HARD_STOP_RESIDUAL,
            reason=(
                f"hard stop reached with CNY {remaining_target} "
                "of the configured target still unlent"
            ),
        )
    controller.apply(
        AfternoonEvent.HARD_STOP_CLEAR,
        details={"residual_cash_yuan": effective},
        data_updates={
            "success": True,
            "cash_cap_yuan": cap,
            "residual_cash_yuan": effective,
            "remaining_target_principal_yuan": remaining_target,
            "finished_at": datetime.now().astimezone().isoformat(),
        },
    )
    return 0


def _query_failure_event(state: AfternoonState) -> AfternoonEvent:
    if state is AfternoonState.INTENT:
        return AfternoonEvent.SUBMIT_EXCEPTION
    if state in {
        AfternoonState.ORDER_ACTIVE,
        AfternoonState.CANCEL_PENDING,
    }:
        return AfternoonEvent.ORDER_QUERY_AMBIGUOUS
    if state in {
        AfternoonState.RECOVERY,
        AfternoonState.SUBMIT_UNKNOWN,
    }:
        return AfternoonEvent.RECOVERY_AMBIGUOUS
    if state is AfternoonState.RECONCILE:
        return AfternoonEvent.RECONCILE_FAILED
    return AfternoonEvent.FAULT


def _fault_event(state: AfternoonState) -> AfternoonEvent:
    if state is AfternoonState.INTENT:
        return AfternoonEvent.SUBMIT_EXCEPTION
    if state in {
        AfternoonState.ORDER_ACTIVE,
        AfternoonState.CANCEL_PENDING,
    }:
        return AfternoonEvent.FAULT
    if state is AfternoonState.RECONCILE:
        return AfternoonEvent.RECONCILE_FAILED
    if state is AfternoonState.SUBMIT_UNKNOWN:
        return AfternoonEvent.RECOVERY_AMBIGUOUS
    return AfternoonEvent.FAULT


def _terminal_exit_code(state: AfternoonState) -> int:
    if state in {
        AfternoonState.COMPLETE,
        AfternoonState.SKIPPED,
    }:
        return 0
    return 1


def _remaining_ratio_budget(
    *,
    data: Mapping[str, object],
    effective_cash: float,
    cash_usage_ratio: float,
    maximum_principal_yuan: int,
) -> tuple[int | None, int, dict[str, object]]:
    ratio = float(cash_usage_ratio)
    if not math.isfinite(ratio) or not 0 < ratio <= 1:
        raise ExecutionSafetyError(
            "active second execution requires a cash usage ratio above 0 "
            "and at most 1"
        )
    maximum = int(maximum_principal_yuan)
    if maximum < 0 or (
        maximum and maximum % PRINCIPAL_STEP_YUAN
    ):
        raise ExecutionSafetyError(
            "maximum principal must be zero or a CNY 1,000 multiple"
        )
    accounted = int(data.get("accounted_filled_principal_yuan", 0))
    if accounted < 0:
        raise ExecutionSafetyError(
            "accounted filled principal cannot be negative"
        )

    target_value = data.get("target_principal_yuan")
    updates: dict[str, object] = {}
    if target_value is None:
        if accounted:
            raise ExecutionSafetyError(
                "filled principal exists before the second target was frozen"
            )
        target = floor_principal_after_commission(effective_cash, ratio)
        if maximum:
            target = min(target, maximum)
        if target < PRINCIPAL_STEP_YUAN:
            return None, 0, updates
        updates = {
            "initial_available_cash_yuan": float(effective_cash),
            "target_principal_yuan": target,
        }
    else:
        target = int(target_value)
        initial_cash = _optional_float(
            data.get("initial_available_cash_yuan")
        )
        if (
            target < PRINCIPAL_STEP_YUAN
            or target % PRINCIPAL_STEP_YUAN
            or initial_cash is None
            or not math.isfinite(initial_cash)
            or initial_cash < 0
        ):
            raise ExecutionSafetyError(
                "durable second target budget is invalid"
            )
        ceiling = floor_principal_after_commission(initial_cash, ratio)
        if maximum:
            ceiling = min(ceiling, maximum)
        if target > ceiling:
            raise ExecutionSafetyError(
                "durable second target exceeds its initial ratio budget"
            )
    if accounted > target:
        raise ExecutionSafetyError(
            "broker cumulative fills exceed the durable second target"
        )
    remaining = target - accounted
    remaining -= remaining % PRINCIPAL_STEP_YUAN
    return target, remaining, updates


def _durable_afternoon_remaining(
    data: Mapping[str, object],
) -> int | None:
    """Return the durable remaining target (CNY 1,000 steps), or None when
    the target has not been frozen yet or the ledger is malformed."""
    try:
        target = int(data.get("target_principal_yuan", -1))
        accounted = int(data.get("accounted_filled_principal_yuan", 0))
    except (TypeError, ValueError):
        return None
    if target < 0 or accounted < 0:
        return None
    remaining = target - accounted
    return max(remaining - (remaining % PRINCIPAL_STEP_YUAN), 0)


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    return float(value)


def _at(
    trade_date: date,
    value: clock_time,
    now: datetime,
) -> datetime:
    return datetime.combine(trade_date, value, tzinfo=now.tzinfo)


def _market_break_resume_at(
    now: datetime,
    trade_date: date,
) -> datetime | None:
    if now.utcoffset() is None:
        raise ValueError("market-break time must include a timezone")
    if MORNING_SESSION_END <= now.time() < AFTERNOON_SESSION_START:
        return _at(trade_date, AFTERNOON_SESSION_START, now)
    return None


def _wait_until(target: datetime) -> None:
    while True:
        remaining = (
            target - datetime.now().astimezone()
        ).total_seconds()
        if remaining <= 0:
            return
        time.sleep(min(1.0, max(0.01, remaining)))


if __name__ == "__main__":
    raise SystemExit(main())
