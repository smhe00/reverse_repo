"""GC001 早盘盘口信号验证（shadow 默认；live 需显式授权；sim 走模拟端）。

用途：明天（以及后续交易日）在真实行情上验证 eat/wallgone 信号 + 提前挂单
策略。与主交易器（gc001_live_daily_90pct_093042.py）完全独立：
- shadow 模式：只读行情，计算信号与"若挂单是否成交"，绝不下单；
- live/sim 模式：按信号挂一笔小额限价卖单（默认 10 万元），未成交到硬
  截止撤单，绝不自动追价。
- sim 模式：仅允许模拟 QMT 路径（含"模拟"），不需要授权 token，用于
  `.\rr dev signal` 在模拟端走完整下单/撤单链路；
- --smoke：只做连接检查（行情订阅 + 模拟/实盘交易通道连接 + 账户查询 +
  轮询若干帧），不下单、不等窗口，用于快速验证模拟端已就绪。

安全护栏（live 模式）：
- 必须提供匹配的 --execute-token；
- --qmt-path 不得含"模拟"字样；
- live/sim 都必须提供 --account-binding（repo_live_account_binding.local.json
  或 repo_simulation_account_binding.local.json），连接前校验 QMT 路径指纹，
  选账户后再校验账户 ID 指纹，任何一项不匹配立即中止，绝不下单；
- 交易日期必须等于本机日期；
- 金额限制在 1 万 ~ 100 万元；
- 下单前检测当天已有 GC001 未成交委托则中止（防与主交易器双卖冲突）；
- 行情帧时效 > 4.5 秒不动作；
- 硬截止 09:31:30：未成交一律撤单，之后不再下任何新单。

离线测试：--replay <jsonl> 用缓存数据重放同一套状态机，不连 QMT。
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import random
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, time as clock_time, timedelta
from pathlib import Path
from typing import Any

from gc001_book_signal import TICK, price_to_tick


SYMBOL = "204001.SH"
QMT_FACE_VALUE_YUAN = 100
WINDOW_START = "09:30:00"
WINDOW_END = "09:35:30"
MIN_TRIGGER = "09:30:03"
MAX_TRIGGER = "09:31:00"
HARD_DEADLINE = clock_time(9, 31, 30)
CONNECT_LEAD = clock_time(9, 28, 0)
MAXIMUM_QUOTE_AGE_SECONDS = 4.5
DEFAULT_OUTPUT_ROOT = Path(r"D:\gitee\miniQMT\data\gc001_validation")
REMARK_PREFIX = "gc001_signal_valid"
AUTHORIZATION_TOKEN = "AUTHORIZE_GC001_SIGNAL_VALIDATION_LIVE_1"
DEFAULT_OFFSETS = {"eat": 2, "wallgone": 6, "jump": 2, "ofi": 2}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument(
        "--mode",
        choices=("shadow", "live", "sim"),
        default="shadow",
        help="shadow=只读信号验证（默认）；live=小额实盘挂单；sim=模拟端挂单",
    )
    parser.add_argument(
        "--exec-model",
        choices=("static", "trail", "tranche", "all"),
        default="all",
        help="执行模型：static=单档；trail=跟踪追高；tranche=分档；all=三种同时验证",
    )
    parser.add_argument("--qmt-path", default=None, help="live 模式必填（真实 QMT 路径）")
    parser.add_argument(
        "--account-binding",
        default=None,
        help="live/sim 必填：账户绑定 JSON（含 qmt_path_fingerprint 与 account_id_fingerprint）",
    )
    parser.add_argument("--amount", type=int, default=100_000, help="live 模式金额（元）")
    parser.add_argument("--execute-token", default=None, help="live 模式显式授权 token")
    parser.add_argument("--anchor", choices=("ask1", "micro1"), default="ask1")
    parser.add_argument("--offset-eat", type=int, default=DEFAULT_OFFSETS["eat"])
    parser.add_argument("--offset-wallgone", type=int, default=DEFAULT_OFFSETS["wallgone"])
    parser.add_argument("--offset-jump", type=int, default=DEFAULT_OFFSETS["jump"])
    parser.add_argument("--offset-ofi", type=int, default=DEFAULT_OFFSETS["ofi"])
    parser.add_argument("--hold-seconds", type=int, default=60)
    parser.add_argument(
        "--eat-tranches",
        default="2,3",
        help="eat 分档偏移（tick，逗号分隔），默认 2,3",
    )
    parser.add_argument(
        "--wallgone-tranches",
        default="6,7",
        help="wallgone 分档偏移（tick，逗号分隔），默认 6,7",
    )
    parser.add_argument("--smoke", action="store_true", help="连接检查：订阅行情+连接交易通道，轮询若干帧后退出")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument(
        "--replay",
        type=Path,
        default=None,
        help="离线重放缓存 JSONL（不连 QMT，shadow 语义）",
    )
    return parser.parse_args()


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _load_binding(binding_path: Path, environment: str) -> dict[str, Any]:
    """读取账户绑定 JSON，返回环境对应的唯一绑定条目（含指纹）。"""
    import hashlib
    import os

    if not binding_path.is_file():
        raise ValueError(f"account binding is missing: {binding_path}")
    payload = json.loads(binding_path.read_text(encoding="utf-8"))
    accounts = payload.get("accounts") or []
    entries = [
        a
        for a in accounts
        if a.get("environment") == environment
        and a.get("account_type") == "SECURITY_ACCOUNT"
        and a.get("account_id_fingerprint")
        and a.get("qmt_path_fingerprint")
    ]
    if len(entries) != 1:
        raise ValueError(
            f"binding must contain exactly one {environment} security account, got {len(entries)}"
        )
    return entries[0]


def _qmt_path_fingerprint(qmt_path: str) -> str:
    import hashlib
    import os

    normalized = os.path.normcase(str(Path(qmt_path).resolve()))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _account_id_fingerprint(account_id: str) -> str:
    import hashlib

    payload = f"miniqmt-account-v1:{str(account_id).strip()}".encode()
    return hashlib.sha256(payload).hexdigest()


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "item"):
        return _json_safe(value.item())
    if hasattr(value, "tolist"):
        return _json_safe(value.tolist())
    return str(value)


def _write_json_atomic(path: Path, payload: dict[str, Any], retries: int = 5) -> bool:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        return False
    for attempt in range(retries):
        try:
            os.replace(temporary, path)
            return True
        except OSError:
            time.sleep(0.2 * (attempt + 1))
    return False


def _tick_to_row(tick: dict[str, Any], exchange_time_epoch_ms: int | None) -> dict[str, Any]:
    ts = datetime.fromtimestamp(exchange_time_epoch_ms / 1000).astimezone() if exchange_time_epoch_ms else None
    return {
        "ts": ts,
        "hms": ts.strftime("%H:%M:%S") if ts else "",
        "lastPrice": float(tick.get("lastPrice", 0.0) or 0.0),
        "askPrice": _json_safe(tick.get("askPrice", [])) or [],
        "askVol": _json_safe(tick.get("askVol", [])) or [],
        "bidPrice": _json_safe(tick.get("bidPrice", [])) or [],
        "bidVol": _json_safe(tick.get("bidVol", [])) or [],
        "_tick": tick,
    }


class ValidationRunner:
    """状态机：等待触发 -> 触发 -> 监控成交/撤单。shadow 与 live 共用决策逻辑。"""

    def __init__(
        self,
        *,
        mode: str,
        offsets: dict[str, int],
        anchor: str,
        hold_seconds: int,
    ) -> None:
        self.mode = mode
        self.offsets = offsets
        self.anchor = anchor
        self.hold_seconds = hold_seconds
        self.state = "waiting_trigger"
        self.trigger_type: str | None = None
        self.trigger_hms: str | None = None
        self.limit_price: float | None = None
        self.trigger_at: datetime | None = None
        self.filled = False
        self.fill_hms: str | None = None
        self.order_id: int | None = None
        self.submitted_at: datetime | None = None
        self.cancelled = False
        self.events: list[dict[str, Any]] = []
        self.base_price: float | None = None

    def log(self, event: str, **details: Any) -> None:
        self.events.append({"event": event, "at": datetime.now().astimezone().isoformat(), **details})

    def on_frame(self, feat: Any, hms: str, now: datetime, tick: dict[str, Any]) -> None:
        from gc001_book_signal import pick_trigger, price_to_tick, triggers

        if self.state == "waiting_trigger" and MIN_TRIGGER <= hms <= MAX_TRIGGER:
            fired = triggers(feat)
            trig = pick_trigger(fired) if fired else None
            if trig is not None:
                self.state = "triggered"
                self.trigger_type = trig
                self.trigger_hms = hms
                self.trigger_at = now
                base = feat.ask1 if self.anchor == "ask1" else feat.micro1
                offset = self.offsets.get(trig, 0)
                self.limit_price = price_to_tick(base + offset * 0.005, direction="up")
                self.base_price = float(base)
                self.log(
                    "trigger",
                    trigger=trig,
                    hms=hms,
                    anchor=self.anchor,
                    offset_ticks=offset,
                    base_price=base,
                    limit_price=self.limit_price,
                    ask1=feat.ask1,
                    bid1=feat.bid1,
                    micro1=feat.micro1,
                    last=feat.last,
                    eaten_frac=feat.ask_eaten_frac,
                    cancel_frac=feat.ask_cancel_frac,
                    wall_disappear=feat.wall_disappear,
                )
                return

        if (
            self.mode == "shadow"
            and self.state == "triggered"
            and self.limit_price is not None
        ):
            # shadow 成交代理与回测一致：ask1 >= L 或 lastPrice >= L
            if (feat.ask1 is not None and feat.ask1 >= self.limit_price - 1e-9) or (
                feat.last >= self.limit_price - 1e-9
            ):
                self.filled = True
                self.fill_hms = hms
                self.state = "filled_shadow"
                self.log("would_fill", hms=hms, limit_price=self.limit_price)
                return
            if now - self.trigger_at >= timedelta(seconds=self.hold_seconds):
                self.state = "not_filled_shadow"
                self.log("would_cancel_unfilled", hms=hms, limit_price=self.limit_price)


def _select_single_normal_account(trader: Any, xtconstant: Any, xttype: Any) -> Any:
    infos = list(trader.query_account_infos() or [])
    statuses = list(trader.query_account_status() or [])
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
        and str(getattr(info, "account_id", "")).strip() in normal_ids
    ]
    if len(selected) != 1:
        raise RuntimeError(
            f"expected exactly one normal stock account, got {len(selected)}"
        )
    return xttype.StockAccount(
        str(getattr(selected[0], "account_id", "")).strip(),
        "STOCK",
    )


def _gc001_orders(trader: Any, account: Any) -> list[Any]:
    try:
        orders = trader.query_stock_orders(account, False)
    except Exception:
        return []
    if not orders:
        return []
    return [
        o
        for o in orders
        if str(getattr(o, "stock_code", "")).upper() == SYMBOL
        and int(getattr(o, "order_volume", 0) or 0) > 0
        and int(getattr(o, "traded_volume", 0) or 0) < int(getattr(o, "order_volume", 0) or 0)
    ]


@dataclass
class OrderLeg:
    model: str
    label: str
    limit: float
    volume: int
    order_id: int | None = None
    state: str = "pending"  # pending / active / filled / cancelled
    fill_hms: str = ""
    fill_price: float = float("nan")
    reprices: int = 0


def _place_leg(
    trader: Any,
    account: Any,
    xtconstant: Any,
    leg: OrderLeg,
    remark: str,
    runner: ValidationRunner,
) -> None:
    order_id = int(
        trader.order_stock(
            account,
            SYMBOL,
            xtconstant.STOCK_SELL,
            leg.volume,
            xtconstant.FIX_PRICE,
            leg.limit,
            "gc001_signal_valid",
            f"{remark}_{leg.label}",
        )
    )
    if order_id <= 0:
        raise RuntimeError(f"order submission failed for {leg.label}: {order_id}")
    leg.order_id = order_id
    leg.state = "active"
    runner.log(
        "order_submitted",
        model=leg.model,
        label=leg.label,
        order_id=order_id,
        limit_price=leg.limit,
        volume=leg.volume,
    )


def _query_leg(trader: Any, account: Any, leg: OrderLeg) -> tuple[int, int]:
    order = trader.query_stock_order(account, leg.order_id)
    if order is None:
        return 0, -1
    traded = int(getattr(order, "traded_volume", 0) or 0)
    status = int(getattr(order, "order_status", -1))
    return traded, status


def _wait_cancel_terminal(
    trader: Any,
    account: Any,
    order_id: int,
    runner: ValidationRunner,
    timeout_seconds: float = 15.0,
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        order = trader.query_stock_order(account, order_id)
        if order is not None:
            status = int(getattr(order, "order_status", -1))
            if status in (53, 54, 57):
                return True
        time.sleep(0.25)
    runner.log("cancel_confirm_timeout", order_id=order_id)
    return False


def _live_submit_models(
    runner: ValidationRunner,
    trader: Any,
    account: Any,
    *,
    exec_model: str,
    amount_yuan: int,
    offsets: dict[str, int],
    tranches_by_trigger: dict[str, list[int]],
    remark: str,
    output_dir: Path,
    summary: dict[str, Any],
) -> None:
    from xtquant import xtconstant, xtdata

    if runner.limit_price is None or runner.base_price is None:
        raise RuntimeError("limit price or base price is missing")
    preexisting = _gc001_orders(trader, account)
    if preexisting:
        raise RuntimeError("preexisting unfilled GC001 order detected; refusing to double-sell")

    trigger_type = runner.trigger_type or ""
    base = runner.base_price
    offset = offsets.get(trigger_type, 0)
    volume_per_model = amount_yuan // QMT_FACE_VALUE_YUAN
    legs: list[OrderLeg] = []
    models = [exec_model] if exec_model != "all" else ["static", "trail", "tranche"]
    for model in models:
        if model == "tranche":
            t_offsets = tranches_by_trigger.get(trigger_type, [offset])
            per_leg = max(1, volume_per_model // len(t_offsets))
            for i, t_off in enumerate(t_offsets):
                legs.append(
                    OrderLeg(
                        model="tranche",
                        label=f"t{i + 1}",
                        limit=price_to_tick(base + t_off * TICK, direction="up"),
                        volume=per_leg,
                    )
                )
        else:
            label = {"static": "st", "trail": "tr"}.get(model, model)
            legs.append(
                OrderLeg(
                    model=model,
                    label=label,
                    limit=runner.limit_price,
                    volume=volume_per_model,
                )
            )

    total_principal = sum(leg.volume for leg in legs) * QMT_FACE_VALUE_YUAN
    asset = trader.query_stock_asset(account)
    if asset is None:
        raise RuntimeError("asset query returned None; cannot verify cash")
    cash_values = [
        float(getattr(asset, field, 0) or 0)
        for field in ("available_cash", "cash")
    ]
    cash_values = [v for v in cash_values if v > 0]
    if not cash_values:
        raise RuntimeError("asset query returned no usable cash field")
    if min(cash_values) < total_principal:
        raise RuntimeError(
            f"available cash {min(cash_values):.0f} below total principal {total_principal}"
        )

    for leg in legs:
        _place_leg(trader, account, xtconstant, leg, remark, runner)
    runner.state = "submitted"
    runner.log(
        "models_submitted",
        models=[leg.model for leg in legs],
        total_principal=total_principal,
    )
    _write_summary(summary, output_dir)

    hard_deadline = datetime.combine(
        date.fromisoformat(summary["trade_date"]),
        HARD_DEADLINE,
        tzinfo=datetime.now().astimezone().tzinfo,
    )
    hold_deadline = runner.trigger_at + timedelta(seconds=runner.hold_seconds)
    cancel_at = min(hold_deadline, hard_deadline)
    last_reprice_at = 0.0

    while datetime.now().astimezone() <= cancel_at:
        now = datetime.now().astimezone()
        trail = next((l for l in legs if l.model == "trail" and l.state == "active"), None)
        if trail is not None and (now.timestamp() - last_reprice_at) >= 1.0:
            last_reprice_at = now.timestamp()
            tick = (xtdata.get_full_tick([SYMBOL]) or {}).get(SYMBOL) or {}
            ask_prices = _json_safe(tick.get("askPrice", [])) or []
            ask1 = float(ask_prices[0]) if ask_prices else None
            if ask1 is not None and ask1 > 0 and trail.reprices < 20:
                new_limit = price_to_tick(ask1 + offset * TICK, direction="up")
                if new_limit > trail.limit + 1e-9:
                    try:
                        trader.cancel_order_stock(account, trail.order_id)
                    except Exception as exc:
                        runner.log(
                            "trail_cancel_failed",
                            order_id=trail.order_id,
                            error=f"{type(exc).__name__}: {exc}",
                        )
                    else:
                        if _wait_cancel_terminal(trader, account, trail.order_id, runner):
                            old_id = trail.order_id
                            trail.order_id = None
                            trail.limit = new_limit
                            trail.reprices += 1
                            _place_leg(trader, account, xtconstant, trail, remark, runner)
                            runner.log(
                                "trail_reprice",
                                old_order_id=old_id,
                                new_order_id=trail.order_id,
                                new_limit=new_limit,
                            )
                            _write_summary(summary, output_dir)

        for leg in legs:
            if leg.state != "active":
                continue
            traded, status = _query_leg(trader, account, leg)
            runner.log(
                "order_status",
                model=leg.model,
                label=leg.label,
                order_id=leg.order_id,
                status=status,
                traded_volume=traded,
            )
            if traded >= leg.volume or status == 56:
                leg.state = "filled"
                leg.fill_hms = now.strftime("%H:%M:%S")
                leg.fill_price = leg.limit
                runner.log(
                    "order_filled",
                    model=leg.model,
                    label=leg.label,
                    order_id=leg.order_id,
                    traded_volume=traded,
                    fill_price=leg.limit,
                )
            elif status in (53, 54, 57):
                leg.state = "cancelled"
                runner.log(
                    "order_terminal_unfilled",
                    model=leg.model,
                    label=leg.label,
                    order_id=leg.order_id,
                    status=status,
                )
        if all(l.state in ("filled", "cancelled") for l in legs):
            break
        time.sleep(0.25)

    for leg in legs:
        if leg.state == "active":
            try:
                trader.cancel_order_stock(account, leg.order_id)
            except Exception as exc:
                runner.log(
                    "cancel_failed",
                    label=leg.label,
                    error=f"{type(exc).__name__}: {exc}",
                )
            leg.state = "cancelled"
            runner.log(
                "order_cancelled",
                model=leg.model,
                label=leg.label,
                order_id=leg.order_id,
            )

    filled_legs = [l for l in legs if l.state == "filled"]
    runner.filled = len(filled_legs) > 0
    runner.cancelled = any(l.state == "cancelled" for l in legs)
    if filled_legs and len(filled_legs) == len(legs):
        runner.state = "all_filled"
    elif filled_legs:
        runner.state = "partial_filled"
    else:
        runner.state = "cancelled_unfilled"
    summary["legs"] = [
        {
            "model": l.model,
            "label": l.label,
            "limit": round(l.limit, 3),
            "volume": l.volume,
            "state": l.state,
            "fill_hms": l.fill_hms,
            "fill_price": round(l.fill_price, 3) if l.fill_hms else None,
            "order_id": l.order_id,
            "reprices": l.reprices,
        }
        for l in legs
    ]
    summary["state"] = runner.state
    _write_summary(summary, output_dir)


def _write_summary(summary: dict[str, Any], output_dir: Path) -> None:
    _write_json_atomic(output_dir / "gc001_validation.summary.json", summary)


def run_replay(args: argparse.Namespace) -> int:
    """离线重放：喂入缓存帧，跑同一套信号+shadow 成交状态机。"""
    from gc001_book_signal import IncrementalSignalEngine

    rows: list[dict[str, Any]] = []
    for line in args.replay.open(encoding="utf-8"):
        rows.append(json.loads(line))
    print(f"replay: loaded {len(rows)} records from {args.replay.name}")

    engine = IncrementalSignalEngine()
    offsets = {
        "eat": args.offset_eat,
        "wallgone": args.offset_wallgone,
        "jump": args.offset_jump,
        "ofi": args.offset_ofi,
    }
    tranches_by_trigger = {
        "eat": [int(x.strip()) for x in args.eat_tranches.split(",") if x.strip()],
        "wallgone": [int(x.strip()) for x in args.wallgone_tranches.split(",") if x.strip()],
        "jump": [int(x.strip()) for x in args.eat_tranches.split(",") if x.strip()],
        "ofi": [int(x.strip()) for x in args.eat_tranches.split(",") if x.strip()],
    }
    runner = ValidationRunner(
        mode="shadow",
        offsets=offsets,
        anchor=args.anchor,
        hold_seconds=args.hold_seconds,
    )

    seen: set[int] = set()
    for raw in rows:
        epoch = _as_int(raw.get("time") or raw.get("_exchange_time_epoch_ms"))
        if epoch is None:
            continue
        if epoch in seen:
            continue
        seen.add(epoch)
        hms = datetime.fromtimestamp(epoch / 1000).astimezone().strftime("%H:%M:%S")
        if not (WINDOW_START <= hms <= WINDOW_END):
            continue
        tick = raw if "lastPrice" in raw else raw.get("_tick", {})
        row = _tick_to_row(tick, epoch)
        feat = engine.update(row)
        now = datetime.fromtimestamp(epoch / 1000).astimezone()
        runner.on_frame(feat, hms, now, tick)

    print(
        f"state={runner.state} trigger={runner.trigger_type}@{runner.trigger_hms} "
        f"limit={runner.limit_price} filled={runner.filled}"
        + (f" fill@{runner.fill_hms}" if runner.fill_hms else "")
    )
    for ev in runner.events:
        keep = {k: v for k, v in ev.items() if k not in ("ask1", "bid1", "micro1", "last")}
        print("  ", json.dumps(keep, ensure_ascii=False))
    return 0


def main() -> int:
    args = parse_args()
    if args.replay is not None:
        return run_replay(args)

    trade_date = date.fromisoformat(args.trade_date)
    today = datetime.now().astimezone().date()
    if trade_date != today:
        raise ValueError("validation may only run for the local trade date")
    binding: dict[str, Any] | None = None
    expected_account_fp: str | None = None
    if args.mode == "sim":
        if not args.qmt_path or "模拟" not in str(Path(args.qmt_path).resolve()):
            raise ValueError("sim mode requires a simulation QMT path (must contain 模拟)")
        if not (10_000 <= args.amount <= 1_000_000):
            raise ValueError("sim amount must be from 10,000 to 1,000,000 yuan")
        if args.amount % 1000 != 0:
            raise ValueError("sim amount must be a multiple of 1,000 yuan")
        if not args.account_binding:
            raise ValueError("sim mode requires --account-binding (repo_simulation_account_binding.local.json)")
        binding = _load_binding(Path(args.account_binding), "simulation")
        path_fp = _qmt_path_fingerprint(args.qmt_path)
        if path_fp != binding["qmt_path_fingerprint"]:
            raise ValueError(
                "simulation QMT path fingerprint does not match the binding; refusing to continue"
            )
        expected_account_fp = binding["account_id_fingerprint"]
    elif args.mode == "live":
        if args.execute_token != AUTHORIZATION_TOKEN:
            raise ValueError("live authorization token is missing or invalid")
        if not args.qmt_path or "模拟" in str(Path(args.qmt_path).resolve()):
            raise ValueError("live mode requires a real (non-simulation) QMT path")
        if not (10_000 <= args.amount <= 1_000_000):
            raise ValueError("live amount must be from 10,000 to 1,000,000 yuan")
        if args.amount % 1000 != 0:
            raise ValueError("live amount must be a multiple of 1,000 yuan")
        if not args.account_binding:
            raise ValueError("live mode requires --account-binding (repo_live_account_binding.local.json)")
        binding = _load_binding(Path(args.account_binding), "live")
        path_fp = _qmt_path_fingerprint(args.qmt_path)
        if path_fp != binding["qmt_path_fingerprint"]:
            raise ValueError(
                "live QMT path fingerprint does not match the binding; refusing to continue"
            )
        expected_account_fp = binding["account_id_fingerprint"]

    output_dir = args.output_root / f"date={trade_date:%Y%m%d}"
    output_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = output_dir / "gc001_validation_l1.jsonl"
    summary_path = output_dir / "gc001_validation.summary.json"

    summary: dict[str, Any] = {
        "mode": f"{args.mode}_gc001_signal_validation",
        "smoke": args.smoke,
        "symbol": SYMBOL,
        "trade_date": trade_date.isoformat(),
        "started_at": datetime.now().astimezone().isoformat(),
        "window_start": WINDOW_START,
        "window_end": WINDOW_END,
        "trigger_window": f"{MIN_TRIGGER}~{MAX_TRIGGER}",
        "hard_deadline": HARD_DEADLINE.isoformat(),
        "anchor": args.anchor,
        "offsets": {
            "eat": args.offset_eat,
            "wallgone": args.offset_wallgone,
            "jump": args.offset_jump,
            "ofi": args.offset_ofi,
        },
        "hold_seconds": args.hold_seconds,
        "amount_yuan": args.amount if args.mode == "live" else None,
        "binding_label": binding.get("label") if binding else None,
        "binding_environment": binding.get("environment") if binding else None,
        "binding_path_fingerprint_ok": binding is not None,
        "output": str(jsonl_path),
        "state": "starting",
    }
    _write_summary(summary, output_dir)

    from gc001_book_signal import IncrementalSignalEngine
    from xtquant import xtdata

    xtdata.enable_hello = False
    tick_queue: queue.Queue = queue.Queue()

    def on_tick(payload: dict[str, Any]) -> None:
        rows = payload.get(SYMBOL, [])
        if isinstance(rows, dict):
            rows = [rows]
        if not isinstance(rows, list):
            rows = [rows]
        for tick in rows:
            if isinstance(tick, dict):
                tick_queue.put((tick, datetime.now().astimezone()))

    stream = jsonl_path.open("a", encoding="utf-8", buffering=1)
    engine = IncrementalSignalEngine()
    offsets = {
        "eat": args.offset_eat,
        "wallgone": args.offset_wallgone,
        "jump": args.offset_jump,
        "ofi": args.offset_ofi,
    }
    tranches_by_trigger = {
        "eat": [int(x.strip()) for x in args.eat_tranches.split(",") if x.strip()],
        "wallgone": [int(x.strip()) for x in args.wallgone_tranches.split(",") if x.strip()],
        "jump": [int(x.strip()) for x in args.eat_tranches.split(",") if x.strip()],
        "ofi": [int(x.strip()) for x in args.eat_tranches.split(",") if x.strip()],
    }
    runner = ValidationRunner(
        mode=args.mode,
        offsets=offsets,
        anchor=args.anchor,
        hold_seconds=args.hold_seconds,
    )
    trader = None
    account = None
    volume = args.amount // QMT_FACE_VALUE_YUAN if args.mode in ("live", "sim") else 0
    remark = f"{REMARK_PREFIX}_{trade_date:%Y%m%d}"
    quote_sequence = 0
    seen_epoch: set[int] = set()

    try:
        quote_sequence = int(xtdata.subscribe_quote(SYMBOL, period="tick", count=0, callback=on_tick) or 0)
        if quote_sequence <= 0:
            raise RuntimeError(f"QMT tick subscription failed: {quote_sequence}")
        summary["state"] = "collecting"
        _write_summary(summary, output_dir)

        if args.mode in ("live", "sim"):
            from xtquant import xtconstant, xttype
            from xtquant.xttrader import XtQuantTrader

            trader = XtQuantTrader(str(Path(args.qmt_path).resolve()), random.randint(100_000_000, 999_999_999))
            trader.start()
            if int(trader.connect()) != 0:
                raise RuntimeError("live QMT trading connection failed")
            account = _select_single_normal_account(trader, xtconstant, xttype)
            account_id = str(getattr(account, "account_id", "")).strip()
            account_fp = _account_id_fingerprint(account_id)
            if expected_account_fp is None or account_fp != expected_account_fp:
                raise RuntimeError(
                    "selected account fingerprint does not match the binding; "
                    "refusing to continue (dev must never trade the live account)"
                )
            summary["binding_account_fingerprint_ok"] = True
            summary["binding_account_fingerprint"] = account_fp
            _write_summary(summary, output_dir)
            if int(trader.subscribe(account)) != 0:
                raise RuntimeError("live account subscription failed")
            if _gc001_orders(trader, account):
                raise RuntimeError("preexisting unfilled GC001 order detected at startup")

        if args.smoke:
            # 连接检查：轮询若干帧行情（模拟端可能在非交易时段无新帧），
            # 只读验证，不下单。
            polled = 0
            smoke_deadline = time.monotonic() + 15
            while time.monotonic() < smoke_deadline and polled < 5:
                snapshot = xtdata.get_full_tick([SYMBOL])
                tick = (snapshot or {}).get(SYMBOL) or {}
                if tick:
                    stream.write(json.dumps(_json_safe(tick), ensure_ascii=False, separators=(",", ":")) + "\n")
                    polled += 1
                time.sleep(0.5)
            summary["state"] = "smoke_ok"
            summary["smoke_polled_frames"] = polled
            summary["smoke_quote"] = _json_safe((xtdata.get_full_tick([SYMBOL]) or {}).get(SYMBOL) or {})
            summary["finished_at"] = datetime.now().astimezone().isoformat()
            _write_summary(summary, output_dir)
            print(json.dumps({k: v for k, v in summary.items() if k != "events"}, ensure_ascii=False, indent=2))
            return 0

        start_at = datetime.combine(trade_date, CONNECT_LEAD, tzinfo=datetime.now().astimezone().tzinfo)
        window_end_at = datetime.combine(trade_date, clock_time.fromisoformat(WINDOW_END), tzinfo=datetime.now().astimezone().tzinfo)
        hard_deadline_at = datetime.combine(trade_date, HARD_DEADLINE, tzinfo=datetime.now().astimezone().tzinfo)

        now = datetime.now().astimezone()
        if now < start_at:
            time.sleep(max(0.0, (start_at - now).total_seconds()))

        while datetime.now().astimezone() < window_end_at:
            now = datetime.now().astimezone()
            while True:
                try:
                    tick, arrived_at = tick_queue.get_nowait()
                except queue.Empty:
                    break
                epoch = _as_int(tick.get("time"))
                if epoch is None or epoch in seen_epoch:
                    continue
                seen_epoch.add(epoch)
                hms = datetime.fromtimestamp(epoch / 1000).astimezone().strftime("%H:%M:%S")
                if not (WINDOW_START <= hms <= WINDOW_END):
                    continue
                stream.write(json.dumps(_json_safe(tick), ensure_ascii=False, separators=(",", ":")) + "\n")
                row = _tick_to_row(tick, epoch)
                feat = engine.update(row)
                runner.on_frame(feat, hms, now, tick)

                if (
                    args.mode in ("live", "sim")
                    and runner.state == "triggered"
                    and now <= hard_deadline_at
                ):
                    _live_submit_models(
                        runner,
                        trader,
                        account,
                        exec_model=args.exec_model,
                        amount_yuan=args.amount,
                        offsets=offsets,
                        tranches_by_trigger=tranches_by_trigger,
                        remark=remark,
                        output_dir=output_dir,
                        summary=summary,
                    )

            if args.mode in ("live", "sim") and datetime.now().astimezone() > hard_deadline_at:
                if runner.state in ("triggered", "submitted"):
                    runner.log("hard_stop_no_order_action", hms=datetime.now().astimezone().strftime("%H:%M:%S"))
                    runner.state = "hard_stopped"
                break
            time.sleep(0.5)

        summary["state"] = runner.state
        summary["trigger_type"] = runner.trigger_type
        summary["trigger_hms"] = runner.trigger_hms
        summary["limit_price"] = runner.limit_price
        summary["filled"] = runner.filled
        summary["fill_hms"] = runner.fill_hms
        summary["order_id"] = runner.order_id
        summary["cancelled"] = runner.cancelled
        summary["events"] = runner.events
        summary["finished_at"] = datetime.now().astimezone().isoformat()
        _write_summary(summary, output_dir)
        print(json.dumps({k: v for k, v in summary.items() if k != "events"}, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        summary["state"] = "error"
        summary["error"] = f"{type(exc).__name__}: {exc}"
        summary["finished_at"] = datetime.now().astimezone().isoformat()
        _write_summary(summary, output_dir)
        raise
    finally:
        if quote_sequence:
            try:
                xtdata.unsubscribe_quote(quote_sequence)
            except Exception:
                pass
        if trader is not None:
            try:
                trader.stop()
            except Exception:
                pass
        stream.close()


if __name__ == "__main__":
    raise SystemExit(main())
