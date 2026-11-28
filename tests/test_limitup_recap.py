import unittest
from datetime import date

import pandas as pd

from scripts.fetch_limitup_recap import build_recap, latest_trade_date


class LimitupRecapTests(unittest.TestCase):
    def setUp(self):
        self.limitups = pd.DataFrame([
            {
                '代码': '000001',
                '名称': '甲公司',
                '连板数': 1,
                '涨停统计': '1/1',
                '首次封板时间': 92500.0,
                '封板资金': 100_000_000,
                '成交额': 200_000_000,
                '换手率': 3.456,
                '流通市值': 10_000_000_000,
                '所属行业': '军工',
            },
            {
                '代码': '000002',
                '名称': '乙公司',
                '连板数': 3,
                '涨停统计': '3/5',
                '首次封板时间': 101500,
                '封板资金': 200_000_000,
                '成交额': 400_000_000,
                '换手率': 6.789,
                '流通市值': 20_000_000_000,
                '所属行业': '军工',
            },
            {
                '代码': '600003',
                '名称': '丙公司',
                '连板数': 2,
                '涨停统计': '2/2',
                '首次封板时间': '09:30:00',
                '封板资金': 50_000_000,
                '成交额': 100_000_000,
                '换手率': 1.2,
                '流通市值': 30_000_000_000,
                '所属行业': '电力',
            },
        ])
        self.broken = pd.DataFrame({'代码': ['000003', '000003', '000001']})

    def test_builds_unique_industry_groups_and_market_summary(self):
        recap = build_recap(
            self.limitups,
            self.broken,
            '2026-11-27',
            '2026-11-28 15:35:00',
        )

        self.assertEqual(recap['summary']['limitup_count'], 3)
        self.assertEqual(recap['summary']['non_first_count'], 2)
        self.assertEqual(recap['summary']['max_streak'], 3)
        self.assertEqual(recap['summary']['industry_count'], 2)
        self.assertEqual(recap['summary']['broken_count'], 1)
        self.assertEqual(recap['summary']['break_rate_pct'], 25.0)
        self.assertEqual(recap['groups'][0]['industry'], '军工')
        self.assertEqual(recap['groups'][0]['count'], 2)
        self.assertEqual(recap['groups'][0]['items'][0]['first_seal_time'], '10:15')
        self.assertEqual(recap['groups'][0]['items'][1]['first_seal_time'], '09:25')
        self.assertEqual(recap['groups'][0]['items'][1]['seal_to_amount'], 0.5)

    def test_uses_latest_available_trade_date_on_weekend(self):
        calendar = pd.DataFrame({
            'trade_date': [
                date(2026, 11, 27),
                date(2026, 11, 30),
            ],
        })

        self.assertEqual(latest_trade_date(calendar, date(2026, 11, 28)), '20261127')

    def test_marks_core_concept_by_full_market_limitup_concentration(self):
        recap = build_recap(
            self.limitups,
            self.broken,
            '2026-11-27',
            '2026-11-28 15:35:00',
            concepts_by_code={
                '000001': ['算力', '并购重组'],
                '000002': ['算力'],
                '600003': ['鸿蒙概念'],
            },
        )
        stocks = {
            stock['code']: stock
            for group in recap['groups']
            for stock in group['items']
        }

        self.assertEqual(
            [(item['name'], item['is_core'], item['limitup_count'])
             for item in stocks['000001']['concepts']],
            [('算力', True, 2), ('并购重组', False, 1)],
        )
        self.assertFalse(stocks['600003']['concepts'][0]['is_core'])
        self.assertIn('不代表公告或新闻', recap['reason_note'])

    def test_rejects_empty_limitup_pool(self):
        with self.assertRaisesRegex(ValueError, '涨停池为空'):
            build_recap(pd.DataFrame(), self.broken, '2026-11-27', 'now')


if __name__ == '__main__':
    unittest.main()
