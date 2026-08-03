from __future__ import annotations

import argparse
import random
import time
from dataclasses import replace
from datetime import date, datetime, timedelta
from datetime import time as clock_time
from pathlib import Path

from gc001_live_daily_90pct_093042 import (
    MAXIMUM_QUOTE_AGE_SECONDS,
    REMARK_PREFIX,
    STRATEGY_NAME,
    MorningController,
    _parse_cash_usage_ratio,
    _parse_morning_execution_time,
    _parse_remark_root,
)
from repo_execution_core import (
    GC001,
    AtomicJournal,
    ExecutionMutex,
    QuoteValidationError,
    build_book_plan,
    first_execution_deadline,
    is_exchange_trading_day,
    principal_to_qmt_volume,
    query_all_orders_strict,
    query_asset_strict,
    read_quote_books,
    select_bound_account,
)
from repo_execution_state_machine import (
    MorningEvent,
    initial_morning_snapshot,
    snapshot_to_payload,
    verify_state_machines,
)

VALIDATION_PRINCIPAL_YUAN = 1_000


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Simulation-only fault injection: persist an intent, submit one "
            "CNY 1,000 GC001 order, and deliberately omit the response from "
            "the journal so the production runner must recover it."
        )
    )
    parser.add_argument("--qmt-path", required=True)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--journal", required=True)
    parser.add_argument("--account-binding", required=True)
    parser.add_argument("--mutex", required=True)
    parser.add_argument(
        "--execution-time",
        type=_parse_morning_execution_time,
        default=clock_time(9, 30, 42),
    )
    parser.add_argument(
        "--cash-usage-ratio",
        type=_parse_cash_usage_ratio,
        default=0.90,
    )
    parser.add_argument(
        "--remark-root",
        type=_parse_remark_root,
        default=REMARK_PREFIX,
    )
    args = parser.parse_args()

    qmt_path = Path(args.qmt_path).resolve()
    if "模拟" not in str(qmt_path):
        raise RuntimeError("fault injection is restricted to simulation QMT")
    trade_date = date.fromisoformat(args.trade_date)
    now = datetime.now().astimezone()
    if trade_date != now.date():
        raise RuntimeError("trade date must be today")
    target_at = datetime.combine(
        trade_date,
        args.execution_time,
        tzinfo=now.tzinfo,
    )
    deadline_at = first_execution_deadline(
        trade_date,
        args.execution_time,
        timezone=now.tzinfo,
    )
    if now > target_at.replace(microsecond=0) and (
        now - target_at
    ).total_seconds() > 5:
        raise RuntimeError("simulation recovery injection started too late")
    journal_path = Path(args.journal).resolve()
    if journal_path.exists():
        raise RuntimeError(
            "simulation recovery journal already exists; refusing a duplicate"
        )
    with ExecutionMutex(Path(args.mutex)):
        return _prepare(
            qmt_path=qmt_path,
            trade_date=trade_date,
            target_at=target_at,
            deadline_at=deadline_at,
            journal_path=journal_path,
            account_binding=Path(args.account_binding),
            cash_usage_ratio=float(args.cash_usage_ratio),
            remark_root=str(args.remark_root),
        )


def _prepare(
    *,
    qmt_path: Path,
    trade_date: date,
    target_at: datetime,
    deadline_at: datetime,
    journal_path: Path,
    account_binding: Path,
    cash_usage_ratio: float,
    remark_root: str,
) -> int:
    from xtquant import xtconstant, xtdata, xttype
    from xtquant.xttrader import XtQuantTrader

    xtdata.enable_hello = False
    if not is_exchange_trading_day(xtdata, trade_date):
        raise RuntimeError("validation date is not a trading day")
    trader = XtQuantTrader(
        str(qmt_path),
        random.randint(100_000_000, 999_999_999),
    )
    sequence = 0
    trader.start()
    try:
        if int(trader.connect()) != 0:
            raise RuntimeError("simulation QMT connection failed")
        account, binding = select_bound_account(
            trader,
            xtconstant,
            xttype,
            environment="simulation",
            qmt_path=qmt_path,
            binding_path=account_binding,
        )
        if int(trader.subscribe(account)) != 0:
            raise RuntimeError("simulation account subscription failed")
        remark_prefix = f"{remark_root}_{trade_date:%Y%m%d}_"
        remark = f"{remark_prefix}0001"
        orders = query_all_orders_strict(trader, account)
        if any(order.remark == remark for order in orders):
            raise RuntimeError(
                "simulation recovery order already exists"
            )
        cash = query_asset_strict(
            trader,
            account,
        ).conservative_available_cash
        if cash * cash_usage_ratio < VALIDATION_PRINCIPAL_YUAN:
            raise RuntimeError(
                "simulation first cash budget is below CNY 1,000"
            )
        sequence = int(
            xtdata.subscribe_quote(GC001, period="tick", count=0) or 0
        )
        if sequence <= 0:
            raise RuntimeError("simulation GC001 quote subscription failed")
        _wait_until(target_at)
        books = _wait_for_post_trigger_book(
            xtdata=xtdata,
            target_at=target_at,
            deadline_at=min(
                deadline_at,
                target_at + timedelta(seconds=10),
            ),
        )
        book = books[GC001]
        bid1_only = replace(
            book,
            bid_prices=book.bid_prices[:1],
            bid_volumes=book.bid_volumes[:1],
            ask_prices=book.ask_prices[:1],
            ask_volumes=book.ask_volumes[:1],
        )
        plan = build_book_plan(
            bid1_only,
            principal_to_qmt_volume(VALIDATION_PRINCIPAL_YUAN),
        )
        if not plan.covers_requested_volume:
            raise RuntimeError(
                "simulation bid1 depth does not cover CNY 1,000"
            )

        proof = verify_state_machines()
        snapshot = initial_morning_snapshot()
        journal = AtomicJournal(
            journal_path,
            strategy=STRATEGY_NAME,
            trade_date=trade_date,
        )
        journal.load_or_initialize(
            machine_payload=snapshot_to_payload(snapshot),
            initial_data={
                "environment": "simulation",
                "symbol": GC001,
                "side": "SELL",
                "target_at": target_at.isoformat(),
                "quote_deadline": deadline_at.isoformat(),
                "cash_usage_ratio": cash_usage_ratio,
                "remark_prefix": remark_prefix,
                "attempt_counter": 1,
                "accounted_filled_principal_yuan": 0,
                "initial_verified_cash_yuan": cash,
                "target_principal_yuan": VALIDATION_PRINCIPAL_YUAN,
                "success": False,
                "account_id_persisted": False,
                "account_label": binding.label,
                "formal_verification": proof,
                "fault_injection": (
                    "crash_after_broker_accept_before_response_journal"
                ),
            },
        )
        controller = MorningController(journal, snapshot)
        for event in (
            MorningEvent.BEGIN,
            MorningEvent.PREFLIGHT_OK,
            MorningEvent.RECOVERY_CLEAR,
            MorningEvent.TRIGGER,
            MorningEvent.SNAPSHOT_OK,
        ):
            controller.apply(event)
        intent = {
            "remark": remark,
            "symbol": GC001,
            "side": "SELL",
            "available_cash_yuan": cash,
            "principal_yuan": VALIDATION_PRINCIPAL_YUAN,
            "qmt_volume": plan.executable_volume,
            "limit_rate_percent": plan.limit_rate_percent,
            "quote_time": plan.quote_time,
            "quote_age_seconds": plan.quote_age_seconds,
            "persisted_before_submission": True,
            "simulation_fault_injection": True,
        }
        controller.apply(
            MorningEvent.INTENT_PERSISTED,
            details=intent,
            data_updates={
                "attempt_counter": 1,
                "current_intent": intent,
            },
        )
        order_id = int(
            trader.order_stock(
                account,
                GC001,
                xtconstant.STOCK_SELL,
                plan.executable_volume,
                xtconstant.FIX_PRICE,
                plan.limit_rate_percent,
                STRATEGY_NAME,
                remark,
            )
        )
        if order_id <= 0:
            raise RuntimeError(
                f"simulation fault-injection order rejected: {order_id}"
            )
        # Deliberately do not persist order_id. This is the crash boundary.
        print(
            "Simulation order accepted; journal intentionally remains at "
            "intent_persisted for production recovery."
        )
        return 0
    finally:
        if sequence:
            xtdata.unsubscribe_quote(sequence)
        trader.stop()


def _wait_until(target: datetime) -> None:
    while True:
        remaining = (
            target - datetime.now().astimezone()
        ).total_seconds()
        if remaining <= 0:
            return
        time.sleep(min(0.2, max(0.01, remaining)))


def _wait_for_post_trigger_book(
    *,
    xtdata: object,
    target_at: datetime,
    deadline_at: datetime,
) -> dict[str, object]:
    last_error: QuoteValidationError | None = None
    while True:
        now = datetime.now().astimezone()
        try:
            return read_quote_books(
                xtdata,
                [GC001],
                now=now,
                maximum_age_seconds=MAXIMUM_QUOTE_AGE_SECONDS,
                not_before_epoch_ms=int(target_at.timestamp() * 1000),
            )
        except QuoteValidationError as exc:
            last_error = exc
        if now >= deadline_at:
            raise QuoteValidationError(
                "no post-trigger GC001 quote arrived before the injection deadline: "
                f"{last_error}"
            ) from last_error
        time.sleep(0.2)


if __name__ == "__main__":
    raise SystemExit(main())
