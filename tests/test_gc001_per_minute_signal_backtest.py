from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from gc001_per_minute_signal_backtest import (  # noqa: E402
    BacktestConfig,
    build_episodes,
    contextual_bandit_evaluate,
    load_tick_files,
    summarize_episodes,
)


def _tick(
    *,
    epoch_ms: int,
    last: float,
    ask: list[float],
    ask_vol: list[float],
    bid: list[float],
    bid_vol: list[float],
) -> dict[str, object]:
    return {
        "time": epoch_ms,
        "lastPrice": last,
        "askPrice": ask,
        "askVol": ask_vol,
        "bidPrice": bid,
        "bidVol": bid_vol,
        "symbol": "204001.SH",
    }


def _write_ticks(path: Path, ticks: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for tick in ticks:
            handle.write(json.dumps(tick) + "\n")


class PerMinuteSignalBacktestTests(unittest.TestCase):
    def test_load_tick_files_normalizes_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ticks.jsonl"
            base = int(
                datetime(2026, 8, 7, 9, 30, tzinfo=timezone(timedelta(hours=8))).timestamp()
                * 1000
            )
            _write_ticks(
                path,
                [
                    _tick(
                        epoch_ms=base,
                        last=1.30,
                        ask=[1.305, 1.31, 0.0, 0.0, 0.0],
                        ask_vol=[1000.0, 500.0, 0.0, 0.0, 0.0],
                        bid=[1.30, 1.295, 0.0, 0.0, 0.0],
                        bid_vol=[800.0, 400.0, 0.0, 0.0, 0.0],
                    ),
                    _tick(
                        epoch_ms=base + 1000,
                        last=1.305,
                        ask=[1.31, 1.315, 0.0, 0.0, 0.0],
                        ask_vol=[900.0, 600.0, 0.0, 0.0, 0.0],
                        bid=[1.305, 1.30, 0.0, 0.0, 0.0],
                        bid_vol=[700.0, 500.0, 0.0, 0.0, 0.0],
                    ),
                ],
            )
            frame = load_tick_files([path])
            self.assertEqual(len(frame), 2)
            self.assertEqual(frame["trade_date"].iloc[0], "2026-08-07")
            self.assertEqual(frame["lastPrice"].iloc[0], 1.30)
            self.assertEqual(list(frame["askPrice"].iloc[0][:2]), [1.305, 1.31])

    def test_build_episodes_produces_minutes_and_trigger(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ticks.jsonl"
            tz = timezone(timedelta(hours=8))
            base = int(datetime(2026, 8, 7, 9, 30, tzinfo=tz).timestamp() * 1000)
            ticks: list[dict[str, object]] = []
            # 两帧：卖一被消耗 + 价格上行，构造 eat 触发。
            ticks.append(
                _tick(
                    epoch_ms=base,
                    last=1.30,
                    ask=[1.305, 1.31, 1.315, 1.32, 1.325],
                    ask_vol=[10000.0, 8000.0, 5000.0, 3000.0, 1000.0],
                    bid=[1.30, 1.295, 1.29, 1.285, 1.28],
                    bid_vol=[9000.0, 7000.0, 4000.0, 2000.0, 1000.0],
                )
            )
            ticks.append(
                _tick(
                    epoch_ms=base + 1000,
                    last=1.315,
                    ask=[1.315, 1.32, 1.325, 1.33, 1.335],
                    ask_vol=[3000.0, 6000.0, 4000.0, 2000.0, 1000.0],
                    bid=[1.31, 1.305, 1.30, 1.295, 1.29],
                    bid_vol=[8000.0, 6000.0, 3000.0, 2000.0, 1000.0],
                )
            )
            _write_ticks(path, ticks)
            frame = load_tick_files([path])
            config = BacktestConfig(
                principal_yuan=1_000,
                decision_seconds=3.0,
                min_ticks=2,
                hold_seconds=60.0,
                anchor="ask1",
            )
            episodes = build_episodes(frame, config)
            self.assertEqual(len(episodes), 1)
            row = episodes.iloc[0]
            self.assertEqual(row["minute"], "09:30:00")
            self.assertIn(row["trigger"], {"eat", "jump", "none"})
            self.assertGreaterEqual(row["decision_ticks"], 2)

    def test_summarize_and_bandit_are_deterministic(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ticks.jsonl"
            tz = timezone(timedelta(hours=8))
            ticks: list[dict[str, object]] = []
            # 两个"交易日"各一个 09:30 分钟，用于留一交叉。
            for day in (6, 7):
                base = int(
                    datetime(2026, 8, day, 9, 30, tzinfo=tz).timestamp() * 1000
                )
                ticks.append(
                    _tick(
                        epoch_ms=base,
                        last=1.30,
                        ask=[1.305, 1.31, 1.315, 1.32, 1.325],
                        ask_vol=[10000.0, 8000.0, 5000.0, 3000.0, 1000.0],
                        bid=[1.30, 1.295, 1.29, 1.285, 1.28],
                        bid_vol=[9000.0, 7000.0, 4000.0, 2000.0, 1000.0],
                    )
                )
                ticks.append(
                    _tick(
                        epoch_ms=base + 1000,
                        last=1.315,
                        ask=[1.315, 1.32, 1.325, 1.33, 1.335],
                        ask_vol=[3000.0, 6000.0, 4000.0, 2000.0, 1000.0],
                        bid=[1.31, 1.305, 1.30, 1.295, 1.29],
                        bid_vol=[8000.0, 6000.0, 3000.0, 2000.0, 1000.0],
                    )
                )
            _write_ticks(path, ticks)
            frame = load_tick_files([path])
            config = BacktestConfig()
            episodes = build_episodes(frame, config)
            self.assertGreaterEqual(episodes["trade_date"].nunique(), 2)
            summary = summarize_episodes(episodes)
            self.assertFalse(summary.empty)
            bandit = contextual_bandit_evaluate(episodes)
            self.assertIn("oos_mean_reward_bp", bandit)
            self.assertIn("baseline_always_act_mean_bp", bandit)


if __name__ == "__main__":
    unittest.main()
