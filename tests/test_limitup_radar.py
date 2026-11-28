import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pandas as pd

from scripts.fetch_limitup_radar import (
    _score_candidate,
    _trend_metrics,
    _update_events,
    _update_volume_history,
)


class LimitupRadarTests(unittest.TestCase):
    def test_strong_trend_and_volume_rank_above_weak_candidate(self):
        strong = _score_candidate(
            {
                "code": "300001",
                "name": "强势股",
                "price": 10.4,
                "change_pct": 2.0,
                "volume": 1000,
                "amount_yi": 3.0,
                "turnover_pct": 3.0,
                "themes": ["CPO"],
            },
            {
                "trend_ready": True,
                "ret_5d_pct": 5.0,
                "ret_20d_pct": 12.0,
                "ma20": 10.0,
                "ma20_rising": True,
                "high_20d": 11.0,
            },
            {"name": "CPO", "change_pct": 4.0, "trigger_count": 2},
            2.0,
        )
        weak = _score_candidate(
            {
                "code": "300002",
                "name": "弱势股",
                "price": 8.0,
                "change_pct": -0.5,
                "volume": 1000,
                "amount_yi": 0.1,
                "turnover_pct": 0.1,
                "themes": ["CPO"],
            },
            {
                "trend_ready": True,
                "ret_5d_pct": -5.0,
                "ret_20d_pct": -10.0,
                "ma20": 10.0,
                "ma20_rising": False,
                "high_20d": 11.0,
            },
            {"name": "CPO", "change_pct": 4.0, "trigger_count": 2},
            None,
        )

        self.assertGreater(strong["score"], weak["score"])
        self.assertEqual(strong["tier"], "强势联动")
        self.assertEqual(weak["tier"], "观察")
        self.assertNotIn("probability", strong)

    def test_volume_ratio_waits_for_two_baseline_intervals(self):
        start = datetime(2026, 11, 28, 9, 30, tzinfo=timezone(timedelta(hours=8)))
        ratio, state = _update_volume_history("000001", 1000, start, None)
        self.assertIsNone(ratio)
        ratio, state = _update_volume_history("000001", 1100, start + timedelta(minutes=5), state)
        self.assertIsNone(ratio)
        ratio, state = _update_volume_history("000001", 1300, start + timedelta(minutes=10), state)
        self.assertIsNone(ratio)
        ratio, _ = _update_volume_history("000001", 1600, start + timedelta(minutes=15), state)
        self.assertEqual(ratio, 2.0)
        ratio, reset = _update_volume_history("000001", 1900, start + timedelta(minutes=30), state)
        self.assertIsNone(ratio)
        self.assertEqual(reset["intervals"], [])

    def test_first_poll_bootstraps_existing_limitups_without_marking_them_new(self):
        state = {
            "events": {},
            "quote_history": {},
            "initialized": False,
        }
        pool = [{
            "code": "000001",
            "name": "测试股",
            "industry": "IT服务",
            "streak": 1,
            "board_label": "首板",
            "first_seal_time": "09:30",
        }]
        now = datetime(2026, 11, 28, 9, 35, tzinfo=timezone(timedelta(hours=8)))
        with patch(
            "scripts.fetch_limitup_radar._fetch_concepts",
            return_value=({"000001": ["CPO"]}, {}),
        ):
            active, new, _, _ = _update_events(pool, set(), state, now)

        self.assertEqual(len(active), 1)
        self.assertEqual(new, [])
        self.assertTrue(state["initialized"])
        self.assertEqual(active[0]["concepts"], ["CPO"])

    def test_resealed_limitup_is_reported_as_new_and_increments_seal_cycles(self):
        state = {
            "events": {},
            "quote_history": {},
            "initialized": False,
        }
        pool = [{
            "code": "000001",
            "name": "测试股",
            "industry": "IT服务",
            "streak": 1,
            "board_label": "首板",
            "first_seal_time": "09:30",
        }]
        now = datetime(2026, 11, 28, 9, 35, tzinfo=timezone(timedelta(hours=8)))
        with patch(
            "scripts.fetch_limitup_radar._fetch_concepts",
            return_value=({"000001": ["CPO"]}, {}),
        ):
            _update_events(pool, set(), state, now)
            active, _, _, _ = _update_events(
                [], {"000001"}, state, now + timedelta(minutes=5)
            )
            resealed, new, _, _ = _update_events(
                pool, set(), state, now + timedelta(minutes=10)
            )

        self.assertEqual(active, [])
        self.assertEqual([event["code"] for event in new], ["000001"])
        self.assertEqual(resealed[0]["seal_cycles"], 2)

    def test_extracts_recent_returns_and_moving_average_from_daily_bars(self):
        close = list(range(10, 40))
        frame = pd.DataFrame({
            "收盘": close,
            "最高": [value + 1 for value in close],
        })

        result = _trend_metrics(frame, "2026-11-27")

        self.assertTrue(result["trend_ready"])
        self.assertEqual(result["ret_5d_pct"], 14.71)
        self.assertEqual(result["ret_20d_pct"], 105.26)
        self.assertTrue(result["ma20_rising"])


if __name__ == "__main__":
    unittest.main()
