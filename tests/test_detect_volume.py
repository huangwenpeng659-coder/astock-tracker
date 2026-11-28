import unittest
from datetime import datetime, timedelta
import sys
import types
from unittest.mock import patch

import pandas as pd

supabase_stub = types.ModuleType('supabase')
supabase_stub.create_client = lambda *_: None
sys.modules.setdefault('supabase', supabase_stub)

from scripts import detect_volume


def market_time(hour, minute):
    tz = detect_volume.TZ
    return datetime(2026, 11, 27, hour, minute, tzinfo=tz)


class AnalyzeMarketSnapshotTests(unittest.TestCase):
    def test_detects_volume_spike_after_five_history_samples(self):
        ts = market_time(10, 30)
        previous = {
            'date': ts.strftime('%Y%m%d'),
            'stocks': {
                '600000': {
                    'volume': 1000,
                    'price': 10,
                    'at': (ts - timedelta(minutes=30)).isoformat(),
                    'history': [100, 100, 100, 100, 100],
                },
            },
            'pending': [],
        }

        signals, _ = detect_volume.analyze_market_snapshot(
            [{'code': '600000', 'name': '浦发银行', 'volume': 1300, 'price': 11}],
            previous,
            ts,
        )

        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]['type'], '放量上涨')
        self.assertEqual(signals[0]['vol_ratio'], 3.0)
        self.assertEqual(signals[0]['interval_minutes'], 30)

    def test_skips_signal_until_five_history_samples_exist(self):
        ts = market_time(10, 30)
        previous = {
            'date': ts.strftime('%Y%m%d'),
            'stocks': {
                '600000': {
                    'volume': 1000,
                    'price': 10,
                    'at': (ts - timedelta(minutes=30)).isoformat(),
                    'history': [100, 100, 100, 100],
                },
            },
            'pending': [],
        }

        signals, next_state = detect_volume.analyze_market_snapshot(
            [{'code': '600000', 'name': '浦发银行', 'volume': 2000, 'price': 11}],
            previous,
            ts,
        )

        self.assertEqual(signals, [])
        self.assertEqual(len(next_state['stocks']['600000']['history']), 5)

    def test_long_gap_resets_baseline_without_creating_signal(self):
        ts = market_time(13, 30)
        previous = {
            'date': ts.strftime('%Y%m%d'),
            'stocks': {
                '600000': {
                    'volume': 1000,
                    'price': 10,
                    'at': (ts - timedelta(minutes=90)).isoformat(),
                    'history': [100, 100, 100, 100, 100],
                },
            },
            'pending': [],
        }

        signals, next_state = detect_volume.analyze_market_snapshot(
            [{'code': '600000', 'name': '浦发银行', 'volume': 100000, 'price': 11}],
            previous,
            ts,
        )

        self.assertEqual(signals, [])
        self.assertEqual(next_state['stocks']['600000']['history'], [100, 100, 100, 100, 100])
        self.assertEqual(next_state['stocks']['600000']['volume'], 100000)

    def test_incomplete_market_snapshot_fails_closed(self):
        frame = pd.DataFrame({
            '代码': ['600000'] * (detect_volume.MIN_MARKET_STOCKS - 1),
            '名称': ['浦发银行'] * (detect_volume.MIN_MARKET_STOCKS - 1),
            '最新价': [10] * (detect_volume.MIN_MARKET_STOCKS - 1),
            '成交量': [100] * (detect_volume.MIN_MARKET_STOCKS - 1),
        })

        with patch.object(detect_volume.ak, 'stock_zh_a_spot_em', return_value=frame):
            with self.assertRaisesRegex(RuntimeError, '低于完整性门槛'):
                detect_volume.get_market_snapshot()

    def test_accepts_snapshot_at_completeness_threshold(self):
        codes = [str(600000 + index) for index in range(detect_volume.MIN_MARKET_STOCKS)]
        frame = pd.DataFrame({
            '代码': codes,
            '名称': ['测试股票'] * len(codes),
            '最新价': [10] * len(codes),
            '成交量': [100] * len(codes),
        })

        with patch.object(detect_volume.ak, 'stock_zh_a_spot_em', return_value=frame):
            snapshot, invalid_count = detect_volume.get_market_snapshot()

        self.assertEqual(len(snapshot), detect_volume.MIN_MARKET_STOCKS)
        self.assertEqual(invalid_count, 0)


if __name__ == '__main__':
    unittest.main()
