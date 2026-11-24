"""自选股盘中放量检测 -> PushPlus 推送微信文件助手"""
import os
import json
from datetime import datetime

import akshare as ak
import requests
from supabase import create_client

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo('Asia/Shanghai')
except Exception:
    TZ = None

SUPABASE_URL = os.getenv('SUPABASE_URL')
SUPABASE_KEY = os.getenv('SUPABASE_SERVICE_KEY') or os.getenv('SUPABASE_KEY')
PUSHPLUS_TOKEN = os.getenv('PUSHPLUS_TOKEN', 'cef6de3969a64da381519bb3bf87a6fe')
FORCE_RUN = os.getenv('FORCE_RUN') == '1'

VOL_RATIO = float(os.getenv('VOL_RATIO', '2.0'))
BAR_PERIOD = '5'
HIST_BARS = 20

sb = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None


def now():
    return datetime.now(TZ) if TZ else datetime.now()


def in_trading_hours(dt):
    hm = dt.hour * 60 + dt.minute
    return (9 * 60 + 30 <= hm <= 11 * 60 + 30) or (13 * 60 <= hm <= 15 * 60)


def kv_get(key):
    try:
        resp = sb.table('config').select('value').eq('key', key).limit(1).execute()
        if not resp.data:
            return None
        v = resp.data[0]['value']
        return json.loads(v) if isinstance(v, str) else v
    except Exception as e:
        print(f'读取 {key} 失败: {e}')
        return None


def kv_set(key, value):
    sb.table('config').upsert(
        {'key': key, 'value': json.dumps(value, ensure_ascii=False)},
        on_conflict='key'
    ).execute()


def get_watchlist():
    try:
        data = sb.table('watchlist').select('code,name').execute()
        return [r for r in (data.data or []) if r.get('code')]
    except Exception as e:
        print(f'获取自选股失败: {e}')
        return []


def get_intraday_bars(code):
    """取当日 5 分钟 K 线（akshare 返回多日，需按日期截断）"""
    try:
        df = ak.stock_zh_a_hist_min_em(symbol=code, period=BAR_PERIOD)
    except Exception as e:
        print(f'获取 {code} 分钟K线失败: {e}')
        return None
    if df is None or df.empty:
        return None

    df['时间'] = df['时间'].astype(str).str.slice(0, 10)
    last_day = df['时间'].iloc[-1]
    today_df = df[df['时间'] == last_day].reset_index(drop=True)
    return today_df if len(today_df) >= 5 else None


def detect_volume_surge(code, name):
    df = get_intraday_bars(code)
    if df is None:
        return None

    volumes = df['成交量'].astype(float)
    closes = df['收盘'].astype(float)

    current_vol = volumes.iloc[-1]
    history = volumes.iloc[:-1].tail(HIST_BARS)
    if len(history) < 5:
        return None

    avg_vol = history.mean()
    if avg_vol <= 0:
        return None

    ratio = current_vol / avg_vol
    if ratio < VOL_RATIO:
        return None

    close = closes.iloc[-1]
    prev_close = closes.iloc[-2]
    pct = (close - prev_close) / prev_close * 100 if prev_close else 0

    return {
        'code': code,
        'name': name or code,
        'type': '放量上涨' if pct > 0 else '放量下跌',
        'current_vol': int(current_vol),
        'avg_vol': int(avg_vol),
        'vol_ratio': round(ratio, 2),
        'close': round(float(close), 2),
        'pct_change': round(float(pct), 2),
        'bar_time': str(df['时间'].iloc[-1]) if '时间' in df else '',
        'time': now().strftime('%Y-%m-%d %H:%M:%S'),
    }


def push_to_pushplus(title, content):
    try:
        resp = requests.post(
            'https://www.pushplus.plus/send',
            json={'token': PUSHPLUS_TOKEN, 'title': title, 'content': content, 'template': 'html'},
            timeout=10,
        )
        result = resp.json()
        if result.get('code') == 0:
            print(f'✓ 推送成功: {title}')
            return True
        print(f'✗ 推送失败: {result.get("msg")}')
    except Exception as e:
        print(f'推送异常: {e}')
    return False


def build_card(s):
    color = '#ef4444' if s['pct_change'] > 0 else '#22c55e'
    return f"""
    <div style="padding:12px;background:#f7f8fa;border-radius:8px;margin-bottom:10px">
      <div style="font-size:16px;font-weight:700">{s['name']} <span style="color:#888;font-size:13px">{s['code']}</span></div>
      <div style="color:{color};font-weight:700;margin-top:4px">{s['type']} {s['pct_change']:+.2f}%</div>
      <div style="margin-top:8px;font-size:13px;line-height:1.8">
        放量倍数：<b style="color:{color}">{s['vol_ratio']}x</b><br>
        当前成交量：{s['current_vol']:,} 手<br>
        {HIST_BARS}根均量：{s['avg_vol']:,} 手<br>
        现价：{s['close']}<br>
        检测时间：{s['time']}
      </div>
    </div>"""


def format_push_message(signals):
    if len(signals) == 1:
        title = f"🚀 {signals[0]['name']} {signals[0]['type']} {signals[0]['vol_ratio']}x"
    else:
        title = f"🚀 {len(signals)} 只自选股放量"
    body = ''.join(build_card(s) for s in signals)
    content = body + '<p style="font-size:12px;color:#999">日内放量提示仅供参考，注意仓位与止损</p>'
    return title, content


def save_signal(signal):
    day = now().strftime('%Y%m%d')
    try:
        kv_set(f'volume_signal:{signal["code"]}:{day}', signal)
    except Exception as e:
        print(f'保存信号失败: {e}')


def check_watchlist():
    if sb is None:
        print('缺少 SUPABASE_URL / SUPABASE_SERVICE_KEY，退出')
        return

    ts = now()
    print(f'[{ts:%Y-%m-%d %H:%M:%S}] 开始检测自选股放量...')

    if not in_trading_hours(ts) and not FORCE_RUN:
        print('非交易时段，跳过（如需强制运行设置 FORCE_RUN=1）')
        return

    watchlist = get_watchlist()
    if not watchlist:
        print('自选股为空，跳过检测')
        return

    day = ts.strftime('%Y%m%d')
    fresh = []
    for stock in watchlist:
        code = str(stock['code']).zfill(6)
        signal = detect_volume_surge(code, stock.get('name'))
        if not signal:
            continue

        save_signal(signal)

        # 同一只票当天只推一次，避免每 15 分钟刷屏
        push_key = f'volume_push:{code}:{day}'
        if not FORCE_RUN and kv_get(push_key):
            print(f'- {signal["name"]} 今日已推送过，跳过')
            continue

        fresh.append(signal)
        try:
            kv_set(push_key, True)
        except Exception:
            pass

    if not fresh:
        print('未检测到新的放量信号')
        return

    print(f'检测到 {len(fresh)} 个放量信号')
    title, content = format_push_message(fresh)
    push_to_pushplus(title, content)


if __name__ == '__main__':
    check_watchlist()
