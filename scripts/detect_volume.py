"""全 A 股盘中放量检测 -> 通过可用推送通道发送信号"""
import os
import json
import math
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
HIST_BARS = 20
MIN_HISTORY_BARS = 5
VOLUME_INTERVAL_MINUTES = 30
MIN_SAMPLE_GAP_MINUTES = 20
MAX_SAMPLE_GAP_MINUTES = 45
MIN_MARKET_STOCKS = 5000
MARKET_STATE_KEY = 'market_volume_history'

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


def get_market_snapshot():
    """单次批量获取沪深京 A 股实时行情快照。"""
    df = ak.stock_zh_a_spot_em()
    if df is None or df.empty:
        raise RuntimeError('全市场行情快照为空')
    required = ['代码', '名称', '最新价', '成交量']
    missing = [column for column in required if column not in df.columns]
    if missing:
        raise ValueError(f'全市场行情快照缺少字段: {", ".join(missing)}')

    raw_count = len(df)
    df = df[required].copy()
    df['代码'] = df['代码'].astype(str).str.replace(r'\.0$', '', regex=True).str.zfill(6)
    df['最新价'] = pd.to_numeric(df['最新价'], errors='coerce')
    df['成交量'] = pd.to_numeric(df['成交量'], errors='coerce')
    df = df.dropna(subset=['最新价', '成交量'])
    finite = df['最新价'].map(math.isfinite) & df['成交量'].map(math.isfinite)
    df = df[finite & (df['最新价'] > 0) & (df['成交量'] >= 0)]
    df = df.drop_duplicates(subset=['代码'], keep='last')

    if len(df) < MIN_MARKET_STOCKS:
        raise RuntimeError(
            f'全市场行情快照只有 {len(df)} 只有效股票，低于完整性门槛 {MIN_MARKET_STOCKS}；'
            '为避免将不完整数据误报为全市场结果，本次扫描已停止'
        )

    snapshot = []
    for _, row in df.iterrows():
        code = row['代码']
        name = str(row['名称']).strip()
        if not name or name.lower() == 'nan':
            name = code
        snapshot.append({
            'code': code,
            'name': name,
            'price': float(row['最新价']),
            'volume': float(row['成交量']),
        })
    return snapshot, max(0, raw_count - len(snapshot))


def load_market_state(day):
    response = sb.table('config').select('value').eq(
        'key', MARKET_STATE_KEY
    ).limit(1).execute()
    if not response.data:
        return {'date': day, 'stocks': {}, 'pending': []}

    value = response.data[0]['value']
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError('全市场成交量历史状态格式无效')
    if value.get('date') != day:
        return {'date': day, 'stocks': {}, 'pending': []}
    if not isinstance(value.get('stocks'), dict) or not isinstance(value.get('pending', []), list):
        raise ValueError('全市场成交量历史状态缺少 stocks 或 pending 字段')
    return value


def analyze_market_snapshot(snapshot, previous_state, ts):
    """以相邻批量快照的成交量增量，计算 30 分钟等效量比。"""
    day = ts.strftime('%Y%m%d')
    previous_stocks = previous_state.get('stocks', {}) if previous_state.get('date') == day else {}
    next_stocks = {}
    signals = []
    timestamp = ts.isoformat()

    for stock in snapshot:
        code = stock['code']
        volume = stock['volume']
        price = stock['price']
        previous = previous_stocks.get(code)
        history = list(previous.get('history', [])) if previous else []

        if previous:
            previous_at = datetime.fromisoformat(previous['at'])
            elapsed = (ts - previous_at).total_seconds() / 60
            previous_volume = float(previous['volume'])
            previous_price = float(previous['price'])

            if 0 < elapsed < MIN_SAMPLE_GAP_MINUTES:
                next_stocks[code] = previous
                continue

            if MIN_SAMPLE_GAP_MINUTES <= elapsed <= MAX_SAMPLE_GAP_MINUTES:
                delta_volume = volume - previous_volume
                if delta_volume < 0:
                    history = []
                else:
                    volume_30m = delta_volume * VOLUME_INTERVAL_MINUTES / elapsed
                    prior_samples = history[-HIST_BARS:]
                    avg_volume = sum(prior_samples) / len(prior_samples) if prior_samples else 0
                    ratio = volume_30m / avg_volume if avg_volume > 0 else 0

                    if len(prior_samples) >= MIN_HISTORY_BARS and avg_volume > 0 and ratio >= VOL_RATIO:
                        pct = (price - previous_price) / previous_price * 100 if previous_price else 0
                        signals.append({
                            'code': code,
                            'name': stock['name'],
                            'type': '放量上涨' if pct > 0 else '放量下跌',
                            'current_vol': int(delta_volume),
                            'avg_vol': int(avg_volume),
                            'vol_ratio': round(ratio, 2),
                            'close': round(price, 2),
                            'pct_change': round(pct, 2),
                            'bar_time': timestamp,
                            'time': ts.strftime('%Y-%m-%d %H:%M:%S'),
                            'interval_minutes': round(elapsed, 1),
                        })
                    history = (history + [volume_30m])[-HIST_BARS:]

        next_stocks[code] = {
            'volume': volume,
            'price': price,
            'at': timestamp,
            'history': history,
        }

    return signals, {
        'date': day,
        'updated_at': timestamp,
        'stocks': next_stocks,
        'pending': previous_state.get('pending', []) if previous_state.get('date') == day else [],
    }


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
    interval = s.get('interval_minutes')
    volume_label = f'本次区间成交量（{interval:g} 分钟）' if interval else '当前成交量'
    average_label = f'前{HIST_BARS}次均量（折算30分钟）' if interval else f'{HIST_BARS}根均量'
    change_label = f'{interval:g}分钟涨跌' if interval else '相对前一根5分钟K线涨跌'
    return f"""
    <div style="padding:12px;background:#f7f8fa;border-radius:8px;margin-bottom:10px">
      <div style="font-size:16px;font-weight:700">{s['name']} <span style="color:#888;font-size:13px">{s['code']}</span></div>
      <div style="color:{color};font-weight:700;margin-top:4px">{s['type']} · {change_label} {s['pct_change']:+.2f}%</div>
      <div style="margin-top:8px;font-size:13px;line-height:1.8">
        放量倍数：<b style="color:{color}">{s['vol_ratio']}x</b><br>
        {volume_label}：{s['current_vol']:,} 手<br>
        {average_label}：{s['avg_vol']:,} 手<br>
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
        title = f"🚀 {len(signals)} 只全市场股票放量"
    body = ''.join(build_card(s) for s in signals)
    content = body + '<p style="font-size:12px;color:#999">日内放量提示仅供参考，注意仓位与止损</p>'
    return title, content


def save_signals(signals, day):
    if not signals:
        return
    rows = [
        {
            'key': f'volume_signal:{signal["code"]}:{day}',
            'value': json.dumps(signal, ensure_ascii=False),
        }
        for signal in signals
    ]
    try:
        sb.table('config').upsert(rows, on_conflict='key').execute()
    except Exception as e:
        print(f'批量保存放量信号失败: {e}')
        raise


def load_pushed_codes(day, codes):
    pushed = set()
    keys = [f'volume_push:{code}:{day}' for code in codes]
    for offset in range(0, len(keys), 500):
        response = sb.table('config').select('key').in_(
            'key', keys[offset:offset + 500]
        ).execute()
        pushed.update(
            row['key'].split(':')[1]
            for row in response.data or []
            if row.get('key')
        )
    return pushed


def mark_signals_pushed(signals, day):
    if not signals:
        return
    rows = [
        {
            'key': f'volume_push:{signal["code"]}:{day}',
            'value': json.dumps(True),
        }
        for signal in signals
    ]
    sb.table('config').upsert(rows, on_conflict='key').execute()


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


def check_market():
    if sb is None:
        print('缺少 SUPABASE_URL / SUPABASE_SERVICE_KEY，退出')
        raise RuntimeError('全市场放量检测需要 Supabase 配置')

    ts = now()
    print(f'[{ts:%Y-%m-%d %H:%M:%S}] 开始批量检测全 A 股放量...')

    if not in_trading_hours(ts) and not FORCE_RUN:
        print('非交易时段，跳过（如需强制运行设置 FORCE_RUN=1）')
        report_status(ts, 0, [], 0, skipped=True)
        return

    day = ts.strftime('%Y%m%d')
    try:
        snapshot, invalid_count = get_market_snapshot()
        previous_state = load_market_state(day)
    except Exception:
        report_status(ts, 0, [], 1)
        raise

    print(f'行情快照有效 {len(snapshot)} 只，过滤无效/重复 {invalid_count} 条')
    signals, next_state = analyze_market_snapshot(snapshot, previous_state, ts)
    print(f'本次区间命中 {len(signals)} 只')
    save_signals(signals, day)

    pending_by_code = {
        signal['code']: signal
        for signal in previous_state.get('pending', [])
        if signal.get('code')
    }
    pending_by_code.update({signal['code']: signal for signal in signals})
    candidates = list(pending_by_code.values())
    pushed = load_pushed_codes(day, [signal['code'] for signal in candidates]) if candidates and not FORCE_RUN else set()
    fresh = [
        signal for signal in candidates
        if FORCE_RUN or signal['code'] not in pushed
    ]

    next_state['pending'] = fresh
    kv_set(MARKET_STATE_KEY, next_state)
    report_status(ts, len(snapshot), fresh, invalid_count)

    if not fresh:
        print(f'未检测到新的全市场放量信号（扫描 {len(snapshot)} 只，过滤 {invalid_count} 条）')
        return

    print(f'检测到 {len(fresh)} 个待推送全市场放量信号')
    title, content = format_push_message(fresh)
    ok = push_all(title, content)
    if ok:
        mark_signals_pushed(fresh, day)
        next_state['pending'] = []
        kv_set(MARKET_STATE_KEY, next_state)
    else:
        print('推送失败，信号已保留为待重试状态')


if __name__ == '__main__':
    check_market()
