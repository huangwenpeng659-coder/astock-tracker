"""自选股盘中放量检测 -> 通过 PushPlus 公众号推送到本人微信"""
import os
import json
import re
from datetime import datetime

import akshare as ak
import pandas as pd
import requests
from supabase import create_client

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo('Asia/Shanghai')
except Exception:
    TZ = None

SUPABASE_URL = os.getenv('SUPABASE_URL')
SUPABASE_KEY = os.getenv('SUPABASE_SERVICE_KEY') or os.getenv('SUPABASE_KEY')
PUSHPLUS_TOKEN = os.getenv('PUSHPLUS_TOKEN', '')
SCT_SENDKEY = os.getenv('SCT_SENDKEY', '')
FORCE_RUN = os.getenv('FORCE_RUN') == '1'

VOL_RATIO = float(os.getenv('VOL_RATIO', '2.0'))
BAR_PERIOD = '5'
HIST_BARS = 20
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/120.0 Safari/537.36')

sb = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None


def now():
    return datetime.now(TZ) if TZ else datetime.now()


def in_trading_hours(dt):
    """含开盘前竞价与收盘后缓冲：GitHub 定时任务常延迟，15:35 前仍按当天数据处理"""
    hm = dt.hour * 60 + dt.minute
    return (9 * 60 + 15 <= hm <= 11 * 60 + 35) or (12 * 60 + 55 <= hm <= 15 * 60 + 35)


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


def _sina_symbol(code):
    c = str(code).zfill(6)
    if c.startswith(('6', '9')):
        return 'sh' + c
    if c.startswith(('4', '8')):
        return 'bj' + c
    return 'sz' + c


def _sina_min_bars(code):
    url = ('https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData'
           f'?symbol={_sina_symbol(code)}&scale={BAR_PERIOD}&ma=no&datalen=200')
    resp = requests.get(url, headers={'User-Agent': UA}, timeout=10)
    resp.raise_for_status()
    rows = resp.json()
    if not rows:
        return None
    df = pd.DataFrame(rows).rename(columns={'day': 'time'})
    for col in ('open', 'high', 'low', 'close', 'volume'):
        df[col] = pd.to_numeric(df[col], errors='coerce')
    df['volume'] = df['volume'] / 100   # 新浪给的是股，统一换成手
    df['date'] = df['time'].astype(str).str[:10]
    return df.dropna(subset=['close', 'volume'])


def _em_min_bars(code):
    df = ak.stock_zh_a_hist_min_em(symbol=code, period=BAR_PERIOD)
    if df is None or df.empty:
        return None
    df = df.rename(columns={'时间': 'time', '开盘': 'open', '最高': 'high',
                            '最低': 'low', '收盘': 'close', '成交量': 'volume'})
    df['date'] = df['time'].astype(str).str[:10]
    for col in ('open', 'high', 'low', 'close', 'volume'):
        df[col] = pd.to_numeric(df[col], errors='coerce')
    return df.dropna(subset=['close', 'volume'])


_NAME_CACHE = {}
_NAME_MAP = None


def load_name_map():
    """A股代码→中文名：akshare 优先，失败用东财全市场列表兜底"""
    global _NAME_MAP
    if _NAME_MAP is not None:
        return _NAME_MAP
    _NAME_MAP = {}
    try:
        df = ak.stock_info_a_code_name()
        _NAME_MAP = {str(r['code']).zfill(6): str(r['name']).strip() for _, r in df.iterrows()}
        print(f'加载中文名映射 {len(_NAME_MAP)} 只（akshare）')
    except Exception as e:
        print(f'akshare 名称表失败({e})，改用东财列表')
        try:
            resp = requests.get('https://push2.eastmoney.com/api/qt/clist/get', timeout=20,
                                 headers={'User-Agent': UA}, params={
                                     'pn': 1, 'pz': 6000, 'po': 0, 'np': 1, 'fltt': 2, 'invt': 2,
                                     'fid': 'f12', 'fields': 'f12,f14',
                                     'fs': 'm:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048',
                                     'ut': 'bd1d9ddb04089700cf9c27f6f7426281'})
            items = (resp.json().get('data') or {}).get('diff') or []
            _NAME_MAP = {str(i['f12']).zfill(6): str(i['f14']).strip()
                         for i in items if i.get('f12') and i.get('f14')}
            print(f'加载中文名映射 {len(_NAME_MAP)} 只（东财）')
        except Exception as e2:
            print(f'东财名称表失败: {e2}')
    return _NAME_MAP


def stock_name(code):
    """自选股里 name 常是代码，这里回查中文名"""
    if code in _NAME_CACHE:
        return _NAME_CACHE[code]
    name = load_name_map().get(code) or code
    if name == code:
        try:
            resp = sb.table('stocks').select('name').eq('code', code).limit(1).execute()
            if resp.data and resp.data[0].get('name'):
                name = resp.data[0]['name']
        except Exception:
            pass
    _NAME_CACHE[code] = name
    return name


def get_intraday_bars(code):
    """取最近交易日当天的 5 分钟 K 线：新浪为主，东财兜底（东财常拦云服务器 IP）"""
    df = None
    for fetch in (_sina_min_bars, _em_min_bars):
        try:
            df = fetch(code)
        except Exception as e:
            print(f'  {code} {fetch.__name__} 失败: {e}')
            df = None
        if df is not None and not df.empty:
            break
    if df is None or df.empty:
        print(f'获取 {code} 分钟K线失败：所有数据源均不可用')
        return None

    last_day = df['date'].iloc[-1]
    today_df = df[df['date'] == last_day].reset_index(drop=True)
    # 15:00 那根是收盘集合竞价，成交量天然是平常的数倍，剔除否则收盘前后必然误报
    if len(today_df) and str(today_df['time'].iloc[-1])[11:16] >= '15:00':
        today_df = today_df.iloc[:-1].reset_index(drop=True)
    return today_df if len(today_df) >= 5 else None


def detect_volume_surge(code, name):
    """返回 (信号, 数据是否成功获取)"""
    df = get_intraday_bars(code)
    if df is None:
        return None, False

    volumes = df['volume'].astype(float)
    closes = df['close'].astype(float)

    current_vol = volumes.iloc[-1]
    history = volumes.iloc[:-1].tail(HIST_BARS)
    if len(history) < 5:
        return None, True

    avg_vol = history.mean()
    if avg_vol <= 0:
        return None, True

    ratio = current_vol / avg_vol
    if ratio < VOL_RATIO:
        return None, True

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
        'bar_time': str(df['time'].iloc[-1]),
        'time': now().strftime('%Y-%m-%d %H:%M:%S'),
    }, True


def push_to_pushplus(title, content):
    if not PUSHPLUS_TOKEN:
        print('未配置 PUSHPLUS_TOKEN，跳过推送（在仓库 Settings → Secrets 里加）')
        return False
    try:
        resp = requests.post(
            'https://www.pushplus.plus/send',
            json={'token': PUSHPLUS_TOKEN, 'title': title, 'content': content, 'template': 'html'},
            timeout=10,
        )
        result = resp.json()
        # PushPlus 成功返回 code=200（旧版是 0），两种都算成功
        if result.get('code') in (0, 200):
            print(f'✓ 推送成功: {title}')
            return True
        print(f'✗ 推送失败(code={result.get("code")}): {result.get("msg") or result}')
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


def push_to_serverchan(title, content):
    """Server酱备用通道（PushPlus 未实名认证时用）"""
    if not SCT_SENDKEY:
        return False
    try:
        resp = requests.post(f'https://sctapi.ftqq.com/{SCT_SENDKEY}.send', timeout=10,
                             data={'title': title, 'desp': content})
        r = resp.json()
        if r.get('code') == 0:
            print('✓ Server酱推送成功')
            return True
        print(f'✗ Server酱推送失败: {r.get("message") or r}')
    except Exception as e:
        print(f'Server酱推送异常: {e}')
    return False


def push_to_email(title, content):
    """邮件备用通道：QQ/163 邮箱 SMTP 授权码即可，无需任何实名认证"""
    user = os.getenv('SMTP_USER', '')
    pwd = os.getenv('SMTP_PASS', '')
    if not user or not pwd:
        return False
    host = os.getenv('SMTP_HOST', 'smtp.qq.com')
    port = int(os.getenv('SMTP_PORT', '465'))
    to = os.getenv('SMTP_TO', user)
    try:
        import smtplib
        from email.mime.text import MIMEText
        msg = MIMEText(re.sub(r'<[^>]+>', ' ', content), 'plain', 'utf-8')
        msg['Subject'] = title
        msg['From'] = user
        msg['To'] = to
        with smtplib.SMTP_SSL(host, port, timeout=15) as s:
            s.login(user, pwd)
            s.sendmail(user, [to], msg.as_string())
        print(f'✓ 邮件推送成功 -> {to}')
        return True
    except Exception as e:
        print(f'✗ 邮件推送失败: {e}')
    return False


def push_all(title, content):
    """按可用性依次尝试各通道，返回是否送达"""
    if push_to_pushplus(title, content):
        return True
    if push_to_serverchan(title, content):
        return True
    if push_to_email(title, content):
        return True
    print('所有推送通道均未送达')
    return False


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


def report_status(ts, checked, signals, failed, skipped=False):
    """每次运行都记录状态，前端可据此判断检测是否真的在跑"""
    try:
        kv_set('volume_last_check', {
            'run_at': ts.strftime('%Y-%m-%d %H:%M:%S'),
            'checked': checked,
            'failed': failed,
            'skipped': skipped,
            'signals': [
                {'code': s['code'], 'name': s['name'], 'type': s['type'],
                 'ratio': s['vol_ratio'], 'pct': s['pct_change']}
                for s in signals
            ],
        })
    except Exception as e:
        print(f'写入检测状态失败: {e}')


def check_watchlist():
    if sb is None:
        print('缺少 SUPABASE_URL / SUPABASE_SERVICE_KEY，退出')
        return

    ts = now()
    print(f'[{ts:%Y-%m-%d %H:%M:%S}] 开始检测自选股放量...')

    if not in_trading_hours(ts) and not FORCE_RUN:
        print('非交易时段，跳过（如需强制运行设置 FORCE_RUN=1）')
        report_status(ts, 0, [], 0, skipped=True)
        return

    watchlist = get_watchlist()
    if not watchlist:
        print('自选股为空，跳过检测')
        report_status(ts, 0, [], 0, skipped=True)
        return

    day = ts.strftime('%Y%m%d')
    fresh = []
    failed = 0
    for stock in watchlist:
        code = str(stock['code']).zfill(6)
        name = stock.get('name') or code
        if name == code:
            name = stock_name(code)
        try:
            signal, ok = detect_volume_surge(code, name)
        except Exception as e:
            print(f'  {code} 检测异常: {e}')
            failed += 1
            continue
        if not ok:
            failed += 1
            continue
        if signal is None:
            continue

        save_signal(signal)

        # 同一只票当天只推一次，避免每 15 分钟刷屏
        push_key = f'volume_push:{code}:{day}'
        if not FORCE_RUN and kv_get(push_key):
            print(f'- {signal["name"]} 今日已推送过，跳过')
            continue

        fresh.append(signal)

    report_status(ts, len(watchlist), fresh, failed)

    if not fresh:
        print(f'未检测到新的放量信号（检查 {len(watchlist)} 只，失败 {failed} 只）')
        return

    print(f'检测到 {len(fresh)} 个放量信号')
    title, content = format_push_message(fresh)
    ok = push_all(title, content)
    if ok:
        # 只有推送成功才标记，避免失败后永远不再重试
        for s in fresh:
            try:
                kv_set(f'volume_push:{s["code"]}:{day}', True)
            except Exception:
                pass
    else:
        print('推送失败，未标记为已推送，下次运行会重试')


if __name__ == '__main__':
    check_watchlist()
