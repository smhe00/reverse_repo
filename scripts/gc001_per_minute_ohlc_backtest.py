"""GC001 逐分钟回测（244 天 1 分钟 OHLC 版，纯数据研究，不下单）。

与 tick 版（gc001_per_minute_signal_backtest.py）互补：
- tick 版用五档盘口计算 eat/wallgone/jump/ofi，数据只有 2 个上午段；
- 本脚本用 244 天 1 分钟 OHLC 把"每个交易分钟决策"放到统计上有意义的
  样本上，寻找一般规律（哪些分钟、哪些量价状态更可能上行），
  并用参数网格 + 按天留一上下文赌博机评估。

成交代理（保守）：
  每个交易分钟 t 用其 open 作为决策基准，挂 1000 元限价卖单
  L = open + offset*tick；后续 hold 分钟内任一分钟 high >= L 视为成交，
  成交价按 L 计；未成交收益 0。
收益 = (L - open) * 100（bp）。

注意：1 分钟数据没有五档盘口，无法计算 eat/wallgone 等盘口信号；
本脚本研究的是"分钟级量价规律 + 参数选择"，与盘口信号回测互补。
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, time as clock_time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


TICK = 0.005
TRADING_SESSIONS = (
    (clock_time(9, 30, 0), clock_time(11, 30, 0)),
    (clock_time(13, 0, 0), clock_time(15, 0, 0)),
)
DEFAULT_INPUT = (
    r"D:\gitee\miniQMT\data\gc001_kronos\gc001_1m_extended_20260806.csv"
)


def load_ohlc(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path, encoding="utf-8-sig")
    frame["timestamp"] = pd.to_datetime(frame["timestamp"])
    frame = frame.set_index("timestamp").sort_index()
    frame["trade_date"] = frame.index.date.astype(str)
    for column in ("open", "high", "low", "close", "volume", "amount"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame.dropna(subset=["open", "high", "low", "close"])


def _session_segments(frame: pd.DataFrame) -> list[dict[str, Any]]:
    """返回每个日/会话的 numpy 数组（一次提取，供多次复用）。"""
    segments: list[dict[str, Any]] = []
    for _, day in frame.groupby("trade_date", sort=True):
        trade_date = str(day["trade_date"].iloc[0])
        for session_start, session_end in TRADING_SESSIONS:
            start_at = pd.Timestamp.combine(
                pd.Timestamp(trade_date).date(),
                session_start,
            )
            end_at = pd.Timestamp.combine(
                pd.Timestamp(trade_date).date(),
                session_end,
            )
            segment = day[(day.index >= start_at) & (day.index < end_at)]
            if len(segment) < 2:
                continue
            minutes = segment.index
            segments.append(
                {
                    "trade_date": trade_date,
                    "minutes": [m.strftime("%H:%M:%S") for m in minutes],
                    "minute_index": [
                        int(m.strftime("%H%M%S")) for m in minutes
                    ],
                    "open": segment["open"].to_numpy(dtype=float),
                    "close": segment["close"].to_numpy(dtype=float),
                    "high": segment["high"].to_numpy(dtype=float),
                    "low": segment["low"].to_numpy(dtype=float),
                    "volume": segment["volume"].to_numpy(dtype=float),
                    "amount": segment["amount"].to_numpy(dtype=float),
                }
            )
    return segments


def _future_high_max(highs: np.ndarray, hold: int) -> np.ndarray:
    """对每根 1 分钟 bar，返回后续 hold 分钟内 high 的最大值（不含当前）。"""
    n = len(highs)
    out = np.full(n, np.nan)
    if n < 2:
        return out
    # 向量化：滚动窗口最大值用累积最大值的差分实现（O(n)）。
    window = min(hold, n - 1)
    # 从后向前做滑动最大值，等价于 future max 且不引入 pandas 开销。
    reversed_cummax = np.maximum.accumulate(highs[::-1])
    future_from_end = reversed_cummax[::-1]
    out[:-1] = future_from_end[1:]
    # 上式计算的是"到收盘为止的最大值"，再按 hold 截断：用窗口内的
    # 最大值，通过反向累积最大在窗口边界处对齐。
    for i in range(n - 1):
        end = min(n, i + 1 + window)
        out[i] = highs[i + 1 : end].max()
    return out


def _episode_arrays(
    segments: list[dict[str, Any]],
    *,
    offset: int,
    hold: int,
) -> pd.DataFrame:
    """对已提取的会话数组一次性生成所有 episode（向量化）。"""
    episodes: list[dict[str, Any]] = []
    for segment in segments:
        trade_date = str(segment["trade_date"])
        minutes = segment["minutes"]
        minute_index = segment["minute_index"]
        open_values = segment["open"]
        close_values = segment["close"]
        high_values = segment["high"]
        low_values = segment["low"]
        volume_values = segment["volume"]
        amount_values = segment["amount"]
        future_high = _future_high_max(high_values, hold)
        n = len(open_values)
        valid = np.isfinite(open_values) & (open_values > 0)
        for i in range(n):
            if not valid[i]:
                continue
            decision_open = float(open_values[i])
            limit = round((decision_open + offset * TICK) * 1000) / 1000
            filled = bool(
                np.isfinite(future_high[i])
                and future_high[i] >= limit - 1e-9
            )
            reward_bp = (
                round((limit - decision_open) * 100, 4) if filled else 0.0
            )
            episodes.append(
                {
                    "trade_date": trade_date,
                    "minute": minutes[i],
                    "minute_index": minute_index[i],
                    "open": decision_open,
                    "close": float(close_values[i]),
                    "high": float(high_values[i]),
                    "low": float(low_values[i]),
                    "range_bp": round(
                        (float(high_values[i]) - float(low_values[i])) * 100, 4
                    ),
                    "body_bp": round(
                        (float(close_values[i]) - decision_open) * 100, 4
                    ),
                    "volume_log": round(
                        float(np.log(max(float(volume_values[i]), 1.0))), 4
                    ),
                    "amount_log": round(
                        float(np.log(max(float(amount_values[i]), 1.0))), 4
                    ),
                    "offset": offset,
                    "hold": hold,
                    "limit_price": limit,
                    "filled": int(filled),
                    "reward_bp": reward_bp,
                }
            )
    return pd.DataFrame(episodes)


def build_episodes(frame: pd.DataFrame, *, offset: int, hold: int) -> pd.DataFrame:
    """每个交易分钟一个 episode（兼容入口，内部走缓存数组）。"""
    return _episode_arrays(_session_segments(frame), offset=offset, hold=hold)


FEATURE_COLUMNS = [
    "minute_index",
    "range_bp",
    "body_bp",
    "volume_log",
    "amount_log",
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


def run_grid(frame: pd.DataFrame) -> pd.DataFrame:
    """在 offset × hold 上做网格。hold 每次只计算一次未来最高价，
    offset 复用同一批数组，避免重复扫描。"""
    segments = _session_segments(frame)
    rows: list[dict[str, Any]] = []
    for offset in (0, 1, 2, 3, 4, 5, 6, 8, 10):
        for hold in (1, 2, 3, 5, 10, 30):
            episodes = _episode_arrays(segments, offset=offset, hold=hold)
            if episodes.empty:
                continue
            rows.append(
                {
                    "offset": offset,
                    "hold_minutes": hold,
                    "n_minutes": len(episodes),
                    "n_signals": int((episodes["filled"] == 1).sum()),
                    "fill_rate": round(episodes["filled"].mean(), 4),
                    "avg_reward_bp": round(episodes["reward_bp"].mean(), 4),
                    "total_reward_bp": round(episodes["reward_bp"].sum(), 4),
                }
            )
    grid = pd.DataFrame(rows)
    if grid.empty:
        return grid
    return grid.sort_values(
        ["avg_reward_bp", "total_reward_bp"],
        ascending=False,
    )


def contextual_bandit_evaluate(
    episodes: pd.DataFrame,
    *,
    ridge_alpha: float = 1.0,
) -> dict[str, Any]:
    """按天留一的线性上下文赌博机：动作 = 是否交易（成交/不成交），
    特征预测收益，与"全交易"基线和"空仓"基线对比。"""
    if episodes["trade_date"].nunique() < 2:
        return {"error": "need at least two trading days"}
    dates = sorted(episodes["trade_date"].unique())
    X = _design_matrix(episodes)
    oos_reward: list[float] = []
    oos_always: list[float] = []
    for test_date in dates:
        train_idx = episodes["trade_date"] != test_date
        test_idx = episodes["trade_date"] == test_date
        X_train_raw = X[train_idx.to_numpy()]
        X_test_raw = X[test_idx.to_numpy()]
        X_train, mean, scale = _standardize(X_train_raw)
        X_test, _, _ = _standardize(X_test_raw, mean=mean, scale=scale)
        y_train = episodes.loc[train_idx, "reward_bp"].to_numpy()
        try:
            coeff = np.linalg.solve(
                X_train.T @ X_train
                + ridge_alpha * np.eye(X_train.shape[1]),
                X_train.T @ y_train,
            )
        except np.linalg.LinAlgError:
            coeff = np.zeros(X_train.shape[1])
        predictions = X_test @ coeff
        test_eps = episodes.loc[test_idx]
        oos_reward.extend(
            np.where(predictions > 0, test_eps["reward_bp"].to_numpy(), 0.0).tolist()
        )
        oos_always.extend(test_eps["reward_bp"].to_numpy().tolist())
    return {
        "oos_mean_reward_bp": round(float(np.mean(oos_reward)), 4),
        "oos_total_reward_bp": round(float(np.sum(oos_reward)), 4),
        "oos_n": len(oos_reward),
        "baseline_always_act_mean_bp": round(float(np.mean(oos_always)), 4),
        "baseline_always_act_total_bp": round(float(np.sum(oos_always)), 4),
        "method": (
            "ridge contextual bandit, leave-one-day-out, "
            "trade if predicted reward > 0"
        ),
    }


def render_report(
    episodes: pd.DataFrame,
    grid: pd.DataFrame,
    bandit: dict[str, Any],
) -> str:
    def table(frame: pd.DataFrame, limit: int = 40) -> str:
        if frame.empty:
            return "(空)"
        return frame.head(limit).to_string(index=False)

    lines = [
        "# GC001 逐分钟回测（244 天 1 分钟 OHLC）",
        "",
        f"- episode 数：{len(episodes)}",
        f"- 交易日：{episodes['trade_date'].nunique()}",
        "- 成交代理：分钟 open + offset*tick 挂单，后续 hold 分钟内 high >= L 即成交",
        "",
        "## 参数网格（offset × hold）",
        "",
    ]
    lines.append(table(grid))
    lines.append("")
    lines.append("## 上下文赌博机（按天留一）")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(bandit, ensure_ascii=False, indent=2))
    lines.append("```")
    lines.append("")
    lines.append("> 仅基于 1 分钟 OHLC 的研究结果；无五档盘口，不能计算 eat/wallgone。")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="GC001 逐分钟 OHLC 回测（244 天，只读研究工具）"
    )
    parser.add_argument("--input", default=DEFAULT_INPUT)
    parser.add_argument("--offset", type=int, default=3)
    parser.add_argument("--hold", type=int, default=3)
    parser.add_argument("--output", default="reports/gc001_per_minute_ohlc")
    args = parser.parse_args()

    frame = load_ohlc(Path(args.input))
    episodes = build_episodes(frame, offset=args.offset, hold=args.hold)
    grid = run_grid(frame)
    bandit = contextual_bandit_evaluate(episodes)

    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    episodes.to_csv(output / "episodes.csv", index=False)
    if not grid.empty:
        grid.to_csv(output / "grid.csv", index=False)
    (output / "report.md").write_text(
        render_report(episodes, grid, bandit),
        encoding="utf-8",
    )
    print(f"output={output.resolve()}")
    print(f"episodes={len(episodes)}")
    print("grid:")
    print(grid.head(12).to_string(index=False) if not grid.empty else "(空)")
    print(json.dumps(bandit, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
