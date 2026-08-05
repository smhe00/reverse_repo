from __future__ import annotations

import argparse
import math
import random
import re
import sys
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta
from datetime import time as clock_time
from pathlib import Path
from typing import Any

from repo_execution_core import (
    GC001,
    PRINCIPAL_STEP_YUAN,
    AccountBinding,
    AtomicJournal,
    BrokerUpdateSignal,
    BookPlan,
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
    first_execution_deadline,
    is_first_execution_time,
    is_exchange_trading_day,
    journal_matches_verification,
    orders_with_prefix,
    principal_to_qmt_volume,
    qmt_volume_to_principal,
    query_all_orders_strict,
    query_asset_strict,
    query_order_strict,
    qmt_strategy_name,
    read_quote_books,
    safe_exception,
    select_bound_account,
    unresolved_repo_orders,
)
from repo_execution_state_machine import (
    MORNING_TERMINAL_STATES,
    MachineSnapshot,
    MorningEvent,
    MorningState,
    advance_morning,
    initial_morning_snapshot,
    morning_snapshot_from_payload,
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

TARGET_TIME = clock_time(9, 30, 42)
MORNING_HARD_STOP_TIME = clock_time(9, 35, 0)
CONNECT_LEAD_SECONDS = 60
MAXIMUM_QUOTE_AGE_SECONDS = 4.5
CASH_USAGE_RATIO = 0.90
ORDER_REPRICE_CHECK_SECONDS = 5.0
# 09:30:00 through 09:35:00 permits at most 60 five-second slots.
# The extra slot is a fail-closed sanity ceiling, not a strategy stop.
MAXIMUM_ORDER_ATTEMPTS = 61
CANCEL_CONFIRM_SECONDS = 15.0
ORDER_STATUS_RECONCILE_SECONDS = 1.0
CANCEL_STATUS_RECONCILE_SECONDS = 0.5
REMARK_PREFIX = "repo_morning_v2"
STRATEGY_NAME = qmt_strategy_name("repo_morning_v2")


def _parse_first_execution_time(value: object) -> clock_time:
    text = str(value)
    if re.fullmatch(r"\d{2}:\d{2}:\d{2}", text) is None:
        raise argparse.ArgumentTypeError(
            "execution time must use HH:MM:SS"
        )
    try:
        parsed = clock_time.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "execution time must use HH:MM:SS"
        ) from exc
    if parsed.tzinfo is not None:
        raise argparse.ArgumentTypeError(
            "execution time must not include a timezone"
        )
    if not is_first_execution_time(parsed):
        raise argparse.ArgumentTypeError(
            "first execution time must be from 09:30:00 through "
            "11:28:00 or from 13:00:00 through 15:28:00"
        )
    return parsed


_parse_morning_execution_time = _parse_first_execution_time


def _parse_cash_usage_ratio(value: object) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            "cash usage ratio must be a number"
        ) from exc
    if not math.isfinite(parsed) or parsed < 0 or parsed > 1:
        raise argparse.ArgumentTypeError(
            "cash usage ratio must be from 0 through 1"
        )
    return parsed


def _parse_remark_root(value: object) -> str:
    text = str(value).strip()
    if re.fullmatch(r"[a-z0-9_]{3,15}", text) is None:
        raise argparse.ArgumentTypeError(
            "remark root must use 3-15 lowercase ASCII letters, digits, or underscore"
        )
    return text


@dataclass(frozen=True)
class MorningLimitPlan:
    symbol: str
    order_volume: int
    immediately_executable_volume: int
    covers_requested_volume_immediately: bool
    principal_yuan: int
    limit_rate_percent: float
    quote_time: str
    quote_age_seconds: float


class MorningController:
    def __init__(
        self,
        journal: AtomicJournal,
        snapshot: MachineSnapshot[MorningState],
        notifier: FailureNotifier | None = None,
    ) -> None:
        self.journal = journal
        self.snapshot = snapshot
        self.notifier = notifier

    def apply(
        self,
        event: MorningEvent,
        *,
        details: Mapping[str, object] | None = None,
        data_updates: Mapping[str, object] | None = None,
    ) -> None:
        self.snapshot = advance_morning(self.snapshot, event)
        self.journal.transition(
            event=event.value,
            machine_payload=snapshot_to_payload(self.snapshot),
            details=details,
            data_updates=data_updates,
        )

    def halt(
        self,
        *,
        event: MorningEvent,
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
        if self.snapshot.state not in MORNING_TERMINAL_STATES:
            if self.snapshot.state is MorningState.SUBMIT_UNKNOWN:
                self.apply(
                    MorningEvent.RECOVERY_AMBIGUOUS,
                    details={
                        "reason": (
                            "safe halt escalated an unresolved submission "
                            "outcome"
                        )
                    },
                )
            else:
                raise ExecutionSafetyError(
                    "halt event did not reach a morning terminal state: "
                    f"{self.snapshot.state.value}"
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
            "Formally model-checked configurable first reverse-repo "
            "executor for GC001."
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
        type=_parse_first_execution_time,
        default=TARGET_TIME,
    )
    parser.add_argument(
        "--cash-usage-ratio",
        type=_parse_cash_usage_ratio,
        default=CASH_USAGE_RATIO,
    )
    parser.add_argument(
        "--remark-root",
        type=_parse_remark_root,
        default=REMARK_PREFIX,
        help="Simulation-only order namespace override for isolated diagnostics.",
    )
    parser.add_argument(
        "--remark-prefix",
        default="",
        help=(
            "Explicit full order-remark namespace. Overrides the default "
            "root+trade-date prefix; used by the live canary to isolate "
            "each certification attempt from earlier same-day attempts."
        ),
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
    parser.add_argument(
        "--live-channel-certification",
        action="store_true",
        help=(
            "Run the production lifecycle as the fixed CNY 1,000 live "
            "channel certification canary."
        ),
    )
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()

    verification = verify_state_machines()
    if args.validate_only:
        print(verification)
        return 0
    if args.cash_usage_ratio == 0:
        print("First reverse-repo execution skipped: cash usage ratio is 0.")
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
        return _run_morning_command(args, verification, notifier)
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


def _run_morning_command(
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
    if args.live_channel_certification:
        if args.environment != "live":
            raise ValueError("live channel certification requires live")
        if args.remark_root != "repo_live_cert":
            raise ValueError(
                "live channel certification requires repo_live_cert remarks"
            )
        certification_prefix = (
            args.remark_prefix
            or f"{args.remark_root}_{trade_date:%Y%m%d}_"
        )
        if (
            re.fullmatch(
                r"repo_live_cert_[0-9]{8}_[0-9]{6}_",
                certification_prefix,
            )
            is None
        ):
            raise ValueError(
                "live channel certification requires a unique "
                "per-attempt remark prefix matching "
                "repo_live_cert_YYYYMMDD_HHMMSS_"
            )
        if int(args.maximum_principal_yuan) != 1000:
            raise ValueError(
                "live channel certification is hard-capped at CNY 1,000"
            )
        if float(args.cash_usage_ratio) != 1.0:
            raise ValueError(
                "live channel certification requires cash usage ratio 1"
            )
    elif (
        args.environment != "simulation"
        and (
            args.remark_root != REMARK_PREFIX
            or args.remark_prefix
        )
    ):
        raise ValueError("custom remark root is restricted to simulation")
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
    target_at = datetime.combine(
        trade_date,
        args.execution_time,
        tzinfo=now.tzinfo,
    )
    quote_deadline = first_execution_deadline(
        trade_date,
        args.execution_time,
        timezone=now.tzinfo,
    )
    remark_prefix = (
        args.remark_prefix
        or f"{args.remark_root}_{trade_date:%Y%m%d}_"
    )
    journal = AtomicJournal(
        Path(args.journal),
        strategy=STRATEGY_NAME,
        trade_date=trade_date,
    )
    with ExecutionMutex(Path(args.mutex)):
        result = run_morning(
            qmt_path=qmt_path,
            account_binding=Path(args.account_binding),
            environment=args.environment,
            trade_date=trade_date,
            target_at=target_at,
            quote_deadline=quote_deadline,
            remark_prefix=remark_prefix,
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


def run_morning(
    *,
    qmt_path: Path,
    account_binding: Path,
    environment: str,
    trade_date: date,
    target_at: datetime,
    quote_deadline: datetime,
    remark_prefix: str,
    maximum_principal_yuan: int,
    cash_usage_ratio: float,
    journal: AtomicJournal,
    formal_verification: Mapping[str, object],
    notifier: FailureNotifier | None = None,
) -> int:
    payload, existed = journal.load_or_initialize(
        machine_payload=snapshot_to_payload(initial_morning_snapshot()),
        initial_data={
            "environment": environment,
            "symbol": GC001,
            "side": "SELL",
            "target_at": target_at.isoformat(),
            "quote_deadline": quote_deadline.isoformat(),
            "cash_usage_ratio": cash_usage_ratio,
            "remark_prefix": remark_prefix,
            "maximum_principal_yuan": maximum_principal_yuan,
            "live_channel_certification": (
                environment == "live"
                and remark_prefix.startswith("repo_live_cert_")
                and maximum_principal_yuan == 1000
                and cash_usage_ratio == 1.0
            ),
            "maximum_order_attempts": MAXIMUM_ORDER_ATTEMPTS,
            "order_reprice_check_seconds": (
                ORDER_REPRICE_CHECK_SECONDS
            ),
            "attempt_counter": 0,
            "accounted_filled_principal_yuan": 0,
            "success": False,
            "account_id_persisted": False,
            "formal_verification": dict(formal_verification),
        },
    )
    if existed:
        data = dict(payload.get("data") or {})
        expected = {
            "target_at": target_at.isoformat(),
            "quote_deadline": quote_deadline.isoformat(),
        }
        for name, value in expected.items():
            if data.get(name) != value:
                raise ExecutionSafetyError(
                    f"existing first journal has different {name}"
                )
        if float(data.get("cash_usage_ratio", -1.0)) != cash_usage_ratio:
            raise ExecutionSafetyError(
                "existing first journal has a different cash usage ratio"
            )
    controller = MorningController(
        journal,
        morning_snapshot_from_payload(payload["machine"]),
        notifier,
    )
    if controller.snapshot.state in MORNING_TERMINAL_STATES:
        if controller.snapshot.state is MorningState.HALTED:
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
    if existed and controller.snapshot.state is not MorningState.NEW:
        controller.apply(
            MorningEvent.RESTART,
            details={"reason": "existing nonterminal journal recovered"},
        )
    if controller.snapshot.state is MorningState.NEW:
        controller.apply(MorningEvent.BEGIN)

    from xtquant import xtconstant, xtdata, xttype
    from xtquant.xttrader import XtQuantTrader, XtQuantTraderCallback

    xtdata.enable_hello = False
    trader: Any = None
    quote_sequence = 0
    account: Any = None
    binding: AccountBinding | None = None
    update_signal = BrokerUpdateSignal(
        strategy_name=STRATEGY_NAME,
        remark_prefix=remark_prefix,
    )

    class MorningPushCallback(XtQuantTraderCallback):
        def on_stock_order(self, order: object) -> None:
            update_signal.on_order(order)

        def on_stock_trade(self, trade: object) -> None:
            update_signal.on_trade(trade)

    try:
        if not is_exchange_trading_day(xtdata, trade_date):
            if controller.snapshot.state is MorningState.PREFLIGHT:
                controller.apply(
                    MorningEvent.NON_TRADING_DAY,
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
                event=MorningEvent.RECOVERY_AMBIGUOUS,
                reason=(
                    "a nonterminal journal exists on a date the exchange "
                    "calendar now reports as non-trading"
                ),
            )
        if datetime.now().astimezone() >= quote_deadline:
            return controller.halt(
                event=MorningEvent.FAULT,
                reason="started after the morning quote deadline",
            )
        connect_at = target_at - timedelta(
            seconds=CONNECT_LEAD_SECONDS
        )
        _wait_until(connect_at)
        trader = XtQuantTrader(
            str(qmt_path),
            random.randint(100_000_000, 999_999_999),
            MorningPushCallback(),
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
        if controller.snapshot.state is MorningState.PREFLIGHT:
            controller.apply(
                MorningEvent.PREFLIGHT_OK,
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

        quote_sequence = int(
            xtdata.subscribe_quote(GC001, period="tick", count=0) or 0
        )
        if quote_sequence <= 0:
            raise ExecutionSafetyError(
                f"GC001 quote subscription failed: {quote_sequence}"
            )

        recovered = _recover_morning_order(
            trader=trader,
            account=account,
            controller=controller,
            remark_prefix=remark_prefix,
            sell_order_type=int(xtconstant.STOCK_SELL),
        )
        if controller.snapshot.state in MORNING_TERMINAL_STATES:
            return _terminal_exit_code(controller.snapshot.state)
        while True:
            if recovered is None:
                if controller.snapshot.state is MorningState.WAIT_TRIGGER:
                    _wait_until(target_at)
                    controller.apply(MorningEvent.TRIGGER)
                if not _wait_for_retry_slot(
                    controller=controller,
                    execution_deadline=quote_deadline,
                ):
                    return controller.halt(
                        event=MorningEvent.DEADLINE,
                        reason=(
                            "next five-second retry slot is at or after "
                            "the configured first execution deadline"
                        ),
                    )
                plan, available_cash = _wait_for_submission_snapshot(
                    trader=trader,
                    account=account,
                    xtdata=xtdata,
                    controller=controller,
                    target_at=target_at,
                    quote_deadline=quote_deadline,
                    maximum_principal_yuan=maximum_principal_yuan,
                    cash_usage_ratio=cash_usage_ratio,
                    remark_prefix=remark_prefix,
                    sell_order_type=int(xtconstant.STOCK_SELL),
                )
                if plan is None:
                    return _terminal_exit_code(
                        controller.snapshot.state
                    )
                data = dict(journal.payload.get("data") or {})
                attempt_number = int(data.get("attempt_counter", 0)) + 1
                if attempt_number > MAXIMUM_ORDER_ATTEMPTS:
                    return controller.halt(
                        event=MorningEvent.FAULT,
                        reason="maximum morning order attempts reached",
                    )
                remark = f"{remark_prefix}{attempt_number:04d}"
                attempt_at = datetime.now().astimezone()
                intent = {
                    "attempt_number": attempt_number,
                    "remark": remark,
                    "symbol": GC001,
                    "side": "SELL",
                    "available_cash_yuan": available_cash,
                    "principal_yuan": plan.principal_yuan,
                    "qmt_volume": plan.order_volume,
                    "immediately_executable_volume": (
                        plan.immediately_executable_volume
                    ),
                    "covers_requested_volume_immediately": (
                        plan.covers_requested_volume_immediately
                    ),
                    "limit_rate_percent": plan.limit_rate_percent,
                    "quote_time": plan.quote_time,
                    "quote_age_seconds": plan.quote_age_seconds,
                    "submission_attempt_at": attempt_at.isoformat(),
                    "persisted_before_submission": True,
                }
                assert_order_budget(
                    principal_yuan=plan.principal_yuan,
                    verified_available_cash_yuan=available_cash,
                    maximum_ratio=1.0,
                )
                controller.apply(
                    MorningEvent.INTENT_PERSISTED,
                    details=intent,
                    data_updates={
                        "attempt_counter": attempt_number,
                        "current_intent": intent,
                        "last_submission_attempt_at": (
                            attempt_at.isoformat()
                        ),
                    },
                )
                order = _submit_or_recover(
                    trader=trader,
                    account=account,
                    xtconstant=xtconstant,
                    controller=controller,
                    intent=intent,
                    remark=remark,
                )
                if order is None:
                    return _terminal_exit_code(
                        controller.snapshot.state
                    )
            else:
                order = recovered

            terminal_order = _finish_order_lifecycle(
                trader=trader,
                account=account,
                controller=controller,
                order=order,
                xtdata=xtdata,
                update_signal=update_signal,
                execution_deadline=quote_deadline,
            )
            if terminal_order is None:
                return _terminal_exit_code(controller.snapshot.state)
            should_retry = _reconcile_terminal(
                trader=trader,
                account=account,
                controller=controller,
                order=terminal_order,
                remark_prefix=remark_prefix,
                execution_deadline=quote_deadline,
                sell_order_type=int(xtconstant.STOCK_SELL),
            )
            if not should_retry:
                return _terminal_exit_code(controller.snapshot.state)
            recovered = None
    except BrokerQueryAmbiguous as exc:
        if controller.snapshot.state in MORNING_TERMINAL_STATES:
            return _terminal_exit_code(controller.snapshot.state)
        event = _query_failure_event(controller.snapshot.state)
        return controller.halt(
            event=event,
            reason="broker query remained ambiguous",
            error=exc,
        )
    except Exception as exc:  # noqa: BLE001
        if controller.snapshot.state in MORNING_TERMINAL_STATES:
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
        event = _fault_event(controller.snapshot.state)
        return controller.halt(
            event=event,
            reason="unhandled fail-closed execution error",
            error=exc,
        )
    finally:
        if quote_sequence:
            try:
                xtdata.unsubscribe_quote(quote_sequence)
            except Exception:  # noqa: BLE001,S110
                pass
        if trader is not None:
            trader.stop()


def _recover_morning_order(
    *,
    trader: object,
    account: object,
    controller: MorningController,
    remark_prefix: str,
    sell_order_type: int,
) -> OrderView | None:
    orders = query_all_orders_strict(trader, account)
    unresolved = unresolved_repo_orders(orders)
    owned = orders_with_prefix(
        orders,
        remark_prefix=remark_prefix,
    )
    identity_error = _owned_order_identity_error(
        owned,
        sell_order_type=sell_order_type,
    )
    if identity_error:
        controller.halt(
            event=MorningEvent.RECOVERY_AMBIGUOUS,
            reason=identity_error,
        )
        return None
    duplicate_remarks = _duplicate_order_remarks(owned)
    if duplicate_remarks:
        controller.halt(
            event=MorningEvent.RECOVERY_AMBIGUOUS,
            reason=(
                "multiple broker orders share an owned remark: "
                + ", ".join(duplicate_remarks)
            ),
        )
        return None
    foreign_unresolved = [
        order
        for order in unresolved
        if not order.remark.startswith(remark_prefix)
    ]
    if foreign_unresolved:
        controller.halt(
            event=MorningEvent.RECOVERY_AMBIGUOUS,
            reason="another unresolved reverse-repo order exists",
        )
        return None
    own_unresolved = [
        order
        for order in unresolved
        if order.remark.startswith(remark_prefix)
    ]
    if len(own_unresolved) > 1:
        controller.halt(
            event=MorningEvent.RECOVERY_AMBIGUOUS,
            reason="multiple owned reverse-repo orders are unresolved",
        )
        return None
    data = dict(controller.journal.payload.get("data") or {})
    current_intent = data.get("current_intent")
    intent_exists = bool(current_intent) or (
        controller.snapshot.facts.intent_persisted
    )
    cumulative_filled = _cumulative_owned_principal(owned)
    try:
        previously_accounted = int(
            data.get("accounted_filled_principal_yuan", 0)
        )
    except (TypeError, ValueError):
        previously_accounted = -1
    if previously_accounted < 0 or cumulative_filled < previously_accounted:
        controller.halt(
            event=MorningEvent.RECOVERY_AMBIGUOUS,
            reason="broker cumulative fills moved backwards during recovery",
        )
        return None
    target_value = data.get("target_principal_yuan")
    if target_value is not None:
        try:
            recovered_target = int(target_value)
        except (TypeError, ValueError):
            recovered_target = -1
        if recovered_target < 0 or cumulative_filled > recovered_target:
            controller.halt(
                event=MorningEvent.RECOVERY_AMBIGUOUS,
                reason="broker cumulative fills exceed the durable target",
            )
            return None
    if not intent_exists:
        if own_unresolved:
            controller.halt(
                event=MorningEvent.RECOVERY_AMBIGUOUS,
                reason="owned unresolved order has no durable intent",
            )
            return None
        if owned and not data.get("target_principal_yuan"):
            controller.halt(
                event=MorningEvent.RECOVERY_AMBIGUOUS,
                reason=(
                    "owned terminal orders exist but the durable target "
                    "principal is missing"
                ),
            )
            return None
        controller.apply(
            MorningEvent.RECOVERY_CLEAR,
            data_updates={
                "accounted_filled_principal_yuan": cumulative_filled,
                "filled_principal_yuan": cumulative_filled,
            },
        )
        return None

    if not isinstance(current_intent, Mapping):
        controller.halt(
            event=MorningEvent.RECOVERY_AMBIGUOUS,
            reason="state facts report an intent but its payload is missing",
        )
        return None
    intent_remark = str(current_intent.get("remark", ""))
    if not intent_remark.startswith(remark_prefix):
        controller.halt(
            event=MorningEvent.RECOVERY_AMBIGUOUS,
            reason="durable intent has an unexpected order remark",
        )
        return None
    matches = [order for order in owned if order.remark == intent_remark]
    if len(matches) != 1:
        controller.halt(
            event=MorningEvent.RECOVERY_AMBIGUOUS,
            reason=(
                "durable intent does not have exactly one matching "
                "broker order"
            ),
        )
        return None
    own = matches[0]
    if own_unresolved and own_unresolved[0].order_id != own.order_id:
        controller.halt(
            event=MorningEvent.RECOVERY_AMBIGUOUS,
            reason="an unresolved owned order differs from durable intent",
        )
        return None
    classification = own.classification
    details = {"order": own.safe_payload()}
    controller.journal.update_data(
        current_order=own.safe_payload(),
        current_order_id=own.order_id,
        accounted_filled_principal_yuan=cumulative_filled,
        filled_principal_yuan=cumulative_filled,
    )
    if classification is OrderClass.ACTIVE:
        controller.apply(MorningEvent.RECOVERY_ACTIVE, details=details)
    elif classification is OrderClass.CANCEL_PENDING:
        controller.apply(
            MorningEvent.RECOVERY_CANCEL_PENDING,
            details=details,
        )
    elif classification in {
        OrderClass.FILLED,
        OrderClass.TERMINAL_PARTIAL,
        OrderClass.CANCELED_ZERO,
        OrderClass.REJECTED,
    }:
        controller.apply(MorningEvent.RECOVERY_TERMINAL, details=details)
    else:
        controller.halt(
            event=MorningEvent.RECOVERY_AMBIGUOUS,
            reason=f"recovered order has unknown status {own.status}",
        )
        return None
    return own


def _wait_for_submission_snapshot(
    *,
    trader: object,
    account: object,
    xtdata: object,
    controller: MorningController,
    target_at: datetime,
    quote_deadline: datetime,
    maximum_principal_yuan: int,
    cash_usage_ratio: float,
    remark_prefix: str,
    sell_order_type: int,
) -> tuple[MorningLimitPlan | None, float]:
    last_error = ""
    while datetime.now().astimezone() < quote_deadline:
        now = datetime.now().astimezone()
        try:
            orders = query_all_orders_strict(trader, account)
            if unresolved_repo_orders(orders):
                raise UnresolvedOrderError(
                    "a reverse-repo order appeared before submission"
                )
            owned = orders_with_prefix(
                orders,
                remark_prefix=remark_prefix,
            )
            identity_error = _owned_order_identity_error(
                owned,
                sell_order_type=sell_order_type,
            )
            if identity_error:
                raise UnresolvedOrderError(identity_error)
            duplicate_remarks = _duplicate_order_remarks(owned)
            if duplicate_remarks:
                raise UnresolvedOrderError(
                    "duplicate owned order remarks: "
                    + ", ".join(duplicate_remarks)
                )
            cumulative_filled = _cumulative_owned_principal(owned)
            reported_cash = query_asset_strict(
                trader,
                account,
            ).conservative_available_cash
            data = dict(controller.journal.payload.get("data") or {})
            try:
                previously_accounted = int(
                    data.get("accounted_filled_principal_yuan", 0)
                )
            except (TypeError, ValueError) as exc:
                raise UnresolvedOrderError(
                    "durable cumulative fill ledger is invalid"
                ) from exc
            if (
                previously_accounted < 0
                or cumulative_filled < previously_accounted
            ):
                raise UnresolvedOrderError(
                    "broker cumulative fills moved backwards"
                )
            initial_cash_value = data.get("initial_verified_cash_yuan")
            target_value = data.get("target_principal_yuan")
            if initial_cash_value is None and target_value is None:
                if owned or previously_accounted:
                    raise UnresolvedOrderError(
                        "owned history exists before the initial target was "
                        "durably frozen"
                    )
                initial_cash = reported_cash
                target_principal = floor_principal_after_commission(
                    initial_cash,
                    cash_usage_ratio,
                )
                if maximum_principal_yuan:
                    target_principal = min(
                        target_principal,
                        maximum_principal_yuan,
                    )
                    target_principal -= (
                        target_principal % PRINCIPAL_STEP_YUAN
                    )
                controller.journal.update_data(
                    initial_verified_cash_yuan=initial_cash,
                    target_principal_yuan=target_principal,
                )
            elif initial_cash_value is None or target_value is None:
                raise UnresolvedOrderError(
                    "initial cash and target principal must be persisted "
                    "together"
                )
            else:
                initial_cash = float(initial_cash_value)
                target_principal = int(target_value)
            maximum_initial_target = floor_principal_after_commission(
                initial_cash,
                cash_usage_ratio,
            )
            if maximum_principal_yuan:
                maximum_initial_target = min(
                    maximum_initial_target,
                    maximum_principal_yuan,
                )
                maximum_initial_target -= (
                    maximum_initial_target % PRINCIPAL_STEP_YUAN
                )
            if (
                target_principal < 0
                or target_principal % PRINCIPAL_STEP_YUAN
                or target_principal > maximum_initial_target
            ):
                raise UnresolvedOrderError(
                    "durable target principal violates the initial budget"
                )
            if cumulative_filled > target_principal:
                raise UnresolvedOrderError(
                    "broker cumulative fills exceed the durable target"
                )
            principal, effective_cash = _remaining_order_principal(
                initial_cash_yuan=initial_cash,
                target_principal_yuan=target_principal,
                cumulative_filled_principal_yuan=cumulative_filled,
                reported_cash_yuan=reported_cash,
            )
            if principal < PRINCIPAL_STEP_YUAN:
                controller.halt(
                    event=MorningEvent.NO_FUNDS,
                    reason=(
                        "verified remaining cash or target is below "
                        "CNY 1,000"
                    ),
                )
                return None, effective_cash
            books = read_quote_books(
                xtdata,
                [GC001],
                now=now,
                maximum_age_seconds=MAXIMUM_QUOTE_AGE_SECONDS,
                not_before_epoch_ms=int(target_at.timestamp() * 1000),
            )
            requested_volume = principal_to_qmt_volume(principal)
            _bid1_limit_plan(books[GC001], requested_volume)

            # The final account and quote reads occur immediately before
            # persisting the order intent. No earlier snapshot is reused.
            final_orders = query_all_orders_strict(trader, account)
            if unresolved_repo_orders(final_orders):
                raise UnresolvedOrderError(
                    "a reverse-repo order appeared during snapshot"
                )
            final_owned = orders_with_prefix(
                final_orders,
                remark_prefix=remark_prefix,
            )
            final_identity_error = _owned_order_identity_error(
                final_owned,
                sell_order_type=sell_order_type,
            )
            if final_identity_error:
                raise UnresolvedOrderError(final_identity_error)
            if _duplicate_order_remarks(final_owned):
                raise UnresolvedOrderError(
                    "owned order history became ambiguous during snapshot"
                )
            final_cumulative = _cumulative_owned_principal(final_owned)
            if final_cumulative != cumulative_filled:
                raise UnresolvedOrderError(
                    "broker cumulative fills changed during snapshot"
                )
            final_reported_cash = query_asset_strict(
                trader,
                account,
            ).conservative_available_cash
            final_principal, final_cash = _remaining_order_principal(
                initial_cash_yuan=initial_cash,
                target_principal_yuan=target_principal,
                cumulative_filled_principal_yuan=final_cumulative,
                reported_cash_yuan=final_reported_cash,
            )
            if final_principal < PRINCIPAL_STEP_YUAN:
                controller.halt(
                    event=MorningEvent.NO_FUNDS,
                    reason="cash fell below the minimum before intent",
                )
                return None, final_cash
            final_now = datetime.now().astimezone()
            final_books = read_quote_books(
                xtdata,
                [GC001],
                now=final_now,
                maximum_age_seconds=MAXIMUM_QUOTE_AGE_SECONDS,
                not_before_epoch_ms=int(target_at.timestamp() * 1000),
            )
            final_plan = _bid1_limit_plan(
                final_books[GC001],
                principal_to_qmt_volume(final_principal),
            )
            controller.apply(
                MorningEvent.SNAPSHOT_OK,
                details={
                    "cash": final_cash,
                    "plan": asdict(final_plan),
                },
                data_updates={
                    "accounted_filled_principal_yuan": final_cumulative,
                    "filled_principal_yuan": final_cumulative,
                    "remaining_target_principal_yuan": (
                        target_principal - final_cumulative
                    ),
                    "effective_cash_cap_yuan": final_cash,
                },
            )
            return final_plan, final_cash
        except QuoteValidationError as exc:
            last_error = safe_exception(exc)
            controller.apply(
                MorningEvent.SNAPSHOT_RETRY,
                details={"reason": last_error},
            )
            time.sleep(0.05)
    controller.halt(
        event=MorningEvent.DEADLINE,
        reason=f"no executable fresh snapshot: {last_error}",
    )
    return None, 0.0


def _submit_or_recover(
    *,
    trader: object,
    account: object,
    xtconstant: object,
    controller: MorningController,
    intent: Mapping[str, object],
    remark: str,
) -> OrderView | None:
    sell_order_type = int(xtconstant.STOCK_SELL)
    try:
        order_id = int(
            trader.order_stock(
                account,
                GC001,
                xtconstant.STOCK_SELL,
                int(intent["qmt_volume"]),
                xtconstant.FIX_PRICE,
                float(intent["limit_rate_percent"]),
                STRATEGY_NAME,
                remark,
            )
        )
    except Exception as exc:  # noqa: BLE001
        controller.apply(
            MorningEvent.SUBMIT_EXCEPTION,
            details={"error": safe_exception(exc)},
        )
        return _recover_unknown_submission(
            trader=trader,
            account=account,
            controller=controller,
            remark=remark,
            sell_order_type=sell_order_type,
        )
    if order_id <= 0:
        orders = query_all_orders_strict(trader, account)
        recovered = find_unique_order_by_remark(orders, remark)
        if recovered is None:
            controller.halt(
                event=MorningEvent.SUBMIT_REJECTED,
                reason=f"order submission rejected: {order_id}",
            )
            return None
        identity_error = _owned_order_identity_error(
            [recovered],
            sell_order_type=sell_order_type,
        )
        if identity_error:
            controller.apply(
                MorningEvent.SUBMIT_EXCEPTION,
                details={
                    "reason": "negative submission response is ambiguous",
                },
            )
            controller.halt(
                event=MorningEvent.RECOVERY_AMBIGUOUS,
                reason=identity_error,
            )
            return None
        order_id = recovered.order_id
    controller.apply(
        MorningEvent.SUBMIT_ACCEPTED,
        details={"order_id": order_id},
        data_updates={
            "current_order_id": order_id,
            "submitted_at": datetime.now().astimezone().isoformat(),
        },
    )
    accepted = query_order_strict(trader, account, order_id)
    identity_error = _owned_order_identity_error(
        [accepted],
        sell_order_type=sell_order_type,
    )
    if identity_error or accepted.remark != remark:
        controller.halt(
            event=MorningEvent.ORDER_STATUS_UNKNOWN,
            reason=(
                identity_error
                or "accepted order has an unexpected durable remark"
            ),
        )
        return None
    return accepted


def _recover_unknown_submission(
    *,
    trader: object,
    account: object,
    controller: MorningController,
    remark: str,
    sell_order_type: int,
) -> OrderView | None:
    try:
        orders = query_all_orders_strict(trader, account, attempts=5)
    except BrokerQueryAmbiguous as exc:
        controller.halt(
            event=MorningEvent.RECOVERY_AMBIGUOUS,
            reason="submission outcome and broker order list are ambiguous",
            error=exc,
        )
        return None
    order = find_unique_order_by_remark(orders, remark)
    if order is None:
        controller.halt(
            event=MorningEvent.RECOVERED_NO_MATCH,
            reason=(
                "submission raised after durable intent; automatic retry "
                "is forbidden even though no matching order was found"
            ),
        )
        return None
    identity_error = _owned_order_identity_error(
        [order],
        sell_order_type=sell_order_type,
    )
    if identity_error:
        controller.halt(
            event=MorningEvent.RECOVERY_AMBIGUOUS,
            reason=identity_error,
        )
        return None
    classification = order.classification
    if classification is OrderClass.ACTIVE:
        event = MorningEvent.RECOVERED_ACTIVE
    elif classification is OrderClass.CANCEL_PENDING:
        event = MorningEvent.RECOVERED_CANCEL_PENDING
    elif classification in {
        OrderClass.FILLED,
        OrderClass.TERMINAL_PARTIAL,
        OrderClass.CANCELED_ZERO,
        OrderClass.REJECTED,
    }:
        event = MorningEvent.RECOVERED_TERMINAL
    else:
        controller.halt(
            event=MorningEvent.RECOVERY_AMBIGUOUS,
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
    controller: MorningController,
    order: OrderView,
    xtdata: object,
    update_signal: BrokerUpdateSignal,
    execution_deadline: datetime,
) -> OrderView | None:
    if controller.snapshot.state is MorningState.RECONCILE:
        return order
    if controller.snapshot.state is MorningState.CANCEL_PENDING:
        return _wait_cancel_terminal(
            trader=trader,
            account=account,
            controller=controller,
            order_id=order.order_id,
            update_signal=update_signal,
        )
    latest = order
    last_signature: tuple[int, int] | None = None
    next_reprice_check = (
        time.monotonic() + ORDER_REPRICE_CHECK_SECONDS
    )
    while True:
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
                MorningEvent.ORDER_TERMINAL,
                details={"order": latest.safe_payload()},
                data_updates={"current_order": latest.safe_payload()},
            )
            return latest
        if classification is OrderClass.CANCEL_PENDING:
            controller.apply(
                MorningEvent.CANCEL_REQUESTED,
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
                event=MorningEvent.ORDER_STATUS_UNKNOWN,
                reason=f"unknown order status {latest.status}",
            )
            return None
        if signature != last_signature:
            controller.apply(
                MorningEvent.ORDER_STILL_ACTIVE,
                details={"order": latest.safe_payload()},
                data_updates={"current_order": latest.safe_payload()},
            )
            last_signature = signature
        now = datetime.now().astimezone()
        if now >= execution_deadline:
            return _cancel_active_order(
                trader=trader,
                account=account,
                controller=controller,
                order=latest,
                update_signal=update_signal,
                reason="morning execution deadline reached",
            )
        monotonic_now = time.monotonic()
        if monotonic_now >= next_reprice_check:
            try:
                books = read_quote_books(
                    xtdata,
                    [GC001],
                    now=now,
                    maximum_age_seconds=MAXIMUM_QUOTE_AGE_SECONDS,
                )
                fresh_rate = _bid1_limit_plan(
                    books[GC001],
                    max(latest.order_volume - latest.traded_volume, 10),
                ).limit_rate_percent
            except QuoteValidationError as exc:
                controller.apply(
                    MorningEvent.ORDER_STILL_ACTIVE,
                    details={
                        "decision": "retain_order_quote_unavailable",
                        "reason": safe_exception(exc),
                        "order": latest.safe_payload(),
                    },
                )
            else:
                if _should_reprice(
                    current_rate=latest.limit_price,
                    fresh_bid1_rate=fresh_rate,
                ):
                    return _cancel_active_order(
                        trader=trader,
                        account=account,
                        controller=controller,
                        order=latest,
                        update_signal=update_signal,
                        reason=(
                            "fresh bid1 changed after the five-second "
                            f"check: {latest.limit_price:.3f} -> "
                            f"{fresh_rate:.3f}"
                        ),
                    )
                controller.apply(
                    MorningEvent.ORDER_STILL_ACTIVE,
                    details={
                        "decision": "retain_same_price_time_priority",
                        "fresh_bid1_rate_percent": fresh_rate,
                        "order": latest.safe_payload(),
                    },
                )
            next_reprice_check = (
                time.monotonic() + ORDER_REPRICE_CHECK_SECONDS
            )
        wait_seconds = min(
            ORDER_STATUS_RECONCILE_SECONDS,
            max(next_reprice_check - time.monotonic(), 0.0),
            max(
                (execution_deadline - datetime.now().astimezone())
                .total_seconds(),
                0.0,
            ),
        )
        update_signal.wait(wait_seconds)


def _cancel_active_order(
    *,
    trader: object,
    account: object,
    controller: MorningController,
    order: OrderView,
    update_signal: BrokerUpdateSignal,
    reason: str,
) -> OrderView | None:
    # The durable cancel intent always precedes the external side effect.
    controller.apply(
        MorningEvent.CANCEL_REQUESTED,
        details={
            "reason": reason,
            "order": order.safe_payload(),
        },
    )
    try:
        cancel_result = int(
            trader.cancel_order_stock(account, order.order_id)
        )
    except Exception as exc:  # noqa: BLE001
        controller.halt(
            event=MorningEvent.CANCEL_REJECTED,
            reason="cancel request raised an exception",
            error=exc,
        )
        return None
    controller.journal.update_data(cancel_result=cancel_result)
    if cancel_result != 0:
        try:
            latest = query_order_strict(
                trader,
                account,
                order.order_id,
            )
        except BrokerQueryAmbiguous as exc:
            controller.halt(
                event=MorningEvent.ORDER_QUERY_AMBIGUOUS,
                reason="cancel failed and order cannot be queried",
                error=exc,
            )
            return None
        if latest.classification in {
            OrderClass.FILLED,
            OrderClass.TERMINAL_PARTIAL,
            OrderClass.CANCELED_ZERO,
            OrderClass.REJECTED,
        }:
            controller.apply(
                MorningEvent.CANCEL_TERMINAL,
                details={"order": latest.safe_payload()},
                data_updates={"current_order": latest.safe_payload()},
            )
            return latest
        controller.halt(
            event=MorningEvent.CANCEL_REJECTED,
            reason=f"cancel request rejected: {cancel_result}",
        )
        return None
    return _wait_cancel_terminal(
        trader=trader,
        account=account,
        controller=controller,
        order_id=order.order_id,
        update_signal=update_signal,
    )


def _wait_cancel_terminal(
    *,
    trader: object,
    account: object,
    controller: MorningController,
    order_id: int,
    update_signal: BrokerUpdateSignal,
) -> OrderView | None:
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
                MorningEvent.CANCEL_TERMINAL,
                details={"order": order.safe_payload()},
                data_updates={"current_order": order.safe_payload()},
            )
            return order
        if classification not in {
            OrderClass.ACTIVE,
            OrderClass.CANCEL_PENDING,
        }:
            controller.halt(
                event=MorningEvent.ORDER_STATUS_UNKNOWN,
                reason=f"unknown cancel status {order.status}",
            )
            return None
        signature = (order.status, order.traded_volume)
        if signature != last_signature:
            controller.apply(
                MorningEvent.CANCEL_STILL_PENDING,
                details={"order": order.safe_payload()},
                data_updates={"current_order": order.safe_payload()},
            )
            last_signature = signature
        update_signal.wait(CANCEL_STATUS_RECONCILE_SECONDS)
    controller.halt(
        event=MorningEvent.CANCEL_TIMEOUT,
        reason="cancel did not reach a terminal broker state",
    )
    return None


def _reconcile_terminal(
    *,
    trader: object,
    account: object,
    controller: MorningController,
    order: OrderView,
    remark_prefix: str,
    execution_deadline: datetime,
    sell_order_type: int,
) -> bool:
    classification = order.classification
    if classification not in {
        OrderClass.FILLED,
        OrderClass.TERMINAL_PARTIAL,
        OrderClass.CANCELED_ZERO,
        OrderClass.REJECTED,
    }:
        controller.halt(
            event=MorningEvent.RECONCILE_FAILED,
            reason="reconciliation received a nonterminal order",
        )
        return False
    orders = query_all_orders_strict(trader, account)
    unresolved = unresolved_repo_orders(orders)
    if unresolved:
        controller.halt(
            event=MorningEvent.RECONCILE_FAILED,
            reason="an unresolved reverse-repo order remains",
        )
        return False
    owned = orders_with_prefix(
        orders,
        remark_prefix=remark_prefix,
    )
    identity_error = _owned_order_identity_error(
        owned,
        sell_order_type=sell_order_type,
    )
    if identity_error:
        controller.halt(
            event=MorningEvent.RECONCILE_FAILED,
            reason=identity_error,
        )
        return False
    duplicate_remarks = _duplicate_order_remarks(owned)
    if duplicate_remarks:
        controller.halt(
            event=MorningEvent.RECONCILE_FAILED,
            reason="owned order remarks are not unique",
        )
        return False
    if not any(item.order_id == order.order_id for item in owned):
        controller.halt(
            event=MorningEvent.RECONCILE_FAILED,
            reason="terminal order is absent from the complete order query",
        )
        return False
    cumulative_filled = _cumulative_owned_principal(owned)
    data = dict(controller.journal.payload.get("data") or {})
    try:
        target_principal = int(data["target_principal_yuan"])
        previously_accounted = int(
            data.get("accounted_filled_principal_yuan", 0)
        )
        attempt_counter = int(data.get("attempt_counter", 0))
    except (KeyError, TypeError, ValueError) as exc:
        controller.halt(
            event=MorningEvent.RECONCILE_FAILED,
            reason="durable target or attempt counter is invalid",
            error=exc,
        )
        return False
    if (
        target_principal < 0
        or target_principal % PRINCIPAL_STEP_YUAN
        or cumulative_filled < previously_accounted
        or cumulative_filled > target_principal
    ):
        controller.halt(
            event=MorningEvent.RECONCILE_FAILED,
            reason="cumulative fill ledger violates its durable bounds",
        )
        return False
    remaining = target_principal - cumulative_filled
    updates = {
        "current_order": order.safe_payload(),
        "accounted_filled_principal_yuan": cumulative_filled,
        "filled_principal_yuan": cumulative_filled,
        "remaining_target_principal_yuan": remaining,
    }
    if remaining == 0:
        controller.apply(
            MorningEvent.RECONCILED_FULL,
            details={"order": order.safe_payload()},
            data_updates={
                **updates,
                "success": True,
                "finished_at": datetime.now().astimezone().isoformat(),
            },
        )
        return False
    retry_now = datetime.now().astimezone()
    if (
        remaining >= PRINCIPAL_STEP_YUAN
        and retry_now + timedelta(
            seconds=ORDER_REPRICE_CHECK_SECONDS
        ) <= execution_deadline
        and attempt_counter < MAXIMUM_ORDER_ATTEMPTS
        and classification is not OrderClass.REJECTED
    ):
        controller.apply(
            MorningEvent.RECONCILED_RETRY,
            details={
                "order": order.safe_payload(),
                "remaining_target_principal_yuan": remaining,
            },
            data_updates={
                **updates,
                "current_intent": None,
                "current_order_id": None,
                "retry_reason": "terminal order left target principal",
            },
        )
        return True
    finished_at = datetime.now().astimezone().isoformat()
    if cumulative_filled > 0:
        controller.apply(
            MorningEvent.RECONCILED_PARTIAL,
            details={"order": order.safe_payload()},
            data_updates={
                **updates,
                "success": False,
                "finished_at": finished_at,
                "final_reason": (
                    "execution window or attempt limit ended with a "
                    "partial cumulative fill"
                ),
            },
        )
        return False
    controller.apply(
        MorningEvent.RECONCILED_ZERO,
        details={"order": order.safe_payload()},
        data_updates={
            **updates,
            "success": False,
            "finished_at": finished_at,
            "final_reason": "terminal order has zero fill",
        },
    )
    return False


def _query_failure_event(state: MorningState) -> MorningEvent:
    if state is MorningState.INTENT:
        return MorningEvent.SUBMIT_EXCEPTION
    if state in {
        MorningState.ORDER_ACTIVE,
        MorningState.CANCEL_PENDING,
    }:
        return MorningEvent.ORDER_QUERY_AMBIGUOUS
    if state in {MorningState.RECOVERY, MorningState.SUBMIT_UNKNOWN}:
        return MorningEvent.RECOVERY_AMBIGUOUS
    if state is MorningState.RECONCILE:
        return MorningEvent.RECONCILE_FAILED
    return MorningEvent.FAULT


def _fault_event(state: MorningState) -> MorningEvent:
    if state is MorningState.INTENT:
        return MorningEvent.SUBMIT_EXCEPTION
    if state in {
        MorningState.ORDER_ACTIVE,
        MorningState.CANCEL_PENDING,
    }:
        return MorningEvent.FAULT
    if state is MorningState.RECONCILE:
        return MorningEvent.RECONCILE_FAILED
    if state is MorningState.SUBMIT_UNKNOWN:
        return MorningEvent.RECOVERY_AMBIGUOUS
    return MorningEvent.FAULT


def _terminal_exit_code(state: MorningState) -> int:
    if state in {
        MorningState.DONE_FILLED,
        MorningState.SKIPPED,
    }:
        return 0
    if state is MorningState.DONE_PARTIAL:
        return 2
    return 1


def _wait_until(target: datetime) -> None:
    while True:
        remaining = (
            target - datetime.now().astimezone()
        ).total_seconds()
        if remaining <= 0:
            return
        time.sleep(min(1.0, max(0.01, remaining)))


def _wait_for_retry_slot(
    *,
    controller: MorningController,
    execution_deadline: datetime,
) -> bool:
    data = dict(controller.journal.payload.get("data") or {})
    raw_attempt_at = data.get("last_submission_attempt_at")
    if raw_attempt_at is None:
        return datetime.now().astimezone() < execution_deadline
    try:
        attempt_at = datetime.fromisoformat(str(raw_attempt_at))
    except ValueError as exc:
        raise ExecutionSafetyError(
            "last submission attempt timestamp is invalid"
        ) from exc
    if attempt_at.tzinfo is None:
        raise ExecutionSafetyError(
            "last submission attempt timestamp lacks a timezone"
        )
    next_attempt_at = attempt_at + timedelta(
        seconds=ORDER_REPRICE_CHECK_SECONDS
    )
    if next_attempt_at >= execution_deadline:
        return False
    _wait_until(next_attempt_at)
    return datetime.now().astimezone() < execution_deadline


def _bid1_plan(book: Any, requested_volume: int) -> BookPlan:
    bid1_only = replace(
        book,
        bid_prices=book.bid_prices[:1],
        bid_volumes=book.bid_volumes[:1],
        ask_prices=book.ask_prices[:1],
        ask_volumes=book.ask_volumes[:1],
    )
    return build_book_plan(bid1_only, requested_volume)


def _bid1_limit_plan(
    book: Any,
    requested_volume: int,
) -> MorningLimitPlan:
    requested = int(requested_volume)
    visible = _bid1_plan(book, requested)
    return MorningLimitPlan(
        symbol=visible.symbol,
        order_volume=requested,
        immediately_executable_volume=visible.executable_volume,
        covers_requested_volume_immediately=(
            visible.covers_requested_volume
        ),
        principal_yuan=qmt_volume_to_principal(requested),
        limit_rate_percent=visible.limit_rate_percent,
        quote_time=visible.quote_time,
        quote_age_seconds=visible.quote_age_seconds,
    )


def _should_reprice(
    *,
    current_rate: float,
    fresh_bid1_rate: float,
) -> bool:
    return abs(float(current_rate) - float(fresh_bid1_rate)) > 1e-9


def _remaining_order_principal(
    *,
    initial_cash_yuan: float,
    target_principal_yuan: int,
    cumulative_filled_principal_yuan: int,
    reported_cash_yuan: float,
) -> tuple[int, float]:
    initial_cash = max(float(initial_cash_yuan), 0.0)
    target = max(int(target_principal_yuan), 0)
    cumulative = max(int(cumulative_filled_principal_yuan), 0)
    reported_cash = max(float(reported_cash_yuan), 0.0)
    cash_cap = max(initial_cash - cumulative, 0.0)
    effective_cash = min(reported_cash, cash_cap)
    remaining_target = max(target - cumulative, 0)
    principal = min(
        remaining_target,
        floor_principal_after_commission(effective_cash, 1.0),
    )
    principal -= principal % PRINCIPAL_STEP_YUAN
    return principal, effective_cash


def _duplicate_order_remarks(
    orders: list[OrderView],
) -> list[str]:
    counts: dict[str, int] = {}
    for order in orders:
        counts[order.remark] = counts.get(order.remark, 0) + 1
    return sorted(
        remark for remark, count in counts.items() if count > 1
    )


def _owned_order_identity_error(
    orders: list[OrderView],
    *,
    sell_order_type: int | None = None,
) -> str:
    for order in orders:
        if order.symbol != GC001:
            return "owned broker order has an unexpected symbol"
        if order.strategy_name != STRATEGY_NAME:
            return "owned broker order has an unexpected strategy name"
        if (
            sell_order_type is not None
            and order.order_type != int(sell_order_type)
        ):
            return "owned broker order has an unexpected side"
    return ""


def _cumulative_owned_principal(orders: list[OrderView]) -> int:
    total = 0
    for order in orders:
        principal = order.principal_yuan
        if principal % PRINCIPAL_STEP_YUAN:
            raise ExecutionSafetyError(
                "broker fill is not a CNY 1,000 principal multiple"
            )
        total += principal
    return total


if __name__ == "__main__":
    raise SystemExit(main())
