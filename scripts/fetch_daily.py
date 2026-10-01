"""
A股每日数据抓取 + 启动形态选股脚本
数据源：新浪财经（主，对境外IP友好）
"""
import os
import json
import time
from datetime import datetime, timedelta
import pandas as pd
import akshare as ak
from supabase import create_client

supabase = create_client(
    os.environ["SUPABASE_URL"],
    os.environ["SUPABASE_SERVICE_KEY"]
)

def retry(func, max_attempts=3, delay=3, *args, **kwargs):
    last_err = None
    for attempt in range(1, max_attempts + 1):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            last_err = e
            print(f"  第{attempt}次尝试失败: {e}")
            if attempt < max_attempts:
                time.sleep(delay)
    print(f"  重试{max_attempts}次后仍失败: {last_err}")
    return None

def get_sina_symbol(code):
    """把代码转换成新浪格式 sh600000 / sz000001 / bj830xxx"""
    code = str(code).zfill(6)
    if code.startswith('6'):
        return f"sh{code}"
    elif code.startswith(('0', '3')):
        return f"sz{code}"
    elif code.startswith(('4', '8', '9')):
        return f"bj{code}"
    return f"sh{code}"

def get_stock_list():
    """获取 A股股票列表，主接口东财失败则用新浪"""
    df = retry(ak.stock_zh_a_spot_em, max_attempts=2, delay=3)
    if df is not None and not df.empty:
        print(f"  东方财富接口成功，获取 {len(df)} 条")
        df = df[~df['名称'].str.contains('ST|退', na=False)]
        return df[['代码', '名称']].head(300)

    print("  东方财富接口失败，使用新浪财经备用接口...")
    df2 = retry(ak.stock_zh_a_spot, max_attempts=3, delay=5)
    if df2 is not None and not df2.empty:
        print(f"  新浪财经接口成功，获取 {len(df2)} 条")
        name_col = '名称' if '名称' in df2.columns else 'name'
        code_col = '代码' if '代码' in df2.columns else 'code'
        df2 = df2[~df2[name_col].astype(str).str.contains('ST|退', na=False)]
        result = df2[[code_col, name_col]].copy()
        result.columns = ['代码', '名称']
        return result.head(300)

    print("  两个数据源都失败")
    return pd.DataFrame()

def save_stock_basic(code, name):
    try:
        supabase.table('stocks').upsert({
            'code': code, 'name': name,
            'is_st': False, 'is_delisted': False
        }).execute()
    except Exception as e:
        print(f"保存股票信息失败 {code}: {e}")

def _fetch_kline_sina(code):
    sina_symbol = get_sina_symbol(code)
    start = (datetime.now() - timedelta(days=180)).strftime("%Y%m%d")
    end = datetime.now().strftime("%Y%m%d")
    return ak.stock_zh_a_daily(symbol=sina_symbol, start_date=start, end_date=end, adjust="qfq")

def fetch_and_save_kline(code):
    """用新浪接口抓K线，带重试"""
    df = retry(_fetch_kline_sina, max_attempts=2, delay=2, code=code)
    if df is None or df.empty:
        return None

    try:
        df = df.reset_index()
        # 新浪字段：date, open, high, low, close, volume, outstanding_share, turnover
        df = df.rename(columns={'date': 'trade_date'})
        for col in ['open', 'high', 'low', 'close', 'volume']:
            if col not in df.columns:
                return None

        df['close'] = df['close'].astype(float)
        df['change_pct'] = df['close'].pct_change() * 100
        df['amount'] = df['close'] * df['volume']
        df['turnover_rate'] = df['turnover'] * 100 if 'turnover' in df.columns else None

        df = df.dropna(subset=['change_pct'])
        if df.empty:
            return None

        records = df.tail(60).to_dict('records')
        for r in records:
            r['code'] = code
            r['trade_date'] = str(r['trade_date'])[:10]
            keep = ['code', 'trade_date', 'open', 'high', 'low', 'close',
                    'volume', 'amount', 'turnover_rate', 'change_pct']
            for k in list(r.keys()):
                if k not in keep:
                    del r[k]
            for k, v in r.items():
                if pd.isna(v):
                    r[k] = None

        supabase.table('daily_kline').upsert(records).execute()
        return df
    except Exception as e:
        print(f"保存 {code} K线失败: {e}")
        return None

def detect_launch_pattern(df):
    if len(df) < 20:
        return None
    df = df.reset_index(drop=True)
    df['ma5'] = df['close'].rolling(5).mean()
    recent = df.tail(15)
    launch_idx = -1
    launch_row = None
    for i in range(len(recent) - 1, -1, -1):
        row = recent.iloc[i]
        if row['change_pct'] > 9 or (row['change_pct'] > 7 and row['volume'] > df['volume'].tail(20).mean() * 2):
            launch_idx = i
            launch_row = row
            break
    if launch_idx == -1:
        return None
    launch_point = launch_row['low']
    after_launch = recent.iloc[launch_idx + 1:]
    if len(after_launch) < 2:
        return None
    avg_vol_after = after_launch['volume'].mean()
    if avg_vol_after > launch_row['volume'] * 0.6:
        return None
    if after_launch['low'].min() < launch_point * 0.99:
        return None
    last_close = df.iloc[-1]['close']
    last_ma5 = df.iloc[-1]['ma5']
    if pd.isna(last_ma5) or abs(last_close - last_ma5) / last_ma5 > 0.03:
        return None
    return {
        'launch_date': str(launch_row['trade_date']),
        'launch_point': float(launch_point),
        'last_price': float(last_close),
        'pullback_days': len(after_launch)
    }

def calc_scores(df, pattern):
    scores = {'price_position': 50, 'volatility': 50, 'launch_pattern': 100 if pattern else 0}
    recent_vol = df['close'].tail(20).pct_change().std()
    hist_vol = df['close'].tail(120).pct_change().std()
    if pd.notna(recent_vol) and pd.notna(hist_vol) and hist_vol > 0 and recent_vol < hist_vol * 0.7:
        scores['volatility'] = 80
    current = df.iloc[-1]['close']
    low_120 = df['close'].tail(120).min()
    high_120 = df['close'].tail(120).max()
    position = (current - low_120) / (high_120 - low_120) if high_120 > low_120 else 0.5
    scores['price_position'] = (1 - position) * 100
    weights = {'price_position': 0.2, 'volatility': 0.2, 'launch_pattern': 0.6}
    total = sum(scores[k] * weights[k] for k in weights)
    return total, scores

def save_candidate(code, name, trade_date, score, last_price, launch_point, signals):
    stop_loss = launch_point * 0.97
    try:
        supabase.table('candidates').upsert({
            'code': code, 'trade_date': trade_date, 'score': round(score, 2),
            'last_price': last_price, 'launch_point': launch_point,
            'stop_loss': round(stop_loss, 2),
            'target_1': round(last_price * 1.05, 2),
            'target_2': round(last_price * 1.10, 2),
            'target_3': round(last_price * 1.15, 2),
            'signals': json.dumps(signals, ensure_ascii=False)
        }).execute()
    except Exception as e:
        print(f"保存候选失败 {code}: {e}")

def main():
    print("开始抓取 A股数据...")
    today = datetime.now().strftime("%Y-%m-%d")

    stocks = get_stock_list()
    if stocks.empty:
        print("无法获取股票列表，退出")
        return

    print(f"共获取 {len(stocks)} 只股票，开始扫描...")
    candidates = []
    success_count = 0

    for idx, row in stocks.iterrows():
        code = row['代码']
        name = row['名称']
        save_stock_basic(code, name)

        df = fetch_and_save_kline(code)
        if df is None or len(df) < 30:
            continue
        success_count += 1

        pattern = detect_launch_pattern(df)
        if pattern:
            score, details = calc_scores(df, pattern)
            if score > 60:
                candidates.append({'code': code, 'name': name, 'score': score})
                save_candidate(code, name, today, score, pattern['last_price'], pattern['launch_point'], details)
                print(f"发现候选: {name}({code}) 分数:{score:.1f}")

        if idx % 50 == 0:
            print(f"进度: {idx}/{len(stocks)}，成功抓取 {success_count} 只")

    candidates.sort(key=lambda x: x['score'], reverse=True)
    for rank, c in enumerate(candidates[:20], 1):
        supabase.table('candidates').update({'rank': rank}).eq('code', c['code']).eq('trade_date', today).execute()

    print(f"\n抓取完成，成功 {success_count}/{len(stocks)} 只")
    print(f"选股完成，共发现 {len(candidates)} 只候选股票")
    for c in candidates[:5]:
        print(f"  {c['name']}({c['code']}): {c['score']:.1f}分")

if __name__ == "__main__":
    main()
    
