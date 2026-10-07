"""
A股每日数据抓取 + 启动形态选股脚本
数据源：新浪财经
股票池：支持动态获取（指数成分股 / 市值前N）或静态列表

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

# ============ 股票池配置 ============
# mode: "indices" = 按指数成分股, "top_n" = 市值前N, "static" = 固定列表
# indices: 逗号分隔的指数代码 (000300=沪深300, 000016=上证50, 000905=中证500)
# top_n: 市值前N只
POOL_CONFIG = {
    "mode": os.environ.get("STOCK_POOL_MODE", "indices"),
    "indices": os.environ.get("STOCK_POOL_INDICES", "000300").split(","),
    "top_n": int(os.environ.get("STOCK_POOL_TOP_N", "500")),
}

# 静态兜底股票池
STOCK_POOL = [
    ("600519", "贵州茅台"), ("600036", "招商银行"), ("601318", "中国平安"),
    ("600000", "浦发银行"), ("601166", "兴业银行"), ("600887", "伊利股份"),
    ("600276", "恒瑞医药"), ("600309", "万华化学"), ("600585", "海螺水泥"),
    ("600690", "海尔智家"), ("600809", "山西汾酒"), ("601888", "中国中免"),
    ("601899", "紫金矿业"), ("601988", "中国银行"), ("600048", "保利发展"),
    ("600104", "上汽集团"), ("600196", "复星医药"), ("600406", "国电南瑞"),
    ("600438", "通威股份"), ("600489", "中金黄金"), ("600600", "青岛啤酒"),
    ("600703", "三安光电"), ("600745", "闻泰科技"), ("600760", "中航沈飞"),
    ("600893", "航发动力"), ("601088", "中国神华"), ("601100", "恒立液压"),
    ("601211", "国泰君安"), ("601231", "环旭电子"), ("601336", "新华保险"),
    ("601390", "中国中铁"), ("601600", "中国铝业"), ("601668", "中国建筑"),
    ("601688", "华泰证券"), ("601766", "中国中车"), ("601788", "光大证券"),
    ("601818", "光大银行"), ("601857", "中国石油"), ("601866", "中远海发"),
    ("601939", "建设银行"), ("601985", "中国核电"), ("603288", "海天味业"),
    ("603501", "韦尔股份"), ("603986", "兆易创新"), ("000001", "平安银行"),
    ("000002", "万科A"), ("000063", "中兴通讯"), ("000066", "中国长城"),
    ("000100", "TCL科技"), ("000157", "中联重科"), ("000333", "美的集团"),
    ("000338", "潍柴动力"), ("000425", "徐工机械"), ("000538", "云南白药"),
    ("000568", "泸州老窖"), ("000596", "古井贡酒"), ("000625", "长安汽车"),
    ("000651", "格力电器"), ("000661", "长春高新"), ("000708", "中信特钢"),
    ("000725", "京东方A"), ("000768", "中航西飞"), ("000858", "五粮液"),
    ("000876", "新希望"), ("000895", "双汇发展"), ("002027", "分众传媒"),
    ("002032", "苏泊尔"), ("002142", "宁波银行"), ("002230", "科大讯飞"),
    ("002241", "歌尔股份"), ("002252", "上海莱士"), ("002415", "海康威视"),
    ("002460", "赣锋锂业"), ("002466", "天齐锂业"), ("002475", "立讯精密"),
    ("002594", "比亚迪"), ("002601", "龙佰集团"), ("002714", "牧原股份"),
    ("300014", "亿纬锂能"), ("300015", "爱尔眼科"), ("300033", "同花顺"),
    ("300059", "东方财富"), ("300122", "智飞生物"), ("300124", "汇川技术"),
    ("300274", "阳光电源"), ("300408", "三环集团"), ("300750", "宁德时代"),
    ("300760", "迈瑞医疗"), ("300896", "爱美客"),
]

# ============ 动态股票池获取 ============

def _fetch_index_constituents(symbol):
    """获取指数成分股，返回 [(code, name), ...]"""
    try:
        df = ak.index_stock_cons_csindex(symbol=symbol)
        results = []
        for _, row in df.iterrows():
            code = str(row['成分券代码']).zfill(6)
            name = row['成分券名称']
            results.append((code, name))
        print(f"  获取指数 {symbol} 成分股 {len(results)} 只")
        return results
    except Exception as e:
        print(f"  获取指数 {symbol} 成分股失败: {e}")
        return []

def _fetch_top_n_by_market_cap(n):
    """获取市值前N只A股，返回 [(code, name), ...]"""
    try:
        df = ak.stock_zh_a_spot_em()
        df = df[~df['名称'].str.contains('ST|退', na=False)]
        df = df.dropna(subset=['总市值'])
        df = df.sort_values('总市值', ascending=False).head(n)
        results = [(str(row['代码']).zfill(6), row['名称']) for _, row in df.iterrows()]
        print(f"  获取市值前 {n} 只股票，实际 {len(results)} 只")
        return results
    except Exception as e:
        print(f"  获取市值前N失败: {e}")
        return []

def get_dynamic_stock_pool():
    """根据 POOL_CONFIG 获取股票池，失败则回退到静态列表"""
    mode = POOL_CONFIG["mode"]

    if mode == "static":
        print(f"使用静态股票池（{len(STOCK_POOL)}只）")
        return STOCK_POOL

    pool = []
    if mode == "indices":
        for symbol in POOL_CONFIG["indices"]:
            symbol = symbol.strip()
            if symbol:
                pool.extend(_fetch_index_constituents(symbol))
    elif mode == "top_n":
        pool = _fetch_top_n_by_market_cap(POOL_CONFIG["top_n"])

    # 去重（按code），过滤北交所（8xx/4xx开头，新浪不支持）
    seen = set()
    deduped = []
    for code, name in pool:
        if code not in seen and not code.startswith('8') and not code.startswith('4'):
            seen.add(code)
            deduped.append((code, name))

    if not deduped:
        print(f"动态获取股票池为空，回退到静态列表（{len(STOCK_POOL)}只）")
        return STOCK_POOL

    print(f"动态获取股票池成功，共 {len(deduped)} 只")
    return deduped


# ============ 工具函数 ============

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
    if code.startswith('6'):
        return f"sh{code}"
    else:
        return f"sz{code}"

def save_stock_basic(code, name):
    try:
        supabase.table('stocks').upsert({
            'code': code, 'name': name,
            'is_st': False, 'is_delisted': False
        }, on_conflict='code').execute()
    except Exception as e:
        print(f"保存股票信息失败 {code}: {e}")

def _fetch_kline_sina(code):
    sina_symbol = get_sina_symbol(code)
    start = (datetime.now() - timedelta(days=180)).strftime("%Y%m%d")
    end = datetime.now().strftime("%Y%m%d")
    return ak.stock_zh_a_daily(symbol=sina_symbol, start_date=start, end_date=end, adjust="qfq")

def fetch_and_save_kline(code):
    df = retry(_fetch_kline_sina, max_attempts=2, delay=2, code=code)
    if df is None or df.empty:
        return None

    try:
        df = df.reset_index()
        if 'date' in df.columns:
            df = df.rename(columns={'date': 'trade_date'})
        elif 'index' in df.columns:
            df = df.rename(columns={'index': 'trade_date'})

        required = ['open', 'high', 'low', 'close', 'volume']
        for col in required:
            if col not in df.columns:
                print(f"  {code} 缺少字段 {col}，跳过")
                return None

        df['close'] = df['close'].astype(float)
        df['open'] = df['open'].astype(float)
        df['high'] = df['high'].astype(float)
        df['low'] = df['low'].astype(float)
        df['volume'] = df['volume'].astype(float).astype(int)
        df['change_pct'] = df['close'].pct_change() * 100
        df['amount'] = (df['close'] * df['volume']).astype(int)
        df['turnover_rate'] = (df['turnover'] * 100) if 'turnover' in df.columns else None

        df = df.dropna(subset=['change_pct']).reset_index(drop=True)
        if df.empty:
            return None

        records = df.tail(60).copy()
        records['trade_date'] = records['trade_date'].astype(str).str[:10]
        records['code'] = code

        keep_cols = ['code', 'trade_date', 'open', 'high', 'low', 'close',
                     'volume', 'amount', 'turnover_rate', 'change_pct']
        records = records[keep_cols]
        rows = records.to_dict('records')
        for r in rows:
            for k, v in r.items():
                if pd.isna(v):
                    r[k] = None

        supabase.table('daily_kline').upsert(rows, on_conflict='code,trade_date').execute()
        return df
    except Exception as e:
        print(f"保存 {code} K线失败: {e}")
        return None


# ============ 选股逻辑 ============

DEFAULT_PARAMS = {
    'min_change_pct': 7,
    'volume_ratio': 2.0,
    'shrink_ratio': 0.6,
    'pullback_tolerance': 0.99,
    'ma5_deviation': 0.03,
    'min_score': 60,
}

def load_config():
    try:
        resp = supabase.table('config').select('*').eq('key','thresholds').execute()
        params = dict(DEFAULT_PARAMS)
        if resp.data and resp.data[0].get('value'):
            val = resp.data[0]['value']
            if isinstance(val, str):
                val = json.loads(val)
            for k in params:
                if k in val:
                    params[k] = type(DEFAULT_PARAMS[k])(val[k])
        print(f"选股参数: {params}")
        return params
    except Exception as e:
        print(f"读取config失败，使用默认参数: {e}")
        return dict(DEFAULT_PARAMS)

def detect_launch_pattern(df, params):
    if len(df) < 20:
        return None
    df = df.reset_index(drop=True)
    df['ma5'] = df['close'].rolling(5).mean()
    df['volume'] = df['volume'].astype(float)
    recent = df.tail(15)
    launch_idx = -1
    launch_row = None
    for i in range(len(recent) - 1, -1, -1):
        row = recent.iloc[i]
        vol_mean20 = df['volume'].tail(20).mean()
        if row['change_pct'] > 9 or (row['change_pct'] > params['min_change_pct'] and row['volume'] > vol_mean20 * params['volume_ratio']):
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
    if avg_vol_after > launch_row['volume'] * params['shrink_ratio']:
        return None
    if after_launch['low'].min() < launch_point * params['pullback_tolerance']:
        return None
    last_close = df.iloc[-1]['close']
    last_ma5 = df.iloc[-1]['ma5']
    if pd.isna(last_ma5) or abs(last_close - last_ma5) / last_ma5 > params['ma5_deviation']:
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
    hist_vol = df['close'].tail(min(120, len(df))).pct_change().std()
    if pd.notna(recent_vol) and pd.notna(hist_vol) and hist_vol > 0 and recent_vol < hist_vol * 0.7:
        scores['volatility'] = 80
    current = df.iloc[-1]['close']
    tail_n = min(120, len(df))
    low_n = df['close'].tail(tail_n).min()
    high_n = df['close'].tail(tail_n).max()
    position = (current - low_n) / (high_n - low_n) if high_n > low_n else 0.5
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
        }, on_conflict='code,trade_date').execute()
    except Exception as e:
        print(f"保存候选失败 {code}: {e}")


# ============ 主函数 ============

def main():
    params = load_config()
    stock_pool = get_dynamic_stock_pool()
    print(f"开始抓取 A股数据（共{len(stock_pool)}只）...")
    today = datetime.now().strftime("%Y-%m-%d")

    candidates = []
    success_count = 0

    for idx, (code, name) in enumerate(stock_pool):
        save_stock_basic(code, name)

        df = fetch_and_save_kline(code)
        if df is None or len(df) < 30:
            continue
        success_count += 1

        pattern = detect_launch_pattern(df, params)
        if pattern:
            score, details = calc_scores(df, pattern)
            if score > params['min_score']:
                candidates.append({'code': code, 'name': name, 'score': score})
                save_candidate(code, name, today, score, pattern['last_price'], pattern['launch_point'], details)
                print(f"发现候选: {name}({code}) 分数:{score:.1f}")

        if idx % 20 == 0:
            print(f"进度: {idx}/{len(stock_pool)}，成功抓取 {success_count} 只")

    candidates.sort(key=lambda x: x['score'], reverse=True)
    for rank, c in enumerate(candidates[:20], 1):
        supabase.table('candidates').update({'rank': rank}).eq('code', c['code']).eq('trade_date', today).execute()

    print(f"\n抓取完成，成功 {success_count}/{len(stock_pool)} 只")
    print(f"选股完成，共发现 {len(candidates)} 只候选股票")
    for c in candidates[:5]:
        print(f"  {c['name']}({c['code']}): {c['score']:.1f}分")

if __name__ == "__main__":
    main()
