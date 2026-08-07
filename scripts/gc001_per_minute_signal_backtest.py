"""逐分钟 GC001 盘口信号回测框架（只读研究工具，不下单）。

目标：验证"用每个交易分钟开头几根 tick 即可决策"的假设，并把 eat /
wallgone / jump / ofi 信号从单一 09:30 窗口扩展到全天任意分钟。

设计：
1. 读取 QMT L1 五档 tick JSONL（多日可合并）。
2. 对每个交易日、每个交易分钟，取该分钟开头 decision_seconds 秒内的
   tick（或最少 min_ticks 根），喂入增量信号引擎得到 BookFeatures，
   用 triggers()/pick_trigger() 判定触发。
3. 若触发，模拟挂 1000 元限价卖单（anchor + offset*tick），成交代理：
   后续 hold_seconds 秒内任一帧 ask1 >= L 或 lastPrice >= L 即视为成交，
   成交价按 L 计；未成交收益记 0。
4. 奖励 = 成交收益（bp）= (L - 决策时刻 rate) * 100；未成交 = 0。
5. 输出逐分钟 episode CSV、按触发/offset 汇总、参数网格结果，
   以及"上下文赌博机"（按天留一交叉验证）的学习策略评估。

仅使用盘口数据本身，不连接交易通道；结果只用于研究，不代表真实成交。
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, time as clock_time, timedelta
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

from gc001_book_signal import (
    BookFeatures,
    IncrementalSignalEngine,
    TICK,
    pick_trigger,
    price_to_tick,
    triggers,
)


SYMBOL = "204001.SH"
TRIGGER_NAMES = ("eat", "wallgone", "jump", "ofi")
DEFAULT_OFFSETS = {"eat": 2, "wallgone": 6, "jump": 2, "ofi": 2}
TRADING_SESSIONS = (
    (clock_time(9, 30, 0), clock_time(11, 30, 0)),
    (clock_time(13, 0, 0), clock_time(15, 0, 0)),
)
QMT_FACE_VALUE_YUAN = 100


@dataclass(frozen=True)
class BacktestConfig:
    principal_yuan: int = 1_000
    decision_seconds: float = 6.0
    min_ticks: int = 2
    hold_seconds: float = 60.0
    anchor: str = "ask1"
    offsets: dict[str, int] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.offsets is None:
            object.__setattr__(self, "offsets", dict(DEFAULT_OFFSETS))
        if self.anchor not in ("ask1", "micro1"):
            raise ValueError("anchor must be ask1 or micro1")
        if self.principal_yuan < 1_000 or self.principal_yuan % 1_000:
            raise ValueError("principal must be a multiple of 1,000 yuan")


def load_tick_files(paths: Iterable[Path]) -> pd.DataFrame:
    """Load QMT L1 tick JSONL files into a normalized DataFrame."""
    rows: list[dict[str, Any]] = []
    for path in paths:
        p = Path(path)
        if not p.is_file():
            continue
        with p.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    continue
                epoch = int(raw.get("time") or raw.get("_exchange_time_epoch_ms") or 0)
                if epoch <= 0:
                    continue
                ask_p = [float(v) for v in (raw.get("askPrice") or [])[:5]]
                ask_v = [float(v) for v in (raw.get("askVol") or [])[:5]]
                bid_p = [float(v) for v in (raw.get("bidPrice") or [])[:5]]
                bid_v = [float(v) for v in (raw.get("bidVol") or [])[:5]]
                rows.append(
                    {
                        "epoch_ms": epoch,
                        "time": datetime.fromtimestamp(epoch / 1000).astimezone(),
                        "lastPrice": float(raw.get("lastPrice", 0.0) or 0.0),
                        "askPrice": ask_p,
                        "askVol": ask_v,
                        "bidPrice": bid_p,
                        "bidVol": bid_v,
                    }
                )
    if not rows:
        raise ValueError("no tick records loaded")
    frame = pd.DataFrame(rows)
    frame["trade_date"] = frame["time"].dt.date.astype(str)
    frame["hms"] = frame["time"].dt.strftime("%H:%M:%S")
    return frame.sort_values(["trade_date", "epoch_ms"]).reset_index(drop=True)


def _session_minutes(trade_date: str) -> list[datetime]:
    tz = datetime.now().astimezone().tzinfo
    out: list[datetime] = []
    for start, end in TRADING_SESSIONS:
        current = datetime.combine(
            datetime.strptime(trade_date, "%Y-%m-%d").date(),
            start,
            tzinfo=tz,
        )
        end_at = datetime.combine(
            datetime.strptime(trade_date, "%Y-%m-%d").date(),
            end,
            tzinfo=tz,
        )
        while current < end_at:
            out.append(current)
            current += timedelta(minutes=1)
    return out


def _decision_anchor(feature: BookFeatures, anchor: str) -> float | None:
    value = feature.ask1 if anchor == "ask1" else feature.micro1
    if value is None or not np.isfinite(float(value)) or float(value) <= 0:
        return None
    return float(value)


def _fill_proxy(
    subsequent: pd.DataFrame,
    limit: float,
) -> bool:
    if subsequent.empty:
        return False
    asks = np.concatenate(subsequent["askPrice"].to_numpy())
    asks = asks[np.isfinite(asks)]
    last = subsequent["lastPrice"].to_numpy()
    return bool(
        (len(asks) > 0 and np.any(asks >= limit - 1e-9))
        or np.any(last >= limit - 1e-9)
    )


def build_episodes(
    ticks: pd.DataFrame,
    config: BacktestConfig,
) -> pd.DataFrame:
    """为每个交易日每个交易分钟生成一个决策 episode。"""
    episodes: list[dict[str, Any]] = []
    for trade_date, day in ticks.groupby("trade_date", sort=True):
        for minute_start in _session_minutes(trade_date):
            minute_end = minute_start + timedelta(minutes=1)
            window_end = minute_start + timedelta(seconds=config.decision_seconds)
            decision = day[
                (day["time"] >= minute_start) & (day["time"] < window_end)
            ].sort_values("epoch_ms")
            if len(decision) < config.min_ticks:
                continue
            engine = IncrementalSignalEngine()
            last_feature: BookFeatures | None = None
            for _, row in decision.iterrows():
                last_feature = engine.update(
                    {
                        "ts": row["time"],
                        "hms": row["hms"],
                        "lastPrice": float(row["lastPrice"]),
                        "askPrice": list(row["askPrice"]),
                        "askVol": list(row["askVol"]),
                        "bidPrice": list(row["bidPrice"]),
                        "bidVol": list(row["bidVol"]),
                    }
                )
            if last_feature is None:
                continue
            fired = triggers(last_feature)
            trigger = pick_trigger(fired) if fired else None
            anchor = _decision_anchor(last_feature, config.anchor)
            decision_rate = float(decision["lastPrice"].iloc[-1])
            if anchor is None or decision_rate <= 0:
                continue
            outcome_end = minute_start + timedelta(seconds=config.hold_seconds)
            subsequent = day[
                (day["time"] > decision["time"].iloc[-1])
                & (day["time"] <= outcome_end)
            ]
            reward_bp = 0.0
            limit_price: float | None = None
            filled = False
            if trigger is not None:
                offset = config.offsets.get(trigger, 0)
                limit_price = price_to_tick(
                    anchor + offset * TICK,
                    direction="up",
                )
                filled = _fill_proxy(subsequent, limit_price)
                if filled:
                    reward_bp = round((limit_price - decision_rate) * 100, 4)
            episodes.append(
                {
                    "trade_date": trade_date,
                    "minute": minute_start.strftime("%H:%M:%S"),
                    "epoch_ms": int(minute_start.timestamp() * 1000),
                    "decision_ticks": len(decision),
                    "lastPrice": decision_rate,
                    "ask1": last_feature.ask1,
                    "micro1": last_feature.micro1,
                    "d_micro1_ticks": (
                        round(last_feature.d_micro1 / TICK, 3)
                        if np.isfinite(last_feature.d_micro1)
                        else 0.0
                    ),
                    "d_ask1_ticks": (
                        round(last_feature.d_ask1 / TICK, 3)
                        if np.isfinite(last_feature.d_ask1)
                        else 0.0
                    ),
                    "ask_eaten_frac": round(last_feature.ask_eaten_frac, 4),
                    "ask_cancel_frac": round(last_feature.ask_cancel_frac, 4),
                    "ofi_norm": round(last_feature.ofi_norm, 4),
                    "wall_disappear": int(bool(last_feature.wall_disappear)),
                    "imb": round(last_feature.imb, 4),
                    "spread_ticks": (
                        round((last_feature.ask1 - last_feature.bid1) / TICK, 3)
                        if (
                            np.isfinite(last_feature.ask1)
                            and np.isfinite(last_feature.bid1)
                        )
                        else 0.0
                    ),
                    "tot_ask_log": round(
                        float(np.log(max(last_feature.tot_ask, 1.0))), 4
                    ),
                    "tot_bid_log": round(
                        float(np.log(max(last_feature.tot_bid, 1.0))), 4
                    ),
                    "trigger": trigger or "none",
                    "limit_price": limit_price,
                    "filled": int(filled),
                    "reward_bp": reward_bp,
                }
            )
    return pd.DataFrame(episodes)


def summarize_episodes(episodes: pd.DataFrame) -> pd.DataFrame:
    if episodes.empty:
        return pd.DataFrame()
    rows: list[dict[str, Any]] = []
    for trigger, group in episodes.groupby("trigger"):
        rows.append(
            {
                "trigger": trigger,
                "n": len(group),
                "fill_rate": round(group["filled"].mean(), 4),
                "avg_reward_bp_filled": round(
                    group.loc[group["filled"] == 1, "reward_bp"].mean(), 4
                )
                if (group["filled"] == 1).any()
                else 0.0,
                "avg_reward_bp_all": round(group["reward_bp"].mean(), 4),
                "total_reward_bp": round(group["reward_bp"].sum(), 4),
            }
        )
    return pd.DataFrame(rows).sort_values("total_reward_bp", ascending=False)


def run_grid(
    episodes_factory,
    ticks: pd.DataFrame,
    base: BacktestConfig,
) -> pd.DataFrame:
    """在 offsets / hold / decision_seconds 上做小网格，返回汇总表。"""
    rows: list[dict[str, Any]] = []
    for offset in (0, 1, 2, 3, 4, 5, 6):
        for hold in (30.0, 60.0, 90.0):
            config = BacktestConfig(
                principal_yuan=base.principal_yuan,
                decision_seconds=base.decision_seconds,
                min_ticks=base.min_ticks,
                hold_seconds=hold,
                anchor=base.anchor,
                offsets={name: offset for name in TRIGGER_NAMES},
            )
            eps = episodes_factory(ticks, config)
            if eps.empty:
                continue
            fired = eps[eps["trigger"] != "none"]
            if fired.empty:
                continue
            rows.append(
                {
                    "offset": offset,
                    "hold_seconds": int(hold),
                    "n_minutes": len(eps),
                    "n_signals": len(fired),
                    "fill_rate": round(fired["filled"].mean(), 4),
                    "avg_reward_bp": round(fired["reward_bp"].mean(), 4),
                    "total_reward_bp": round(fired["reward_bp"].sum(), 4),
                }
            )
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    return frame.sort_values(
        ["total_reward_bp", "fill_rate"],
        ascending=False,
    )


FEATURE_COLUMNS = [
    "d_micro1_ticks",
    "d_ask1_ticks",
    "ask_eaten_frac",
    "ask_cancel_frac",
    "ofi_norm",
    "wall_disappear",
    "imb",
    "spread_ticks",
    "tot_ask_log",
    "tot_bid_log",
]


def _design_matrix(episodes: pd.DataFrame) -> np.ndarray:
    values = episodes[FEATURE_COLUMNS].to_numpy(dtype=float)
    return np.column_stack([np.ones(len(values)), values])


def _standardize(
    matrix: np.ndarray,
    *,
    mean: np.ndarray | None = None,
    scale: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    if mean is None:
        mean = matrix.mean(axis=0)
    if scale is None:
        scale = matrix.std(axis=0)
        scale[scale < 1e-12] = 1.0
    return (matrix - mean) / scale, mean, scale


def contextual_bandit_evaluate(
    episodes: pd.DataFrame,
    *,
    ridge_alpha: float = 1.0,
) -> dict[str, Any]:
    """按天留一的线性上下文赌博机评估（仅 numpy，无额外依赖）。

    action = 各触发类型（不触发 = 空仓 0 收益）；用岭回归对每个 action
    拟合 特征→收益，然后每天用"仅前一天拟合"的模型取 argmax 决策。
    报告 OOS 平均收益 vs 固定最优规则 vs 空仓基线。
    """
    if episodes.empty or episodes["trade_date"].nunique() < 2:
        return {"error": "need at least two trading days for leave-one-day-out"}
    dates = sorted(episodes["trade_date"].unique())
    actions = [None] + list(TRIGGER_NAMES)  # None = 不触发
    X = _design_matrix(episodes)
    oos_reward: list[float] = []
    oos_baseline: list[float] = []
    oos_always_act: list[float] = []
    chosen_actions: list[str] = []
    for test_date in dates:
        train_idx = episodes["trade_date"] != test_date
        test_idx = episodes["trade_date"] == test_date
        X_train_raw = X[train_idx.to_numpy()]
        X_test_raw = X[test_idx.to_numpy()]
        X_train, mean, scale = _standardize(X_train_raw)
        X_test, _, _ = _standardize(X_test_raw, mean=mean, scale=scale)
        test_eps = episodes.loc[test_idx]
        models: dict[str, np.ndarray] = {}
        for action in actions:
            mask = (test_eps["trigger"].to_numpy() == action) if action else np.zeros(
                len(test_eps), dtype=bool
            )
            # 训练标签：仅在该 action 实际触发且有信号时才把奖励计入该 action
            # 的价值；空仓 action 恒为 0。为减少小样本噪声，用全局特征岭回归
            # 对该 action 的样本拟合。
            sample_idx = np.zeros(len(episodes), dtype=bool)
            sample_idx[train_idx.to_numpy()] = (
                episodes.loc[train_idx, "trigger"].to_numpy() == action
            ) if action else np.zeros(train_idx.sum(), dtype=bool)
            if action is None:
                models[action] = np.zeros(X_train.shape[1])
                continue
            if sample_idx.sum() < 3:
                models[action] = np.zeros(X_train.shape[1])
                continue
            Xa, _, _ = _standardize(
                X[sample_idx],
                mean=mean,
                scale=scale,
            )
            ya = episodes.loc[sample_idx, "reward_bp"].to_numpy()
            try:
                coeff = np.linalg.solve(
                    Xa.T @ Xa
                    + ridge_alpha * np.eye(Xa.shape[1]),
                    Xa.T @ ya,
                )
            except np.linalg.LinAlgError:
                coeff = np.zeros(X_train.shape[1])
            models[action] = coeff
        values = {
            action: (X_test @ models[action]) if action else np.zeros(len(X_test))
            for action in actions
        }
        chosen = np.full(len(X_test), None, dtype=object)
        for i in range(len(X_test)):
            best = max(actions, key=lambda a: float(values[a][i]))
            chosen[i] = best
        chosen_actions.extend(
            "none" if c is None else str(c) for c in chosen
        )
        # OOS 收益：选择非空仓 action 时用该 action 实际奖励（未触发也计 0），
        # 空仓计 0。同时记录两个对照基线：
        #   always_act = 任何信号触发都按最优固定 offset 交易；
        #   no_trade   = 永远不交易（收益恒 0）。
        always_act_reward = test_eps["reward_bp"].to_numpy().astype(float)
        always_act_reward[test_eps["trigger"] == "none"] = 0.0
        for i, action in enumerate(chosen):
            if action is None:
                oos_reward.append(0.0)
            else:
                actual = test_eps.iloc[i]
                oos_reward.append(
                    float(actual["reward_bp"])
                    if actual["trigger"] == action
                    else 0.0
                )
            oos_baseline.append(0.0)
        oos_always_act.extend(always_act_reward.tolist())
    return {
        "oos_mean_reward_bp": round(float(np.mean(oos_reward)), 4),
        "oos_total_reward_bp": round(float(np.sum(oos_reward)), 4),
        "oos_n": len(oos_reward),
        "chosen_action_counts": {
            name: chosen_actions.count(name) for name in set(chosen_actions)
        },
        "baseline_mean_reward_bp": round(float(np.mean(oos_baseline)), 4),
        "baseline_always_act_mean_bp": round(
            float(np.mean(oos_always_act)), 4
        ),
        "baseline_always_act_total_bp": round(
            float(np.sum(oos_always_act)), 4
        ),
        "method": (
            "ridge contextual bandit, leave-one-day-out, "
            "action = trigger or no-trade"
        ),
    }


def render_report(
    episodes: pd.DataFrame,
    summary: pd.DataFrame,
    grid: pd.DataFrame,
    bandit: dict[str, Any],
) -> str:
    def table(frame: pd.DataFrame, limit: int = 40) -> str:
        if frame.empty:
            return "(空)"
        return frame.head(limit).to_string(index=False)

    lines = [
        "# GC001 逐分钟盘口信号回测",
        "",
        f"- episode 数：{len(episodes)}",
        f"- 交易日：{sorted(episodes['trade_date'].unique())}",
        "- 金额：1000 元/笔；成交代理：ask1 或 lastPrice 到达限价即成交",
        "",
        "## 按触发类型汇总",
        "",
    ]
    if not summary.empty:
        lines.append(table(summary))
    lines.append("")
    lines.append("## 参数网格（offset x hold）")
    lines.append("")
    if not grid.empty:
        lines.append(table(grid))
    lines.append("")
    lines.append("## 上下文赌博机（按天留一）")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(bandit, ensure_ascii=False, indent=2))
    lines.append("```")
    lines.append("")
    lines.append("> 仅基于盘口快照的研究结果，不代表真实成交；数据量小，结论仅供方向参考。")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="GC001 逐分钟盘口信号回测（只读研究工具）"
    )
    parser.add_argument(
        "--input",
        nargs="+",
        default=[
            r"D:\gitee\miniQMT\data\gc001_ticks\date=20260731\gc001_qmt_l1_ticks.jsonl",
            r"D:\gitee\miniQMT\data\gc001_morning\date=20260806\gc001_morning_l1_from_cache.jsonl",
            r"D:\gitee\miniQMT\data\gc001_validation\date=20260807\gc001_validation_l1.jsonl",
        ],
    )
    parser.add_argument("--principal", type=int, default=1_000)
    parser.add_argument("--decision-seconds", type=float, default=6.0)
    parser.add_argument("--min-ticks", type=int, default=2)
    parser.add_argument("--hold-seconds", type=float, default=60.0)
    parser.add_argument("--anchor", choices=("ask1", "micro1"), default="ask1")
    parser.add_argument("--output", default="reports/gc001_per_minute_signal")
    args = parser.parse_args()

    ticks = load_tick_files(Path(p) for p in args.input)
    config = BacktestConfig(
        principal_yuan=args.principal,
        decision_seconds=args.decision_seconds,
        min_ticks=args.min_ticks,
        hold_seconds=args.hold_seconds,
        anchor=args.anchor,
    )
    episodes = build_episodes(ticks, config)
    summary = summarize_episodes(episodes)
    grid = run_grid(lambda t, c: build_episodes(t, c), ticks, config)
    bandit = contextual_bandit_evaluate(episodes)

    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    episodes.to_csv(output / "episodes.csv", index=False)
    if not summary.empty:
        summary.to_csv(output / "summary.csv", index=False)
    if not grid.empty:
        grid.to_csv(output / "grid.csv", index=False)
    (output / "report.md").write_text(
        render_report(episodes, summary, grid, bandit),
        encoding="utf-8",
    )
    print(f"output={output.resolve()}")
    print(f"episodes={len(episodes)}")
    if not summary.empty:
        print(summary.to_string(index=False))
    print(json.dumps(bandit, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
