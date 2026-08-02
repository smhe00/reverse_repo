from __future__ import annotations

import argparse
import ctypes
import json
import math
import random
import statistics
import threading
import time
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as clock_time
from pathlib import Path
from typing import Any

from repo_execution_core import (
    ExecutionMutex,
    atomic_write_json,
    is_exchange_trading_day,
    query_asset_strict,
    safe_exception,
    select_bound_account,
)
from repo_failure_alert import (
    load_optional_smtp_failure_notifier,
    send_standalone_failure,
)


STRATEGY_NAME = "simulation_interface_stress_5hz_v1"
PRIMARY_SYMBOL = "511880.SH"
QUOTE_SYMBOLS = (
    PRIMARY_SYMBOL,
    "511010.SH",
    "518880.SH",
    "513100.SH",
    "510300.SH",
    "600000.SH",
    "000001.SZ",
    "204001.SH",
    "131810.SZ",
)
ROUND_TRIP_SPECS = (
    ("money_etf", "511880.SH", 0.80),
    ("bond_etf", "511010.SH", 0.20),
    ("gold_etf", "518880.SH", 0.20),
    ("cross_border_etf", "513100.SH", 0.20),
)
TERMINAL_ORDER_STATUSES = {52, 53, 54, 55, 56, 57}
CANCELABLE_ORDER_STATUSES = {48, 49, 50, 55}
FILLED_ORDER_STATUS = 56
LOT_SIZE = 100
PRICE_TICK = 0.001


@dataclass(frozen=True)
class StressWindow:
    start: datetime
    end: datetime


class JsonlWriter:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a", encoding="utf-8", buffering=1)
        self._lock = threading.Lock()

    def write(self, kind: str, **payload: object) -> None:
        record = {
            "at": datetime.now().astimezone().isoformat(),
            "kind": kind,
            **payload,
        }
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self._handle.write(line + "\n")

    def close(self) -> None:
        with self._lock:
            self._handle.flush()
            self._handle.close()


class StressMetrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.cycle_latencies: list[float] = []
        self.cycle_lags: list[float] = []
        self.query_latencies: dict[str, list[float]] = defaultdict(list)
        self.query_errors: dict[str, int] = defaultdict(int)
        self.tick_counts: dict[str, int] = defaultdict(int)
        self.tick_timestamp_regressions: dict[str, int] = defaultdict(int)
        self.latest_tick_epoch_ms: dict[str, int] = {}
        self.order_callbacks = 0
        self.trade_callbacks = 0
        self.disconnect_callbacks = 0
        self.completed_round_trips: dict[str, int] = defaultdict(int)
        self.skipped_round_trips: dict[str, int] = defaultdict(int)
        self.round_trip_errors: list[str] = []
        self.maximum_consecutive_query_failures = 0
        self.current_consecutive_query_failures = 0
        self.position_residuals: dict[str, int] = {}
        self.unresolved_stress_order_count = 0

    def record_cycle(self, latency: float, lag: float, ok: bool) -> None:
        with self._lock:
            self.cycle_latencies.append(float(latency))
            self.cycle_lags.append(max(float(lag), 0.0))
            if ok:
                self.current_consecutive_query_failures = 0
            else:
                self.current_consecutive_query_failures += 1
                self.maximum_consecutive_query_failures = max(
                    self.maximum_consecutive_query_failures,
                    self.current_consecutive_query_failures,
                )

    def record_query(
        self,
        name: str,
        latency: float,
        error: BaseException | None = None,
    ) -> None:
        with self._lock:
            self.query_latencies[name].append(float(latency))
            if error is not None:
                self.query_errors[name] += 1

    def record_tick(self, symbol: str, payload: object) -> None:
        if isinstance(payload, Mapping) and isinstance(
            payload.get(symbol), Mapping
        ):
            payload = payload[symbol]
        raw_time = 0
        if isinstance(payload, Mapping):
            try:
                raw_time = int(payload.get("time") or 0)
            except (TypeError, ValueError):
                raw_time = 0
        with self._lock:
            previous = self.latest_tick_epoch_ms.get(symbol, 0)
            if raw_time and previous and raw_time < previous:
                self.tick_timestamp_regressions[symbol] += 1
            if raw_time:
                self.latest_tick_epoch_ms[symbol] = max(previous, raw_time)
            self.tick_counts[symbol] += 1

    def summary(self, *, expected_cycles: int) -> dict[str, object]:
        with self._lock:
            cycles = list(self.cycle_latencies)
            lags = list(self.cycle_lags)
            queries = {
                name: _distribution(values)
                for name, values in self.query_latencies.items()
            }
            cycle_count = len(cycles)
            late_cycles = sum(value > 0.05 for value in lags)
            slow_cycles = sum(value > 0.20 for value in cycles)
            total_query_count = sum(
                len(values) for values in self.query_latencies.values()
            )
            total_query_errors = sum(self.query_errors.values())
            return {
                "expected_cycles": int(expected_cycles),
                "cycle_count": cycle_count,
                "cycle_coverage_ratio": (
                    cycle_count / expected_cycles if expected_cycles else 0.0
                ),
                "cycle_latency_seconds": _distribution(cycles),
                "schedule_lag_seconds": _distribution(lags),
                "late_cycle_ratio_over_50ms": (
                    late_cycles / cycle_count if cycle_count else 1.0
                ),
                "slow_cycle_ratio_over_200ms": (
                    slow_cycles / cycle_count if cycle_count else 1.0
                ),
                "query_latency_seconds": queries,
                "query_count": total_query_count,
                "query_errors": dict(self.query_errors),
                "query_error_ratio": (
                    total_query_errors / total_query_count
                    if total_query_count
                    else 1.0
                ),
                "maximum_consecutive_query_failures": (
                    self.maximum_consecutive_query_failures
                ),
                "tick_counts": dict(self.tick_counts),
                "tick_timestamp_regressions": dict(
                    self.tick_timestamp_regressions
                ),
                "latest_tick_epoch_ms": dict(self.latest_tick_epoch_ms),
                "order_callbacks": self.order_callbacks,
                "trade_callbacks": self.trade_callbacks,
                "disconnect_callbacks": self.disconnect_callbacks,
                "completed_round_trips": dict(
                    self.completed_round_trips
                ),
                "skipped_round_trips": dict(self.skipped_round_trips),
                "round_trip_errors": list(self.round_trip_errors),
                "position_residuals": dict(self.position_residuals),
                "unresolved_stress_order_count": (
                    self.unresolved_stress_order_count
                ),
            }


def _distribution(values: Sequence[float]) -> dict[str, float | int | None]:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return {
            "count": 0,
            "min": None,
            "p50": None,
            "p95": None,
            "p99": None,
            "max": None,
            "mean": None,
        }
    return {
        "count": len(ordered),
        "min": ordered[0],
        "p50": _percentile(ordered, 0.50),
        "p95": _percentile(ordered, 0.95),
        "p99": _percentile(ordered, 0.99),
        "max": ordered[-1],
        "mean": statistics.fmean(ordered),
    }


def _percentile(ordered: Sequence[float], fraction: float) -> float:
    if not ordered:
        raise ValueError("percentile input cannot be empty")
    rank = (len(ordered) - 1) * float(fraction)
    lower = int(math.floor(rank))
    upper = int(math.ceil(rank))
    if lower == upper:
        return float(ordered[lower])
    weight = rank - lower
    return float(ordered[lower] * (1 - weight) + ordered[upper] * weight)


def _parse_clock(value: object) -> clock_time:
    text = str(value)
    try:
        parsed = clock_time.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("time must use HH:MM:SS") from exc
    if len(text) != 8 or parsed.tzinfo is not None:
        raise argparse.ArgumentTypeError("time must use HH:MM:SS")
    return parsed


def build_windows(
    trade_date: date,
    *,
    morning_start: clock_time,
    morning_end: clock_time,
    afternoon_start: clock_time,
    afternoon_end: clock_time,
    tzinfo: object,
) -> tuple[StressWindow, StressWindow]:
    values = (morning_start, morning_end, afternoon_start, afternoon_end)
    if not (
        clock_time(9, 41) <= morning_start < morning_end <= clock_time(11, 30)
        and clock_time(13, 0) <= afternoon_start < afternoon_end <= clock_time(15, 5)
    ):
        raise ValueError("stress windows overlap a reserved functional test")
    return tuple(
        StressWindow(
            datetime.combine(trade_date, start, tzinfo=tzinfo),
            datetime.combine(trade_date, end, tzinfo=tzinfo),
        )
        for start, end in (
            (morning_start, morning_end),
            (afternoon_start, afternoon_end),
        )
    )  # type: ignore[return-value]


def _expected_cycles(windows: Sequence[StressWindow], frequency_hz: float) -> int:
    return int(
        sum((window.end - window.start).total_seconds() for window in windows)
        * float(frequency_hz)
    )


def _window_for_now(
    now: datetime,
    windows: Sequence[StressWindow],
) -> StressWindow | None:
    for window in windows:
        if window.start <= now < window.end:
            return window
    return None


def _next_window_start(
    now: datetime,
    windows: Sequence[StressWindow],
) -> datetime | None:
    return next((window.start for window in windows if now < window.start), None)


def _query_timed(
    metrics: StressMetrics,
    name: str,
    function: Any,
    *args: object,
) -> tuple[object | None, BaseException | None]:
    started = time.perf_counter()
    try:
        value = function(*args)
        if value is None:
            raise RuntimeError(f"{name} query returned None")
        metrics.record_query(name, time.perf_counter() - started)
        return value, None
    except Exception as exc:  # noqa: BLE001
        metrics.record_query(name, time.perf_counter() - started, exc)
        return None, exc


def _required_sequence(value: object, label: str) -> list[object]:
    if value is None or isinstance(value, (str, bytes, Mapping)):
        raise RuntimeError(f"{label} query returned an invalid payload")
    try:
        return list(value)  # type: ignore[arg-type]
    except TypeError as exc:
        raise RuntimeError(
            f"{label} query returned a non-iterable payload"
        ) from exc


def _position_volume(positions: Sequence[object], symbol: str) -> int:
    return sum(
        int(getattr(position, "volume", 0) or 0)
        for position in positions
        if str(getattr(position, "stock_code", "")) == symbol
    )


def _fresh_quote(xtdata: object, symbol: str) -> tuple[float, float, int]:
    payload = (xtdata.get_full_tick([symbol]) or {}).get(symbol)
    if not isinstance(payload, Mapping):
        raise RuntimeError(f"quote is unavailable for {symbol}")
    bids = payload.get("bidPrice") or []
    asks = payload.get("askPrice") or []
    raw_time = int(payload.get("time") or 0)
    if not bids or not asks or raw_time <= 0:
        raise RuntimeError(f"two-sided fresh quote is unavailable for {symbol}")
    bid = float(bids[0])
    ask = float(asks[0])
    if bid <= 0 or ask <= 0 or ask < bid or ask / bid - 1 > 0.01:
        raise RuntimeError(f"quote is invalid or too wide for {symbol}")
    age = time.time() - raw_time / 1000
    if not 0 <= age <= 3:
        raise RuntimeError(f"quote is stale for {symbol}: {age:.3f}s")
    return bid, ask, raw_time


def _split_volume(total_volume: int, children: int = 5) -> list[int]:
    lots = int(total_volume) // LOT_SIZE
    if lots <= 0:
        return []
    child_count = min(max(int(children), 1), lots)
    base, extra = divmod(lots, child_count)
    return [
        (base + (1 if index < extra else 0)) * LOT_SIZE
        for index in range(child_count)
    ]


def _crossing_price(reference: float, *, buy: bool) -> float:
    adjustment = max(PRICE_TICK * 2, reference * 0.0002)
    value = reference + adjustment if buy else reference - adjustment
    return round(max(value, PRICE_TICK), 3)


def _wait_order(
    trader: object,
    account: object,
    order_id: int,
    *,
    timeout_seconds: float,
    stop_event: threading.Event,
) -> object:
    deadline = time.monotonic() + max(float(timeout_seconds), 1.0)
    latest = None
    while time.monotonic() < deadline and not stop_event.is_set():
        orders = _required_sequence(
            trader.query_stock_orders(account, False),
            "order list",
        )
        for order in orders:
            if int(getattr(order, "order_id", -1)) != int(order_id):
                continue
            latest = order
            if int(getattr(order, "order_status", 255)) in TERMINAL_ORDER_STATUSES:
                return order
        time.sleep(0.2)
    if latest is None:
        raise RuntimeError(f"submitted order {order_id} was never queryable")
    if int(getattr(latest, "order_status", 255)) in CANCELABLE_ORDER_STATUSES:
        trader.cancel_order_stock(account, int(order_id))
        cancel_deadline = time.monotonic() + 15
        while time.monotonic() < cancel_deadline:
            for order in _required_sequence(
                trader.query_stock_orders(account, False),
                "order list",
            ):
                if int(getattr(order, "order_id", -1)) == int(order_id):
                    latest = order
                    if int(getattr(order, "order_status", 255)) in TERMINAL_ORDER_STATUSES:
                        return order
            time.sleep(0.2)
    return latest


def _submit_children(
    *,
    trader: object,
    account: object,
    xtconstant: object,
    symbol: str,
    volumes: Sequence[int],
    price: float,
    buy: bool,
    cycle_number: int,
    stop_event: threading.Event,
    writer: JsonlWriter,
) -> int:
    traded_total = 0
    side = int(xtconstant.STOCK_BUY if buy else xtconstant.STOCK_SELL)
    for index, volume in enumerate(volumes, 1):
        if stop_event.is_set():
            break
        remark = (
            f"sim_stress_{cycle_number:03d}_"
            f"{'b' if buy else 's'}_{index:02d}"
        )
        order_id = int(
            trader.order_stock(
                account,
                symbol,
                side,
                int(volume),
                int(xtconstant.FIX_PRICE),
                float(price),
                STRATEGY_NAME,
                remark,
            )
        )
        writer.write(
            "order_submitted",
            symbol=symbol,
            side="BUY" if buy else "SELL",
            volume=volume,
            price=price,
            order_id=order_id,
            remark=remark,
        )
        if order_id <= 0:
            raise RuntimeError(f"order submission rejected for {symbol}: {order_id}")
        order = _wait_order(
            trader,
            account,
            order_id,
            timeout_seconds=8,
            stop_event=stop_event,
        )
        status = int(getattr(order, "order_status", 255))
        if status not in TERMINAL_ORDER_STATUSES:
            raise RuntimeError(
                f"order {order_id} did not reach a terminal state: {status}"
            )
        traded = int(getattr(order, "traded_volume", 0) or 0)
        traded_total += traded
        writer.write(
            "order_terminal",
            symbol=symbol,
            side="BUY" if buy else "SELL",
            order_id=order_id,
            status=status,
            requested_volume=volume,
            traded_volume=traded,
        )
        time.sleep(0.2)
    return traded_total


def _round_trip_once(
    *,
    trader: object,
    account: object,
    xtconstant: object,
    xtdata: object,
    asset_class: str,
    symbol: str,
    cash_ratio: float,
    cycle_number: int,
    stop_event: threading.Event,
    writer: JsonlWriter,
) -> None:
    positions = _required_sequence(
        trader.query_stock_positions(account),
        "position list",
    )
    baseline = _position_volume(positions, symbol)
    available_cash = query_asset_strict(
        trader,
        account,
    ).conservative_available_cash
    bid, ask, _ = _fresh_quote(xtdata, symbol)
    budget = max(available_cash, 0.0) * float(cash_ratio)
    volume = int(budget / _crossing_price(ask, buy=True) / LOT_SIZE) * LOT_SIZE
    volumes = _split_volume(volume)
    if not volumes:
        raise RuntimeError(
            f"insufficient simulation cash for {asset_class} {symbol}"
        )
    bought = _submit_children(
        trader=trader,
        account=account,
        xtconstant=xtconstant,
        symbol=symbol,
        volumes=volumes,
        price=_crossing_price(ask, buy=True),
        buy=True,
        cycle_number=cycle_number,
        stop_event=stop_event,
        writer=writer,
    )
    if bought <= 0:
        raise RuntimeError(f"no buy fill was produced for {asset_class} {symbol}")
    sold_total = 0
    for attempt in range(3):
        remaining = bought - sold_total
        if remaining <= 0:
            break
        bid, _, _ = _fresh_quote(xtdata, symbol)
        sold_total += _submit_children(
            trader=trader,
            account=account,
            xtconstant=xtconstant,
            symbol=symbol,
            volumes=_split_volume(remaining),
            price=_crossing_price(bid, buy=False),
            buy=False,
            cycle_number=cycle_number * 10 + attempt,
            stop_event=stop_event,
            writer=writer,
        )
    final_positions = _required_sequence(
        trader.query_stock_positions(account),
        "position list",
    )
    final_volume = _position_volume(final_positions, symbol)
    if sold_total != bought or final_volume != baseline:
        raise RuntimeError(
            f"round trip left residual {symbol}: bought={bought}, "
            f"sold={sold_total}, baseline={baseline}, final={final_volume}"
        )


def _trade_worker(
    *,
    trader: object,
    account: object,
    xtconstant: object,
    xtdata: object,
    windows: Sequence[StressWindow],
    interval_minutes: int,
    stop_new_orders_at: datetime,
    stop_event: threading.Event,
    metrics: StressMetrics,
    writer: JsonlWriter,
) -> None:
    cycle_number = 0
    next_at = windows[0].start + timedelta(minutes=3)
    while not stop_event.is_set() and next_at < stop_new_orders_at:
        now = datetime.now().astimezone()
        active = _window_for_now(now, windows)
        if active is None or now < next_at:
            stop_event.wait(min(max((next_at - now).total_seconds(), 0.2), 1.0))
            if now >= windows[0].end and now < windows[1].start:
                next_at = max(next_at, windows[1].start + timedelta(minutes=3))
            continue
        asset_class, symbol, ratio = ROUND_TRIP_SPECS[
            cycle_number % len(ROUND_TRIP_SPECS)
        ]
        cycle_number += 1
        try:
            _round_trip_once(
                trader=trader,
                account=account,
                xtconstant=xtconstant,
                xtdata=xtdata,
                asset_class=asset_class,
                symbol=symbol,
                cash_ratio=ratio,
                cycle_number=cycle_number,
                stop_event=stop_event,
                writer=writer,
            )
            with metrics._lock:
                metrics.completed_round_trips[asset_class] += 1
            writer.write(
                "round_trip_complete",
                asset_class=asset_class,
                symbol=symbol,
                cash_ratio=ratio,
            )
        except Exception as exc:  # noqa: BLE001
            message = f"{asset_class} {symbol}: {safe_exception(exc)}"
            with metrics._lock:
                metrics.round_trip_errors.append(message)
                metrics.skipped_round_trips[asset_class] += 1
            writer.write("round_trip_error", error=message)
        next_at += timedelta(minutes=max(interval_minutes, 5))


def _working_set_bytes() -> int | None:
    if not hasattr(ctypes, "windll"):
        return None

    class Counters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    result = ctypes.windll.psapi.GetProcessMemoryInfo(  # type: ignore[attr-defined]
        ctypes.windll.kernel32.GetCurrentProcess(),  # type: ignore[attr-defined]
        ctypes.byref(counters),
        counters.cb,
    )
    return int(counters.WorkingSetSize) if result else None


def _evaluate(summary: Mapping[str, object]) -> tuple[bool, list[str]]:
    failures: list[str] = []
    if float(summary.get("cycle_coverage_ratio", 0)) < 0.98:
        failures.append("5Hz cycle coverage is below 98%")
    if float(summary.get("slow_cycle_ratio_over_200ms", 1)) > 0.01:
        failures.append("more than 1% of query cycles exceed 200ms")
    if float(summary.get("query_error_ratio", 1)) > 0.001:
        failures.append("broker query error ratio exceeds 0.1%")
    if int(summary.get("maximum_consecutive_query_failures", 999)) > 2:
        failures.append("more than two consecutive broker query cycles failed")
    if int(summary.get("disconnect_callbacks", 0)):
        failures.append("QMT disconnect callback was observed")
    tick_counts = dict(summary.get("tick_counts") or {})
    if int(tick_counts.get(PRIMARY_SYMBOL, 0)) <= 0:
        failures.append("primary money-ETF tick callback was never observed")
    regressions = dict(summary.get("tick_timestamp_regressions") or {})
    if sum(int(value) for value in regressions.values()) > 0:
        failures.append("tick timestamp regression was observed")
    completed = dict(summary.get("completed_round_trips") or {})
    if int(completed.get("money_etf", 0)) <= 0:
        failures.append("money-ETF T+0 round trip did not complete")
    if sum(1 for value in completed.values() if int(value) > 0) < 3:
        failures.append("fewer than three T+0 asset classes completed")
    if int(summary.get("order_callbacks", 0)) <= 0:
        failures.append("no order callback was observed")
    if int(summary.get("trade_callbacks", 0)) <= 0:
        failures.append("no trade callback was observed")
    residuals = dict(summary.get("position_residuals") or {})
    if any(int(value) != 0 for value in residuals.values()):
        failures.append("a stress-test symbol has a residual position")
    if int(summary.get("unresolved_stress_order_count", 999)):
        failures.append("a stress-test order is still unresolved")
    return not failures, failures


def run_stress(args: argparse.Namespace) -> dict[str, object]:
    from xtquant import xtconstant, xtdata, xttype
    from xtquant.xttrader import XtQuantTrader, XtQuantTraderCallback

    now = datetime.now().astimezone()
    trade_date = date.fromisoformat(args.trade_date)
    if trade_date != now.date():
        raise ValueError("trade date must equal the local calendar date")
    qmt_path = Path(args.qmt_path).resolve()
    if "模拟" not in str(qmt_path) or not qmt_path.is_dir():
        raise ValueError("stress test requires an explicit simulation QMT path")
    frequency = float(args.frequency_hz)
    if not math.isfinite(frequency) or frequency != 5.0:
        raise ValueError("this stress test is fixed at exactly 5Hz")
    windows = build_windows(
        trade_date,
        morning_start=args.morning_start,
        morning_end=args.morning_end,
        afternoon_start=args.afternoon_start,
        afternoon_end=args.afternoon_end,
        tzinfo=now.tzinfo,
    )
    stop_new_orders_at = datetime.combine(
        trade_date,
        args.stop_new_orders,
        tzinfo=now.tzinfo,
    )
    if not windows[1].start < stop_new_orders_at <= windows[1].end - timedelta(minutes=5):
        raise ValueError("stop-new-orders must leave at least five cleanup minutes")

    metrics = StressMetrics()
    writer = JsonlWriter(Path(args.samples))
    stop_event = threading.Event()
    subscriptions: list[int] = []
    trader: Any = None
    worker: threading.Thread | None = None
    baseline_positions: dict[str, int] = {}
    started_wall = time.perf_counter()
    started_cpu = time.process_time()
    report: dict[str, object] = {
        "schema_version": 1,
        "strategy": STRATEGY_NAME,
        "environment": "simulation",
        "trade_date": trade_date.isoformat(),
        "frequency_hz": frequency,
        "windows": [
            {"start": window.start.isoformat(), "end": window.end.isoformat()}
            for window in windows
        ],
        "quote_symbols": list(QUOTE_SYMBOLS),
        "started_at": now.isoformat(),
        "passed": False,
    }

    class Callback(XtQuantTraderCallback):
        def on_stock_order(self, order: object) -> None:
            del order
            with metrics._lock:
                metrics.order_callbacks += 1

        def on_stock_trade(self, trade: object) -> None:
            del trade
            with metrics._lock:
                metrics.trade_callbacks += 1

        def on_disconnected(self) -> None:
            with metrics._lock:
                metrics.disconnect_callbacks += 1
            writer.write("qmt_disconnected")

    try:
        if not is_exchange_trading_day(xtdata, trade_date):
            raise RuntimeError("configured date is not an exchange trading day")
        xtdata.enable_hello = False
        trader = XtQuantTrader(
            str(qmt_path),
            random.randint(100_000_000, 999_999_999),
            Callback(),
        )
        trader.start()
        connect_started = time.perf_counter()
        connect_result = int(trader.connect())
        report["connect_latency_seconds"] = time.perf_counter() - connect_started
        if connect_result != 0:
            raise RuntimeError(f"simulation QMT connection failed: {connect_result}")
        account, binding = select_bound_account(
            trader,
            xtconstant,
            xttype,
            environment="simulation",
            qmt_path=qmt_path,
            binding_path=Path(args.account_binding),
        )
        report["account_label"] = binding.label
        report["account_id_persisted"] = False
        if int(trader.subscribe(account)) != 0:
            raise RuntimeError("simulation account subscription failed")
        positions = _required_sequence(
            trader.query_stock_positions(account),
            "position list",
        )
        baseline_positions = {
            symbol: _position_volume(positions, symbol)
            for _, symbol, _ in ROUND_TRIP_SPECS
        }

        for symbol in QUOTE_SYMBOLS:
            def callback(payload: object, *, _symbol: str = symbol) -> None:
                metrics.record_tick(_symbol, payload)

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
                writer.write("quote_subscription_failed", symbol=symbol)
            else:
                subscriptions.append(sequence)
        if not subscriptions:
            raise RuntimeError("all quote subscriptions failed")

        worker = threading.Thread(
            target=_trade_worker,
            kwargs={
                "trader": trader,
                "account": account,
                "xtconstant": xtconstant,
                "xtdata": xtdata,
                "windows": windows,
                "interval_minutes": int(args.trade_interval_minutes),
                "stop_new_orders_at": stop_new_orders_at,
                "stop_event": stop_event,
                "metrics": metrics,
                "writer": writer,
            },
            name="simulation-stress-orders",
            daemon=True,
        )
        worker.start()

        interval = 1.0 / frequency
        cycle_number = 0
        last_resource_sample = time.monotonic()
        next_due: float | None = None
        while datetime.now().astimezone() < windows[-1].end:
            current = datetime.now().astimezone()
            active = _window_for_now(current, windows)
            if active is None:
                next_start = _next_window_start(current, windows)
                if next_start is None:
                    break
                wait = max((next_start - current).total_seconds(), 0.0)
                stop_event.wait(min(wait, 1.0))
                next_due = None
                continue
            monotonic_now = time.monotonic()
            if next_due is None:
                next_due = monotonic_now
            if monotonic_now < next_due:
                stop_event.wait(next_due - monotonic_now)
            cycle_started = time.perf_counter()
            lag = max(time.monotonic() - next_due, 0.0)
            cycle_ok = True
            _, asset_error = _query_timed(
                metrics, "asset", trader.query_stock_asset, account
            )
            _, orders_error = _query_timed(
                metrics, "orders", trader.query_stock_orders, account, False
            )
            cycle_ok = asset_error is None and orders_error is None
            if cycle_number % 5 == 0:
                _, positions_error = _query_timed(
                    metrics,
                    "positions",
                    trader.query_stock_positions,
                    account,
                )
                _, trades_error = _query_timed(
                    metrics,
                    "trades",
                    trader.query_stock_trades,
                    account,
                )
                cycle_ok = (
                    cycle_ok
                    and positions_error is None
                    and trades_error is None
                )
            latency = time.perf_counter() - cycle_started
            metrics.record_cycle(latency, lag, cycle_ok)
            if cycle_number % 25 == 0 or not cycle_ok:
                writer.write(
                    "query_cycle",
                    cycle=cycle_number,
                    latency_seconds=latency,
                    schedule_lag_seconds=lag,
                    success=cycle_ok,
                )
            if time.monotonic() - last_resource_sample >= 60:
                writer.write(
                    "resource_sample",
                    working_set_bytes=_working_set_bytes(),
                    process_cpu_seconds=time.process_time() - started_cpu,
                    elapsed_seconds=time.perf_counter() - started_wall,
                )
                last_resource_sample = time.monotonic()
                snapshot = metrics.summary(
                    expected_cycles=_expected_cycles(windows, frequency)
                )
                atomic_write_json(
                    Path(args.checkpoint),
                    {**report, "intermediate": True, "metrics": snapshot},
                )
            cycle_number += 1
            next_due += interval
            if time.monotonic() - next_due > interval:
                missed = int((time.monotonic() - next_due) // interval)
                next_due += missed * interval

        stop_event.set()
        if worker is not None:
            worker.join(timeout=30)
        final_orders = _required_sequence(
            trader.query_stock_orders(account, False),
            "order list",
        )
        for order in final_orders:
            remark = str(getattr(order, "order_remark", "") or "")
            status = int(getattr(order, "order_status", 255))
            if (
                remark.startswith("sim_stress_")
                and status in CANCELABLE_ORDER_STATUSES
            ):
                trader.cancel_order_stock(
                    account,
                    int(getattr(order, "order_id")),
                )
        unresolved: list[object] = []
        cleanup_deadline = time.monotonic() + 15
        while time.monotonic() < cleanup_deadline:
            final_orders = _required_sequence(
                trader.query_stock_orders(account, False),
                "order list",
            )
            unresolved = [
                order
                for order in final_orders
                if str(getattr(order, "order_remark", "") or "").startswith(
                    "sim_stress_"
                )
                and int(getattr(order, "order_status", 255))
                not in TERMINAL_ORDER_STATUSES
            ]
            if not unresolved:
                break
            time.sleep(0.2)
        with metrics._lock:
            metrics.unresolved_stress_order_count = len(unresolved)
        final_positions = _required_sequence(
            trader.query_stock_positions(account),
            "position list",
        )
        with metrics._lock:
            metrics.position_residuals = {
                symbol: _position_volume(final_positions, symbol) - baseline
                for symbol, baseline in baseline_positions.items()
            }
        summary = metrics.summary(
            expected_cycles=_expected_cycles(windows, frequency)
        )
        passed, failures = _evaluate(summary)
        report.update(
            {
                "passed": passed,
                "failures": failures,
                "metrics": summary,
                "process": {
                    "elapsed_seconds": time.perf_counter() - started_wall,
                    "cpu_seconds": time.process_time() - started_cpu,
                    "working_set_bytes": _working_set_bytes(),
                },
            }
        )
        return report
    except Exception as exc:  # noqa: BLE001
        report["error"] = safe_exception(exc)
        report["failures"] = ["stress executor failed before completion"]
        report["metrics"] = metrics.summary(
            expected_cycles=_expected_cycles(windows, frequency)
        )
        return report
    finally:
        stop_event.set()
        if worker is not None and worker.is_alive():
            worker.join(timeout=5)
        for sequence in subscriptions:
            try:
                xtdata.unsubscribe_quote(sequence)
            except Exception:
                pass
        if trader is not None:
            try:
                trader.stop()
            except Exception:
                pass
        report["finished_at"] = datetime.now().astimezone().isoformat()
        atomic_write_json(Path(args.output), report)
        writer.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="One-day simulation-only miniQMT 5Hz interface stress test."
    )
    parser.add_argument("--qmt-path", required=True)
    parser.add_argument("--account-binding", required=True)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--samples", required=True)
    parser.add_argument("--mutex", required=True)
    parser.add_argument("--alert-config", default="")
    parser.add_argument("--frequency-hz", type=float, default=5.0)
    parser.add_argument("--morning-start", type=_parse_clock, default=clock_time(9, 42))
    parser.add_argument("--morning-end", type=_parse_clock, default=clock_time(11, 30))
    parser.add_argument("--afternoon-start", type=_parse_clock, default=clock_time(13, 0))
    parser.add_argument("--afternoon-end", type=_parse_clock, default=clock_time(15, 5))
    parser.add_argument("--stop-new-orders", type=_parse_clock, default=clock_time(14, 50))
    parser.add_argument("--trade-interval-minutes", type=int, default=20)
    args = parser.parse_args()

    notifier = None
    if args.alert_config:
        notifier, alert_warning = load_optional_smtp_failure_notifier(
            Path(args.alert_config)
        )
        if alert_warning:
            print(
                "WARNING: optional failure email is disabled: "
                f"{alert_warning}"
            )
    try:
        with ExecutionMutex(Path(args.mutex)):
            report = run_stress(args)
    except Exception as exc:  # noqa: BLE001
        report = {
            "schema_version": 1,
            "strategy": STRATEGY_NAME,
            "environment": "simulation",
            "passed": False,
            "error": safe_exception(exc),
            "finished_at": datetime.now().astimezone().isoformat(),
        }
        atomic_write_json(Path(args.output), report)
    if report.get("passed") is not True and notifier is not None:
        try:
            send_standalone_failure(
                notifier,
                strategy=STRATEGY_NAME,
                trade_date=str(args.trade_date),
                environment="simulation",
                reason="simulation interface stress test failed",
                journal_path=Path(args.output),
                error=RuntimeError(
                    str(report.get("failures") or report.get("error"))
                ),
            )
        except Exception as exc:  # noqa: BLE001
            print(
                "WARNING: optional failure email could not be delivered: "
                f"{type(exc).__name__}: {exc}"
            )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("passed") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
