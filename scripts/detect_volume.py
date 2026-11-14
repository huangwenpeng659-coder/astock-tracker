import os
import json
from datetime import datetime
from supabase import create_client
import akshare as ak
import requests

# 初始化
SUPABASE_URL = os.getenv('SUPABASE_URL')
SUPABASE_KEY = os.getenv('SUPABASE_KEY')
PUSHPLUS_TOKEN = os.getenv('PUSHPLUS_TOKEN', 'cef6de3969a64da381519bb3bf87a6fe')

sb = create_client(SUPABASE_URL, SUPABASE_KEY)

def get_watchlist():
    """获取自选股列表"""
    try:
        data = sb.table('watchlist').select('*').execute()
        return data.data or []
    except Exception as e:
        print(f'获取自选股失败: {e}')
        return []

def get_today_kline(code):
    """获取当天日K线数据"""
    try:
        df = ak.stock_zh_a_hist_min_em(symbol=code, period='5')
        if df is None or df.empty:
            return None
        return df
    except Exception as e:
        print(f'获取 {code} 分钟K线失败: {e}')
        return None

def detect_volume_surge(code, name):
    """检测成交量突增"""
    try:
        # 获取当天5分钟K线
        df = get_today_kline(code)
        if df is None or len(df) < 20:
            return None

        # 计算成交量特征
        volumes = df['成交量'].values
        current_vol = volumes[-1]
        avg_vol_20 = volumes[-20:].mean()

        # 放量判断：当前成交量 > 前20根均量的2倍
        if current_vol > avg_vol_20 * 2:
            close = df['收盘'].values[-1]
            close_prev = df['收盘'].values[-2] if len(df) > 1 else close
            pct_change = (close - close_prev) / close_prev * 100 if close_prev != 0 else 0

            signal_type = '放量上涨' if pct_change > 0 else '放量下跌'
            return {
                'code': code,
                'name': name,
                'type': signal_type,
                'current_vol': int(current_vol),
                'avg_vol': int(avg_vol_20),
                'vol_ratio': round(current_vol / avg_vol_20, 2),
                'close': close,
                'pct_change': round(pct_change, 2),
                'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
    except Exception as e:
        print(f'检测 {code} 放量失败: {e}')

    return None

def push_to_pushplus(title, content):
    """通过 PushPlus 推送到微信文件助手"""
    try:
        url = 'https://www.pushplus.plus/send'
        payload = {
            'token': PUSHPLUS_TOKEN,
            'title': title,
            'content': content,
            'template': 'html'
        }
        response = requests.post(url, json=payload, timeout=10)
        result = response.json()

        if result.get('code') == 0:
            print(f'✓ 推送成功: {title}')
            return True
        else:
            print(f'✗ 推送失败: {result.get("msg")}')
            return False
    except Exception as e:
        print(f'推送异常: {e}')
        return False

def format_push_message(signal):
    """格式化推送消息"""
    title = f"🚀 {signal['name']} ({signal['code']}) {signal['type']}"

    content = f"""
    <div style="padding: 12px; background: #f5f5f5; border-radius: 8px;">
        <h3>{signal['name']} ({signal['code']})</h3>
        <p><b>信号类型:</b> {signal['type']}</p>
        <p><b>当前成交量:</b> {signal['current_vol']:,}</p>
        <p><b>20分钟均量:</b> {signal['avg_vol']:,}</p>
        <p><b>放量倍数:</b> {signal['vol_ratio']}x</p>
        <p><b>收盘价:</b> {signal['close']:.2f}</p>
        <p><b>涨跌:</b> <span style="color: {'red' if signal['pct_change'] > 0 else 'green'};">{signal['pct_change']:+.2f}%</span></p>
        <p><b>检测时间:</b> {signal['time']}</p>
        <p style="font-size: 12px; color: #999; margin-top: 12px;">
            💡 日内做T仅供参考，注意风险管理
        </p>
    </div>
    """

    return title, content

def save_signal_to_db(signal):
    """保存信号到数据库"""
    try:
        sb.table('config').insert({
            'key': f'volume_signal:{signal["code"]}:{datetime.now().strftime("%Y%m%d")}',
            'value': json.dumps(signal)
        }).execute()
        print(f'✓ 信号已保存: {signal["code"]}')
    except Exception as e:
        print(f'保存信号失败: {e}')

def check_watchlist():
    """定期检测自选股放量"""
    print(f'[{datetime.now()}] 开始检测自选股放量...')

    watchlist = get_watchlist()
    if not watchlist:
        print('自选股为空，跳过检测')
        return

    signals = []
    for stock in watchlist:
        signal = detect_volume_surge(stock['code'], stock['name'])
        if signal:
            signals.append(signal)
            save_signal_to_db(signal)

            # 推送到微信
            title, content = format_push_message(signal)
            push_to_pushplus(title, content)

    if signals:
        print(f'检测到 {len(signals)} 个放量信号')
    else:
        print('未检测到放量信号')

if __name__ == '__main__':
    check_watchlist()
