from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from gc001_per_minute_ohlc_backtest import (  # noqa: E402
    TICK,
    _future_high_max,
    _session_segments,
    build_episodes,
    contextual_bandit_evaluate,
    load_ohlc,
    minute_pattern_analysis,
    run_grid,
)


def _frame(trade_date: str = "2026-08-06") -> pd.DataFrame:
    index = pd.date_range(f"{trade_date} 09:30:00", periods=8, freq="min")
    index.name = "timestamp"
    rows = [
        (1.30, 1.305, 1.295, 1.30, 1000.0, 1000.0),
        (1.305, 1.310, 1.300, 1.305, 2000.0, 2000.0),
        (1.310, 1.315, 1.305, 1.310, 3000.0, 3000.0),
        (1.315, 1.320, 1.310, 1.315, 4000.0, 4000.0),
        (1.320, 1.325, 1.315, 1.320, 5000.0, 5000.0),
        (1.325, 1.330, 1.320, 1.325, 6000.0, 6000.0),
        (1.330, 1.335, 1.325, 1.330, 7000.0, 7000.0),
        (1.335, 1.340, 1.330, 1.335, 8000.0, 8000.0),
    ]
    frame = pd.DataFrame(
        rows,
        columns=["open", "high", "low", "close", "volume", "amount"],
        index=index,
    )
    frame["trade_date"] = trade_date
    return frame


class PerMinuteOhlcBacktestTests(unittest.TestCase):
    def test_load_ohlc_reads_exported_csv(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gc001_1m.csv"
            _frame().to_csv(path, encoding="utf-8-sig")
            frame = load_ohlc(path)
            self.assertEqual(len(frame), 8)
            self.assertEqual(frame["trade_date"].iloc[0], "2026-08-06")

    def test_future_high_max_is_correct(self):
        highs = _frame()["high"].to_numpy(dtype=float)
        out = _future_high_max(highs, hold=2)
        self.assertEqual(out[0], 1.315)  # 后续 2 分钟最高 1.310/1.315
        self.assertEqual(out[-2], 1.340)  # 最后一根前一根 → 仅剩 1.335
        self.assertTrue(out[-1] != out[-1])  # 最后一根无未来 → NaN

    def test_build_episodes_and_grid_are_deterministic(self):
        frame = _frame()
        episodes = build_episodes(frame, offset=2, hold=3)
        self.assertEqual(len(episodes), 8)
        self.assertIn("reward_bp", episodes.columns)
        grid = run_grid(frame)
        self.assertFalse(grid.empty)
        self.assertIn("fill_rate", grid.columns)
        self.assertGreaterEqual(grid["n_minutes"].iloc[0], 8)

    def test_bandit_requires_two_days(self):
        single = build_episodes(_frame(), offset=2, hold=3)
        result = contextual_bandit_evaluate(single)
        self.assertIn("error", result)
        two = pd.concat(
            [
                build_episodes(_frame(), offset=2, hold=3),
                build_episodes(
                    _frame("2026-08-07"),
                    offset=2,
                    hold=3,
                ),
            ],
            ignore_index=True,
        )
        result = contextual_bandit_evaluate(two)
        self.assertIn("oos_mean_reward_bp", result)
        self.assertIn("baseline_always_act_mean_bp", result)

    def test_minute_pattern_analysis_reports_best_offset_per_minute(self):
        frame = _frame()
        pattern = minute_pattern_analysis(
            frame,
            offsets=(0, 2, 4, 6),
            hold=3,
        )
        self.assertFalse(pattern.empty)
        self.assertIn("minute", pattern.columns)
        self.assertIn("best_offset", pattern.columns)
        self.assertIn("best_avg_reward_bp", pattern.columns)
        # 每分钟一行
        self.assertEqual(len(pattern), 8)
        # 09:30 后价格持续上行，offset 越高成交价越高（但成交率降低），
        # 最优 offset 应 >= 0 且为合法候选之一。
        self.assertIn(int(pattern.loc[0, "best_offset"]), (0, 2, 4, 6))


if __name__ == "__main__":
    unittest.main()
