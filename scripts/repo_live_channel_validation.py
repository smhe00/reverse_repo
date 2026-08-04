from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import platform
import random
import re
import time
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time as clock_time, timedelta
from pathlib import Path
from typing import Any

from repo_execution_core import (
    GC001,
    R001,
    OrderClass,
    OrderView,
    atomic_write_json,
    is_exchange_trading_day,
    load_account_binding,
    query_all_orders_strict,
    query_asset_strict,
    read_quote_books,
    select_bound_account,
    unresolved_repo_orders,
    xtquant_runtime_sha256,
)
from repo_execution_state_machine import verify_state_machines


CERTIFICATE_TYPE = "live_channel"
CERTIFICATE_PRINCIPAL_YUAN = 1000
CERTIFICATE_REMARK_ROOT = "repo_live_cert"
PRODUCTION_STRATEGY_NAME = "repo_morning_v2"
TERMINAL_CLASSES = {
    OrderClass.FILLED,
    OrderClass.TERMINAL_PARTIAL,
    OrderClass.CANCELED_ZERO,
    OrderClass.REJECTED,
}
LIVE_WINDOWS = (
    (clock_time(9, 29, 30), clock_time(11, 25, 0)),
    (clock_time(12, 59, 30), clock_time(15, 25, 0)),
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="CNY 1,000 live reverse-repo channel certification."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    preflight = subparsers.add_parser("preflight")
    _common_arguments(preflight)
    preflight.add_argument("--output", required=True)

    certify = subparsers.add_parser("certify")
    _common_arguments(certify)
    certify.add_argument("--journal", required=True)
    certify.add_argument("--preflight", required=True)
    certify.add_argument("--signing-key", required=True)
    certify.add_argument("--output", required=True)

    status = subparsers.add_parser("status")
    status.add_argument("--qmt-path", required=True)
    status.add_argument("--account-binding", required=True)
    status.add_argument("--certificate", required=True)
    status.add_argument("--signing-key", required=True)

    notify = subparsers.add_parser("notify-failure")
    notify.add_argument("--alert-config", required=True)
    notify.add_argument("--journal", required=True)
    notify.add_argument("--reason", required=True)

    args = parser.parse_args()
    if args.command == "notify-failure":
        from repo_failure_alert import (
            load_optional_smtp_failure_notifier,
            send_standalone_failure,
        )

        notifier, warning = load_optional_smtp_failure_notifier(
            Path(args.alert_config)
        )
        if notifier is None:
            print("Optional failure email is disabled: " + str(warning or ""))
            return 0
        send_standalone_failure(
            notifier,
            strategy="repo_live_cert",
            trade_date=date.today().isoformat(),
            environment="live",
            reason=str(args.reason),
            journal_path=Path(args.journal),
        )
        print("Live-channel certification failure email was sent.")
        return 0
    if args.command == "preflight":
        result = live_preflight(
            qmt_path=Path(args.qmt_path),
            account_binding=Path(args.account_binding),
            now=datetime.now().astimezone(),
        )
        atomic_write_json(Path(args.output), result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["passed"] else 1
    if args.command == "certify":
        result = certify_live_channel(
            qmt_path=Path(args.qmt_path),
            account_binding=Path(args.account_binding),
            journal_path=Path(args.journal),
            preflight_path=Path(args.preflight),
        )
        result["signature_hmac_sha256"] = sign_payload(
            result, load_signing_key(Path(args.signing_key))
        )
        atomic_write_json(Path(args.output), result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["passed"] else 1

    try:
        certificate = load_json(Path(args.certificate), "certificate")
        verify_live_channel_certificate(
            certificate=certificate,
            certificate_path=Path(args.certificate),
            signing_key=Path(args.signing_key),
            qmt_path=Path(args.qmt_path),
            account_binding=Path(args.account_binding),
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Live-channel certification: invalid; {exc}")
        return 1
    print(
        "Live-channel certification: valid; fixed CNY 1,000 real channel; "
        "does not include fault-injection recovery proof."
    )
    print(
        "Filled principal: CNY {0}; certified at: {1}".format(
            certificate.get("filled_principal_yuan", 0),
            certificate.get("certified_at", ""),
        )
    )
    return 0


def _common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--qmt-path", required=True)
    parser.add_argument("--account-binding", required=True)


def plan_live_execution(now: datetime) -> tuple[datetime, datetime | None]:
    if now.utcoffset() is None:
        raise ValueError("current time must include a timezone")
    local = now.timetz().replace(tzinfo=None)
    for start, end in LIVE_WINDOWS:
        start_at = datetime.combine(now.date(), start, tzinfo=now.tzinfo)
        end_at = datetime.combine(now.date(), end, tzinfo=now.tzinfo)
        if start <= local <= end:
            return max(now + timedelta(seconds=5), start_at), None
        if local < start:
            return start_at, start_at
    next_day = now.date() + timedelta(days=1)
    while next_day.weekday() >= 5:
        next_day += timedelta(days=1)
    next_start = datetime.combine(next_day, LIVE_WINDOWS[0][0], tzinfo=now.tzinfo)
    return next_start, next_start


def require_live_window(now: datetime) -> datetime:
    trigger, unavailable_until = plan_live_execution(now)
    if unavailable_until is not None:
        raise RuntimeError(
            "不在快速实盘认证窗口；下个可用时间："
            + unavailable_until.isoformat()
        )
    return trigger


def live_preflight(
    *, qmt_path: Path, account_binding: Path, now: datetime
) -> dict[str, Any]:
    from xtquant import xtconstant, xtdata, xttype
    from xtquant.xttrader import XtQuantTrader

    trigger = require_live_window(now)
    xtdata.enable_hello = False
    if not is_exchange_trading_day(xtdata, now.date()):
        raise RuntimeError("current date is not an exchange trading day")
    normalized_path = Path(qmt_path).resolve()
    checks = {
        "live_path_bound": False,
        "account_bound": False,
        "connection_ok": False,
        "asset_query_ok": False,
        "funds_at_least_1000": False,
        "order_query_ok": False,
        "no_unresolved_repo_orders": False,
        "gc001_quote_ok": False,
        "r001_quote_ok": False,
    }
    trader = XtQuantTrader(
        str(normalized_path), random.randint(100_000_000, 999_999_999)
    )
    subscriptions: list[int] = []
    trader.start()
    try:
        result = int(trader.connect())
        if result != 0:
            raise RuntimeError(f"live QMT connection failed: {result}")
        checks["connection_ok"] = True
        account, binding = select_bound_account(
            trader,
            xtconstant,
            xttype,
            environment="live",
            qmt_path=normalized_path,
            binding_path=account_binding,
        )
        checks["live_path_bound"] = binding.qmt_path_fingerprint is not None
        checks["account_bound"] = True
        if int(trader.subscribe(account)) != 0:
            raise RuntimeError("live account subscription failed")
        asset = query_asset_strict(trader, account)
        checks["asset_query_ok"] = True
        checks["funds_at_least_1000"] = (
            asset.conservative_available_cash >= CERTIFICATE_PRINCIPAL_YUAN
        )
        orders = query_all_orders_strict(trader, account)
        checks["order_query_ok"] = True
        checks["no_unresolved_repo_orders"] = not unresolved_repo_orders(orders)
        for symbol in (GC001, R001):
            sequence = int(
                xtdata.subscribe_quote(symbol, period="tick", count=0) or 0
            )
            if sequence <= 0:
                raise RuntimeError(f"quote subscription failed: {symbol}")
            subscriptions.append(sequence)
        deadline = time.monotonic() + 12.0
        books: dict[str, Any] = {}
        last_error = ""
        while time.monotonic() < deadline:
            try:
                books = read_quote_books(
                    xtdata,
                    (GC001, R001),
                    now=datetime.now().astimezone(),
                    maximum_age_seconds=10.0,
                )
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
            if all(symbol in books for symbol in (GC001, R001)):
                break
            time.sleep(1.0)
        if not all(symbol in books for symbol in (GC001, R001)):
            raise RuntimeError(
                "both GC001 and R-001 fresh quotes are required: " + last_error
            )
        checks["gc001_quote_ok"] = True
        checks["r001_quote_ok"] = True
        passed = all(checks.values())
        return {
            "schema_version": 1,
            "certificate_type": CERTIFICATE_TYPE,
            "environment": "live",
            "checked_at": datetime.now().astimezone().isoformat(),
            "planned_trigger_at": trigger.isoformat(),
            "fixed_principal_yuan": CERTIFICATE_PRINCIPAL_YUAN,
            "account_label": binding.label,
            "account_id_fingerprint": binding.account_id_fingerprint,
            "qmt_path_fingerprint": binding.qmt_path_fingerprint,
            "machine_fingerprint": machine_fingerprint(),
            "available_cash_yuan": asset.conservative_available_cash,
            "quote_times": {
                symbol: books[symbol].quote_time for symbol in (GC001, R001)
            },
            "checks": checks,
            "passed": passed,
        }
    finally:
        for sequence in subscriptions:
            try:
                xtdata.unsubscribe_quote(sequence)
            except Exception:  # noqa: BLE001,S110
                pass
        trader.stop()


def certify_live_channel(
    *,
    qmt_path: Path,
    account_binding: Path,
    journal_path: Path,
    preflight_path: Path,
) -> dict[str, Any]:
    journal = load_json(journal_path, "journal")
    preflight = load_json(preflight_path, "preflight")
    binding = load_account_binding(
        account_binding, environment="live", qmt_path=qmt_path
    )
    orders = query_live_orders(qmt_path=qmt_path, account_binding=account_binding)
    verification = verify_state_machines()
    evidence, checks = validate_live_channel_evidence(
        journal=journal,
        broker_orders=orders,
        expected_account_fingerprint=binding.account_id_fingerprint,
        expected_path_fingerprint=binding.qmt_path_fingerprint,
        preflight=preflight,
        expected_source_hash=str(verification["execution_source_sha256"]),
        expected_transition_hash=str(verification["transition_spec_sha256"]),
    )
    result: dict[str, Any] = {
        "schema_version": 1,
        "certificate_type": CERTIFICATE_TYPE,
        "environment": "live",
        "certified_at": datetime.now().astimezone().isoformat(),
        "passed": all(checks.values()),
        "transition_spec_sha256": verification["transition_spec_sha256"],
        "execution_source_sha256": verification["execution_source_sha256"],
        "xtquant_runtime_sha256": xtquant_runtime_sha256(),
        "account_id_fingerprint": binding.account_id_fingerprint,
        "qmt_path_fingerprint": binding.qmt_path_fingerprint,
        "machine_fingerprint": machine_fingerprint(),
        "fixed_principal_limit_yuan": CERTIFICATE_PRINCIPAL_YUAN,
        "filled_principal_yuan": evidence["filled_principal_yuan"],
        "broker_orders": evidence["broker_orders"],
        "checks": checks,
        "scope": {
            "live_physical_channel": True,
            "fault_injection_recovery": False,
            "afternoon_orchestration": False,
            "shenzhen_route": False,
        },
        "evidence": {
            "journal_name": journal_path.name,
            "journal_sha256": file_sha256(journal_path),
            "preflight_name": preflight_path.name,
            "preflight_sha256": file_sha256(preflight_path),
        },
    }
    if not result["passed"]:
        failed = sorted(name for name, ok in checks.items() if not ok)
        raise RuntimeError("live channel evidence failed: " + ", ".join(failed))
    return result


def validate_live_channel_evidence(
    *,
    journal: Mapping[str, Any],
    broker_orders: Sequence[OrderView],
    expected_account_fingerprint: str,
    expected_path_fingerprint: str | None,
    preflight: Mapping[str, Any],
    expected_source_hash: str,
    expected_transition_hash: str,
) -> tuple[dict[str, Any], dict[str, bool]]:
    data = journal.get("data") if isinstance(journal.get("data"), Mapping) else {}
    machine = (
        journal.get("machine")
        if isinstance(journal.get("machine"), Mapping)
        else {}
    )
    facts = machine.get("facts") if isinstance(machine.get("facts"), Mapping) else {}
    history = journal.get("history") if isinstance(journal.get("history"), list) else []
    remark_prefix = str(data.get("remark_prefix", ""))
    intents = {
        str(item.get("details", {}).get("remark", ""))
        for item in history
        if isinstance(item, Mapping)
        and item.get("event") == "intent_persisted"
        and isinstance(item.get("details"), Mapping)
    }
    intents.discard("")
    relevant = [order for order in broker_orders if order.remark in intents]
    broker_remarks = {order.remark for order in relevant}
    filled = sum(order.principal_yuan for order in relevant)
    formal = data.get("formal_verification") if isinstance(data.get("formal_verification"), Mapping) else {}
    preflight_checks = preflight.get("checks") if isinstance(preflight.get("checks"), Mapping) else {}
    checks = {
        "journal_schema_ok": journal.get("schema_version") == 2,
        "production_strategy_ok": journal.get("strategy") == PRODUCTION_STRATEGY_NAME,
        "live_environment_ok": data.get("environment") == "live",
        "certification_mode_ok": data.get("live_channel_certification") is True,
        "fixed_cap_ok": int(data.get("maximum_principal_yuan", 0) or 0) == 1000,
        "fixed_cash_ratio_ok": float(data.get("cash_usage_ratio", -1)) == 1.0,
        "namespace_ok": re.fullmatch(r"repo_live_cert_[0-9]{8}_", remark_prefix) is not None,
        "terminal_success_state_ok": machine.get("state") in {"done_filled", "done_partial"},
        "journal_has_no_unresolved_order": facts.get("unresolved_order") is False,
        "journal_reports_positive_fill": 0 < int(data.get("filled_principal_yuan", 0) or 0) <= 1000,
        "intent_set_nonempty": bool(intents),
        "broker_identity_exact": bool(relevant) and all(
            order.strategy_name == PRODUCTION_STRATEGY_NAME
            and order.symbol == GC001
            and order.remark.startswith(remark_prefix)
            for order in relevant
        ),
        "all_intents_have_one_broker_order": intents == broker_remarks and len(relevant) == len(intents),
        "all_orders_terminal": bool(relevant) and all(order.classification in TERMINAL_CLASSES for order in relevant),
        "positive_broker_fill": 0 < filled <= 1000,
        "journal_and_broker_fill_match": filled == int(data.get("filled_principal_yuan", -1)),
        "account_binding_match": preflight.get("account_id_fingerprint") == expected_account_fingerprint,
        "qmt_path_binding_match": preflight.get("qmt_path_fingerprint") == expected_path_fingerprint,
        "machine_binding_match": preflight.get("machine_fingerprint") == machine_fingerprint(),
        "preflight_passed": preflight.get("passed") is True and all(preflight_checks.values()),
        "state_machine_hash_match": formal.get("transition_spec_sha256") == expected_transition_hash,
        "execution_source_hash_match": formal.get("execution_source_sha256") == expected_source_hash,
    }
    return {
        "filled_principal_yuan": filled,
        "broker_orders": [order.safe_payload() for order in relevant],
    }, checks


def query_live_orders(*, qmt_path: Path, account_binding: Path) -> list[OrderView]:
    from xtquant import xtconstant, xttype
    from xtquant.xttrader import XtQuantTrader

    trader = XtQuantTrader(
        str(Path(qmt_path).resolve()),
        random.randint(100_000_000, 999_999_999),
    )
    trader.start()
    try:
        if int(trader.connect()) != 0:
            raise RuntimeError("live QMT connection failed during certification")
        account, _ = select_bound_account(
            trader,
            xtconstant,
            xttype,
            environment="live",
            qmt_path=qmt_path,
            binding_path=account_binding,
        )
        if int(trader.subscribe(account)) != 0:
            raise RuntimeError("live account subscription failed")
        orders = query_all_orders_strict(trader, account)
        if unresolved_repo_orders(orders):
            raise RuntimeError("an unresolved reverse-repo order still exists")
        return orders
    finally:
        trader.stop()


def verify_live_channel_certificate(
    *,
    certificate: Mapping[str, Any],
    certificate_path: Path,
    signing_key: Path,
    qmt_path: Path,
    account_binding: Path,
    expected_transition_hash: str | None = None,
    expected_source_hash: str | None = None,
    expected_runtime_hash: str | None = None,
) -> None:
    if certificate.get("schema_version") != 1 or certificate.get("certificate_type") != CERTIFICATE_TYPE:
        raise RuntimeError("unsupported live-channel certificate")
    if certificate.get("environment") != "live" or certificate.get("passed") is not True:
        raise RuntimeError("certificate is not a passed live-channel proof")
    verify_signature(certificate, signing_key)
    try:
        certified_at = datetime.fromisoformat(str(certificate["certified_at"]))
    except (KeyError, ValueError) as exc:
        raise RuntimeError("live-channel certificate timestamp is invalid") from exc
    if certified_at.utcoffset() is None:
        raise RuntimeError("live-channel certificate timestamp lacks a timezone")
    if certified_at.astimezone() > datetime.now().astimezone() + timedelta(minutes=5):
        raise RuntimeError("live-channel certificate timestamp is in the future")
    verification = verify_state_machines()
    transition_hash = expected_transition_hash or str(verification["transition_spec_sha256"])
    source_hash = expected_source_hash or str(verification["execution_source_sha256"])
    runtime_hash = expected_runtime_hash or xtquant_runtime_sha256()
    if certificate.get("transition_spec_sha256") != transition_hash:
        raise RuntimeError("state-machine hash changed")
    if certificate.get("execution_source_sha256") != source_hash:
        raise RuntimeError("execution source hash changed")
    if certificate.get("xtquant_runtime_sha256") != runtime_hash:
        raise RuntimeError("XtQuant runtime hash changed")
    binding = load_account_binding(account_binding, environment="live", qmt_path=qmt_path)
    if certificate.get("account_id_fingerprint") != binding.account_id_fingerprint:
        raise RuntimeError("live account binding changed")
    if not binding.qmt_path_fingerprint:
        raise RuntimeError("live account binding does not bind the QMT path")
    if certificate.get("qmt_path_fingerprint") != binding.qmt_path_fingerprint:
        raise RuntimeError("live QMT path binding changed")
    if certificate.get("machine_fingerprint") != machine_fingerprint():
        raise RuntimeError("machine binding changed")
    if not (0 < int(certificate.get("filled_principal_yuan", 0)) <= 1000):
        raise RuntimeError("certified principal is outside the fixed limit")
    if int(certificate.get("fixed_principal_limit_yuan", 0)) != 1000:
        raise RuntimeError("certificate does not retain the fixed CNY 1,000 limit")
    broker_orders = certificate.get("broker_orders")
    if not isinstance(broker_orders, list) or not broker_orders:
        raise RuntimeError("signed broker-order evidence is missing")
    order_ids: set[int] = set()
    remarks: set[str] = set()
    broker_filled = 0
    for payload in broker_orders:
        if not isinstance(payload, Mapping):
            raise RuntimeError("broker-order evidence is malformed")
        order_id = int(payload.get("order_id", 0) or 0)
        remark = str(payload.get("remark", ""))
        classification = str(payload.get("classification", ""))
        if order_id <= 0 or order_id in order_ids or not remark or remark in remarks:
            raise RuntimeError("broker-order evidence is not unique")
        if (
            payload.get("strategy_name") != PRODUCTION_STRATEGY_NAME
            or payload.get("symbol") != GC001
            or not remark.startswith("repo_live_cert_")
            or classification not in {item.value for item in TERMINAL_CLASSES}
        ):
            raise RuntimeError("broker-order identity or terminal state is invalid")
        order_ids.add(order_id)
        remarks.add(remark)
        broker_filled += int(payload.get("filled_principal_yuan", 0) or 0)
    if broker_filled != int(certificate.get("filled_principal_yuan", 0)):
        raise RuntimeError("broker-order fill does not match certified principal")
    checks = certificate.get("checks")
    if not isinstance(checks, Mapping) or not checks or not all(checks.values()):
        raise RuntimeError("certificate checks are incomplete")
    evidence = certificate.get("evidence")
    if not isinstance(evidence, Mapping):
        raise RuntimeError("certificate evidence is missing")
    for prefix in ("journal", "preflight"):
        name = str(evidence.get(f"{prefix}_name", ""))
        if not name or Path(name).name != name:
            raise RuntimeError(f"{prefix} evidence filename is invalid")
        path = certificate_path.parent / name
        if file_sha256(path) != evidence.get(f"{prefix}_sha256"):
            raise RuntimeError(f"{prefix} evidence hash mismatch")


def machine_fingerprint() -> str:
    identity = "|".join(
        (
            platform.node().strip().lower(),
            os.environ.get("USERDOMAIN", "").strip().lower(),
            os.environ.get("USERNAME", "").strip().lower(),
        )
    )
    return hashlib.sha256(("reverse-repo-machine-v1:" + identity).encode()).hexdigest()


def load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{label} is missing or unreadable: {path}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{label} must be a JSON object")
    return payload


def load_signing_key(path: Path) -> bytes:
    payload = load_json(path, "release-gate signing key")
    try:
        key = bytes.fromhex(str(payload["hmac_sha256_key_hex"]))
    except (KeyError, ValueError) as exc:
        raise RuntimeError("release-gate signing key is invalid") from exc
    if payload.get("version") != 1 or len(key) != 32:
        raise RuntimeError("release-gate signing key is invalid")
    return key


def sign_payload(payload: Mapping[str, Any], key: bytes) -> str:
    unsigned = {name: value for name, value in payload.items() if name != "signature_hmac_sha256"}
    message = json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def verify_signature(certificate: Mapping[str, Any], signing_key: Path) -> None:
    provided = str(certificate.get("signature_hmac_sha256", ""))
    expected = sign_payload(certificate, load_signing_key(signing_key))
    if not hmac.compare_digest(provided, expected):
        raise RuntimeError("live-channel certificate signature is invalid")


def file_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError as exc:
        raise RuntimeError(f"evidence is unreadable: {path}") from exc


if __name__ == "__main__":
    raise SystemExit(main())
