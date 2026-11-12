"""
A股每日数据抓取 + 启动形态选股脚本
数据源：新浪财经
股票池：支持动态获取（指数成分股 / 市值前N）或静态列表

"""
import os
import json
import time
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
import pandas as pd
import akshare as ak
import requests
from supabase import create_client

# 统一使用北京时间：CI runner 默认 UTC，直接用 datetime.now() 会导致交易日错位
try:
    from zoneinfo import ZoneInfo
    CN_TZ = ZoneInfo("Asia/Shanghai")
except Exception:
    CN_TZ = timezone(timedelta(hours=8))

def now_cn():
    return datetime.now(CN_TZ)

supabase = create_client(
    os.environ["SUPABASE_URL"],
    os.environ["SUPABASE_SERVICE_KEY"]
)

# ============ 股票池配置 ============
# mode: "indices" = 按指数成分股, "top_n" = 市值前N, "static" = 固定列表
# indices: 逗号分隔的指数代码 (000300=沪深300, 000016=上证50, 000905=中证500)
# top_n: 市值前N只
# 并发线程数：新浪/东财对高频请求敏感，8线程是比较稳的上限
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "8"))

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

# ============ 全市场中文名映射 ============
# 覆盖全部A股（含非沪深300成分股），保证任何来源的代码都能显示中文名
NAME_MAP = {}
_NAME_MAP_LOADED = False

def load_name_map():
    global NAME_MAP, _NAME_MAP_LOADED
    if _NAME_MAP_LOADED:
        return NAME_MAP
    try:
        df = ak.stock_info_a_code_name()
        NAME_MAP = {str(r['code']).zfill(6): str(r['name']).strip() for _, r in df.iterrows()}
        print(f"加载A股中文名映射 {len(NAME_MAP)} 只")
    except Exception as e:
        print(f"加载中文名映射失败: {e}")
    _NAME_MAP_LOADED = True
    return NAME_MAP

def get_stock_name(code, fallback=None):
    """取中文名：映射表 > 入参 > 代码本身"""
    if not _NAME_MAP_LOADED:
        load_name_map()
    n = NAME_MAP.get(str(code).zfill(6))
    if n:
        return n
    if fallback and fallback != code:
        return fallback
    return str(code)

# ============ 概念板块 ============
_CONCEPT_BOARDS = None
_CONCEPT_CODE_SET = set()

def _http_json(url, params, tries=3):
    """带重试的GET请求（东财接口偶发断连）"""
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, params=params, timeout=25, headers={'User-Agent': 'Mozilla/5.0'})
            r.encoding = 'utf-8'
            return r.json()
        except Exception as e:
            last = e
            time.sleep(1.5 * (i + 1))
    print(f"  请求失败 {url}: {last}")
    return None

def _load_concept_boards():
    """概念板块代码集合（东财 m:90+t:3）"""
    global _CONCEPT_BOARDS, _CONCEPT_CODE_SET
    if _CONCEPT_BOARDS is not None:
        return _CONCEPT_BOARDS
    boards = {}
    j = _http_json('https://push2.eastmoney.com/api/qt/clist/get',
                   {'pn': '1', 'pz': '600', 'po': '1', 'np': '1', 'fltt': '2', 'invt': '2',
                    'fid': 'f3', 'fs': 'm:90+t:3+f:!50', 'fields': 'f12,f14'})
    for x in ((j or {}).get('data') or {}).get('diff') or []:
        boards[x['f12']] = x['f14']
    _CONCEPT_BOARDS = boards
    _CONCEPT_CODE_SET = set(boards.keys())
    print(f"加载概念板块 {len(boards)} 个")
    return boards

def get_stock_concepts(code):
    """返回该股所属概念板块名称列表"""
    boards = _load_concept_boards()
    if not boards:
        return []
    j = _http_json('https://push2.eastmoney.com/api/qt/slist/get',
                   {'fltt': '2', 'invt': '2', 'fields': 'f12,f14',
                    'secid': f'{1 if code.startswith("6") else 0}.{code}', 'pn': '1', 'pz': '100',
                    'po': '1', 'np': '1', 'fid': 'f62', 'spt': '3'})
    diff = ((j or {}).get('data') or {}).get('diff') or []
    concepts = []
    for x in diff:
        bid = x.get('f12')
        if bid in _CONCEPT_CODE_SET:
            nm = boards.get(bid) or x.get('f14')
            if nm and nm not in concepts:
                concepts.append(nm)
    return concepts[:12]

def save_stock_concepts(code, name, concepts):
    """概念写入 config KV（键 concepts:<code>），无需新建表"""
    if not concepts:
        return
    kv_set(f'concepts:{code}', {'name': name, 'concepts': concepts})

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
    start = (now_cn() - timedelta(days=180)).strftime("%Y%m%d")
    end = now_cn().strftime("%Y%m%d")
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

# 行业龙头按行业名缓存：多个候选股常属同一行业，避免重复请求
_leaders_cache = {}

def get_stock_industry(code):
    """获取行业。akshare 封装随版本变动易失效，失败时直接回退东财接口"""
    try:
        df = ak.stock_individual_info_em(symbol=code)
        row = df.loc[df['item'] == '行业', 'value']
        if len(row):
            return row.values[0]
    except Exception:
        pass
    try:
        market = 1 if code.startswith('6') else 0
        r = requests.get(
            'https://push2.eastmoney.com/api/qt/stock/get',
            params={'fltt': '2', 'invt': '2', 'fields': 'f127', 'secid': f'{market}.{code}'},
            timeout=15)
        return ((r.json() or {}).get('data') or {}).get('f127')
    except Exception:
        return None

def get_industry_leaders(industry_name):
    if not industry_name:
        return []
    if industry_name in _leaders_cache:
        return _leaders_cache[industry_name]
    try:
        df = ak.stock_board_industry_cons_em(symbol=industry_name)
        df = df.sort_values('涨跌幅', ascending=False).head(3)
        leaders = [{'code': str(r['代码']).zfill(6), 'name': r['名称'], 'pct': round(float(r['涨跌幅']), 2)} for _, r in df.iterrows()]
    except:
        leaders = []
    _leaders_cache[industry_name] = leaders
    return leaders

def fetch_min_kline(code, period='30', days=5):
    """拉取最近N个交易日的分钟K线，period 取 '5'/'15'/'30'/'60'"""
    try:
        end_date = now_cn().strftime('%Y-%m-%d %H:%M:%S')
        start_date = (now_cn() - timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')
        df = ak.stock_zh_a_hist_min_em(symbol=code, start_date=start_date, end_date=end_date, period=period, adjust='')
        if df is None or df.empty:
            return None
        df = df.rename(columns={'时间': 'time', '开盘': 'open', '收盘': 'close', '最高': 'high', '最低': 'low', '成交量': 'volume', '成交额': 'amount'})
        for c in ['open', 'close', 'high', 'low', 'volume', 'amount']:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors='coerce')
        df = df.dropna(subset=['close']).reset_index(drop=True)
        if df.empty:
            return None
        df['day'] = df['time'].astype(str).str[:10]
        return df
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

# ============ 日内做T：标准化信号规则 ============
# 每条规则统一输出：指标 / 类型 / 触发值 / 阈值 / 强度(强中弱) / 原因 / 价格
# 强度权重：强=2 中=1 弱=0.5；同向强度分 >=3 视为共振
T_RULES = {
    'VWAP': {'dev_strong': 0.8, 'dev_mid': 0.5, 'desc': '价格偏离分时均价线(VWAP)的幅度'},
    '量能': {'ratio_strong': 2.0, 'ratio_mid': 1.5, 'ma_5m': 20, 'ma_30m': 10, 'desc': '当前K线成交量 / 前N根均量'},
    'MACD': {'desc': 'MACD柱状线在零轴上下穿越'},
    'KDJ': {'j_low_strong': 15, 'j_low_mid': 25, 'j_high_strong': 85, 'j_high_mid': 75, 'desc': 'KDJ的J值超买超卖'},
    '布林带': {'near_pct': 0.01, 'desc': '价格触及或接近布林带上下轨'},
    '板块': {'leader_threshold': 1.0, 'lag_ratio': 0.5, 'desc': '板块龙头涨跌幅与目标股的背离'},
    '5分钟确认': {'desc': '30分钟信号方向与最新5分钟K线方向一致'},
}

def _mk_signal(time, stype, indicator, value, threshold, strength, reason, price):
    return {'time': time, 'type': stype, 'indicator': indicator, 'value': value,
            'threshold': threshold, 'strength': strength, 'reason': reason, 'price': price}

def calc_t_signals(df_min, leaders=None, period='30', code=None):
    """标准化做T信号引擎。
    - 指标(MA/量能/MACD/KDJ/布林)在完整历史上计算，保证30分钟级别有足够样本
    - VWAP 每天独立累计（分时均价线）
    - 只在最后一个交易日的最近若干根K线上评估买卖信号
    """
    if df_min is None or len(df_min) < 8:
        return None
    df = df_min.copy().reset_index(drop=True)
    signals = []
    r = T_RULES

    # 分时均价线：每天从0开始累计
    df['cum_amount'] = df.groupby('day')['amount'].cumsum()
    df['cum_volume'] = df.groupby('day')['volume'].cumsum()
    # akshare 分钟K：成交额单位为元，成交量单位为手（1手=100股）
    df['vwap'] = df['cum_amount'] / (df['cum_volume'] * 100)
    vwap_last = float(df['vwap'].iloc[-1])

    last_day = df['day'].iloc[-1]
    day_idx = [i for i in range(len(df)) if df['day'].iloc[i] == last_day]
    recent_idx = day_idx[-4:] if len(day_idx) >= 4 else day_idx
    row = df.iloc[-1]

    def tstr(idx):
        return df.iloc[idx]['time']

    # 1) VWAP 偏离
    for i in recent_idx:
        rw = df.iloc[i]
        if not rw['vwap'] or rw['vwap'] <= 0:
            continue
        dev = (rw['close'] - rw['vwap']) / rw['vwap'] * 100
        adev = abs(dev)
        if adev >= r['VWAP']['dev_strong']:
            strength, thr = '强', r['VWAP']['dev_strong']
        elif adev >= r['VWAP']['dev_mid']:
            strength, thr = '中', r['VWAP']['dev_mid']
        else:
            continue
        stype = '买入' if dev < 0 else '卖出'
        word = '低于' if dev < 0 else '高于'
        signals.append(_mk_signal(tstr(i), stype, 'VWAP', round(dev, 2), thr, strength,
                                   '%sVWAP %.2f%%（阈值%s%%）' % (word, adev, thr), float(rw['close'])))

    # 2) 量能突变（均线长度按周期自适应：5分钟20根，30分钟10根）
    ma_n = r['量能']['ma_5m'] if period == '5' else r['量能']['ma_30m']
    df['vol_ma'] = df['volume'].rolling(ma_n).mean()
    for i in recent_idx:
        rw = df.iloc[i]
        if not pd.notna(rw['vol_ma']) or rw['vol_ma'] <= 0:
            continue
        ratio = rw['volume'] / rw['vol_ma']
        if ratio >= r['量能']['ratio_strong']:
            strength, thr = '强', r['量能']['ratio_strong']
        elif ratio >= r['量能']['ratio_mid']:
            strength, thr = '中', r['量能']['ratio_mid']
        else:
            continue
        stype = '买入' if rw['close'] > rw['open'] else '卖出'
        word = '放量上涨' if stype == '买入' else '放量下跌'
        signals.append(_mk_signal(tstr(i), stype, '量能', round(ratio, 2), thr, strength,
                                   '%s，量比%.2f倍（阈值%s倍）' % (word, ratio, thr), float(rw['close'])))

    # 3) MACD 零轴穿越
    dif, dea, hist = calc_macd(df['close'], 12, 26, 9)
    for i in recent_idx:
        if i < 1 or not pd.notna(hist.iloc[i]) or not pd.notna(hist.iloc[i - 1]):
            continue
        above_zero = hist.iloc[i] > 0
        cross_up = above_zero and hist.iloc[i - 1] <= 0
        cross_dn = (not above_zero) and hist.iloc[i - 1] >= 0
        if not (cross_up or cross_dn):
            continue
        stype = '买入' if cross_up else '卖出'
        strength = '强' if above_zero else '中'
        word = '零轴上方金叉' if cross_up else '零轴下方死叉'
        signals.append(_mk_signal(tstr(i), stype, 'MACD', round(float(hist.iloc[i]), 4), 0, strength,
                                   word, float(df.iloc[i]['close'])))

    # 4) KDJ 超买超卖
    k, d, j = calc_kdj(df['high'], df['low'], df['close'])
    jv = float(j.iloc[-1]) if pd.notna(j.iloc[-1]) else 50
    if jv <= r['KDJ']['j_low_strong']:
        signals.append(_mk_signal(tstr(len(df) - 1), '买入', 'KDJ', round(jv, 1), r['KDJ']['j_low_strong'], '强',
                                   'J值%.0f 深度超卖（阈值%d）' % (jv, r['KDJ']['j_low_strong']), float(row['close'])))
    elif jv <= r['KDJ']['j_low_mid']:
        signals.append(_mk_signal(tstr(len(df) - 1), '买入', 'KDJ', round(jv, 1), r['KDJ']['j_low_mid'], '中',
                                   'J值%.0f 超卖（阈值%d）' % (jv, r['KDJ']['j_low_mid']), float(row['close'])))
    elif jv >= r['KDJ']['j_high_strong']:
        signals.append(_mk_signal(tstr(len(df) - 1), '卖出', 'KDJ', round(jv, 1), r['KDJ']['j_high_strong'], '强',
                                   'J值%.0f 深度超买（阈值%d）' % (jv, r['KDJ']['j_high_strong']), float(row['close'])))
    elif jv >= r['KDJ']['j_high_mid']:
        signals.append(_mk_signal(tstr(len(df) - 1), '卖出', 'KDJ', round(jv, 1), r['KDJ']['j_high_mid'], '中',
                                   'J值%.0f 超买（阈值%d）' % (jv, r['KDJ']['j_high_mid']), float(row['close'])))

    # 5) 布林带
    boll_mid = df['close'].rolling(20).mean()
    boll_std = df['close'].rolling(20).std()
    boll_upper = boll_mid + 2 * boll_std
    boll_lower = boll_mid - 2 * boll_std
    if len(df) >= 20:
        last_close = float(df.iloc[-1]['close'])
        lo = boll_lower.iloc[-1]
        up = boll_upper.iloc[-1]
        if pd.notna(lo):
            if last_close <= lo:
                signals.append(_mk_signal(tstr(len(df) - 1), '买入', '布林带', round(last_close, 2), round(float(lo), 2), '强',
                                           '收盘跌破布林下轨', last_close))
            elif last_close <= lo * (1 + r['布林带']['near_pct']):
                signals.append(_mk_signal(tstr(len(df) - 1), '买入', '布林带', round(last_close, 2), round(float(lo), 2), '中',
                                           '接近布林下轨', last_close))
        if pd.notna(up):
            if last_close >= up:
                signals.append(_mk_signal(tstr(len(df) - 1), '卖出', '布林带', round(last_close, 2), round(float(up), 2), '强',
                                           '收盘突破布林上轨', last_close))
            elif last_close >= up * (1 - r['布林带']['near_pct']):
                signals.append(_mk_signal(tstr(len(df) - 1), '卖出', '布林带', round(last_close, 2), round(float(up), 2), '中',
                                           '接近布林上轨', last_close))

    # 6) 板块联动
    leader_corr = ''
    if leaders:
        day_rows = df[df['day'] == last_day]
        target_pct = (day_rows.iloc[-1]['close'] - day_rows.iloc[0]['open']) / day_rows.iloc[0]['open'] * 100 if day_rows.iloc[0]['open'] > 0 else 0
        leader_avg = sum(l['pct'] for l in leaders) / len(leaders)
        thr = r['板块']['leader_threshold']
        lag = r['板块']['lag_ratio']
        if leader_avg >= thr and target_pct < leader_avg * lag:
            leader_corr = '板块龙头平均涨%.1f%%，目标股仅涨%.1f%%，存在补涨机会' % (leader_avg, target_pct)
            signals.append(_mk_signal(tstr(len(df) - 1), '买入', '板块', round(leader_avg, 2), thr, '强',
                                       '板块龙头均涨%.1f%%，目标股滞后（补涨）' % leader_avg, float(row['close'])))
        elif leader_avg <= -thr and target_pct > leader_avg * lag:
            leader_corr = '板块龙头平均跌%.1f%%，目标股仅跌%.1f%%，注意补跌风险' % (leader_avg, target_pct)
            signals.append(_mk_signal(tstr(len(df) - 1), '卖出', '板块', round(leader_avg, 2), thr, '强',
                                       '板块龙头均跌%.1f%%，目标股滞后（补跌风险）' % leader_avg, float(row['close'])))
        else:
            leader_corr = '板块龙头平均%+.1f%%，目标股%+.1f%%，走势基本同步' % (leader_avg, target_pct)

    # 7) 5分钟确认（移除 - 冗余逻辑）

    w = {'强': 2.0, '中': 1.0, '弱': 0.5}
    def score_of(t):
        return sum(w.get(sg['strength'], 0) for sg in signals if sg['type'] == t)

    buy_score = score_of('买入')
    sell_score = score_of('卖出')

    if buy_score >= 3 and buy_score > sell_score:
        advice, confidence = '建议做T买入', '高'
    elif buy_score >= 2 and buy_score > sell_score:
        advice, confidence = '可考虑做T买入', '中'
    elif sell_score >= 3 and sell_score > buy_score:
        advice, confidence = '建议做T卖出', '高'
    elif sell_score >= 2 and sell_score > buy_score:
        advice, confidence = '可考虑做T卖出', '中'
    elif buy_score > 0 and sell_score > 0:
        advice, confidence = '信号矛盾，建议观望', '低'
    else:
        advice, confidence = '信号不足，继续观察', '低'

    # 标准化交易计划
    last_close = float(row['close'])
    atr = float((df['high'] - df['low']).rolling(14).mean().iloc[-1]) if len(df) >= 14 else last_close * 0.01
    if not pd.notna(atr) or atr <= 0:
        atr = last_close * 0.01
    if advice.endswith('买入'):
        entry = round(min(last_close, vwap_last), 2)
        stop = round(min(entry - atr * 1.0, vwap_last * 0.99), 2)
        target = round(vwap_last * 1.005, 2)
    elif advice.endswith('卖出'):
        entry = round(max(last_close, vwap_last), 2)
        stop = round(max(entry + atr * 1.0, vwap_last * 1.01), 2)
        target = round(vwap_last * 0.995, 2)
    else:
        entry = stop = target = round(last_close, 2)
    position = {'高': '3成', '中': '2成', '低': '1成'}[confidence]
    plan = {
        'entry': entry, 'stop': stop, 'target': target,
        'position': position, 'atr': round(atr, 2),
        'note': 'VWAP回归为主目标，破VWAP×0.99止损；做T只用底仓，不新增总仓位'
    }

    # 图表数据：保留最近40根K线（跨日上下文），时间格式统一便于信号定位
    chart = df.tail(40)
    min_data = [{'t': r0['time'], 'o': round(r0['open'], 2), 'c': round(r0['close'], 2),
                 'h': round(r0['high'], 2), 'l': round(r0['low'], 2), 'v': int(r0['volume']),
                 'vwap': round(r0['vwap'], 2) if pd.notna(r0['vwap']) else None}
                for _, r0 in chart.iterrows()]

    return {
        'vwap': round(vwap_last, 2),
        'signals': signals,
        'leaders': leaders or [],
        'leader_correlation': leader_corr,
        'advice': advice,
        'confidence': confidence,
        'min_kline': min_data,
        'period': period,
        'trade_date': last_day,
        'buy_score': round(buy_score, 1),
        'sell_score': round(sell_score, 1),
        'plan': plan,
        'rules': T_RULES
    }

def save_t_strategy(code, name, trade_date, industry, result, concepts=None):
    row = {
        'code': code, 'name': name, 'trade_date': trade_date,
        'vwap': result['vwap'],
        'signals': json.dumps(result['signals'], ensure_ascii=False),
        'industry': industry or '',
        'leaders': json.dumps(result['leaders'], ensure_ascii=False),
        'leader_correlation': result['leader_correlation'],
        'advice': result['advice'],
        'confidence': result['confidence'],
        'min_kline': json.dumps(result['min_kline'], ensure_ascii=False),
        'period': result.get('period', '30'),
        'plan': json.dumps(result.get('plan', {}), ensure_ascii=False),
        'buy_score': result.get('buy_score', 0),
        'sell_score': result.get('sell_score', 0),
        'concepts': json.dumps(concepts or [], ensure_ascii=False),
    }
    try:
        supabase.table('t_strategy').upsert(row, on_conflict='code,trade_date').execute()
    except Exception as e:
        # 新列可能还没建，降级为旧字段写入
        legacy = {k: row[k] for k in ['code', 'name', 'trade_date', 'vwap', 'signals', 'industry',
                                     'leaders', 'leader_correlation', 'advice', 'confidence', 'min_kline']}
        try:
            supabase.table('t_strategy').upsert(legacy, on_conflict='code,trade_date').execute()
        except Exception as e2:
            print(f"  保存做T分析失败 {code}: {e2}")
            return
    # 附加字段（周期/计划/强度分/概念）写 config KV，避免依赖 t_strategy 新增列
    kv_set(f'tstr:{code}', {
        'trade_date': trade_date,
        'name': name,
        'period': result.get('period', '30'),
        'plan': result.get('plan', {}),
        'buy_score': result.get('buy_score', 0),
        'sell_score': result.get('sell_score', 0),
        'advice': result.get('advice'),
        'confidence': result.get('confidence'),
        'vwap': result.get('vwap'),
        'concepts': concepts or [],
    })
    print(f"  做T分析: {name}({code}) → {result['advice']}[{result['confidence']}] 买分{result.get('buy_score')} 卖分{result.get('sell_score')}")

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

    # 补齐追踪股票的概念标签（复盘页要用）
    try:
        codes = list({t['code'] for t in tracking})
        names = {t['code']: get_stock_name(t['code'], t.get('name')) for t in tracking}

        def _fetch(c):
            try:
                cs = get_stock_concepts(c)
                if cs:
                    save_stock_concepts(c, names.get(c, c), cs)
            except Exception:
                pass
        with ThreadPoolExecutor(max_workers=6) as ex:
            list(ex.map(_fetch, codes))
        print(f"  已补齐 {len(codes)} 只追踪股的概念标签")
    except Exception as e:
        print(f"  补齐概念失败: {e}")


# ============ 通用 KV 存储（复用已有的 config 表） ============
# 键值约定：min_bars:<code> / sig_bars:<code> / concepts:<code> / backtest_t / backtest_s
def kv_get(key):
    try:
        resp = supabase.table('config').select('value').eq('key', key).limit(1).execute()
        rows = resp.data or []
        if not rows:
            return None
        v = rows[0].get('value')
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except Exception:
                return None
        return v
    except Exception:
        return None

def kv_set(key, value):
    try:
        supabase.table('config').upsert(
            {'key': key, 'value': json.dumps(value, ensure_ascii=False)},
            on_conflict='key').execute()
        return True
    except Exception as e:
        print(f"  KV写入失败 {key}: {e}")
        return False

# ============ 分钟数据落库（回测用） ============
# 表不存在时跳过相关写入，避免每次运行刷几百行错误
_missing_tables = set()

def table_exists(table):
    """检查表是否存在（结果缓存，避免重复请求）"""
    if table in _missing_tables:
        return False
    try:
        supabase.table(table).select('*').limit(1).execute()
        return True
    except Exception as e:
        msg = str(e)
        if 'PGRST205' in msg or 'does not exist' in msg or 'schema cache' in msg:
            _missing_tables.add(table)
            print(f"⚠️ 表 {table} 不存在，跳过相关写入（请在 Supabase 执行 supabase_schema.sql）")
            return False
        return True

# 只落 30分钟K，且只对自选股+候选股落库；滚动保留 keep_days 天
MIN_KEEP_DAYS = int(os.environ.get("MIN_KEEP_DAYS", "90"))
SIGNAL_WINDOW = 6  # 信号前后各保留几根K

def _bars_with_vwap(df_min):
    """给分钟K补上每日累计VWAP，返回 bar 列表"""
    if df_min is None or df_min.empty:
        return []
    df = df_min.copy()
    df['cum_amount'] = df.groupby('day')['amount'].cumsum()
    df['cum_volume'] = df.groupby('day')['volume'].cumsum()
    df['vwap'] = df['cum_amount'] / (df['cum_volume'] * 100)
    out = []
    for _, r in df.iterrows():
        out.append({
            't': r['time'],
            'd': r['day'],
            'o': round(float(r['open']), 3),
            'h': round(float(r['high']), 3),
            'l': round(float(r['low']), 3),
            'c': round(float(r['close']), 3),
            'v': int(r['volume']),
            'vwap': round(float(r['vwap']), 3) if pd.notna(r['vwap']) else None,
        })
    return out

def save_min_bars(code, df_min, period='30'):
    """分钟K落库到 config KV（键 min_bars:<code>），滚动保留 MIN_KEEP_DAYS 天"""
    bars = _bars_with_vwap(df_min)
    if not bars:
        return 0
    cutoff = (now_cn() - timedelta(days=MIN_KEEP_DAYS)).strftime('%Y-%m-%d')
    bars = [b for b in bars if b['d'] >= cutoff]
    old = kv_get(f'min_bars:{code}') or {}
    merged = {}
    for b in (old.get('bars') or []) + bars:
        merged[b['t']] = b
    kept = sorted(merged.values(), key=lambda x: x['t'])[-800:]
    if kv_set(f'min_bars:{code}', {'period': period, 'bars': kept,
                                   'updated_at': now_cn().strftime('%Y-%m-%d %H:%M:%S')}):
        return len(kept)
    return 0

def prune_min_bars(keep_days=MIN_KEEP_DAYS):
    """分钟K已按日期滚动过滤（save_min_bars 内部），这里无需额外清理"""
    return

def save_signal_snapshots(code, df_min, signals, period='30'):
    """信号窗口K线快照存 config KV（键 sig_bars:<code>）
    历史信号会在后续交易日被再次刷新，补上信号之后的K线，才能算前瞻收益"""
    bars = _bars_with_vwap(df_min)
    if not bars:
        return 0
    idx_by_time = {b['t']: i for i, b in enumerate(bars)}

    old = kv_get(f'sig_bars:{code}') or {}
    existing = {}
    for item in (old.get('signals') or []):
        k = f"{item.get('signal_time')}|{item.get('indicator')}|{item.get('signal_type')}"
        existing[k] = item

    def build(t, indicator, stype, strength, price):
        i = idx_by_time.get(t)
        if i is None:
            return None
        lo = max(0, i - SIGNAL_WINDOW)
        hi = min(len(bars), i + SIGNAL_WINDOW + 1)
        return {
            'signal_time': t,
            'trade_date': bars[i]['d'],
            'indicator': indicator,
            'signal_type': stype,
            'strength': strength,
            'signal_price': price,
            'sig_idx': i - lo,
            'bars': bars[lo:hi],
        }

    for item in existing.values():
        fresh = build(item.get('signal_time'), item.get('indicator', ''), item.get('signal_type', ''),
                      item.get('strength', ''), item.get('signal_price'))
        if fresh:
            existing[f"{fresh['signal_time']}|{fresh['indicator']}|{fresh['signal_type']}"] = fresh

    for sg in signals or []:
        fresh = build(sg.get('time'), sg.get('indicator', ''), sg.get('type', ''),
                      sg.get('strength', ''), sg.get('price'))
        if fresh:
            existing[f"{fresh['signal_time']}|{fresh['indicator']}|{fresh['signal_type']}"] = fresh

    items = sorted(existing.values(), key=lambda x: x.get('signal_time', ''))[-300:]
    if kv_set(f'sig_bars:{code}', {'signals': items,
                                   'updated_at': now_cn().strftime('%Y-%m-%d %H:%M:%S')}):
        return len(items)
    return 0

def run_t_analysis(code, name, trade_date):
    try:
        name = get_stock_name(code, name)
        min_df = fetch_min_kline(code, '30')
        if min_df is None or len(min_df) < 8:
            print(f"  {name}({code}) 30分钟K线不足，跳过做T分析")
            return
        industry = get_stock_industry(code)
        leaders = get_industry_leaders(industry) if industry else []
        concepts = get_stock_concepts(code)
        t_result = calc_t_signals(min_df, leaders, period='30', code=code)
        if t_result:
            save_t_strategy(code, name, trade_date, industry, t_result, concepts)
            save_stock_concepts(code, name, concepts)
            # 分钟K与信号快照落库（供回测）
            save_min_bars(code, min_df, '30')
            save_signal_snapshots(code, min_df, t_result['signals'], '30')
    except Exception as e:
        print(f"  {name}({code}) 做T分析异常: {e}")


def run_t_analysis_batch(stocks, trade_date, max_workers=6, label="股票"):
    """并发做T分析：每只股票需3次网络请求（分钟K/行业/龙头），串行会很慢"""
    if not stocks:
        return
    print(f"\n===== 做T分析 {len(stocks)} 只{label} =====")
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        list(ex.map(lambda s: run_t_analysis(s['code'], s['name'], trade_date), stocks))


def main():
    t_start = time.time()
    def elapsed():
        return f"{time.time() - t_start:.0f}s"

    params = load_config()
    now = now_cn()
    today = now.strftime("%Y-%m-%d")
    load_name_map()

    # --- 自选股分析 ---
    try:
        wl_resp = supabase.table('watchlist').select('*').execute()
        watchlist = wl_resp.data or []
    except Exception as e:
        print(f"读取自选股失败: {e}")
        watchlist = []

    if watchlist:
        print(f"\n===== 分析 {len(watchlist)} 只自选股 =====")
        t_targets = []
        for item in watchlist:
            code = item['code']
            name = get_stock_name(code, item.get('name'))
            df = fetch_and_save_kline(code)
            if df is None or len(df) < 20:
                print(f"  {name}({code}) 数据不足，跳过")
                continue
            result, _ = analyze_stock(df)
            if result:
                save_analysis(code, name, today, result)
                print(f"  {name}({code}): {result['score']}分 [{result['grade']}]")
            t_targets.append({'code': code, 'name': name})
        print(f"  自选股评分耗时 {elapsed()}")
        run_t_analysis_batch(t_targets, today, label="自选股")
        print(f"  自选股做T分析耗时 {elapsed()}")

    # --- 全池选股（并发抓取，串行会浪费 90% 时间在等待网络） ---
    stock_pool = get_dynamic_stock_pool()
    print(f"\n开始抓取 A股数据（共{len(stock_pool)}只，{MAX_WORKERS}线程并发）...")
    candidates = []
    success_count = 0
    latest_data_date = None
    done_count = [0]

    def _screen(item):
        """单只股票的抓取+检测，返回候选/信号，供主线程汇总"""
        code, pool_name = item
        try:
            name = get_stock_name(code, pool_name)
            save_stock_basic(code, name)
            df = fetch_and_save_kline(code)
            if df is None or len(df) < 30:
                return None
            out = {'code': code, 'name': name, 'data_date': str(df.iloc[-1]['trade_date'])}
            pattern = detect_launch_pattern(df, params)
            if pattern:
                score, details = calc_scores(df, pattern)
                if score > params['min_score']:
                    out['candidate'] = {'code': code, 'name': name, 'score': score,
                                        'last_price': pattern['last_price'],
                                        'launch_point': pattern['launch_point'],
                                        'details': details}
            lu = detect_limit_up(df)
            if lu:
                out['limit_up'] = lu
            sr = detect_steady_rise(df)
            if sr:
                out['steady_rise'] = sr
            return out
        except Exception as e:
            print(f"  {code} 处理异常: {e}")
            return None

    failed_items = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        for item, res in zip(stock_pool, ex.map(_screen, stock_pool)):
            done_count[0] += 1
            if not res:
                failed_items.append(item)
                continue
            success_count += 1
            if latest_data_date is None or res['data_date'] > latest_data_date:
                latest_data_date = res['data_date']
            cand = res.get('candidate')
            if cand:
                candidates.append(cand)
                save_candidate(cand['code'], cand['name'], today, cand['score'],
                               cand['last_price'], cand['launch_point'], cand['details'])
                print(f"发现候选: {cand['name']}({cand['code']}) 分数:{cand['score']:.1f}")
            if res.get('limit_up'):
                save_signal(res['code'], res['name'], today, '涨停', res['limit_up'])
                print(f"  涨停: {res['name']}({res['code']}) {res['limit_up']['pct']:.1f}%")
            if res.get('steady_rise'):
                save_signal(res['code'], res['name'], today, '放量上涨', res['steady_rise'])
                print(f"  放量上涨: {res['name']}({res['code']}) 3日{res['steady_rise']['total_pct']:.1f}%")
            if done_count[0] % 50 == 0:
                print(f"进度: {done_count[0]}/{len(stock_pool)}，成功抓取 {success_count} 只")

    # 并发过高可能被行情源限流，失败率高的部分串行重试一次
    if failed_items and success_count < len(stock_pool) * 0.9:
        print(f"\n{len(failed_items)} 只抓取失败，串行重试一次（防限流）...")
        for item in failed_items:
            res = _screen(item)
            if res:
                success_count += 1
                latest_data_date = res['data_date'] if latest_data_date is None else max(latest_data_date, res['data_date'])
                cand = res.get('candidate')
                if cand:
                    candidates.append(cand)
                    save_candidate(cand['code'], cand['name'], today, cand['score'],
                                   cand['last_price'], cand['launch_point'], cand['details'])
                    print(f"发现候选: {cand['name']}({cand['code']}) 分数:{cand['score']:.1f}")
                if res.get('limit_up'):
                    save_signal(res['code'], res['name'], today, '涨停', res['limit_up'])
                if res.get('steady_rise'):
                    save_signal(res['code'], res['name'], today, '放量上涨', res['steady_rise'])
            time.sleep(0.2)

    print(f"全池抓取与检测耗时 {elapsed()}（{success_count}/{len(stock_pool)} 只成功）")
    candidates.sort(key=lambda x: x['score'], reverse=True)
    for rank, c in enumerate(candidates[:20], 1):
        supabase.table('candidates').update({'rank': rank}).eq('code', c['code']).eq('trade_date', today).execute()

    # --- 候选股做T分析（限制前10只加快速度）---
    run_t_analysis_batch(candidates[:10], today, label="候选股")
    print(f"候选股做T分析耗时 {elapsed()}")

    print(f"\n抓取完成，成功 {success_count}/{len(stock_pool)} 只，总耗时 {elapsed()}")
    print(f"选股完成，共发现 {len(candidates)} 只候选股票")
    for c in candidates[:5]:
        print(f"  {c['name']}({c['code']}): {c['score']:.1f}分")

    # 更新历史追踪
    update_tracking()
    print(f"追踪更新耗时 {elapsed()}")

    prune_min_bars()
    print(f"分钟K清理完成（保留{MIN_KEEP_DAYS}天），总耗时 {elapsed()}")

    # --- 数据时效性检查 + 运行状态上报 ---
    data_date = latest_data_date or '未知'
    stale = data_date not in ('未知', today)
    if stale:
        print(f"\n⚠️ 警告：行情数据最新日期为 {data_date}，不是今天({today})")
        print("   可能是收盘后行情尚未发布（建议15:30后再跑）或今天不是交易日")
        print("   本次写入都会被打上今天的日期，但K线内容仍是旧的，请谨慎参考")
    else:
        print(f"\n✅ 数据日期 {data_date}，已是最新")

    try:
        supabase.table('config').upsert({
            'key': 'last_run',
            'value': json.dumps({
                'run_at': now.strftime('%Y-%m-%d %H:%M:%S'),
                'data_date': data_date,
                'today': today,
                'stale': stale,
                'candidates': len(candidates),
                'pool': len(stock_pool),
                'fetched': success_count,
            }, ensure_ascii=False)
        }, on_conflict='key').execute()
    except Exception as e:
        print(f"写入运行状态失败: {e}")

if __name__ == "__main__":
    main()
