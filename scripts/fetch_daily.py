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
        # 获取沪深 A股列表
        df = ak.stock_zh_a_spot_em()
        # 过滤 ST 和退市
        df = df[~df['名称'].str.contains('ST|退', na=False)]
        return df[['代码', '名称']].head(500)  # 先限制 500 只测试，后续放开
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
        # 东方财富接口
        df = ak.stock_zh_a_hist(symbol=code, period="daily", 
                                start_date=(datetime.now() - timedelta(days=180)).strftime("%Y%m%d"),
                                end_date=datetime.now().strftime("%Y%m%d"), adjust="qfq")
        if df.empty:
            return None
            
        # 标准化字段名
        df = df.rename(columns={
            '日期': 'trade_date', '开盘': 'open', '最高': 'high', 
            '最低': 'low', '收盘': 'close', '成交量': 'volume',
            '成交额': 'amount', '换手率': 'turnover_rate', '涨跌幅': 'change_pct'
        })
        
        # 批量插入（使用 upsert 避免重复）
        records = df.tail(60).to_dict('records')  # 最近 60 天
        for r in records:
            r['code'] = code
            r['trade_date'] = str(r['trade_date'])
            # 清理 None 值
            for k, v in r.items():
                if pd.isna(v):
                    r[k] = None
                    
        supabase.table('daily_kline').upsert(records).execute()
        return df
    except Exception as e:
        print(f"抓取 {code} 失败: {e}")
        return None

def detect_launch_pattern(df):
    """
    检测"启动涨停 -> 缩量回踩不破 -> 站稳五日线"形态
    返回：匹配成功返回 dict，否则返回 None
    """
    if len(df) < 20:
        return None
        
    df = df.reset_index(drop=True)
    df['ma5'] = df['close'].rolling(5).mean()
    
    # 找最近 15 天内的涨停或大阳线
    recent = df.tail(15)
    launch_idx = -1
    
    for i in range(len(recent)-1, -1, -1):
        row = recent.iloc[i]
        # 涨停（涨幅>9%）或 大阳线（涨幅>7% 且 量能>2倍）
        if row['change_pct'] > 9 or (row['change_pct'] > 7 and row['volume'] > df['volume'].tail(20).mean() * 2):
            launch_idx = i
            launch_row = row
            break
            
    if launch_idx == -1:
        return None
        
    # 检查启动点后的走势
    launch_point = launch_row['low']  # 启动点取最低价
    after_launch = recent.iloc[launch_idx+1:]
    
    if len(after_launch) < 2:  # 至少要有2天回踩
        return None
        
    # 条件1：缩量（后续日均量 < 启动日量的 60%）
    avg_vol_after = after_launch['volume'].mean()
    if avg_vol_after > launch_row['volume'] * 0.6:
        return None
        
    # 条件2：未跌破启动点
    if after_launch['low'].min() < launch_point * 0.99:
        return None
        
    # 条件3：当前价在 5日线附近（±3%）
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
        'price_position': 50,  # 简化版，后续优化
        'volatility': 50,
        'launch_pattern': 100 if pattern else 0
    }
    # 波动率分数（近期波动率越低越好）
    recent_vol = df['close'].tail(20).pct_change().std()
    hist_vol = df['close'].tail(120).pct_change().std()
    if recent_vol < hist_vol * 0.7:
        scores['volatility'] = 80
        
    # 价格位置（当前价在过去 120 天的分位）
    current = df.iloc[-1]['close']
    low_120 = df['close'].tail(120).min()
    high_120 = df['close'].tail(120).max()
    position = (current - low_120) / (high_120 - low_120) if high_120 > low_120 else 0.5
    scores['price_position'] = (1 - position) * 100  # 越低越好
    
    # 加权总分
    weights = {'price_position': 0.2, 'volatility': 0.2, 'launch_pattern': 0.6}
    total = sum(scores[k] * weights[k] for k in weights)
    return total, scores

def save_candidate(code, name, trade_date, score, last_price, launch_point, signals):
    """保存选股结果"""
    stop_loss = launch_point * 0.97  # 启动点下方 3%
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
    
    # 1. 获取股票列表
    stocks = get_stock_list()
    if stocks.empty:
        print("无法获取股票列表，退出")
        return
        
    print(f"共获取 {len(stocks)} 只股票，开始扫描...")
    
    candidates = []
    
    # 2. 遍历股票，抓取数据并检测形态
    for idx, row in stocks.iterrows():
        code = row['代码']
        name = row['名称']
        
        # 保存基本信息
        save_stock_basic(code, name)
        
        # 抓取 K线
        df = fetch_and_save_kline(code)
        if df is None or len(df) < 30:
            continue
            
        # 检测启动形态
        pattern = detect_launch_pattern(df)
        if pattern:
            score, details = calc_scores(df, pattern)
            # 只有总分>60 才入选
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
        
        # 每 50 只打印进度
        if idx % 50 == 0:
            print(f"进度: {idx}/{len(stocks)}")
            
    # 3. 更新排名
    candidates.sort(key=lambda x: x['score'], reverse=True)
    for rank, c in enumerate(candidates[:20], 1):  # 只保留前20
        supabase.table('candidates').update({'rank': rank}).eq('code', c['code']).eq('trade_date', today).execute()
        
    print(f"\n选股完成，共发现 {len(candidates)} 只候选股票")
    print("Top 5:")
    for c in candidates[:5]:
        print(f"  {c['name']}({c['code']}): {c['score']:.1f}分")

if __name__ == "__main__":
    main()
