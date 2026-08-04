from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import random
import re
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
    certify.add_argument("--morning-normal-journal", required=True)
    certify.add_argument("--afternoon-normal-journal", required=True)
    certify.add_argument("--morning-recovery-journal", required=True)
    certify.add_argument("--signing-key", required=True)
    certify.add_argument("--strategy-config", required=True)
    certify.add_argument("--validation-first-execution-time", required=True)
    certify.add_argument("--validation-second-execution-time", required=True)
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
        morning_normal_journal=Path(args.morning_normal_journal),
        afternoon_normal_journal=Path(args.afternoon_normal_journal),
        morning_recovery_journal=Path(args.morning_recovery_journal),
        strategy_config=Path(args.strategy_config),
        validation_first_execution_time=args.validation_first_execution_time,
        validation_second_execution_time=args.validation_second_execution_time,
    )
    result["evidence"] = {
        "morning_normal_journal_name": Path(
            args.morning_normal_journal
        ).name,
        "morning_normal_journal_sha256": _file_sha256(
            Path(args.morning_normal_journal)
        ),
        "afternoon_normal_journal_name": Path(
            args.afternoon_normal_journal
        ).name,
        "afternoon_normal_journal_sha256": _file_sha256(
            Path(args.afternoon_normal_journal)
        ),
        "morning_recovery_journal_name": Path(
            args.morning_recovery_journal
        ).name,
        "morning_recovery_journal_sha256": _file_sha256(
            Path(args.morning_recovery_journal)
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
    morning_normal_journal: Path,
    afternoon_normal_journal: Path,
    morning_recovery_journal: Path,
    strategy_config: Path,
    validation_first_execution_time: str,
    validation_second_execution_time: str,
) -> dict[str, Any]:
    preflight = simulation_preflight(
        qmt_path=qmt_path,
        account_binding=account_binding,
    )
    formal = verify_state_machines()
    morning_normal = _load_journal(morning_normal_journal)
    afternoon_normal = _load_journal(afternoon_normal_journal)
    morning_recovery = _load_journal(morning_recovery_journal)
    broker_orders = _simulation_orders(
        qmt_path=qmt_path,
        account_binding=account_binding,
    )
    morning_normal_data = dict(morning_normal.get("data") or {})
    afternoon_normal_data = dict(afternoon_normal.get("data") or {})
    morning_recovery_data = dict(morning_recovery.get("data") or {})
    morning_normal_machine = dict(morning_normal.get("machine") or {})
    afternoon_normal_machine = dict(afternoon_normal.get("machine") or {})
    morning_recovery_machine = dict(morning_recovery.get("machine") or {})
    morning_normal_history = list(morning_normal.get("history") or [])
    morning_recovery_history = list(morning_recovery.get("history") or [])

    relevant, evidence_checks = _validation_order_evidence_checks(
        broker_orders=broker_orders,
        morning_normal_data=morning_normal_data,
        afternoon_normal_data=afternoon_normal_data,
        morning_recovery_data=morning_recovery_data,
    )
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
    normal_events = {
        str(record.get("event")) for record in morning_normal_history
    }
    recovery_events = {
        str(record.get("event")) for record in morning_recovery_history
    }
    morning_normal_ok = (
        morning_normal_machine.get("state")
        in {"done_filled", "done_partial"}
        and int(morning_normal_data.get("filled_principal_yuan", 0)) > 0
        and not bool(
            (morning_normal_machine.get("facts") or {}).get(
                "unresolved_order"
            )
        )
    )
    morning_normal_production_path_ok = (
        {
            "begin",
            "preflight_ok",
            "recovery_clear",
            "trigger",
            "snapshot_ok",
            "intent_persisted",
            "submit_accepted",
        }
        <= normal_events
        and "restart" not in normal_events
        and not morning_normal_data.get("fault_injection")
    )
    afternoon_normal_ok = (
        afternoon_normal_machine.get("state") == "complete_at_hard_stop"
        and int(
            afternoon_normal_data.get(
                "accounted_filled_principal_yuan",
                0,
            )
        )
        > 0
        and not bool(
            (afternoon_normal_machine.get("facts") or {}).get(
                "unresolved_order"
            )
        )
    )
    morning_recovery_ok = (
        morning_recovery_machine.get("state")
        in {"done_filled", "done_partial"}
        and int(morning_recovery_data.get("filled_principal_yuan", 0)) > 0
        and "restart" in recovery_events
        and bool(
            recovery_events
            & {
                "recovery_active",
                "recovery_cancel_pending",
                "recovery_terminal",
            }
        )
        and (
            "reconciled_full" in recovery_events
            or "reconciled_partial" in recovery_events
        )
        and not bool(
            (morning_recovery_machine.get("facts") or {}).get(
                "unresolved_order"
            )
        )
    )
    normal_prefix = str(morning_normal_data.get("remark_prefix", ""))
    recovery_prefix = str(morning_recovery_data.get("remark_prefix", ""))
    afternoon_prefix = str(afternoon_normal_data.get("remark_prefix", ""))
    fault_injection_isolated = (
        morning_normal.get("trade_date") == afternoon_normal.get("trade_date")
        and morning_recovery.get("trade_date")
        == morning_normal.get("trade_date")
        and normal_prefix.startswith("repo_morn_no")
        and recovery_prefix.startswith("repo_morn_re")
        and afternoon_prefix.startswith("repo_afternoon_v2_")
        and len({normal_prefix, recovery_prefix, afternoon_prefix}) == 3
        and morning_recovery_data.get("fault_injection")
        == "crash_after_broker_accept_before_response_journal"
        and not morning_normal_data.get("fault_injection")
        and not afternoon_normal_data.get("fault_injection")
    )
    configured_schedule_ok = _journals_match_validation_schedule(
        first_time=validation_first_execution_time,
        second_time=validation_second_execution_time,
        morning_normal_data=morning_normal_data,
        afternoon_normal_data=afternoon_normal_data,
        morning_recovery_data=morning_recovery_data,
    )
    fault_state_space_verified = all(
        int(formal[phase][field]) == 0
        for phase in ("morning", "afternoon")
        for field in (
            "unreachable_states",
            "unreachable_transitions",
            "states_without_terminal_path",
            "invariant_violations",
        )
    )
    checks = {
        **dict(preflight["checks"]),
        "morning_normal_order_lifecycle_ok": morning_normal_ok,
        "morning_normal_production_path_ok": (
            morning_normal_production_path_ok
        ),
        "afternoon_normal_order_lifecycle_ok": afternoon_normal_ok,
        "morning_fault_recovery_ok": morning_recovery_ok,
        "fault_injection_isolated": fault_injection_isolated,
        "fault_state_space_verified": fault_state_space_verified,
        "normal_schedule_paths_match_validation_plan": configured_schedule_ok,
        "evidence_files_isolated": len(
            {
                morning_normal_journal.resolve(),
                afternoon_normal_journal.resolve(),
                morning_recovery_journal.resolve(),
            }
        )
        == 3,
        "all_validation_orders_terminal": broker_terminal,
        "all_validation_remarks_unique": (
            len(remarks) == len(set(remarks))
        ),
        **evidence_checks,
    }
    return {
        "schema_version": 3,
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
        "checks": checks,
        "account_label": preflight["account_label"],
        "account_id_persisted": False,
        "morning_normal_state": morning_normal_machine.get("state"),
        "afternoon_normal_state": afternoon_normal_machine.get("state"),
        "morning_recovery_state": morning_recovery_machine.get("state"),
        "validation_order_count": len(relevant),
    }


def _journals_match_validation_schedule(
    *,
    first_time: str,
    second_time: str,
    morning_normal_data: Mapping[str, object],
    afternoon_normal_data: Mapping[str, object],
    morning_recovery_data: Mapping[str, object],
) -> bool:
    try:
        def journal_time(data: Mapping[str, object], name: str) -> str:
            return datetime.fromisoformat(str(data[name])).strftime("%H:%M:%S")

        recovery_time = journal_time(morning_recovery_data, "target_at")
        recovery_clock = datetime.strptime(recovery_time, "%H:%M:%S").time()
        recovery_is_trading_time = (
            datetime.strptime("09:30:00", "%H:%M:%S").time()
            <= recovery_clock
            <= datetime.strptime("11:28:00", "%H:%M:%S").time()
        ) or (
            datetime.strptime("13:00:00", "%H:%M:%S").time()
            <= recovery_clock
            <= datetime.strptime("15:28:00", "%H:%M:%S").time()
        )
        return (
            journal_time(morning_normal_data, "target_at") == first_time
            and journal_time(afternoon_normal_data, "execution_at")
            == second_time
            and recovery_is_trading_time
            and recovery_time not in {first_time, second_time}
        )
    except (KeyError, TypeError, ValueError):
        return False


def _validation_order_evidence_checks(
    *,
    broker_orders: list[Any],
    morning_normal_data: Mapping[str, object],
    afternoon_normal_data: Mapping[str, object],
    morning_recovery_data: Mapping[str, object],
) -> tuple[list[Any], dict[str, bool]]:
    """Bind signed journals to their exact broker-side namespaces/orders."""

    prefixes = (
        str(morning_normal_data.get("remark_prefix", "")),
        str(afternoon_normal_data.get("remark_prefix", "")),
        str(morning_recovery_data.get("remark_prefix", "")),
    )
    namespace_ok = all(
        re.fullmatch(r"[a-z0-9_]{3,23}_[0-9]{8}_", prefix)
        is not None
        for prefix in prefixes
    ) and len(set(prefixes)) == 3
    relevant = (
        [
            order
            for order in broker_orders
            if any(order.remark.startswith(prefix) for prefix in prefixes)
        ]
        if namespace_ok
        else []
    )

    morning_normal_payload = morning_normal_data.get("current_order")
    afternoon_normal_payload = afternoon_normal_data.get(
        "last_terminal_order"
    )
    morning_recovery_payload = morning_recovery_data.get("current_order")
    morning_normal_remark = (
        str(morning_normal_payload.get("remark", ""))
        if isinstance(morning_normal_payload, Mapping)
        else ""
    )
    afternoon_normal_remark = (
        str(afternoon_normal_payload.get("remark", ""))
        if isinstance(afternoon_normal_payload, Mapping)
        else ""
    )
    morning_recovery_remark = (
        str(morning_recovery_payload.get("remark", ""))
        if isinstance(morning_recovery_payload, Mapping)
        else ""
    )

    def matches(
        *,
        remark: str,
        prefix: str,
        strategy: str,
        symbols: set[str],
    ) -> bool:
        found = [order for order in relevant if order.remark == remark]
        return (
            bool(remark)
            and remark.startswith(prefix)
            and len(found) == 1
            and found[0].strategy_name == strategy
            and found[0].symbol in symbols
            and found[0].principal_yuan > 0
        )

    identity_ok = namespace_ok and all(
        (
            order.strategy_name == "repo_morning_v2"
            and order.symbol == GC001
        )
        if (
            order.remark.startswith(prefixes[0])
            or order.remark.startswith(prefixes[2])
        )
        else (
            order.strategy_name == "repo_afternoon_v2"
            and order.symbol in {GC001, R001}
        )
        for order in relevant
    )
    checks = {
        "validation_namespaces_ok": namespace_ok,
        "validation_order_identity_ok": identity_ok,
        "morning_normal_broker_evidence_ok": matches(
            remark=morning_normal_remark,
            prefix=prefixes[0],
            strategy="repo_morning_v2",
            symbols={GC001},
        ),
        "afternoon_normal_broker_evidence_ok": matches(
            remark=afternoon_normal_remark,
            prefix=prefixes[1],
            strategy="repo_afternoon_v2",
            symbols={GC001, R001},
        ),
        "morning_recovery_broker_evidence_ok": matches(
            remark=morning_recovery_remark,
            prefix=prefixes[2],
            strategy="repo_morning_v2",
            symbols={GC001},
        ),
    }
    return relevant, checks


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
