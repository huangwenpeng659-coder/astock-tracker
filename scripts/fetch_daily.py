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

# ============ 技术分析 ============

def calc_macd(close, fast=12, slow=26, signal=9):
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=signal, adjust=False).mean()
    return dif, dea, (dif - dea) * 2

def calc_rsi(close, period=14):
    delta = close.diff()
    gain = delta.where(delta > 0, 0).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
    rs = gain / loss.replace(0, float('nan'))
    return 100 - 100 / (1 + rs)

def analyze_stock(df):
    """
    对单只股票做技术分析，返回评分(0-100)和信号列表
    维度：趋势 / 动量 / 量能 / 波动率 / 形态
    """
    if len(df) < 30:
        return None, ['数据不足(需至少30天)']

    df = df.reset_index(drop=True).copy()
    df['ma5'] = df['close'].rolling(5).mean()
    df['ma10'] = df['close'].rolling(10).mean()
    df['ma20'] = df['close'].rolling(20).mean()
    df['ma60'] = df['close'].rolling(60).mean()
    df['volume'] = df['volume'].astype(float)
    df['vol_ma5'] = df['volume'].rolling(5).mean()
    df['vol_ma20'] = df['volume'].rolling(20).mean()
    dif, dea, macd_hist = calc_macd(df['close'])
    df['macd_dif'] = dif
    df['macd_dea'] = dea
    df['macd_hist'] = macd_hist
    df['rsi'] = calc_rsi(df['close'])
    # 布林带
    df['boll_mid'] = df['close'].rolling(20).mean()
    df['boll_std'] = df['close'].rolling(20).std()
    df['boll_upper'] = df['boll_mid'] + 2 * df['boll_std']
    df['boll_lower'] = df['boll_mid'] - 2 * df['boll_std']

    last = df.iloc[-1]
    prev = df.iloc[-2] if len(df) >= 2 else last
    signals = []
    score = 50  # 基准分

    # 各维度明细: {名称: {weight, raw, points, reasons[]}}
    dims = {}

    # --- 1. 趋势 (权重30, 满分30) ---
    trend_score = 0
    reasons = []
    if last['close'] > last['ma20']:
        trend_score += 12; reasons.append('股价站上MA20(+12)')
    else:
        reasons.append('股价跌破MA20(0)')
    if last['ma5'] > last['ma10'] > last['ma20']:
        trend_score += 10; reasons.append('均线多头排列(+10)')
    elif last['ma5'] < last['ma10'] < last['ma20']:
        reasons.append('均线空头排列(0)')
    if not pd.isna(last['ma60']):
        if last['close'] > last['ma60']:
            trend_score += 8; reasons.append('站上MA60长期向上(+8)')
        else:
            reasons.append('低于MA60(0)')
    dims['趋势'] = {'weight':30, 'raw':trend_score, 'max':30, 'reasons':reasons}
    score += trend_score * 0.3
    signals.extend([('✅ ' if '+' in r else '⚠️ ') + r for r in reasons])

    # --- 2. 动量/MACD (权重20, 满分20) ---
    mom_score = 0
    reasons = []
    if not pd.isna(last['macd_dif']):
        if last['macd_dif'] > last['macd_dea']:
            mom_score += 10; reasons.append('MACD金叉，DIF在DEA上方(+10)')
        else:
            reasons.append('MACD死叉，DIF在DEA下方(0)')
        if last['macd_hist'] > 0 and prev['macd_hist'] <= 0:
            mom_score += 10; reasons.append('MACD柱由负转正，金叉信号(+10)')
        elif last['macd_hist'] > prev['macd_hist']:
            mom_score += 5; reasons.append('MACD柱放大(+5)')
    dims['动量'] = {'weight':20, 'raw':mom_score, 'max':20, 'reasons':reasons}
    score += mom_score * 0.2

    # --- 3. RSI (权重15, 满分15) ---
    rsi_score = 0
    reasons = []
    if not pd.isna(last['rsi']):
        rsi = last['rsi']
        if rsi < 30:
            rsi_score += 15; reasons.append(f'RSI={rsi:.1f} 超卖区，反弹机会(+15)')
        elif rsi < 45:
            rsi_score += 10; reasons.append(f'RSI={rsi:.1f} 偏低，低位区域(+10)')
        elif rsi < 70:
            rsi_score += 5; reasons.append(f'RSI={rsi:.1f} 中性(+5)')
        else:
            reasons.append(f'RSI={rsi:.1f} 超买区，回调风险(0)')
    dims['RSI'] = {'weight':15, 'raw':rsi_score, 'max':15, 'reasons':reasons}
    score += rsi_score * 0.15

    # --- 4. 量能 (权重20, 满分20) ---
    vol_score = 0
    reasons = []
    if not pd.isna(last['vol_ma5']) and not pd.isna(last['vol_ma20']):
        vol_ratio = last['vol_ma5'] / last['vol_ma20'] if last['vol_ma20'] > 0 else 1
        if 0.8 < vol_ratio < 1.5 and last['close'] > last['open']:
            vol_score += 10; reasons.append(f'量比{vol_ratio:.2f}，温和放量上涨(+10)')
        elif vol_ratio >= 1.5 and last['close'] > last['open']:
            vol_score += 8; reasons.append(f'量比{vol_ratio:.2f}，明显放量上涨(+8)')
        elif vol_ratio < 0.6:
            vol_score += 3; reasons.append(f'量比{vol_ratio:.2f}，缩量(只+3)')
        else:
            vol_score += 5; reasons.append(f'量比{vol_ratio:.2f}(+5)')
        recent5 = df.tail(5)
        price_up = recent5['close'].iloc[-1] > recent5['close'].iloc[0]
        vol_down = recent5['volume'].mean() < df['volume'].tail(20).mean()
        if price_up and vol_down:
            reasons.append('量价背离：上涨但缩量(-3)')
            vol_score -= 3
    dims['量能'] = {'weight':20, 'raw':max(0,vol_score), 'max':20, 'reasons':reasons}
    score += vol_score * 0.2

    # --- 5. 波动率/位置 (权重15, 满分15) ---
    vol2_score = 0
    reasons = []
    recent_vol = df['close'].tail(20).pct_change().std()
    if pd.notna(recent_vol):
        if recent_vol < 0.015:
            vol2_score += 8; reasons.append('低波动，可能酝酿方向(+8)')
        elif recent_vol > 0.04:
            reasons.append(f'高波动σ={recent_vol:.3f}(0)')
        else:
            vol2_score += 4; reasons.append(f'波动适中(+4)')
    boll_width = (last['boll_upper'] - last['boll_lower']) / last['boll_mid'] if last['boll_mid'] > 0 else 0
    if boll_width < 0.1:
        vol2_score += 7; reasons.append('布林带极度收口，即将变盘(+7)')
    elif boll_width < 0.15:
        vol2_score += 4; reasons.append('布林带收口(+4)')
    if last['close'] <= last['boll_lower']:
        vol2_score += 5; reasons.append('触及布林下轨，超卖(+5)')
    dims['波动'] = {'weight':15, 'raw':min(15,vol2_score), 'max':15, 'reasons':reasons}
    score += vol2_score * 0.15

    score = max(0, min(100, round(score, 1)))
    grade = '强烈关注' if score >= 75 else '关注' if score >= 60 else '中性' if score >= 45 else '回避'
    summary = (f"基准分50 + 趋势{dims['趋势']['raw']}×30% + 动量{dims['动量']['raw']}×20% "
               f"+ RSI{dims['RSI']['raw']}×15% + 量能{dims['量能']['raw']}×20% + 波动{dims['波动']['raw']}×15% = {score}")
    return {'score': score, 'grade': grade, 'signals': signals, 'dims': dims, 'summary': summary}, signals

def save_analysis(code, name, trade_date, result):
    try:
        supabase.table('stock_scores').upsert({
            'code': code, 'name': name, 'trade_date': trade_date,
            'score': result['score'], 'grade': result['grade'],
            'signals': json.dumps(result['signals'], ensure_ascii=False),
            'dims': json.dumps(result['dims'], ensure_ascii=False),
            'summary': result['summary']
        }, on_conflict='code,trade_date').execute()
    except Exception as e:
        print(f"保存分析失败 {code}: {e}")

def save_candidate(code, name, trade_date, score, last_price, launch_point, signals):
    stop_loss = launch_point * 0.97
    try:
        supabase.table('candidates').upsert({
            'code': code, 'name': name, 'trade_date': trade_date, 'score': round(score, 2),
            'last_price': last_price, 'launch_point': launch_point,
            'stop_loss': round(stop_loss, 2),
            'target_1': round(last_price * 1.05, 2),
            'target_2': round(last_price * 1.10, 2),
            'target_3': round(last_price * 1.15, 2),
            'signals': json.dumps(signals, ensure_ascii=False)
        }, on_conflict='code,trade_date').execute()
        # 同时写入追踪表
        supabase.table('candidate_tracking').upsert({
            'code': code, 'select_date': trade_date, 'name': name,
            'select_price': last_price, 'select_score': round(score, 2),
            'current_price': last_price, 'days_since': 0,
            'pct_change': 0, 'max_gain': 0, 'max_loss': 0,
            'hit_target': 0, 'hit_stop': False, 'status': '追踪中'
        }, on_conflict='code,select_date').execute()
    except Exception as e:
        print(f"保存候选失败 {code}: {e}")


# ============ 主函数 ============

# ============ 日内做T分析 ============

def get_stock_industry(code):
    try:
        df = ak.stock_individual_info_em(symbol=code)
        row = df.loc[df['item'] == '行业', 'value']
        return row.values[0] if len(row) else None
    except:
        return None

def get_industry_leaders(industry_name):
    try:
        df = ak.stock_board_industry_cons_em(symbol=industry_name)
        df = df.sort_values('涨跌幅', ascending=False).head(3)
        return [{'code': str(r['代码']).zfill(6), 'name': r['名称'], 'pct': round(float(r['涨跌幅']), 2)} for _, r in df.iterrows()]
    except:
        return []

def fetch_min_kline(code, period='5'):
    try:
        df = ak.stock_zh_a_hist_min_em(symbol=code, period=period, adjust='')
        if df is None or df.empty:
            return None
        df = df.rename(columns={'时间': 'time', '开盘': 'open', '收盘': 'close', '最高': 'high', '最低': 'low', '成交量': 'volume', '成交额': 'amount'})
        for c in ['open', 'close', 'high', 'low', 'volume', 'amount']:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors='coerce')
        today_str = datetime.now().strftime('%Y-%m-%d')
        df = df[df['time'].str.startswith(today_str)]
        if df.empty:
            df_all = ak.stock_zh_a_hist_min_em(symbol=code, period=period, adjust='')
            df_all = df_all.rename(columns={'时间': 'time', '开盘': 'open', '收盘': 'close', '最高': 'high', '最低': 'low', '成交量': 'volume', '成交额': 'amount'})
            for c in ['open', 'close', 'high', 'low', 'volume', 'amount']:
                if c in df_all.columns:
                    df_all[c] = pd.to_numeric(df_all[c], errors='coerce')
            last_date = df_all['time'].iloc[-1][:10]
            df = df_all[df_all['time'].str.startswith(last_date)]
        return df.reset_index(drop=True) if not df.empty else None
    except Exception as e:
        print(f"  获取分钟K线失败 {code}: {e}")
        return None

def calc_kdj(high, low, close, n=9, m1=3, m2=3):
    lowest = low.rolling(n).min()
    highest = high.rolling(n).max()
    rsv = (close - lowest) / (highest - lowest) * 100
    rsv = rsv.fillna(50)
    k = rsv.ewm(com=m1-1, adjust=False).mean()
    d = k.ewm(com=m2-1, adjust=False).mean()
    j = 3 * k - 2 * d
    return k, d, j

def calc_t_signals(df_min, leaders=None):
    if df_min is None or len(df_min) < 10:
        return None
    df = df_min.copy()
    signals = []

    df['cum_amount'] = df['amount'].cumsum()
    df['cum_volume'] = df['volume'].cumsum()
    df['vwap'] = df['cum_amount'] / df['cum_volume']
    vwap_last = float(df['vwap'].iloc[-1])

    # VWAP信号
    for i in range(max(1, len(df)-6), len(df)):
        row = df.iloc[i]
        dev = (row['close'] - row['vwap']) / row['vwap'] * 100 if row['vwap'] > 0 else 0
        if dev < -0.5:
            signals.append({'time': row['time'][-8:], 'type': '买入', 'reason': f'价格低于VWAP {abs(dev):.1f}%', 'indicator': 'VWAP'})
        elif dev > 0.5:
            signals.append({'time': row['time'][-8:], 'type': '卖出', 'reason': f'价格高于VWAP {dev:.1f}%', 'indicator': 'VWAP'})

    # 量能突变
    df['vol_ma20'] = df['volume'].rolling(20).mean()
    for i in range(max(20, len(df)-6), len(df)):
        row = df.iloc[i]
        if pd.notna(row['vol_ma20']) and row['vol_ma20'] > 0:
            ratio = row['volume'] / row['vol_ma20']
            if ratio >= 2:
                if row['close'] > row['open']:
                    signals.append({'time': row['time'][-8:], 'type': '买入', 'reason': f'放量上涨(量比{ratio:.1f}倍)', 'indicator': '量能'})
                else:
                    signals.append({'time': row['time'][-8:], 'type': '卖出', 'reason': f'放量下跌(量比{ratio:.1f}倍)', 'indicator': '量能'})

    # 分钟MACD
    dif, dea, hist = calc_macd(df['close'], 12, 26, 9)
    for i in range(max(1, len(df)-6), len(df)):
        if i < 1:
            continue
        if pd.notna(hist.iloc[i]) and pd.notna(hist.iloc[i-1]):
            if hist.iloc[i] > 0 and hist.iloc[i-1] <= 0:
                signals.append({'time': df.iloc[i]['time'][-8:], 'type': '买入', 'reason': 'MACD金叉', 'indicator': 'MACD'})
            elif hist.iloc[i] < 0 and hist.iloc[i-1] >= 0:
                signals.append({'time': df.iloc[i]['time'][-8:], 'type': '卖出', 'reason': 'MACD死叉', 'indicator': 'MACD'})

    # KDJ
    k, d, j = calc_kdj(df['high'], df['low'], df['close'])
    last_j = float(j.iloc[-1]) if pd.notna(j.iloc[-1]) else 50
    if last_j < 20:
        signals.append({'time': df.iloc[-1]['time'][-8:], 'type': '买入', 'reason': f'KDJ超卖(J={last_j:.0f})', 'indicator': 'KDJ'})
    elif last_j > 80:
        signals.append({'time': df.iloc[-1]['time'][-8:], 'type': '卖出', 'reason': f'KDJ超买(J={last_j:.0f})', 'indicator': 'KDJ'})

    # 布林带
    boll_mid = df['close'].rolling(20).mean()
    boll_std = df['close'].rolling(20).std()
    boll_upper = boll_mid + 2 * boll_std
    boll_lower = boll_mid - 2 * boll_std
    if len(df) >= 20:
        last_close = df.iloc[-1]['close']
        if pd.notna(boll_lower.iloc[-1]) and last_close <= boll_lower.iloc[-1]:
            signals.append({'time': df.iloc[-1]['time'][-8:], 'type': '买入', 'reason': '触及布林下轨', 'indicator': '布林带'})
        if pd.notna(boll_upper.iloc[-1]) and last_close >= boll_upper.iloc[-1]:
            signals.append({'time': df.iloc[-1]['time'][-8:], 'type': '卖出', 'reason': '触及布林上轨', 'indicator': '布林带'})

    # 板块联动
    leader_corr = ''
    if leaders:
        target_pct = (df.iloc[-1]['close'] - df.iloc[0]['open']) / df.iloc[0]['open'] * 100 if df.iloc[0]['open'] > 0 else 0
        leader_avg = sum(l['pct'] for l in leaders) / len(leaders)
        if leader_avg > 1 and target_pct < leader_avg * 0.5:
            leader_corr = f'板块龙头平均涨{leader_avg:.1f}%，目标股仅涨{target_pct:.1f}%，存在补涨机会'
            signals.append({'time': df.iloc[-1]['time'][-8:], 'type': '买入', 'reason': f'板块联动补涨(龙头+{leader_avg:.1f}%)', 'indicator': '板块'})
        elif leader_avg < -1 and target_pct > leader_avg * 0.5:
            leader_corr = f'板块龙头平均跌{leader_avg:.1f}%，目标股仅跌{target_pct:.1f}%，注意补跌风险'
            signals.append({'time': df.iloc[-1]['time'][-8:], 'type': '卖出', 'reason': f'板块联动补跌风险(龙头{leader_avg:.1f}%)', 'indicator': '板块'})
        else:
            leader_corr = f'板块龙头平均{leader_avg:+.1f}%，目标股{target_pct:+.1f}%，走势基本同步'

    # 综合建议
    buy_count = len([s for s in signals if s['type'] == '买入'])
    sell_count = len([s for s in signals if s['type'] == '卖出'])
    if buy_count >= 3:
        advice, confidence = '建议做T买入', '高'
    elif buy_count >= 2:
        advice, confidence = '可考虑做T买入', '中'
    elif sell_count >= 3:
        advice, confidence = '建议做T卖出', '高'
    elif sell_count >= 2:
        advice, confidence = '可考虑做T卖出', '中'
    elif buy_count > 0 and sell_count > 0:
        advice, confidence = '信号矛盾，建议观望', '低'
    else:
        advice, confidence = '信号不足，继续观察', '低'

    min_data = [{'t': r['time'][-8:], 'o': round(r['open'], 2), 'c': round(r['close'], 2),
                 'h': round(r['high'], 2), 'l': round(r['low'], 2), 'v': int(r['volume']),
                 'vwap': round(r['vwap'], 2) if pd.notna(r['vwap']) else None}
                for _, r in df.iterrows()]

    return {
        'vwap': round(vwap_last, 2),
        'signals': signals,
        'leaders': leaders or [],
        'leader_correlation': leader_corr,
        'advice': advice,
        'confidence': confidence,
        'min_kline': min_data
    }

def save_t_strategy(code, name, trade_date, industry, result):
    try:
        supabase.table('t_strategy').upsert({
            'code': code, 'name': name, 'trade_date': trade_date,
            'vwap': result['vwap'],
            'signals': json.dumps(result['signals'], ensure_ascii=False),
            'industry': industry or '',
            'leaders': json.dumps(result['leaders'], ensure_ascii=False),
            'leader_correlation': result['leader_correlation'],
            'advice': result['advice'],
            'confidence': result['confidence'],
            'min_kline': json.dumps(result['min_kline'], ensure_ascii=False)
        }, on_conflict='code,trade_date').execute()
        print(f"  做T分析: {name}({code}) → {result['advice']}[{result['confidence']}]")
    except Exception as e:
        print(f"  保存做T分析失败 {code}: {e}")

# ============ 信号检测 ============

def detect_limit_up(df):
    if len(df) < 1:
        return None
    last = df.iloc[-1]
    if last['change_pct'] >= 9.8:
        return {'price': float(last['close']), 'pct': round(float(last['change_pct']), 2)}
    return None

def detect_steady_rise(df):
    if len(df) < 4:
        return None
    recent = df.tail(3)
    prev_vol = float(df.iloc[-4]['volume'])
    prices = []
    volumes = []
    for _, row in recent.iterrows():
        if row['close'] <= row['open']:
            return None
        if row['change_pct'] <= 0:
            return None
        if float(row['volume']) <= prev_vol:
            return None
        prev_vol = float(row['volume'])
        prices.append(float(row['close']))
        volumes.append(int(row['volume']))
    total_pct = round((prices[-1] - float(df.iloc[-4]['close'])) / float(df.iloc[-4]['close']) * 100, 2)
    return {'prices': prices, 'volumes': volumes, 'total_pct': total_pct}

def save_signal(code, name, trade_date, signal_type, details):
    try:
        supabase.table('daily_signals').upsert({
            'code': code, 'name': name, 'trade_date': trade_date,
            'signal_type': signal_type,
            'price': details.get('price', details.get('prices', [0])[-1]),
            'details': details
        }, on_conflict='code,trade_date,signal_type').execute()
    except Exception as e:
        print(f"保存信号失败 {code} {signal_type}: {e}")

# ============ 追踪更新 ============

def update_tracking():
    """更新所有追踪中的候选股票的涨跌幅"""
    try:
        resp = supabase.table('candidate_tracking').select('*').eq('status', '追踪中').execute()
        tracking = resp.data or []
    except Exception as e:
        print(f"读取追踪列表失败: {e}")
        return

    if not tracking:
        print("无追踪中的候选")
        return

    print(f"\n===== 更新 {len(tracking)} 只追踪股票 =====")
    for t in tracking:
        code = t['code']
        select_price = float(t['select_price'])
        select_date = t['select_date']

        try:
            kl_resp = supabase.table('daily_kline').select('close,trade_date').eq('code', code).gte('trade_date', select_date).order('trade_date', desc=False).execute()
            klines = kl_resp.data or []
        except Exception as e:
            print(f"  {code} 读取K线失败: {e}")
            continue

        if not klines:
            continue

        days_since = len(klines) - 1
        current_price = float(klines[-1]['close'])
        pct_change = round((current_price - select_price) / select_price * 100, 2)

        prices = [float(k['close']) for k in klines]
        max_price = max(prices)
        min_price = min(prices)
        max_gain = round((max_price - select_price) / select_price * 100, 2)
        max_loss = round((min_price - select_price) / select_price * 100, 2)

        update = {
            'current_price': current_price,
            'days_since': days_since,
            'pct_change': pct_change,
            'max_gain': max(max_gain, float(t.get('max_gain') or 0)),
            'max_loss': min(max_loss, float(t.get('max_loss') or 0)),
        }

        # 固化 5/10/20 日涨跌幅
        if days_since >= 5 and t.get('pct_5d') is None:
            p5 = float(klines[5]['close']) if len(klines) > 5 else current_price
            update['pct_5d'] = round((p5 - select_price) / select_price * 100, 2)
        if days_since >= 10 and t.get('pct_10d') is None:
            p10 = float(klines[10]['close']) if len(klines) > 10 else current_price
            update['pct_10d'] = round((p10 - select_price) / select_price * 100, 2)
        if days_since >= 20 and t.get('pct_20d') is None:
            p20 = float(klines[20]['close']) if len(klines) > 20 else current_price
            update['pct_20d'] = round((p20 - select_price) / select_price * 100, 2)

        # 检查是否触及止损/目标
        try:
            cand_resp = supabase.table('candidates').select('stop_loss,target_1,target_2,target_3').eq('code', code).eq('trade_date', select_date).execute()
            if cand_resp.data:
                cand = cand_resp.data[0]
                stop = float(cand.get('stop_loss') or 0)
                targets = [float(cand.get(f'target_{i}') or 0) for i in [1, 2, 3]]
                if stop > 0 and min_price <= stop:
                    update['hit_stop'] = True
                hit = 0
                for i, tgt in enumerate(targets, 1):
                    if tgt > 0 and max_price >= tgt:
                        hit = i
                update['hit_target'] = hit
        except:
            pass

        if days_since >= 20:
            update['status'] = '已完成'

        try:
            supabase.table('candidate_tracking').update(update).eq('code', code).eq('select_date', select_date).execute()
            status_tag = update.get('status', '追踪中')
            print(f"  {t.get('name', code)}({code}): {days_since}天 {pct_change:+.2f}% [{status_tag}]")
        except Exception as e:
            print(f"  {code} 更新追踪失败: {e}")


def main():
    params = load_config()
    today = datetime.now().strftime("%Y-%m-%d")

    # --- 自选股分析 ---
    try:
        wl_resp = supabase.table('watchlist').select('*').execute()
        watchlist = wl_resp.data or []
    except Exception as e:
        print(f"读取自选股失败: {e}")
        watchlist = []

    if watchlist:
        print(f"\n===== 分析 {len(watchlist)} 只自选股 =====")
        for item in watchlist:
            code = item['code']
            name = item.get('name', code)
            df = fetch_and_save_kline(code)
            if df is None or len(df) < 20:
                print(f"  {name}({code}) 数据不足，跳过")
                continue
            result, _ = analyze_stock(df)
            if result:
                save_analysis(code, name, today, result)
                print(f"  {name}({code}): {result['score']}分 [{result['grade']}]")
            # 做T分析
            try:
                min_df = fetch_min_kline(code)
                if min_df is not None and len(min_df) >= 10:
                    industry = get_stock_industry(code)
                    leaders = get_industry_leaders(industry) if industry else []
                    t_result = calc_t_signals(min_df, leaders)
                    if t_result:
                        save_t_strategy(code, name, today, industry, t_result)
                else:
                    print(f"  {name}({code}) 分钟K线不足，跳过做T分析")
            except Exception as e:
                print(f"  {name}({code}) 做T分析异常: {e}")

    # --- 全池选股 ---
    stock_pool = get_dynamic_stock_pool()
    print(f"\n开始抓取 A股数据（共{len(stock_pool)}只）...")
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

        lu = detect_limit_up(df)
        if lu:
            save_signal(code, name, today, '涨停', lu)
            print(f"  涨停: {name}({code}) {lu['pct']:.1f}%")

        sr = detect_steady_rise(df)
        if sr:
            save_signal(code, name, today, '放量上涨', sr)
            print(f"  放量上涨: {name}({code}) 3日{sr['total_pct']:.1f}%")

        if idx % 20 == 0:
            print(f"进度: {idx}/{len(stock_pool)}，成功抓取 {success_count} 只")

    candidates.sort(key=lambda x: x['score'], reverse=True)
    for rank, c in enumerate(candidates[:20], 1):
        supabase.table('candidates').update({'rank': rank}).eq('code', c['code']).eq('trade_date', today).execute()

    print(f"\n抓取完成，成功 {success_count}/{len(stock_pool)} 只")
    print(f"选股完成，共发现 {len(candidates)} 只候选股票")
    for c in candidates[:5]:
        print(f"  {c['name']}({c['code']}): {c['score']:.1f}分")

    # 更新历史追踪
    update_tracking()

if __name__ == "__main__":
    main()
