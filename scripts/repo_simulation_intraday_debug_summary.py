from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from repo_execution_core import atomic_write_json


ORDER_EVIDENCE_FIELDS = (
    "order_id",
    "symbol",
    "strategy_name",
    "remark",
    "order_type",
    "status",
    "classification",
    "requested_volume",
    "traded_volume",
    "principal_yuan",
    "price",
    "traded_price",
)


def _load(path: Path) -> Mapping[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"debug artifact is not an object: {path}")
    return payload


def _order_evidence(payload: Mapping[str, Any]) -> list[dict[str, object]]:
    """Extract de-duplicated, account-free broker order evidence."""
    found: dict[tuple[int, str], dict[str, object]] = {}

    def visit(value: object) -> None:
        if isinstance(value, Mapping):
            if "order_id" in value and ("remark" in value or "order_remark" in value):
                try:
                    order_id = int(value.get("order_id", 0) or 0)
                except (TypeError, ValueError):
                    order_id = 0
                remark = str(value.get("remark", value.get("order_remark", "")) or "")
                if order_id > 0 and remark:
                    evidence: dict[str, object] = {}
                    for field in ORDER_EVIDENCE_FIELDS:
                        source = "order_remark" if field == "remark" else field
                        if source in value:
                            evidence[field] = value[source]
                    found.setdefault((order_id, remark), {}).update(evidence)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    return sorted(found.values(), key=lambda item: int(item["order_id"]))


def _stress_order_evidence(path: Path) -> list[dict[str, object]]:
    orders: dict[int, dict[str, object]] = {}
    for line_number, line in enumerate(
        Path(path).read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"stress sample JSON is invalid at line {line_number}: {path}"
            ) from exc
        if not isinstance(record, dict) or record.get("kind") not in {
            "order_submitted",
            "order_terminal",
        }:
            continue
        try:
            order_id = int(record.get("order_id", 0) or 0)
        except (TypeError, ValueError):
            order_id = 0
        if order_id <= 0:
            continue
        evidence = orders.setdefault(order_id, {"order_id": order_id})
        for field in (
            "symbol",
            "side",
            "volume",
            "price",
            "remark",
            "status",
            "requested_volume",
            "traded_volume",
        ):
            if field in record:
                evidence[field] = record[field]
        if record.get("kind") == "order_submitted":
            evidence["submitted_at"] = record.get("at")
        else:
            evidence["terminal_at"] = record.get("at")
    return [orders[order_id] for order_id in sorted(orders)]


def _journal_result(payload: Mapping[str, Any], *, afternoon: bool) -> dict[str, object]:
    machine = dict(payload.get("machine") or {})
    facts = dict(machine.get("facts") or {})
    data = dict(payload.get("data") or {})
    history = list(payload.get("history") or [])
    events = [str(item.get("event")) for item in history if isinstance(item, dict)]
    if afternoon:
        filled = int(data.get("accounted_filled_principal_yuan", 0) or 0)
        expected_states = {"complete_at_hard_stop"}
    else:
        filled = int(data.get("filled_principal_yuan", 0) or 0)
        expected_states = {"done_filled", "done_partial"}
    state = str(machine.get("state", ""))
    unresolved = bool(facts.get("unresolved_order"))
    passed = state in expected_states and filled > 0 and not unresolved
    if not afternoon:
        passed = passed and "restart" in events and bool(
            {"recovery_active", "recovery_cancel_pending", "recovery_terminal"}
            & set(events)
        ) and bool({"reconciled_full", "reconciled_partial"} & set(events))
    return {
        "passed": passed,
        "state": state,
        "filled_principal_yuan": filled,
        "unresolved_order": unresolved,
        "final_reason": data.get("final_reason"),
        "events": events,
        "orders": _order_evidence(payload),
    }


KNOWN_CALLBACK_PARSER_FALSE_NEGATIVE = (
    "primary money-ETF produced no unique tick timestamp"
)


def _stress_result(
    payload: Mapping[str, Any],
    callback_probe: Mapping[str, Any] | None = None,
) -> dict[str, object]:
    failures = list(payload.get("failures") or [])
    raw_passed = payload.get("passed") is True
    probe_passed = bool(
        callback_probe and callback_probe.get("passed") is True
    )
    metrics = dict(payload.get("metrics") or {})
    tick_counts = dict(metrics.get("tick_counts") or {})
    missing = dict(metrics.get("tick_missing_timestamp_counts") or {})
    primary_count = int(tick_counts.get("511880.SH", 0) or 0)
    parser_signature = (
        primary_count > 0
        and int(missing.get("511880.SH", 0) or 0) == primary_count
    )
    accepted_false_negative = (
        not raw_passed
        and probe_passed
        and failures == [KNOWN_CALLBACK_PARSER_FALSE_NEGATIVE]
        and parser_signature
    )
    return {
        "passed": raw_passed or accepted_false_negative,
        "raw_passed": raw_passed,
        "failures": failures,
        "accepted_known_parser_false_negative": accepted_false_negative,
        "supplemental_callback_probe": dict(callback_probe or {}),
        "metrics": payload.get("metrics"),
        "remark_prefix": payload.get("remark_prefix"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--recovery-journal", required=True)
    parser.add_argument("--stress-report", required=True)
    parser.add_argument("--stress-samples")
    parser.add_argument("--callback-probe")
    parser.add_argument("--afternoon-journal", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    recovery = _journal_result(
        _load(Path(args.recovery_journal)), afternoon=False
    )
    stress_payload = _load(Path(args.stress_report))
    afternoon = _journal_result(
        _load(Path(args.afternoon_journal)), afternoon=True
    )
    stress_samples = (
        Path(args.stress_samples)
        if args.stress_samples
        else Path(args.stress_report).with_name("stress.samples.jsonl")
    )
    callback_probe = (
        _load(Path(args.callback_probe)) if args.callback_probe else None
    )
    stress = _stress_result(stress_payload, callback_probe)
    stress["orders"] = _stress_order_evidence(stress_samples)
    result = {
        "schema_version": 1,
        "environment": "simulation",
        "generated_at": datetime.now().astimezone().isoformat(),
        "passed": bool(recovery["passed"] and stress["passed"] and afternoon["passed"]),
        "checks": {
            "fault_injection_recovery": recovery,
            "partial_session_5hz_stress": stress,
            "afternoon_reverse_repo": afternoon,
        },
    }
    atomic_write_json(Path(args.output), result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
