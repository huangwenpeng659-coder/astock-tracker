"""
A股每日数据抓取 + 启动形态选股脚本
数据源：东方财富（通过 akshare 库）
"""
import os
import json
from datetime import datetime, timedelta
import pandas as pd
import akshare as ak
from supabase import create_client

# 初始化 Supabase
supabase = create_client(
    os.environ["SUPABASE_URL"],
    os.environ["SUPABASE_SERVICE_KEY"]
)

def get_stock_list():
    """获取 A股股票列表"""
    try:
        df = ak.stock_zh_a_spot_em()
        df = df[~df['名称'].str.contains('ST|退', na=False)]
        return df[['代码', '名称']].head(500)
    except Exception as e:
        print(f"获取股票列表失败: {e}")
        return pd.DataFrame()

def save_stock_basic(code, name):
    """保存股票基本信息"""
    try:
        supabase.table('stocks').upsert({
            'code': code,
            'name': name,
            'is_st': False,
            'is_delisted': False
        }).execute()
    except Exception as e:
        print(f"保存股票信息失败 {code}: {e}")

def fetch_and_save_kline(code):
    """抓取单只股票最近 120 天日线数据"""
    try:
        df = ak.stock_zh_a_hist(symbol=code, period="daily", 
                                start_date=(datetime.now() - timedelta(days=180)).strftime("%Y%m%d"),
                                end_date=datetime.now().strftime("%Y%m%d"), adjust="qfq")
        if df.empty:
            return None
            
        df = df.rename(columns={
            '日期': 'trade_date', '开盘': 'open', '最高': 'high', 
            '最低': 'low', '收盘': 'close', '成交量': 'volume',
            '成交额': 'amount', '换手率': 'turnover_rate', '涨跌幅': 'change_pct'
        })
        
        records = df.tail(60).to_dict('records')
        for r in records:
            r['code'] = code
            r['trade_date'] = str(r['trade_date'])
            for k, v in r.items():
                if pd.isna(v):
                    r[k] = None
                    
        supabase.table('daily_kline').upsert(records).execute()
        return df
    except Exception as e:
        print(f"抓取 {code} 失败: {e}")
        return None

def detect_launch_pattern(df):
    """检测"启动涨停 -> 缩量回踩不破 -> 站稳五日线"形态"""
    if len(df) < 20:
        return None
        
    df = df.reset_index(drop=True)
    df['ma5'] = df['close'].rolling(5).mean()
    
    recent = df.tail(15)
    launch_idx = -1
    
    for i in range(len(recent)-1, -1, -1):
        row = recent.iloc[i]
        if row['change_pct'] > 9 or (row['change_pct'] > 7 and row['volume'] > df['volume'].tail(20).mean() * 2):
            launch_idx = i
            launch_row = row
            break
            
    if launch_idx == -1:
        return None
        
    launch_point = launch_row['low']
    after_launch = recent.iloc[launch_idx+1:]
    
    if len(after_launch) < 2:
        return None
        
    avg_vol_after = after_launch['volume'].mean()
    if avg_vol_after > launch_row['volume'] * 0.6:
        return None
        
    if after_launch['low'].min() < launch_point * 0.99:
        return None
        
    last_close = df.iloc[-1]['close']
    last_ma5 = df.iloc[-1]['ma5']
    if abs(last_close - last_ma5) / last_ma5 > 0.03:
        return None
        
    return {
        'launch_date': str(launch_row['trade_date']),
        'launch_point': float(launch_point),
        'last_price': float(last_close),
        'pullback_days': len(after_launch)
    }

def calc_scores(df, pattern):
    """计算综合评分"""
    scores = {
        'price_position': 50,
        'volatility': 50,
        'launch_pattern': 100 if pattern else 0
    }
    recent_vol = df['close'].tail(20).pct_change().std()
    hist_vol = df['close'].tail(120).pct_change().std()
    if recent_vol < hist_vol * 0.7:
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
    """保存选股结果"""
    stop_loss = launch_point * 0.97
    target_1 = last_price * 1.05
    target_2 = last_price * 1.10
    target_3 = last_price * 1.15
    
    try:
        supabase.table('candidates').upsert({
            'code': code,
            'trade_date': trade_date,
            'score': round(score, 2),
            'last_price': last_price,
            'launch_point': launch_point,
            'stop_loss': round(stop_loss, 2),
            'target_1': round(target_1, 2),
            'target_2': round(target_2, 2),
            'target_3': round(target_3, 2),
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
    
    for idx, row in stocks.iterrows():
        code = row['代码']
        name = row['名称']
        
        save_stock_basic(code, name)
        
        df = fetch_and_save_kline(code)
        if df is None or len(df) < 30:
            continue
            
        pattern = detect_launch_pattern(df)
        if pattern:
            score, details = calc_scores(df, pattern)
            if score > 60:
                candidates.append({
                    'code': code,
                    'name': name,
                    'score': score,
                    'pattern': pattern,
                    'details': details
                })
                save_candidate(code, name, today, score, 
                             pattern['last_price'], pattern['launch_point'], details)
                print(f"发现候选: {name}({code}) 分数:{score:.1f}")
        
        if idx % 50 == 0:
            print(f"进度: {idx}/{len(stocks)}")
            
    candidates.sort(key=lambda x: x['score'], reverse=True)
    for rank, c in enumerate(candidates[:20], 1):
        supabase.table('candidates').update({'rank': rank}).eq('code', c['code']).eq('trade_date', today).execute()
        
    print(f"\n选股完成，共发现 {len(candidates)} 只候选股票")
    print("Top 5:")
    for c in candidates[:5]:
        print(f"  {c['name']}({c['code']}): {c['score']:.1f}分")

if __name__ == "__main__":
    main()
