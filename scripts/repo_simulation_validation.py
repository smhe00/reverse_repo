from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import random
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from repo_execution_core import (
    GC001,
    R001,
    OrderClass,
    atomic_write_json,
    query_all_orders_strict,
    query_asset_strict,
    select_bound_account,
    xtquant_runtime_sha256,
    reverse_repo_schedule_config_sha256,
    reverse_repo_strategy_config_sha256,
)
from repo_execution_state_machine import verify_state_machines


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate the v2 executors against the real simulation QMT."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    preflight = subparsers.add_parser("preflight")
    _common_arguments(preflight)
    preflight.add_argument("--output", required=True)

    certify = subparsers.add_parser("certify")
    _common_arguments(certify)
    certify.add_argument("--morning-journal", required=True)
    certify.add_argument("--afternoon-journal", required=True)
    certify.add_argument("--signing-key", required=True)
    certify.add_argument("--strategy-config", required=True)
    certify.add_argument("--output", required=True)
    args = parser.parse_args()

    if args.command == "preflight":
        result = simulation_preflight(
            qmt_path=Path(args.qmt_path),
            account_binding=Path(args.account_binding),
        )
        atomic_write_json(Path(args.output), result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["passed"] else 1
    result = certify_simulation(
        qmt_path=Path(args.qmt_path),
        account_binding=Path(args.account_binding),
        morning_journal=Path(args.morning_journal),
        afternoon_journal=Path(args.afternoon_journal),
        strategy_config=Path(args.strategy_config),
    )
    result["evidence"] = {
        "morning_journal_name": Path(args.morning_journal).name,
        "morning_journal_sha256": _file_sha256(
            Path(args.morning_journal)
        ),
        "afternoon_journal_name": Path(args.afternoon_journal).name,
        "afternoon_journal_sha256": _file_sha256(
            Path(args.afternoon_journal)
        ),
    }
    result["signature_hmac_sha256"] = _sign_payload(
        result,
        _load_signing_key(Path(args.signing_key)),
    )
    atomic_write_json(Path(args.output), result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


def simulation_preflight(
    *,
    qmt_path: Path,
    account_binding: Path,
) -> dict[str, Any]:
    from xtquant import xtconstant, xtdata, xttype
    from xtquant.xttrader import XtQuantTrader

    xtdata.enable_hello = False
    normalized_path = Path(qmt_path).resolve()
    checks = {
        "simulation_path_bound": False,
        "account_bound": False,
        "connection_ok": False,
        "asset_query_ok": False,
        "order_query_ok": False,
        "quote_subscription_ok": False,
    }
    trader = XtQuantTrader(
        str(normalized_path),
        random.randint(100_000_000, 999_999_999),
    )
    subscriptions: list[int] = []
    trader.start()
    try:
        result = int(trader.connect())
        if result != 0:
            raise RuntimeError(
                f"simulation QMT connection failed: {result}"
            )
        checks["connection_ok"] = True
        account, binding = select_bound_account(
            trader,
            xtconstant,
            xttype,
            environment="simulation",
            qmt_path=normalized_path,
            binding_path=account_binding,
        )
        checks["simulation_path_bound"] = (
            binding.qmt_path_fingerprint is not None
        )
        checks["account_bound"] = True
        subscribe_result = int(trader.subscribe(account))
        if subscribe_result != 0:
            raise RuntimeError(
                f"simulation account subscription failed: {subscribe_result}"
            )
        asset = query_asset_strict(trader, account)
        checks["asset_query_ok"] = True
        orders = query_all_orders_strict(trader, account)
        checks["order_query_ok"] = True
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
                    f"simulation quote subscription failed: {symbol}"
                )
            subscriptions.append(sequence)
        payload = xtdata.get_full_tick([GC001, R001])
        if not isinstance(payload, dict):
            raise RuntimeError("simulation quote payload is not a mapping")
        checks["quote_subscription_ok"] = True
        return {
            "schema_version": 1,
            "environment": "simulation",
            "checked_at": datetime.now().astimezone().isoformat(),
            "passed": all(checks.values()),
            "checks": checks,
            "account_label": binding.label,
            "account_id_persisted": False,
            "available_cash_yuan": (
                asset.conservative_available_cash
            ),
            "broker_order_count": len(orders),
        }
    finally:
        for sequence in subscriptions:
            try:
                xtdata.unsubscribe_quote(sequence)
            except Exception:
                pass
        trader.stop()


def certify_simulation(
    *,
    qmt_path: Path,
    account_binding: Path,
    morning_journal: Path,
    afternoon_journal: Path,
    strategy_config: Path,
) -> dict[str, Any]:
    preflight = simulation_preflight(
        qmt_path=qmt_path,
        account_binding=account_binding,
    )
    formal = verify_state_machines()
    morning = _load_journal(morning_journal)
    afternoon = _load_journal(afternoon_journal)
    broker_orders = _simulation_orders(
        qmt_path=qmt_path,
        account_binding=account_binding,
    )
    morning_data = dict(morning.get("data") or {})
    afternoon_data = dict(afternoon.get("data") or {})
    morning_machine = dict(morning.get("machine") or {})
    afternoon_machine = dict(afternoon.get("machine") or {})
    morning_history = list(morning.get("history") or [])

    relevant = [
        order
        for order in broker_orders
        if order.remark.startswith("repo_morning_v2_")
        or order.remark.startswith("repo_afternoon_v2_")
    ]
    remarks = [order.remark for order in relevant]
    broker_terminal = all(
        order.classification
        in {
            OrderClass.FILLED,
            OrderClass.TERMINAL_PARTIAL,
            OrderClass.CANCELED_ZERO,
            OrderClass.REJECTED,
        }
        for order in relevant
    )
    morning_ok = (
        morning_machine.get("state")
        in {"done_filled", "done_partial"}
        and int(morning_data.get("filled_principal_yuan", 0)) > 0
        and not bool(
            (morning_machine.get("facts") or {}).get(
                "unresolved_order"
            )
        )
    )
    afternoon_ok = (
        afternoon_machine.get("state") == "complete_at_hard_stop"
        and int(
            afternoon_data.get(
                "accounted_filled_principal_yuan",
                0,
            )
        )
        > 0
        and not bool(
            (afternoon_machine.get("facts") or {}).get(
                "unresolved_order"
            )
        )
    )
    history_events = {
        str(record.get("event")) for record in morning_history
    }
    restart_ok = (
        "restart" in history_events
        and bool(
            history_events
            & {
                "recovery_active",
                "recovery_cancel_pending",
                "recovery_terminal",
            }
        )
        and (
            "reconciled_full" in history_events
            or "reconciled_partial" in history_events
        )
    )
    checks = {
        **dict(preflight["checks"]),
        "morning_order_lifecycle_ok": morning_ok,
        "afternoon_order_lifecycle_ok": afternoon_ok,
        "restart_recovery_ok": restart_ok,
        "all_validation_orders_terminal": broker_terminal,
        "all_validation_remarks_unique": (
            len(remarks) == len(set(remarks))
        ),
    }
    return {
        "schema_version": 2,
        "environment": "simulation",
        "certified_at": datetime.now().astimezone().isoformat(),
        "passed": all(checks.values()),
        "transition_spec_sha256": formal[
            "transition_spec_sha256"
        ],
        "execution_source_sha256": formal[
            "execution_source_sha256"
        ],
        "xtquant_runtime_sha256": xtquant_runtime_sha256(),
        "strategy_config_sha256": (
            reverse_repo_strategy_config_sha256(strategy_config)
        ),
        "schedule_config_sha256": (
            reverse_repo_schedule_config_sha256(strategy_config)
        ),
        "checks": checks,
        "account_label": preflight["account_label"],
        "account_id_persisted": False,
        "morning_state": morning_machine.get("state"),
        "afternoon_state": afternoon_machine.get("state"),
        "validation_order_count": len(relevant),
    }


def _simulation_orders(
    *,
    qmt_path: Path,
    account_binding: Path,
) -> list[Any]:
    from xtquant import xtconstant, xttype
    from xtquant.xttrader import XtQuantTrader

    trader = XtQuantTrader(
        str(Path(qmt_path).resolve()),
        random.randint(100_000_000, 999_999_999),
    )
    trader.start()
    try:
        result = int(trader.connect())
        if result != 0:
            raise RuntimeError(
                f"simulation QMT connection failed: {result}"
            )
        account, _ = select_bound_account(
            trader,
            xtconstant,
            xttype,
            environment="simulation",
            qmt_path=qmt_path,
            binding_path=account_binding,
        )
        if int(trader.subscribe(account)) != 0:
            raise RuntimeError("simulation account subscription failed")
        return query_all_orders_strict(trader, account)
    finally:
        trader.stop()


def _load_journal(path: Path) -> Mapping[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"journal is unreadable: {path}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 2:
        raise RuntimeError(f"journal has an invalid schema: {path}")
    return payload


def _common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--qmt-path", required=True)
    parser.add_argument("--account-binding", required=True)


def _load_signing_key(path: Path) -> bytes:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        key_hex = str(payload["hmac_sha256_key_hex"])
        key = bytes.fromhex(key_hex)
    except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "release-gate signing key is unreadable"
        ) from exc
    if payload.get("version") != 1 or len(key) != 32:
        raise RuntimeError("release-gate signing key is invalid")
    return key


def _sign_payload(payload: Mapping[str, Any], key: bytes) -> str:
    unsigned = {
        name: value
        for name, value in payload.items()
        if name != "signature_hmac_sha256"
    }
    message = json.dumps(
        unsigned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
