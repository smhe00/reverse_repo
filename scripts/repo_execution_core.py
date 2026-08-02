from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from collections.abc import Iterable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, tzinfo
from datetime import time as clock_time
from enum import Enum
from pathlib import Path
from queue import Empty, SimpleQueue
from typing import Any, Callable, TypeVar

DEFAULT_FIRST_EXECUTION_TIME = "09:30:42"
DEFAULT_SECOND_EXECUTION_TIME = "15:10:00"
DEFAULT_FIRST_CASH_USAGE_RATIO = 0.90
DEFAULT_SECOND_CASH_USAGE_RATIO = 1.0


class BrokerUpdateSignal:
    """Wake an executor when one of its broker orders changes."""

    def __init__(self, *, strategy_name: str, remark_prefix: str) -> None:
        self.strategy_name = str(strategy_name)
        self.remark_prefix = str(remark_prefix)
        self._queue: SimpleQueue[int] = SimpleQueue()

    def on_order(self, order: object) -> None:
        self._notify_if_owned(order)

    def on_trade(self, trade: object) -> None:
        self._notify_if_owned(trade)

    def wait(self, timeout_seconds: float) -> bool:
        try:
            self._queue.get(timeout=max(float(timeout_seconds), 0.0))
        except Empty:
            return False
        while True:
            try:
                self._queue.get_nowait()
            except Empty:
                return True

    def _notify_if_owned(self, payload: object) -> None:
        strategy = str(getattr(payload, "strategy_name", "") or "")
        remark = str(getattr(payload, "order_remark", "") or "")
        if (
            strategy == self.strategy_name
            and remark.startswith(self.remark_prefix)
        ):
            self._queue.put(1)


def reverse_repo_strategy_config(
    path: Path,
) -> dict[str, object]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExecutionSafetyError(
            f"runtime strategy configuration is unreadable: {path}"
        ) from exc
    if not isinstance(payload, dict):
        raise ExecutionSafetyError(
            "runtime strategy configuration must be a JSON object"
        )
    first = _strategy_clock_value(
        _strategy_alias_value(
            payload,
            name="first_execution_time",
            legacy_name="morning_execution_time",
            default=DEFAULT_FIRST_EXECUTION_TIME,
        ),
        name="first_execution_time",
    )
    if not is_first_execution_time(first):
        raise ExecutionSafetyError(
            "first_execution_time must be from 09:30:00 through "
            "11:28:00 or from 13:00:00 through 15:28:00"
        )
    second = _strategy_clock_value(
        _strategy_alias_value(
            payload,
            name="second_execution_time",
            legacy_name="afternoon_execution_time",
            default=DEFAULT_SECOND_EXECUTION_TIME,
        ),
        name="second_execution_time",
    )
    if not is_repo_continuous_time(second):
        raise ExecutionSafetyError(
            "second_execution_time must be from 09:30:00 before "
            "11:30:00 or from 13:00:00 before 15:30:00"
        )
    if _clock_seconds(second) - _clock_seconds(first) < 5 * 60:
        raise ExecutionSafetyError(
            "second_execution_time must be at least five minutes "
            "after first_execution_time"
        )
    first_ratio = _strategy_ratio_value(
        _strategy_alias_value(
            payload,
            name="first_cash_usage_ratio",
            legacy_name="morning_cash_usage_ratio",
            default=DEFAULT_FIRST_CASH_USAGE_RATIO,
        ),
        name="first_cash_usage_ratio",
    )
    second_ratio = _strategy_ratio_value(
        _strategy_alias_value(
            payload,
            name="second_cash_usage_ratio",
            legacy_name="afternoon_cash_usage_ratio",
            default=DEFAULT_SECOND_CASH_USAGE_RATIO,
        ),
        name="second_cash_usage_ratio",
    )
    return {
        "first_execution_time": first.isoformat(),
        "second_execution_time": second.isoformat(),
        "first_cash_usage_ratio": first_ratio,
        "second_cash_usage_ratio": second_ratio,
    }


def reverse_repo_strategy_config_sha256(path: Path) -> str:
    canonical = json.dumps(
        reverse_repo_strategy_config(path),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def reverse_repo_schedule_config_sha256(path: Path) -> str:
    configuration = reverse_repo_strategy_config(path)
    canonical = json.dumps(
        {
            "first_execution_time": configuration["first_execution_time"],
            "second_execution_time": configuration["second_execution_time"],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def _strategy_ratio_value(value: object, *, name: str) -> float:
    try:
        ratio = float(value)
    except (TypeError, ValueError) as exc:
        raise ExecutionSafetyError(f"{name} must be a number") from exc
    if not math.isfinite(ratio) or not 0 <= ratio <= 1:
        raise ExecutionSafetyError(f"{name} must be from 0 through 1")
    return ratio


def _strategy_clock_value(
    value: object,
    *,
    name: str,
) -> clock_time:
    text = str(value)
    if re.fullmatch(r"\d{2}:\d{2}:\d{2}", text) is None:
        raise ExecutionSafetyError(
            f"{name} must use HH:MM:SS"
        )
    try:
        parsed = clock_time.fromisoformat(text)
    except ValueError as exc:
        raise ExecutionSafetyError(
            f"{name} must use HH:MM:SS"
        ) from exc
    if parsed.tzinfo is not None:
        raise ExecutionSafetyError(
            f"{name} must not include a timezone"
        )
    return parsed


def _strategy_alias_value(
    payload: Mapping[str, object],
    *,
    name: str,
    legacy_name: str,
    default: object,
) -> object:
    current = payload.get(name)
    legacy = payload.get(legacy_name)
    if current is not None and legacy is not None:
        if str(current) != str(legacy):
            raise ExecutionSafetyError(
                f"{name} conflicts with legacy {legacy_name}"
            )
        return current
    if current is not None:
        return current
    if legacy is not None:
        return legacy
    return default


def _clock_seconds(value: clock_time) -> int:
    return value.hour * 3600 + value.minute * 60 + value.second


def is_repo_continuous_time(value: clock_time) -> bool:
    return (
        clock_time(9, 30) <= value < clock_time(11, 30)
        or clock_time(13, 0) <= value < clock_time(15, 30)
    )


def is_first_execution_time(value: clock_time) -> bool:
    return (
        clock_time(9, 30) <= value <= clock_time(11, 28)
        or clock_time(13, 0) <= value <= clock_time(15, 28)
    )


def first_execution_deadline(
    trade_date: date,
    first_time: clock_time,
    *,
    timezone: tzinfo | None,
) -> datetime:
    if not is_first_execution_time(first_time):
        raise ExecutionSafetyError("invalid first execution time")
    start = datetime.combine(trade_date, first_time, tzinfo=timezone)
    session_end = clock_time(11, 30)
    if first_time >= clock_time(13, 0):
        session_end = clock_time(15, 30)
    return min(
        start + timedelta(minutes=5),
        datetime.combine(trade_date, session_end, tzinfo=timezone),
    )

GC001 = "204001.SH"
R001 = "131810.SZ"
REPO_SYMBOLS = (GC001, R001)
REPO_NAMES = {GC001: "GC001", R001: "R-001"}
QMT_FACE_VALUE_YUAN = 100
PRINCIPAL_STEP_YUAN = 1_000
REPO_TICK_PERCENT = 0.005

ORDER_UNREPORTED = 48
ORDER_WAIT_REPORTING = 49
ORDER_REPORTED = 50
ORDER_REPORTED_CANCEL = 51
ORDER_PARTSUCC_CANCEL = 52
ORDER_PART_CANCEL = 53
ORDER_CANCELED = 54
ORDER_PART_SUCC = 55
ORDER_SUCCEEDED = 56
ORDER_JUNK = 57
ORDER_UNKNOWN = 255

ACTIVE_ORDER_STATUSES = {
    ORDER_UNREPORTED,
    ORDER_WAIT_REPORTING,
    ORDER_REPORTED,
    ORDER_PART_SUCC,
}
PENDING_CANCEL_STATUSES = {
    ORDER_REPORTED_CANCEL,
    ORDER_PARTSUCC_CANCEL,
}
TERMINAL_ORDER_STATUSES = {
    ORDER_PART_CANCEL,
    ORDER_CANCELED,
    ORDER_SUCCEEDED,
    ORDER_JUNK,
}
KNOWN_ORDER_STATUSES = (
    ACTIVE_ORDER_STATUSES
    | PENDING_CANCEL_STATUSES
    | TERMINAL_ORDER_STATUSES
)


class ExecutionSafetyError(RuntimeError):
    """Base class for fail-closed execution errors."""


class BrokerQueryAmbiguous(ExecutionSafetyError):
    """The broker did not provide a trustworthy query result."""


class AccountBindingError(ExecutionSafetyError):
    """The connected account is not the explicitly bound environment."""


class ConcurrentExecutionError(ExecutionSafetyError):
    """Another reverse-repo execution process owns the mutex."""


class QuoteValidationError(ExecutionSafetyError):
    """The order book cannot pass freshness or structural checks."""


class UnresolvedOrderError(ExecutionSafetyError):
    """An accepted or possibly accepted order lacks a confirmed terminal state."""


class OrderClass(str, Enum):
    ACTIVE = "active"
    CANCEL_PENDING = "cancel_pending"
    FILLED = "filled"
    TERMINAL_PARTIAL = "terminal_partial"
    CANCELED_ZERO = "canceled_zero"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class AccountBinding:
    environment: str
    label: str
    account_id_fingerprint: str
    qmt_path_fingerprint: str | None


@dataclass(frozen=True)
class CashSnapshot:
    cash_field: float | None
    available_cash_field: float | None
    total_asset: float | None
    market_value: float | None
    frozen_cash: float | None
    derived_cash: float | None
    conservative_available_cash: float


@dataclass(frozen=True)
class QuoteBook:
    symbol: str
    quote_time_epoch_ms: int
    quote_time: str
    quote_age_seconds: float
    bid_prices: tuple[float, ...]
    bid_volumes: tuple[int, ...]
    ask_prices: tuple[float, ...]
    ask_volumes: tuple[int, ...]


@dataclass(frozen=True)
class BookPlan:
    symbol: str
    name: str
    requested_volume: int
    executable_volume: int
    covers_requested_volume: bool
    principal_yuan: int
    limit_rate_percent: float
    expected_vwap_percent: float
    levels: tuple[Mapping[str, float | int], ...]
    quote_time: str
    quote_age_seconds: float


@dataclass(frozen=True)
class CashLedgerUpdate:
    previously_accounted_principal_yuan: int
    broker_filled_principal_yuan: int
    newly_filled_principal_yuan: int
    cash_cap_yuan: float | None


@dataclass(frozen=True)
class OrderView:
    order_id: int
    symbol: str
    order_type: int
    status: int
    order_volume: int
    traded_volume: int
    traded_price: float
    limit_price: float
    status_msg: str
    strategy_name: str
    remark: str

    @classmethod
    def from_qmt(cls, order: object) -> OrderView:
        return cls(
            order_id=int(getattr(order, "order_id", 0) or 0),
            symbol=str(getattr(order, "stock_code", "") or "").upper(),
            order_type=int(getattr(order, "order_type", -1)),
            status=int(getattr(order, "order_status", -1)),
            order_volume=max(
                int(getattr(order, "order_volume", 0) or 0),
                0,
            ),
            traded_volume=max(
                int(getattr(order, "traded_volume", 0) or 0),
                0,
            ),
            traded_price=float(
                getattr(order, "traded_price", 0) or 0
            ),
            limit_price=float(getattr(order, "price", 0) or 0),
            status_msg=str(getattr(order, "status_msg", "") or ""),
            strategy_name=str(
                getattr(order, "strategy_name", "") or ""
            ),
            remark=str(getattr(order, "order_remark", "") or ""),
        )

    @property
    def principal_yuan(self) -> int:
        return self.traded_volume * QMT_FACE_VALUE_YUAN

    @property
    def classification(self) -> OrderClass:
        return classify_order(self)

    def safe_payload(self) -> dict[str, object]:
        payload = asdict(self)
        payload["classification"] = self.classification.value
        payload["filled_principal_yuan"] = self.principal_yuan
        return payload


def classify_order(order: OrderView) -> OrderClass:
    if (
        order.order_volume > 0
        and order.traded_volume >= order.order_volume
    ):
        return OrderClass.FILLED
    if order.status in ACTIVE_ORDER_STATUSES:
        return OrderClass.ACTIVE
    if order.status in PENDING_CANCEL_STATUSES:
        return OrderClass.CANCEL_PENDING
    if order.status == ORDER_SUCCEEDED:
        return OrderClass.FILLED
    if order.status == ORDER_JUNK:
        return OrderClass.REJECTED
    if order.status in {ORDER_PART_CANCEL, ORDER_CANCELED}:
        if order.traded_volume > 0:
            return OrderClass.TERMINAL_PARTIAL
        return OrderClass.CANCELED_ZERO
    return OrderClass.UNKNOWN


def validate_principal(principal_yuan: int) -> int:
    principal = int(principal_yuan)
    if principal < PRINCIPAL_STEP_YUAN:
        raise ValueError("principal must be at least CNY 1,000")
    if principal % PRINCIPAL_STEP_YUAN:
        raise ValueError("principal must be a CNY 1,000 multiple")
    return principal


def principal_to_qmt_volume(principal_yuan: int) -> int:
    return validate_principal(principal_yuan) // QMT_FACE_VALUE_YUAN


def qmt_volume_to_principal(volume: int) -> int:
    units = int(volume)
    if units < 0:
        raise ValueError("QMT volume cannot be negative")
    return units * QMT_FACE_VALUE_YUAN


def floor_principal(available_cash: float, ratio: float = 1.0) -> int:
    cash = float(available_cash)
    fraction = float(ratio)
    if (
        not math.isfinite(cash)
        or cash <= 0
        or not math.isfinite(fraction)
        or not 0 < fraction <= 1
    ):
        return 0
    return int(
        (cash * fraction) // PRINCIPAL_STEP_YUAN
    ) * PRINCIPAL_STEP_YUAN


def assert_order_budget(
    *,
    principal_yuan: int,
    verified_available_cash_yuan: float,
    maximum_ratio: float,
) -> None:
    principal = validate_principal(principal_yuan)
    ceiling = floor_principal(
        verified_available_cash_yuan,
        maximum_ratio,
    )
    if principal > ceiling:
        raise ExecutionSafetyError(
            f"principal {principal} exceeds verified budget {ceiling}"
        )


def normalize_repo_rate(value: float) -> float:
    rate = float(value)
    if not math.isfinite(rate) or rate <= 0:
        raise QuoteValidationError("reverse-repo rate must be positive")
    ticks = round(rate / REPO_TICK_PERCENT)
    normalized = round(ticks * REPO_TICK_PERCENT, 3)
    if abs(rate - normalized) > 1e-6:
        raise QuoteValidationError(
            f"rate {rate} is not on the 0.005-percent tick"
        )
    return normalized


def read_cash_snapshot(asset: object | None) -> CashSnapshot:
    if asset is None:
        raise BrokerQueryAmbiguous("asset query returned None")
    cash = _finite_nonnegative(getattr(asset, "cash", None))
    available = _finite_nonnegative(
        getattr(asset, "available_cash", None)
    )
    total = _finite_nonnegative(getattr(asset, "total_asset", None))
    market = _finite_nonnegative(getattr(asset, "market_value", None))
    frozen = _finite_nonnegative(getattr(asset, "frozen_cash", None))
    candidates: list[float] = []
    if cash is not None:
        candidates.append(cash)
    if available is not None:
        candidates.append(available)
    derived = None
    if total is not None and market is not None:
        derived = max(0.0, total - market - (frozen or 0.0))
        candidates.append(derived)
    if not candidates:
        raise BrokerQueryAmbiguous(
            "asset contains no finite nonnegative cash field"
        )
    return CashSnapshot(
        cash_field=cash,
        available_cash_field=available,
        total_asset=total,
        market_value=market,
        frozen_cash=frozen,
        derived_cash=derived,
        conservative_available_cash=min(candidates),
    )


def reconcile_cash_cap(
    reported_cash: float,
    cash_cap: float | None,
    *,
    tolerance_yuan: float = 1.0,
) -> tuple[float, float | None]:
    cash = max(float(reported_cash), 0.0)
    if cash_cap is None:
        return cash, None
    cap = max(float(cash_cap), 0.0)
    if cash <= cap + tolerance_yuan:
        return cash, None
    return min(cash, cap), cap


def reconcile_broker_fills(
    *,
    previously_accounted_principal_yuan: int,
    broker_filled_principal_yuan: int,
    cash_cap_yuan: float | None,
    intent_available_cash_yuan: float | None,
) -> CashLedgerUpdate:
    accounted = int(previously_accounted_principal_yuan)
    broker_filled = int(broker_filled_principal_yuan)
    if accounted < 0 or broker_filled < 0:
        raise ExecutionSafetyError(
            "cumulative filled principal cannot be negative"
        )
    delta = broker_filled - accounted
    if delta < 0:
        raise ExecutionSafetyError(
            "broker cumulative fills moved backwards"
        )
    cap = (
        max(float(cash_cap_yuan), 0.0)
        if cash_cap_yuan is not None
        else None
    )
    if delta > 0:
        if cap is not None:
            base = cap
        elif intent_available_cash_yuan is not None:
            base = max(float(intent_available_cash_yuan), 0.0)
        else:
            raise ExecutionSafetyError(
                "new fills have no durable pre-submit cash baseline"
            )
        cap = max(0.0, base - delta)
    return CashLedgerUpdate(
        previously_accounted_principal_yuan=accounted,
        broker_filled_principal_yuan=broker_filled,
        newly_filled_principal_yuan=delta,
        cash_cap_yuan=cap,
    )


T = TypeVar("T")


def strict_query(
    operation: Callable[[], T | None],
    *,
    name: str,
    attempts: int = 3,
    delay_seconds: float = 0.15,
) -> T:
    if attempts < 1:
        raise ValueError("attempts must be positive")
    errors: list[str] = []
    for attempt in range(1, attempts + 1):
        try:
            result = operation()
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
        else:
            if result is not None:
                return result
            errors.append("None")
        if attempt < attempts:
            time.sleep(delay_seconds)
    raise BrokerQueryAmbiguous(
        f"{name} remained ambiguous after {attempts} attempts: "
        + " | ".join(errors)
    )


def query_all_orders_strict(
    trader: object,
    account: object,
    *,
    attempts: int = 3,
) -> list[OrderView]:
    raw = strict_query(
        lambda: trader.query_stock_orders(account, False),
        name="query_stock_orders(all)",
        attempts=attempts,
    )
    return [OrderView.from_qmt(order) for order in list(raw)]


def query_order_strict(
    trader: object,
    account: object,
    order_id: int,
    *,
    attempts: int = 3,
) -> OrderView:
    raw = strict_query(
        lambda: trader.query_stock_order(account, int(order_id)),
        name=f"query_stock_order({int(order_id)})",
        attempts=attempts,
    )
    return OrderView.from_qmt(raw)


def query_asset_strict(
    trader: object,
    account: object,
    *,
    attempts: int = 3,
) -> CashSnapshot:
    raw = strict_query(
        lambda: trader.query_stock_asset(account),
        name="query_stock_asset",
        attempts=attempts,
    )
    return read_cash_snapshot(raw)


def orders_with_prefix(
    orders: Iterable[OrderView],
    *,
    remark_prefix: str,
) -> list[OrderView]:
    return [
        order
        for order in orders
        if order.remark.startswith(remark_prefix)
    ]


def unresolved_repo_orders(
    orders: Iterable[OrderView],
) -> list[OrderView]:
    return [
        order
        for order in orders
        if order.symbol in REPO_SYMBOLS
        and order.classification
        in {
            OrderClass.ACTIVE,
            OrderClass.CANCEL_PENDING,
            OrderClass.UNKNOWN,
        }
    ]


def find_unique_order_by_remark(
    orders: Iterable[OrderView],
    remark: str,
) -> OrderView | None:
    matches = [order for order in orders if order.remark == remark]
    if len(matches) > 1:
        raise UnresolvedOrderError(
            f"multiple broker orders share remark {remark!r}"
        )
    return matches[0] if matches else None


def load_account_binding(
    path: Path,
    *,
    environment: str,
    qmt_path: Path,
) -> AccountBinding:
    binding_path = Path(path)
    if not binding_path.is_file():
        raise AccountBindingError(
            f"account binding does not exist: {binding_path}"
        )
    try:
        payload = json.loads(binding_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AccountBindingError("account binding is unreadable") from exc
    if payload.get("version") not in {1, 2}:
        raise AccountBindingError("unsupported account binding version")
    accounts = payload.get("accounts")
    if not isinstance(accounts, list):
        raise AccountBindingError("account binding accounts must be a list")
    matches = [
        item
        for item in accounts
        if isinstance(item, dict)
        and item.get("environment") == environment
        and item.get("account_type") == "SECURITY_ACCOUNT"
    ]
    if len(matches) != 1:
        raise AccountBindingError(
            "expected exactly one bound account for the environment"
        )
    entry = matches[0]
    if "account_id" in entry:
        raise AccountBindingError(
            "plaintext account IDs are forbidden in account bindings"
        )
    fingerprint = _validate_sha256(
        entry.get("account_id_fingerprint"),
        "account fingerprint",
    )
    path_fingerprint = entry.get("qmt_path_fingerprint")
    if path_fingerprint is not None:
        path_fingerprint = _validate_sha256(
            path_fingerprint,
            "QMT path fingerprint",
        )
        actual_path_fingerprint = qmt_path_fingerprint(qmt_path)
        if path_fingerprint != actual_path_fingerprint:
            raise AccountBindingError(
                "QMT path does not match the bound environment"
            )
    label = str(entry.get("label", "")).strip()
    if not label:
        raise AccountBindingError("account binding label is missing")
    return AccountBinding(
        environment=environment,
        label=label,
        account_id_fingerprint=fingerprint,
        qmt_path_fingerprint=path_fingerprint,
    )


def select_bound_account(
    trader: object,
    xtconstant: object,
    xttype: object,
    *,
    environment: str,
    qmt_path: Path,
    binding_path: Path,
    matching_attempts: int = 3,
    matching_delay_seconds: float = 3.0,
) -> tuple[object, AccountBinding]:
    if matching_attempts < 1:
        raise ValueError("matching_attempts must be at least 1")
    if matching_delay_seconds < 0:
        raise ValueError(
            "matching_delay_seconds cannot be negative"
        )
    normalized_path = Path(qmt_path).resolve()
    path_text = str(normalized_path)
    if environment == "simulation":
        if "模拟" not in path_text:
            raise AccountBindingError(
                "simulation execution requires a simulation QMT path"
            )
    elif environment == "live":
        if "模拟" in path_text:
            raise AccountBindingError(
                "live execution cannot use a simulation QMT path"
            )
    else:
        raise AccountBindingError(
            f"unsupported execution environment: {environment!r}"
        )
    binding = load_account_binding(
        binding_path,
        environment=environment,
        qmt_path=normalized_path,
    )
    for attempt in range(1, matching_attempts + 1):
        infos = list(
            strict_query(
                trader.query_account_infos,
                name="query_account_infos",
            )
        )
        statuses = list(
            strict_query(
                trader.query_account_status,
                name="query_account_status",
            )
        )
        normal_ids = {
            str(getattr(status, "account_id", "")).strip()
            for status in statuses
            if int(getattr(status, "account_type", -1))
            == int(xtconstant.SECURITY_ACCOUNT)
            and int(getattr(status, "status", -1))
            == int(xtconstant.ACCOUNT_STATUS_OK)
        }
        selected = [
            info
            for info in infos
            if int(getattr(info, "account_type", -1))
            == int(xtconstant.SECURITY_ACCOUNT)
            and str(getattr(info, "account_id", "")).strip()
            in normal_ids
            and account_id_fingerprint(
                getattr(info, "account_id", "")
            )
            == binding.account_id_fingerprint
        ]
        if len(selected) == 1:
            account_id = str(
                getattr(selected[0], "account_id", "")
            ).strip()
            return xttype.StockAccount(account_id, "STOCK"), binding
        if attempt < matching_attempts:
            time.sleep(matching_delay_seconds)
    raise AccountBindingError(
        "expected exactly one normal account matching the binding "
        f"after {matching_attempts} attempts"
    )


def account_id_fingerprint(account_id: object) -> str:
    normalized = str(account_id).strip()
    if not normalized:
        raise AccountBindingError("account ID is missing")
    payload = f"miniqmt-account-v1:{normalized}".encode()
    return hashlib.sha256(payload).hexdigest()


def qmt_path_fingerprint(qmt_path: Path) -> str:
    normalized = os.path.normcase(str(Path(qmt_path).resolve()))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def xtquant_runtime_sha256() -> str:
    from xtquant import xtconstant, xttrader, xttype

    digest = hashlib.sha256()
    for module in (xtconstant, xttrader, xttype):
        path = Path(str(module.__file__)).resolve()
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def read_quote_books(
    xtdata: object,
    symbols: Sequence[str],
    *,
    now: datetime,
    maximum_age_seconds: float,
    not_before_epoch_ms: int | None = None,
) -> dict[str, QuoteBook]:
    raw_payload = xtdata.get_full_tick(list(symbols))
    if not isinstance(raw_payload, dict):
        raise QuoteValidationError("get_full_tick returned no mapping")
    books: dict[str, QuoteBook] = {}
    errors: list[str] = []
    for symbol in symbols:
        try:
            books[symbol] = _parse_quote_book(
                symbol,
                raw_payload.get(symbol),
                now=now,
                maximum_age_seconds=maximum_age_seconds,
                not_before_epoch_ms=not_before_epoch_ms,
            )
        except QuoteValidationError as exc:
            errors.append(f"{symbol}: {exc}")
    if not books:
        raise QuoteValidationError(
            "no symbol has a valid fresh book: " + " | ".join(errors)
        )
    return books


def build_book_plan(
    book: QuoteBook,
    requested_volume: int,
) -> BookPlan:
    remaining = int(requested_volume)
    if remaining <= 0:
        raise QuoteValidationError("requested volume must be positive")
    executable = 0
    weighted_value = 0.0
    limit_rate = 0.0
    levels: list[Mapping[str, float | int]] = []
    previous_price = math.inf
    for price, raw_volume in zip(book.bid_prices, book.bid_volumes):
        normalized = normalize_repo_rate(price)
        if normalized > previous_price + 1e-9:
            raise QuoteValidationError(
                f"{book.symbol} bid ladder is not descending"
            )
        previous_price = normalized
        volume = max(int(raw_volume), 0)
        volume -= volume % 10
        if volume <= 0:
            continue
        take = min(remaining, volume)
        take -= take % 10
        if take <= 0:
            continue
        executable += take
        remaining -= take
        weighted_value += take * normalized
        limit_rate = normalized
        levels.append(
            {
                "price_percent": normalized,
                "available_volume": volume,
                "planned_volume": take,
            }
        )
        if remaining <= 0:
            break
    if executable <= 0:
        raise QuoteValidationError(
            f"{book.symbol} has no executable bid volume"
        )
    return BookPlan(
        symbol=book.symbol,
        name=REPO_NAMES[book.symbol],
        requested_volume=requested_volume,
        executable_volume=executable,
        covers_requested_volume=executable >= requested_volume,
        principal_yuan=qmt_volume_to_principal(executable),
        limit_rate_percent=limit_rate,
        expected_vwap_percent=weighted_value / executable,
        levels=tuple(levels),
        quote_time=book.quote_time,
        quote_age_seconds=book.quote_age_seconds,
    )


def rank_book_plans(plans: Iterable[BookPlan]) -> list[BookPlan]:
    choices = list(plans)
    full = [plan for plan in choices if plan.covers_requested_volume]
    if full:
        return sorted(
            full,
            key=lambda plan: plan.expected_vwap_percent,
            reverse=True,
        )
    return sorted(
        choices,
        key=lambda plan: (
            plan.executable_volume,
            plan.expected_vwap_percent,
        ),
        reverse=True,
    )


def is_exchange_trading_day(
    xtdata: object,
    trade_date: date,
) -> bool:
    stamp = trade_date.strftime("%Y%m%d")
    result = xtdata.get_trading_dates(
        "SH",
        stamp,
        stamp,
        count=-1,
    )
    if result is None:
        raise BrokerQueryAmbiguous(
            "exchange trading-day query returned None"
        )
    return bool(list(result))


class AtomicJournal:
    def __init__(
        self,
        path: Path,
        *,
        strategy: str,
        trade_date: date,
    ) -> None:
        self.path = Path(path).resolve()
        self.strategy = str(strategy)
        self.trade_date = trade_date.isoformat()
        self.payload: dict[str, Any] = {}

    def load_or_initialize(
        self,
        *,
        machine_payload: Mapping[str, object],
        initial_data: Mapping[str, object] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        if self.path.exists():
            try:
                payload = json.loads(
                    self.path.read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError) as exc:
                raise ExecutionSafetyError(
                    "execution journal is unreadable; manual review required"
                ) from exc
            if not isinstance(payload, dict):
                raise ExecutionSafetyError(
                    "execution journal root must be an object"
                )
            if payload.get("schema_version") != 2:
                raise ExecutionSafetyError(
                    "unexpected execution journal schema"
                )
            if payload.get("strategy") != self.strategy:
                raise ExecutionSafetyError(
                    "execution journal strategy mismatch"
                )
            if payload.get("trade_date") != self.trade_date:
                raise ExecutionSafetyError(
                    "execution journal trade-date mismatch"
                )
            self.payload = payload
            return self.payload, True
        self.payload = {
            "schema_version": 2,
            "strategy": self.strategy,
            "trade_date": self.trade_date,
            "created_at": datetime.now().astimezone().isoformat(),
            "updated_at": datetime.now().astimezone().isoformat(),
            "event_count": 0,
            "machine": dict(machine_payload),
            "history": [],
            "data": dict(initial_data or {}),
        }
        self.write()
        return self.payload, False

    def transition(
        self,
        *,
        event: str,
        machine_payload: Mapping[str, object],
        details: Mapping[str, object] | None = None,
        data_updates: Mapping[str, object] | None = None,
    ) -> None:
        if not self.payload:
            raise ExecutionSafetyError("journal is not initialized")
        record = {
            "sequence": int(self.payload.get("event_count", 0)) + 1,
            "at": datetime.now().astimezone().isoformat(),
            "event": str(event),
            "state": machine_payload["state"],
            "details": dict(details or {}),
        }
        history = list(self.payload.get("history") or [])
        history.append(record)
        self.payload["history"] = history[-500:]
        self.payload["event_count"] = record["sequence"]
        self.payload["machine"] = dict(machine_payload)
        if data_updates:
            data = dict(self.payload.get("data") or {})
            data.update(dict(data_updates))
            self.payload["data"] = data
        self.payload["updated_at"] = record["at"]
        self.write()

    def update_data(self, **values: object) -> None:
        if not self.payload:
            raise ExecutionSafetyError("journal is not initialized")
        data = dict(self.payload.get("data") or {})
        data.update(values)
        self.payload["data"] = data
        self.payload["updated_at"] = (
            datetime.now().astimezone().isoformat()
        )
        self.write()

    def write(self) -> None:
        atomic_write_json(self.path, self.payload)


class ExecutionMutex(AbstractContextManager["ExecutionMutex"]):
    def __init__(
        self,
        path: Path,
        *,
        timeout_seconds: float = 0.0,
        poll_seconds: float = 0.20,
    ) -> None:
        self.path = Path(path).resolve()
        self.timeout_seconds = max(float(timeout_seconds), 0.0)
        self.poll_seconds = max(float(poll_seconds), 0.01)
        self._handle: Any = None

    def __enter__(self) -> ExecutionMutex:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            handle.seek(0)
            deadline = time.monotonic() + self.timeout_seconds
            while True:
                try:
                    self._lock_handle(handle)
                    break
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise ConcurrentExecutionError(
                            "another reverse-repo executor owns the lock"
                        ) from exc
                    time.sleep(
                        min(
                            self.poll_seconds,
                            max(0.0, deadline - time.monotonic()),
                        )
                    )
            handle.seek(0)
            handle.truncate()
            handle.write(
                (
                    f"pid={os.getpid()} "
                    f"at={datetime.now().astimezone().isoformat()}\n"
                ).encode()
            )
            handle.flush()
            os.fsync(handle.fileno())
            self._handle = handle
            return self
        except Exception:
            handle.close()
            raise

    @staticmethod
    def _lock_handle(handle: Any) -> None:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(
                handle.fileno(),
                msvcrt.LK_NBLCK,
                1,
            )
        else:
            import fcntl

            fcntl.flock(
                handle.fileno(),
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def atomic_write_json(path: Path, payload: Mapping[str, object]) -> None:
    output = Path(path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(
        f".{output.name}.{os.getpid()}.tmp"
    )
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(
                payload,
                handle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(20):
            try:
                os.replace(temporary, output)
                return
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.05)
    finally:
        if temporary.exists():
            temporary.unlink()


def safe_exception(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def journal_matches_verification(
    journal_payload: Mapping[str, object],
    current_verification: Mapping[str, object],
) -> bool:
    data = journal_payload.get("data")
    if not isinstance(data, Mapping):
        return False
    recorded = data.get("formal_verification")
    if not isinstance(recorded, Mapping):
        return False
    return (
        recorded.get("transition_spec_sha256")
        == current_verification.get("transition_spec_sha256")
        and recorded.get("execution_source_sha256")
        == current_verification.get("execution_source_sha256")
    )


def _parse_quote_book(
    symbol: str,
    payload: object,
    *,
    now: datetime,
    maximum_age_seconds: float,
    not_before_epoch_ms: int | None,
) -> QuoteBook:
    if not isinstance(payload, dict) or not payload:
        raise QuoteValidationError("empty quote payload")
    raw_time = int(payload.get("time") or 0)
    if raw_time <= 0:
        raise QuoteValidationError("missing quote timestamp")
    if (
        not_before_epoch_ms is not None
        and raw_time < int(not_before_epoch_ms)
    ):
        raise QuoteValidationError("quote predates the execution trigger")
    quote_time = datetime.fromtimestamp(
        raw_time / 1000,
        tz=now.tzinfo,
    )
    age = (now - quote_time).total_seconds()
    if not 0 <= age <= float(maximum_age_seconds):
        raise QuoteValidationError(
            f"quote age {age:.3f}s is outside the freshness window"
        )
    bids = tuple(float(item or 0) for item in payload.get("bidPrice") or [])
    bid_volumes = tuple(
        max(int(item or 0), 0) for item in payload.get("bidVol") or []
    )
    asks = tuple(float(item or 0) for item in payload.get("askPrice") or [])
    ask_volumes = tuple(
        max(int(item or 0), 0) for item in payload.get("askVol") or []
    )
    if not bids or not bid_volumes:
        raise QuoteValidationError("bid ladder is missing")
    if len(bids) != len(bid_volumes):
        raise QuoteValidationError("bid price/volume lengths differ")
    normalize_repo_rate(bids[0])
    return QuoteBook(
        symbol=symbol,
        quote_time_epoch_ms=raw_time,
        quote_time=quote_time.isoformat(),
        quote_age_seconds=age,
        bid_prices=bids,
        bid_volumes=bid_volumes,
        ask_prices=asks,
        ask_volumes=ask_volumes,
    )


def _finite_nonnegative(value: object) -> float | None:
    if value is None:
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0:
        return None
    return number


def _validate_sha256(value: object, label: str) -> str:
    digest = str(value or "").strip().lower()
    if (
        len(digest) != 64
        or any(char not in "0123456789abcdef" for char in digest)
    ):
        raise AccountBindingError(f"invalid {label}")
    return digest
