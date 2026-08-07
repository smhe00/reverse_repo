from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from gc001_book_signal import (  # noqa: E402
    BookFeatures,
    IncrementalSignalEngine,
    TICK,
    WALL_ABS,
    build_book_features,
    pick_trigger,
    price_to_tick,
    triggers,
    _features_from_row,
    _level_map,
)
from gc001_live_microprice_validation import (  # noqa: E402
    ValidationRunner,
    _tick_to_row,
)


def row(
    *,
    ask_prices: list[float],
    ask_vols: list[float],
    bid_prices: list[float],
    bid_vols: list[float],
    last: float,
    ts: str = "2026-08-07 09:30:00",
    hms: str = "09:30:00",
) -> dict[str, object]:
    return {
        "ts": pd.Timestamp(ts),
        "hms": hms,
        "lastPrice": float(last),
        "askPrice": [float(p) for p in ask_prices],
        "askVol": [float(v) for v in ask_vols],
        "bidPrice": [float(p) for p in bid_prices],
        "bidVol": [float(v) for v in bid_vols],
    }


def feat(**overrides: object) -> BookFeatures:
    base: dict[str, object] = {
        "ts": pd.Timestamp("2026-08-07 09:30:00"),
        "hms": "09:30:00",
        "last": 1.495,
        "ask1": 1.500,
        "bid1": 1.495,
        "micro1": 1.4975,
        "micro5": 1.4975,
        "tot_bid": 100000.0,
        "tot_ask": 100000.0,
        "imb": 0.0,
        "ofi": 0.0,
        "ofi_norm": 0.0,
        "touch_ofi": 0.0,
        "ask_lost_frac": 0.0,
        "ask_eaten_frac": 0.0,
        "ask_cancel_frac": 0.0,
        "wall_disappear": False,
        "d_micro1": 0.0,
        "d_ask1": 0.0,
        "d_last": 0.0,
    }
    base.update(overrides)
    return BookFeatures(**base)


class PriceToTickTests(unittest.TestCase):
    def test_up_rounds_up_to_next_tick(self):
        self.assertEqual(price_to_tick(1.5024, direction="up"), 1.505)
        self.assertEqual(price_to_tick(1.5000, direction="up"), 1.500)

    def test_down_rounds_down_to_previous_tick(self):
        self.assertEqual(price_to_tick(1.5025, direction="down"), 1.500)
        self.assertEqual(price_to_tick(1.5076, direction="down"), 1.505)

    def test_exact_tick_is_stable(self):
        for direction in ("up", "down"):
            self.assertEqual(price_to_tick(1.505, direction=direction), 1.505)


class LevelMapTests(unittest.TestCase):
    def test_merges_duplicate_prices_and_skips_none(self):
        merged = _level_map([1.5, 1.5, 1.505, None], [10.0, 20.0, 30.0, None])
        self.assertEqual(merged, {1.5: 30.0, 1.505: 30.0})

    def test_skips_zero_volume(self):
        merged = _level_map([1.5, 1.505], [0.0, 40.0])
        self.assertEqual(merged, {1.505: 40.0})


class FeatureComputationTests(unittest.TestCase):
    def test_first_frame_has_neutral_deltas(self):
        first = _features_from_row(
            row(
                ask_prices=[1.500, 1.505],
                ask_vols=[10000.0, 10000.0],
                bid_prices=[1.495, 1.490],
                bid_vols=[10000.0, 10000.0],
                last=1.495,
            ),
            prev=None,
            prev2=None,
            prev_feat=None,
        )
        self.assertEqual(first.d_micro1, 0.0)
        self.assertEqual(first.d_last, 0.0)
        self.assertTrue(np.isnan(first.d_ask1))
        self.assertTrue(np.isnan(first.ofi))
        self.assertEqual(first.ask_eaten_frac, 0.0)
        self.assertEqual(first.ask_cancel_frac, 0.0)
        self.assertFalse(first.wall_disappear)

    def test_microprice_uses_top_of_book_volumes(self):
        frame = _features_from_row(
            row(
                ask_prices=[1.500, 1.505],
                ask_vols=[3000.0, 10000.0],
                bid_prices=[1.495, 1.490],
                bid_vols=[10000.0, 5000.0],
                last=1.498,
            ),
            prev=None,
            prev2=None,
            prev_feat=None,
        )
        expected = (1.495 * 3000 + 1.500 * 10000) / 13000
        self.assertAlmostEqual(frame.micro1, expected, places=9)

    def test_micro5_is_volume_weighted_across_levels(self):
        frame = _features_from_row(
            row(
                ask_prices=[1.500, 1.505],
                ask_vols=[10000.0, 10000.0],
                bid_prices=[1.495, 1.490],
                bid_vols=[10000.0, 10000.0],
                last=1.497,
            ),
            prev=None,
            prev2=None,
            prev_feat=None,
        )
        numerator = 1.5 * 10000 + 1.505 * 10000 + 1.495 * 10000 + 1.490 * 10000
        self.assertAlmostEqual(frame.micro5, numerator / 40000, places=9)

    def test_rising_frame_classifies_ask_loss_as_eaten(self):
        prev = row(
            ask_prices=[1.500, 1.505],
            ask_vols=[10000.0, 10000.0],
            bid_prices=[1.495],
            bid_vols=[10000.0],
            last=1.495,
            hms="09:30:01",
        )
        current = row(
            ask_prices=[1.505, 1.510],
            ask_vols=[5000.0, 10000.0],
            bid_prices=[1.500],
            bid_vols=[10000.0],
            last=1.500,
            hms="09:30:02",
        )
        prev_feat = _features_from_row(prev, prev=None, prev2=None, prev_feat=None)
        frame = _features_from_row(
            current,
            prev=prev,
            prev2=None,
            prev_feat=prev_feat,
        )
        # 1.500 gone (p <= last) counts as eaten, 1.505 same-price loss counts as eaten.
        self.assertAlmostEqual(frame.ask_eaten_frac, 15000 / 20000, places=9)
        self.assertEqual(frame.ask_cancel_frac, 0.0)
        self.assertAlmostEqual(frame.d_ask1, TICK, places=9)

    def test_falling_frame_classifies_ask_loss_as_cancel(self):
        prev = row(
            ask_prices=[1.500, 1.505],
            ask_vols=[10000.0, 10000.0],
            bid_prices=[1.495],
            bid_vols=[10000.0],
            last=1.498,
            hms="09:30:01",
        )
        current = row(
            ask_prices=[1.500, 1.505],
            ask_vols=[10000.0, 3000.0],
            bid_prices=[1.495],
            bid_vols=[10000.0],
            last=1.495,
            hms="09:30:02",
        )
        frame = _features_from_row(current, prev=prev, prev2=None, prev_feat=None)
        self.assertAlmostEqual(frame.ask_cancel_frac, 7000 / 20000, places=9)
        self.assertEqual(frame.ask_eaten_frac, 0.0)

    def test_flat_frame_lost_fraction_is_bounded(self):
        # ask_lost_frac counts the union of same-price loss and gone levels
        # once, so it stays <= 1.0 even when eaten/cancel attribution overlaps
        # in a flat market.
        prev = row(
            ask_prices=[1.500],
            ask_vols=[10000.0],
            bid_prices=[1.495],
            bid_vols=[10000.0],
            last=1.497,
            hms="09:30:01",
        )
        current = row(
            ask_prices=[1.500],
            ask_vols=[3000.0],
            bid_prices=[1.495],
            bid_vols=[10000.0],
            last=1.497,
            hms="09:30:02",
        )
        frame = _features_from_row(current, prev=prev, prev2=None, prev_feat=None)
        self.assertAlmostEqual(frame.ask_eaten_frac, 0.7, places=9)
        self.assertAlmostEqual(frame.ask_cancel_frac, 0.7, places=9)
        self.assertAlmostEqual(frame.ask_lost_frac, 0.7, places=9)

    def test_wall_disappear_detected(self):
        prev = row(
            ask_prices=[1.500, 1.505, 1.510],
            ask_vols=[100000.0, 3000000.0, 100000.0],
            bid_prices=[1.495],
            bid_vols=[100000.0],
            last=1.498,
            hms="09:30:01",
        )
        current = row(
            ask_prices=[1.500, 1.505, 1.510],
            ask_vols=[100000.0, 300000.0, 100000.0],
            bid_prices=[1.495],
            bid_vols=[100000.0],
            last=1.495,
            hms="09:30:02",
        )
        frame = _features_from_row(current, prev=prev, prev2=None, prev_feat=None)
        self.assertTrue(frame.wall_disappear)
        self.assertGreaterEqual(frame.ask_cancel_frac, 0.30)

    def test_wall_not_disappear_when_total_drop_is_small(self):
        prev = row(
            ask_prices=[1.505],
            ask_vols=[3000000.0],
            bid_prices=[1.495],
            bid_vols=[100000.0],
            last=1.498,
            hms="09:30:01",
        )
        current = row(
            ask_prices=[1.505],
            ask_vols=[2600000.0],
            bid_prices=[1.495],
            bid_vols=[100000.0],
            last=1.497,
            hms="09:30:02",
        )
        frame = _features_from_row(current, prev=prev, prev2=None, prev_feat=None)
        self.assertFalse(frame.wall_disappear)

    def test_wall_below_threshold_never_triggers_wallgone(self):
        prev = row(
            ask_prices=[1.505],
            ask_vols=[WALL_ABS - 1],
            bid_prices=[1.495],
            bid_vols=[100000.0],
            last=1.498,
            hms="09:30:01",
        )
        current = row(
            ask_prices=[1.505],
            ask_vols=[1.0],
            bid_prices=[1.495],
            bid_vols=[100000.0],
            last=1.497,
            hms="09:30:02",
        )
        frame = _features_from_row(current, prev=prev, prev2=None, prev_feat=None)
        self.assertFalse(frame.wall_disappear)

    def test_missing_ask1_falls_back_to_last(self):
        frame = _features_from_row(
            row(
                ask_prices=[],
                ask_vols=[],
                bid_prices=[1.495],
                bid_vols=[0.0],
                last=1.492,
            ),
            prev=None,
            prev2=None,
            prev_feat=None,
        )
        self.assertTrue(np.isnan(frame.ask1))
        self.assertEqual(frame.micro1, 1.492)
        self.assertEqual(frame.micro5, 1.492)


class TriggerTests(unittest.TestCase):
    def test_eat_fires_above_threshold_with_price_move(self):
        fired = triggers(
            feat(
                ask_eaten_frac=0.75,
                d_ask1=TICK,
                d_micro1=TICK,
            )
        )
        self.assertIn("eat", fired)

    def test_eat_does_not_fire_without_price_move(self):
        fired = triggers(feat(ask_eaten_frac=0.75, d_ask1=0.0, d_micro1=0.0))
        self.assertNotIn("eat", fired)

    def test_eat_boundary_at_exact_threshold(self):
        self.assertIn(
            "eat",
            triggers(feat(ask_eaten_frac=0.20, d_ask1=TICK)),
        )
        self.assertNotIn(
            "eat",
            triggers(feat(ask_eaten_frac=0.1999, d_ask1=TICK)),
        )

    def test_wallgone_fires_with_wall_disappear_and_cancel(self):
        fired = triggers(
            feat(
                wall_disappear=True,
                ask_cancel_frac=0.60,
            )
        )
        self.assertIn("wallgone", fired)

    def test_wallgone_does_not_fire_without_wall_disappear(self):
        fired = triggers(feat(wall_disappear=False, ask_cancel_frac=0.60))
        self.assertNotIn("wallgone", fired)

    def test_wallgone_boundary_at_exact_threshold(self):
        self.assertIn(
            "wallgone",
            triggers(feat(wall_disappear=True, ask_cancel_frac=0.30)),
        )
        self.assertNotIn(
            "wallgone",
            triggers(feat(wall_disappear=True, ask_cancel_frac=0.2999)),
        )

    def test_jump_fires_at_two_ticks(self):
        self.assertIn("jump", triggers(feat(d_micro1=2 * TICK)))
        self.assertNotIn("jump", triggers(feat(d_micro1=2 * TICK - 0.0001)))

    def test_ofi_fires_with_micro_move(self):
        fired = triggers(feat(ofi_norm=0.20, d_micro1=TICK))
        self.assertIn("ofi", fired)

    def test_ofi_requires_micro_move(self):
        fired = triggers(feat(ofi_norm=0.20, d_micro1=0.0))
        self.assertNotIn("ofi", fired)

    def test_ofi_boundary(self):
        self.assertIn("ofi", triggers(feat(ofi_norm=0.15, d_micro1=TICK)))
        self.assertNotIn("ofi", triggers(feat(ofi_norm=0.1499, d_micro1=TICK)))


class PickTriggerTests(unittest.TestCase):
    def test_priority_eat_over_wallgone_over_jump_over_ofi(self):
        self.assertEqual(pick_trigger(["eat", "wallgone", "jump", "ofi"]), "eat")
        self.assertEqual(pick_trigger(["wallgone", "jump", "ofi"]), "wallgone")
        self.assertEqual(pick_trigger(["jump", "ofi"]), "jump")
        self.assertEqual(pick_trigger(["ofi"]), "ofi")
        self.assertIsNone(pick_trigger([]))


class IncrementalEngineTests(unittest.TestCase):
    def test_engine_matches_batch_builder(self):
        rows = [
            row(
                ask_prices=[1.500, 1.505],
                ask_vols=[10000.0, 10000.0],
                bid_prices=[1.495],
                bid_vols=[10000.0],
                last=1.495,
                ts="2026-08-07 09:30:00",
                hms="09:30:00",
            ),
            row(
                ask_prices=[1.505, 1.510],
                ask_vols=[5000.0, 10000.0],
                bid_prices=[1.500],
                bid_vols=[10000.0],
                last=1.500,
                ts="2026-08-07 09:30:01",
                hms="09:30:01",
            ),
            row(
                ask_prices=[1.505, 1.510],
                ask_vols=[2000.0, 10000.0],
                bid_prices=[1.500],
                bid_vols=[10000.0],
                last=1.500,
                ts="2026-08-07 09:30:02",
                hms="09:30:02",
            ),
        ]
        engine = IncrementalSignalEngine()
        for item in rows:
            engine.update(dict(item))
        batch = build_book_features(pd.DataFrame(rows))
        self.assertEqual(len(engine.frames), len(batch))
        for left, right in zip(engine.frames, batch):
            for name in (
                "micro1",
                "micro5",
                "ofi",
                "ofi_norm",
                "ask_eaten_frac",
                "ask_cancel_frac",
                "wall_disappear",
                "d_micro1",
                "d_ask1",
                "d_last",
            ):
                left_value = getattr(left, name)
                right_value = getattr(right, name)
                if isinstance(left_value, float) and np.isnan(left_value):
                    self.assertTrue(np.isnan(right_value), name)
                else:
                    self.assertEqual(left_value, right_value, name)

    def test_engine_accumulates_frames(self):
        engine = IncrementalSignalEngine()
        engine.update(
            dict(
                row(
                    ask_prices=[1.500],
                    ask_vols=[10000.0],
                    bid_prices=[1.495],
                    bid_vols=[10000.0],
                    last=1.495,
                )
            )
        )
        engine.update(
            dict(
                row(
                    ask_prices=[1.505],
                    ask_vols=[10000.0],
                    bid_prices=[1.500],
                    bid_vols=[10000.0],
                    last=1.500,
                    hms="09:30:01",
                )
            )
        )
        self.assertEqual(len(engine.frames), 2)
        self.assertAlmostEqual(engine.frames[1].d_ask1, TICK, places=9)
        self.assertEqual(engine.prev_feat, engine.frames[-1])


class ValidationRunnerTests(unittest.TestCase):
    def _runner(self, **overrides: object) -> ValidationRunner:
        settings = {
            "mode": "shadow",
            "offsets": {"eat": 2, "wallgone": 6, "jump": 2, "ofi": 2},
            "anchor": "ask1",
            "hold_seconds": 60,
        }
        settings.update(overrides)
        return ValidationRunner(**settings)

    def test_no_trigger_outside_window(self):
        runner = self._runner()
        runner.on_frame(
            feat(ask_eaten_frac=0.9, d_ask1=TICK),
            "09:29:59",
            datetime.now(timezone.utc),
            {},
        )
        self.assertEqual(runner.state, "waiting_trigger")

    def test_trigger_within_window_sets_limit_from_ask1(self):
        runner = self._runner()
        runner.on_frame(
            feat(ask_eaten_frac=0.9, d_ask1=TICK, ask1=1.500),
            "09:30:30",
            datetime.now(timezone.utc),
            {},
        )
        self.assertEqual(runner.state, "triggered")
        self.assertEqual(runner.trigger_type, "eat")
        # ask1 1.500 + 2 ticks = 1.510
        self.assertEqual(runner.limit_price, 1.510)

    def test_wallgone_trigger_uses_larger_offset(self):
        runner = self._runner()
        runner.on_frame(
            feat(wall_disappear=True, ask_cancel_frac=0.9, ask1=1.500),
            "09:30:30",
            datetime.now(timezone.utc),
            {},
        )
        self.assertEqual(runner.trigger_type, "wallgone")
        self.assertEqual(runner.limit_price, 1.530)

    def test_trigger_fires_only_once(self):
        runner = self._runner()
        now = datetime.now(timezone.utc)
        runner.on_frame(
            feat(ask_eaten_frac=0.9, d_ask1=TICK),
            "09:30:30",
            now,
            {},
        )
        runner.on_frame(
            feat(ask_eaten_frac=0.9, d_ask1=TICK),
            "09:30:31",
            now + timedelta(seconds=1),
            {},
        )
        self.assertEqual(runner.state, "triggered")
        self.assertEqual(runner.trigger_hms, "09:30:30")

    def test_shadow_fill_when_ask1_reaches_limit(self):
        runner = self._runner()
        now = datetime.now(timezone.utc)
        runner.on_frame(
            feat(ask_eaten_frac=0.9, d_ask1=TICK, ask1=1.500),
            "09:30:30",
            now,
            {},
        )
        runner.on_frame(
            feat(ask1=1.510, last=1.510),
            "09:30:31",
            now + timedelta(seconds=1),
            {},
        )
        self.assertTrue(runner.filled)
        self.assertEqual(runner.state, "filled_shadow")

    def test_shadow_not_filled_then_expires_after_hold(self):
        runner = self._runner(hold_seconds=60)
        now = datetime.now(timezone.utc)
        runner.on_frame(
            feat(ask_eaten_frac=0.9, d_ask1=TICK, ask1=1.500),
            "09:30:30",
            now,
            {},
        )
        runner.on_frame(
            feat(ask1=1.495, last=1.495),
            "09:30:40",
            now + timedelta(seconds=10),
            {},
        )
        self.assertFalse(runner.filled)
        runner.on_frame(
            feat(ask1=1.495, last=1.495),
            "09:31:31",
            now + timedelta(seconds=61),
            {},
        )
        self.assertEqual(runner.state, "not_filled_shadow")


class TickToRowTests(unittest.TestCase):
    def test_converts_qmt_tick_into_engine_row(self):
        tick = {
            "lastPrice": 1.498,
            "askPrice": [1.500, 1.505],
            "askVol": [10000, 20000],
            "bidPrice": [1.495, 1.490],
            "bidVol": [10000, 15000],
        }
        converted = _tick_to_row(tick, 1754530200000)
        self.assertEqual(converted["lastPrice"], 1.498)
        self.assertEqual(converted["askPrice"], [1.5, 1.505])
        self.assertEqual(converted["askVol"], [10000, 20000])
        self.assertEqual(converted["hms"], "09:30:00")

    def test_handles_missing_epoch_without_crash(self):
        converted = _tick_to_row({}, None)
        self.assertIsNone(converted["ts"])
        self.assertEqual(converted["hms"], "")
        self.assertEqual(converted["askPrice"], [])


if __name__ == "__main__":
    unittest.main()
