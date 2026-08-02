from __future__ import annotations

import argparse
import math
import random
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from repo_execution_core import (
    GC001,
    PRINCIPAL_STEP_YUAN,
    R001,
    ExecutionMutex,
    QuoteValidationError,
    assert_order_budget,
    atomic_write_json,
    build_book_plan,
    floor_principal,
    is_exchange_trading_day,
    principal_to_qmt_volume,
    query_all_orders_strict,
    query_asset_strict,
    query_order_strict,
    rank_book_plans,
    read_quote_books,
    reconcile_cash_cap,
    safe_exception,
    select_bound_account,
    unresolved_repo_orders,
)

READ_ONLY_TRADER_METHODS = frozenset(
    {
        "start",
        "stop",
        "connect",
        "query_account_infos",
        "query_account_status",
        "subscribe",
        "query_stock_asset",
        "query_stock_order",
        "query_stock_orders",
    }
)
AFTER_HOURS_QUOTE_MAXIMUM_AGE_SECONDS = 4 * 24 * 60 * 60


class ReadOnlyViolation(RuntimeError):
    """A non-read-only XtQuantTrader method was requested."""


class ReadOnlyTraderProxy:
    def __init__(self, trader: object) -> None:
        self._trader = trader
        self.accessed_methods: list[str] = []

    def __getattr__(self, name: str) -> Any:
        if name not in READ_ONLY_TRADER_METHODS:
            raise ReadOnlyViolation(
                f"XtQuantTrader method is not read-only allowlisted: {name}"
            )
        value = getattr(self._trader, name)
        if not callable(value):
            raise ReadOnlyViolation(
                f"allowlisted XtQuantTrader attribute is not callable: {name}"
            )
        self.accessed_methods.append(name)
        return value


def run_live_readonly_preflight(
    *,
    qmt_path: Path,
    account_binding: Path,
    output_path: Path,
    mutex_path: Path,
    maximum_quote_age_seconds: float = (
        AFTER_HOURS_QUOTE_MAXIMUM_AGE_SECONDS
    ),
    first_cash_usage_ratio: float = 0.9,
    second_cash_usage_ratio: float = 1.0,
) -> dict[str, object]:
    from xtquant import xtconstant, xtdata, xttype
    from xtquant.xttrader import XtQuantTrader

    normalized_qmt_path = Path(qmt_path).resolve()
    report: dict[str, object] = {
        "schema_version": 1,
        "mode": "live_read_only_preflight",
        "environment": "live",
        "started_at": datetime.now().astimezone().isoformat(),
        "maximum_quote_age_seconds": maximum_quote_age_seconds,
        "first_cash_usage_ratio": first_cash_usage_ratio,
        "second_cash_usage_ratio": second_cash_usage_ratio,
        "passed": False,
        "no_order_or_cancel_methods_available": True,
        "checks": {
            "qmt_path_is_live": False,
            "trading_calendar_query_ok": False,
            "connection_ok": False,
            "account_bound": False,
            "account_subscription_ok": False,
            "asset_query_ok": False,
            "order_query_ok": False,
            "individual_order_query_ok": False,
            "quote_subscription_ok": False,
            "quote_payload_ok": False,
            "morning_plan_calculation_ok": False,
            "afternoon_plan_calculation_ok": False,
            "final_asset_query_ok": False,
        },
    }
    trader: ReadOnlyTraderProxy | None = None
    quote_sequences: list[int] = []
    xtdata.enable_hello = False
    try:
        if maximum_quote_age_seconds <= 0:
            raise ValueError(
                "maximum_quote_age_seconds must be positive"
            )
        for name, ratio in (
            ("first_cash_usage_ratio", first_cash_usage_ratio),
            ("second_cash_usage_ratio", second_cash_usage_ratio),
        ):
            if not math.isfinite(ratio) or not 0 <= ratio <= 1:
                raise ValueError(f"{name} must be from 0 through 1")
        if "模拟" in str(normalized_qmt_path):
            raise ReadOnlyViolation(
                "live read-only preflight cannot use a simulation QMT path"
            )
        checks = dict(report["checks"])
        checks["qmt_path_is_live"] = True
        report["exchange_trading_day"] = is_exchange_trading_day(
            xtdata,
            datetime.now().astimezone().date(),
        )
        checks["trading_calendar_query_ok"] = True
        report["checks"] = checks
        with ExecutionMutex(Path(mutex_path)):
            raw_trader = XtQuantTrader(
                str(normalized_qmt_path),
                random.randint(100_000_000, 999_999_999),
            )
            trader = ReadOnlyTraderProxy(raw_trader)
            trader.start()
            connect_result = int(trader.connect())
            if connect_result != 0:
                raise RuntimeError(
                    f"live QMT connection failed: {connect_result}"
                )
            checks["connection_ok"] = True
            account, binding = select_bound_account(
                trader,
                xtconstant,
                xttype,
                environment="live",
                qmt_path=normalized_qmt_path,
                binding_path=Path(account_binding),
            )
            checks["account_bound"] = True
            subscribe_result = int(trader.subscribe(account))
            if subscribe_result != 0:
                raise RuntimeError(
                    f"live account subscription failed: {subscribe_result}"
                )
            checks["account_subscription_ok"] = True
            cash = query_asset_strict(trader, account)
            checks["asset_query_ok"] = True
            orders = query_all_orders_strict(trader, account)
            checks["order_query_ok"] = True
            individual_orders = [
                query_order_strict(trader, account, order.order_id)
                for order in orders
            ]
            if any(
                individual.order_id != listed.order_id
                for listed, individual in zip(
                    orders,
                    individual_orders,
                )
            ):
                raise RuntimeError(
                    "individual order query disagreed with order list"
                )
            checks["individual_order_query_ok"] = True
            for symbol in (GC001, R001):
                sequence = int(
                    xtdata.subscribe_quote(
                        symbol,
                        period="tick",
                        count=0,
                    )
                    or 0
                )
                if sequence <= 0:
                    raise RuntimeError(
                        f"live quote subscription failed for {symbol}"
                    )
                quote_sequences.append(sequence)
            checks["quote_subscription_ok"] = True
            time.sleep(0.5)
            books = read_quote_books(
                xtdata,
                [GC001, R001],
                now=datetime.now().astimezone(),
                maximum_age_seconds=maximum_quote_age_seconds,
            )
            if set(books) != {GC001, R001}:
                raise RuntimeError(
                    "live quote payload did not contain both repo symbols"
                )
            checks["quote_payload_ok"] = True
            morning_plan = None
            if first_cash_usage_ratio > 0:
                morning_principal = floor_principal(
                    cash.conservative_available_cash,
                    first_cash_usage_ratio,
                )
                if morning_principal < PRINCIPAL_STEP_YUAN:
                    raise RuntimeError(
                        "live cash is below the first planning minimum"
                    )
                morning_book = books[GC001]
                morning_bid1_book = replace(
                    morning_book,
                    bid_prices=morning_book.bid_prices[:1],
                    bid_volumes=morning_book.bid_volumes[:1],
                    ask_prices=morning_book.ask_prices[:1],
                    ask_volumes=morning_book.ask_volumes[:1],
                )
                morning_plan = build_book_plan(
                    morning_bid1_book,
                    principal_to_qmt_volume(morning_principal),
                )
                assert_order_budget(
                    principal_yuan=morning_plan.principal_yuan,
                    verified_available_cash_yuan=(
                        cash.conservative_available_cash
                    ),
                    maximum_ratio=first_cash_usage_ratio,
                )
            checks["morning_plan_calculation_ok"] = True
            effective_afternoon_cash, cash_cap = reconcile_cash_cap(
                cash.conservative_available_cash,
                None,
            )
            afternoon_candidates = []
            if second_cash_usage_ratio > 0:
                afternoon_principal = floor_principal(
                    effective_afternoon_cash,
                    second_cash_usage_ratio,
                )
                if afternoon_principal < PRINCIPAL_STEP_YUAN:
                    raise RuntimeError(
                        "live cash is below the second planning minimum"
                    )
                for book in books.values():
                    try:
                        afternoon_candidates.append(
                            build_book_plan(
                                book,
                                principal_to_qmt_volume(
                                    afternoon_principal
                                ),
                            )
                        )
                    except QuoteValidationError:
                        continue
            afternoon_plans = rank_book_plans(
                afternoon_candidates
            )
            if second_cash_usage_ratio > 0 and not afternoon_plans:
                raise RuntimeError(
                    "no live second-execution order book can be planned"
                )
            if afternoon_plans:
                assert_order_budget(
                    principal_yuan=afternoon_plans[0].principal_yuan,
                    verified_available_cash_yuan=effective_afternoon_cash,
                    maximum_ratio=second_cash_usage_ratio,
                )
            checks["afternoon_plan_calculation_ok"] = True
            final_cash = query_asset_strict(trader, account)
            checks["final_asset_query_ok"] = True
            report.update(
                {
                    "passed": all(checks.values()),
                    "account_label": binding.label,
                    "account_id_persisted": False,
                    "cash_fields_present": {
                        name: getattr(cash, name) is not None
                        for name in (
                            "cash_field",
                            "available_cash_field",
                            "total_asset",
                            "market_value",
                            "frozen_cash",
                            "derived_cash",
                        )
                    },
                    "order_count": len(orders),
                    "individual_order_query_count": len(
                        individual_orders
                    ),
                    "individual_order_query_exercised": bool(
                        individual_orders
                    ),
                    "unresolved_repo_order_count": len(
                        unresolved_repo_orders(orders)
                    ),
                    "cash_semantics": {
                        "initial_and_final_queries_valid": True,
                        "initial_conservative_cash_positive": bool(
                            cash.conservative_available_cash > 0
                        ),
                        "final_conservative_cash_positive": bool(
                            final_cash.conservative_available_cash > 0
                        ),
                        "afternoon_cash_cap_initially_none": (
                            cash_cap is None
                        ),
                    },
                    "dry_run_plans": {
                        "morning": {
                            "skipped_by_zero_ratio": morning_plan is None,
                            "symbol": (
                                morning_plan.symbol if morning_plan else None
                            ),
                            "uses_bid1_only": True,
                            "covers_requested_volume": (
                                morning_plan.covers_requested_volume
                                if morning_plan
                                else None
                            ),
                            "budget_check_passed": True,
                        },
                        "afternoon": {
                            "skipped_by_zero_ratio": not afternoon_plans,
                            "selected_symbol": (
                                afternoon_plans[0].symbol
                                if afternoon_plans
                                else None
                            ),
                            "candidate_count": len(afternoon_plans),
                            "covers_requested_volume": (
                                afternoon_plans[
                                    0
                                ].covers_requested_volume
                                if afternoon_plans
                                else None
                            ),
                            "budget_check_passed": True,
                        },
                        "submission_boundary_reached": True,
                        "submission_called": False,
                        "cancel_called": False,
                    },
                    "quotes": {
                        symbol: {
                            "quote_time": book.quote_time,
                            "quote_age_seconds": book.quote_age_seconds,
                            "bid1_positive": bool(book.bid_prices[0] > 0),
                        }
                        for symbol, book in books.items()
                    },
                    "trader_methods_called": list(
                        trader.accessed_methods
                    ),
                }
            )
    except Exception as exc:  # noqa: BLE001
        report["error"] = safe_exception(exc)
    finally:
        cleanup_errors: list[str] = []
        for sequence in quote_sequences:
            try:
                xtdata.unsubscribe_quote(sequence)
            except Exception as exc:  # noqa: BLE001
                cleanup_errors.append(
                    f"quote unsubscribe: {safe_exception(exc)}"
                )
        if trader is not None:
            try:
                trader.stop()
            except Exception as exc:  # noqa: BLE001
                cleanup_errors.append(
                    f"trader stop: {safe_exception(exc)}"
                )
            report["trader_methods_called"] = list(
                trader.accessed_methods
            )
        if cleanup_errors:
            report["passed"] = False
            report["cleanup_errors"] = cleanup_errors
        report["finished_at"] = datetime.now().astimezone().isoformat()
        atomic_write_json(Path(output_path), report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Connect to live QMT through a strict read-only method proxy."
        )
    )
    parser.add_argument("--qmt-path", required=True)
    parser.add_argument("--account-binding", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--mutex", required=True)
    parser.add_argument(
        "--maximum-quote-age-seconds",
        type=float,
        default=AFTER_HOURS_QUOTE_MAXIMUM_AGE_SECONDS,
    )
    parser.add_argument(
        "--first-cash-usage-ratio",
        type=float,
        default=0.9,
    )
    parser.add_argument(
        "--second-cash-usage-ratio",
        type=float,
        default=1.0,
    )
    args = parser.parse_args()
    report = run_live_readonly_preflight(
        qmt_path=Path(args.qmt_path),
        account_binding=Path(args.account_binding),
        output_path=Path(args.output),
        mutex_path=Path(args.mutex),
        maximum_quote_age_seconds=args.maximum_quote_age_seconds,
        first_cash_usage_ratio=args.first_cash_usage_ratio,
        second_cash_usage_ratio=args.second_cash_usage_ratio,
    )
    safe_summary = {
        "passed": report.get("passed"),
        "checks": report.get("checks"),
        "account_label": report.get("account_label"),
        "account_id_persisted": report.get("account_id_persisted"),
        "cash_fields_present": report.get("cash_fields_present"),
        "order_count": report.get("order_count"),
        "individual_order_query_count": report.get(
            "individual_order_query_count"
        ),
        "individual_order_query_exercised": report.get(
            "individual_order_query_exercised"
        ),
        "unresolved_repo_order_count": report.get(
            "unresolved_repo_order_count"
        ),
        "cash_semantics": report.get("cash_semantics"),
        "dry_run_plans": report.get("dry_run_plans"),
        "quotes": report.get("quotes"),
        "trader_methods_called": report.get("trader_methods_called"),
        "error": report.get("error"),
        "output": str(Path(args.output).resolve()),
    }
    print(safe_summary)
    return 0 if report.get("passed") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
