from __future__ import annotations

import argparse
import json
import threading
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Mapping

from repo_execution_core import atomic_write_json, safe_exception
from repo_simulation_interface_stress import PRIMARY_SYMBOL, StressMetrics


def _payload_shape(payload: object, symbol: str) -> str:
    if isinstance(payload, Mapping) and symbol in payload:
        value = payload.get(symbol)
        if isinstance(value, list):
            return "symbol_to_list"
        if isinstance(value, Mapping):
            return "symbol_to_mapping"
        return f"symbol_to_{type(value).__name__}"
    if isinstance(payload, Mapping):
        return "direct_mapping"
    return type(payload).__name__


def run_probe(*, symbol: str, duration_seconds: float) -> dict[str, object]:
    from xtquant import xtdata

    xtdata.enable_hello = False
    metrics = StressMetrics()
    shapes: Counter[str] = Counter()
    callbacks = 0
    lock = threading.Lock()

    def callback(payload: object) -> None:
        nonlocal callbacks
        with lock:
            callbacks += 1
            shapes[_payload_shape(payload, symbol)] += 1
        metrics.record_tick(symbol, payload)

    sequence = int(
        xtdata.subscribe_quote(
            symbol,
            period="tick",
            count=0,
            callback=callback,
        )
        or 0
    )
    if sequence <= 0:
        raise RuntimeError(f"quote subscription failed: {sequence}")
    started = datetime.now().astimezone()
    try:
        time.sleep(max(float(duration_seconds), 0.0))
    finally:
        xtdata.unsubscribe_quote(sequence)
    summary = metrics.summary(expected_cycles=0)
    counts = dict(summary.get("tick_counts") or {})
    unique = dict(summary.get("tick_unique_counts") or {})
    missing = dict(summary.get("tick_missing_timestamp_counts") or {})
    regressions = dict(summary.get("tick_timestamp_regressions") or {})
    passed = (
        callbacks > 0
        and int(counts.get(symbol, 0)) > 0
        and int(unique.get(symbol, 0)) >= 2
        and int(missing.get(symbol, 0)) == 0
        and int(regressions.get(symbol, 0)) == 0
    )
    return {
        "schema_version": 1,
        "environment": "market_data_only",
        "symbol": symbol,
        "started_at": started.isoformat(),
        "finished_at": datetime.now().astimezone().isoformat(),
        "duration_seconds": float(duration_seconds),
        "passed": passed,
        "callback_count": callbacks,
        "payload_shapes": dict(shapes),
        "tick_count": int(counts.get(symbol, 0)),
        "unique_timestamp_count": int(unique.get(symbol, 0)),
        "missing_timestamp_count": int(missing.get(symbol, 0)),
        "timestamp_regression_count": int(regressions.get(symbol, 0)),
        "source_interval_seconds": dict(
            summary.get("tick_source_interval_seconds") or {}
        ).get(symbol),
        "arrival_age_seconds": dict(
            summary.get("tick_arrival_age_seconds") or {}
        ).get(symbol),
        "account_id_persisted": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only probe for the real xtdata tick callback shape."
    )
    parser.add_argument("--symbol", default=PRIMARY_SYMBOL)
    parser.add_argument("--duration-seconds", type=float, default=10.0)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        report = run_probe(
            symbol=str(args.symbol).upper(),
            duration_seconds=float(args.duration_seconds),
        )
    except Exception as exc:  # noqa: BLE001
        report = {
            "schema_version": 1,
            "environment": "market_data_only",
            "symbol": str(args.symbol).upper(),
            "passed": False,
            "error": safe_exception(exc),
            "finished_at": datetime.now().astimezone().isoformat(),
            "account_id_persisted": False,
        }
    atomic_write_json(Path(args.output), report)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("passed") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
