"""
策略回测引擎
1) 做T信号回测：用 t_signal_bars 的信号窗口快照，统计信号后 1/3/6 根30分钟K的胜率与收益（按指标/强度分组）
2) 选股回测：用 candidates + daily_kline，统计入选后 5/10/20 日的胜率与收益（按评分/概念分组）

成本模型：佣金万2.5双边(约0.05%) + 卖出印花税千1(0.1%) => 单次做T往返约 0.2%
运行：python scripts/backtest.py（写回 Supabase backtest_result 表）
"""
import os
import json
import statistics
from datetime import datetime, timedelta, timezone

from supabase import create_client

try:
    from zoneinfo import ZoneInfo
    CN_TZ = ZoneInfo("Asia/Shanghai")
except Exception:
    CN_TZ = timezone(timedelta(hours=8))

COST = 0.2          # 做T单次往返成本(%)
SCREEN_COST = 0.25  # 选股一次买入+卖出成本(%)

supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])


def now_cn():
    return datetime.now(CN_TZ)


def fetch_all(table, select='*', **filters):
    """带分页地拉全表（Supabase 单次默认最多1000行）"""
    rows = []
    offset = 0
    page = 1000
    while True:
        q = supabase.table(table).select(select)
        for k, v in filters.items():
            q = q.eq(k, v) if not isinstance(v, (list, tuple)) else q.in_(k, v)
        resp = q.range(offset, offset + page - 1).execute()
        data = resp.data or []
        rows.extend(data)
        if len(data) < page:
            break
        offset += page
        if offset > 200000:
            break
    return rows


def pct(a, b):
    if not b:
        return None
    return (a / b - 1) * 100


# ============ 1. 做T信号回测 ============

def backtest_t_signals():
    rows = fetch_all('t_signal_bars')
    if not rows:
        return {'samples': 0, 'note': '暂无信号快照数据'}

    horizons = [1, 3, 6]
    # key: 维度 -> 指标 -> 统计
    groups = {}
    samples = 0

    for r in rows:
        try:
            payload = r.get('bars')
            if isinstance(payload, str):
                payload = json.loads(payload)
            if not payload:
                continue
            bars = payload.get('bars') or []
            si = int(payload.get('sig_idx', 0))
            if si >= len(bars):
                continue
            sig = bars[si]
            sig_day = sig.get('d')
            sig_price = sig.get('c')
            if not sig_price:
                continue
            stype = r.get('signal_type')
            ind = r.get('indicator')
            strength = r.get('strength') or ''

            # 只统计信号之后仍在同一天的K线
            fut = [b for b in bars[si + 1:] if b.get('d') == sig_day]
            if not fut:
                continue

            samples += 1
            for h in horizons:
                if len(fut) < h:
                    continue
                ret = pct(fut[h - 1]['c'], sig_price)
                if ret is None:
                    continue
                # 买入信号：扣成本后上涨才算赢；卖出信号：扣成本后下跌才算赢
                if stype == '买入':
                    win = ret > COST
                    gain = ret - COST
                else:
                    win = ret < -COST
                    gain = -ret - COST
                for dim, key in (('全部', '全部'), ('指标', ind), ('强度', strength),
                                 ('指标×方向', f"{ind}·{stype}")):
                    g = groups.setdefault(dim, {}).setdefault(key, {f'h{h}': {'n': 0, 'win': 0, 'sum': 0.0, 'rets': []}
                                                                   for h in horizons})
                    cell = g[f'h{h}']
                    cell['n'] += 1
                    cell['win'] += 1 if win else 0
                    cell['sum'] += gain
                    cell['rets'].append(gain)
        except Exception:
            continue

    out = {'samples': samples, 'cost': COST, 'horizons': horizons, 'groups': {}}
    for dim, keys in groups.items():
        out['groups'][dim] = {}
        for key, cells in keys.items():
            item = {}
            for h in horizons:
                c = cells[f'h{h}']
                if not c['n']:
                    continue
                rets = sorted(c['rets'])
                item[f'h{h}'] = {
                    'n': c['n'],
                    'win_rate': round(c['win'] / c['n'] * 100, 1),
                    'avg': round(c['sum'] / c['n'], 2),
                    'median': round(statistics.median(rets), 2) if rets else 0,
                    'best': round(rets[-1], 2) if rets else 0,
                    'worst': round(rets[0], 2) if rets else 0,
                }
            if item:
                out['groups'][dim][key] = item
    return out


# ============ 2. 选股回测 ============

def backtest_screening():
    cands = fetch_all('candidates')
    if not cands:
        return {'samples': 0, 'note': '暂无候选记录'}

    codes = sorted({c['code'] for c in cands})
    # daily_kline 批量拉取后本地按入选日切片
    kl = fetch_all('daily_kline', select='code,trade_date,close,change_pct', code=codes)
    kmap = {}
    for k in kl:
        kmap.setdefault(k['code'], []).append((k['trade_date'], float(k['close'])))
    for c in kmap:
        kmap[c].sort()

    # 概念映射
    concepts = {}
    for c in fetch_all('stock_concepts', select='code,concepts'):
        v = c.get('concepts')
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except Exception:
                v = []
        concepts[c['code']] = v or []

    horizons = [5, 10, 20]
    groups = {}
    samples = 0
    rows_out = []

    for cand in cands:
        code = cand['code']
        series = kmap.get(code, [])
        sel = cand['trade_date']
        sel_price = float(cand.get('last_price') or 0)
        if not sel_price or not series:
            continue
        after = [(d, p) for d, p in series if d > sel]
        if not after:
            continue
        samples += 1
        rets = {}
        for h in horizons:
            if len(after) >= h:
                rets[h] = pct(after[h - 1][1], sel_price)
        # 入选当日涨幅
        sel_bar = next(((d, p) for d, p in series if d == sel), None)
        sel_change = None
        if sel_bar:
            idx = series.index(sel_bar)
            if idx > 0:
                sel_change = round((sel_bar[1] / series[idx - 1][1] - 1) * 100, 2)

        score = float(cand.get('score') or 0)
        score_bucket = '≥80' if score >= 80 else ('60-80' if score >= 60 else '<60')
        launch_gap = None
        sig = cand.get('signals')
        if isinstance(sig, str):
            try:
                sig = json.loads(sig)
            except Exception:
                sig = {}
        if isinstance(sig, dict):
            launch_gap = sig.get('pullback_days')

        tags = ['全部', f"评分{score_bucket}"]
        if sel_change is not None:
            tags.append('涨幅' + ('≥9%' if sel_change >= 9 else ('7-9%' if sel_change >= 7 else '<7%')))
        if launch_gap is not None:
            tags.append(f"回踩{launch_gap}天")
        for cc in (concepts.get(code) or [])[:2]:
            tags.append('概念:' + cc)

        for h in horizons:
            if h not in rets or rets[h] is None:
                continue
            net = rets[h] - SCREEN_COST
            for tag in tags:
                g = groups.setdefault(tag, {f'h{h}': {'n': 0, 'win': 0, 'sum': 0.0, 'rets': []}})
                cell = g[f'h{h}']
                cell['n'] += 1
                cell['win'] += 1 if net > 0 else 0
                cell['sum'] += net
                cell['rets'].append(net)

        rows_out.append({
            'code': code, 'name': cand.get('name'), 'select_date': sel,
            'score': round(score, 1), 'select_change': sel_change,
            'rets': {str(h): (round(rets[h], 2) if rets.get(h) is not None else None) for h in horizons},
            'concepts': (concepts.get(code) or [])[:5],
        })

    out = {'samples': samples, 'cost': SCREEN_COST, 'horizons': horizons, 'groups': {}, 'rows': rows_out[-200:]}
    for tag, cells in groups.items():
        item = {}
        for h in horizons:
            c = cells.get(f'h{h}')
            if not c or not c['n']:
                continue
            rets = sorted(c['rets'])
            item[f'h{h}'] = {
                'n': c['n'],
                'win_rate': round(c['win'] / c['n'] * 100, 1),
                'avg': round(c['sum'] / c['n'], 2),
                'median': round(statistics.median(rets), 2) if rets else 0,
                'best': round(rets[-1], 2) if rets else 0,
                'worst': round(rets[0], 2) if rets else 0,
            }
        if item:
            out['groups'][tag] = item
    return out


def save_result(key, value):
    try:
        supabase.table('backtest_result').upsert({
            'key': key, 'value': json.dumps(value, ensure_ascii=False),
            'updated_at': now_cn().strftime('%Y-%m-%d %H:%M:%S')
        }, on_conflict='key').execute()
        print(f"  已写入回测结果: {key}")
    except Exception as e:
        print(f"  写入回测结果失败 {key}: {e}")


def main():
    print("===== 做T信号回测 =====")
    t_res = backtest_t_signals()
    print(f"  样本 {t_res.get('samples', 0)} 个信号")
    save_result('t_signal', t_res)

    print("===== 选股策略回测 =====")
    s_res = backtest_screening()
    print(f"  样本 {s_res.get('samples', 0)} 只候选")
    save_result('screening', s_res)

    print(f"回测完成 {now_cn().strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == "__main__":
    main()
