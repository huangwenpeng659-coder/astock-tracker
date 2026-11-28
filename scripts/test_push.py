"""推送通道自检：逐个测试 PushPlus / Server酱 / 邮箱是否可用

用法（在仓库根目录）：
  python scripts/test_push.py
需要的环境变量：
  SUPABASE_URL / SUPABASE_SERVICE_KEY  （历史回放必需；普通自检可选）
  PUSHPLUS_TOKEN                       PushPlus token
  SCT_SENDKEY                          Server酱 SendKey
  SMTP_USER / SMTP_PASS / SMTP_TO      邮箱账号与授权码
  REPLAY_DATE                          YYYY-MM-DD 或 yesterday；回放已保存的历史放量信号
"""
import os
import sys
import json
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import detect_volume as dv


def load_replay_signals(date_key):
    if dv.sb is None:
        raise RuntimeError('历史信号回放需要配置 SUPABASE_URL 和 SUPABASE_SERVICE_KEY')

    response = dv.sb.table('config').select('key,value').like(
        'key', f'volume_signal:%:{date_key}'
    ).execute()
    signals = []
    for row in response.data or []:
        value = row.get('value')
        if isinstance(value, str):
            value = json.loads(value)
        if isinstance(value, dict):
            signals.append(value)
    return sorted(signals, key=lambda signal: (signal.get('time', ''), signal.get('code', '')))


def build_replay_message(replay_date):
    date_key = replay_date.replace('-', '')
    signals = load_replay_signals(date_key)
    if not signals:
        title = f'【历史回放 {replay_date}】未找到已保存的放量信号'
        content = (
            f'<div style="padding:12px;background:#f7f8fa;border-radius:8px">'
            f'数据库中没有找到 {replay_date} 的放量信号记录。'
            '这可能表示当天未运行检测或没有命中，不代表确认没有信号。</div>'
        )
        return title, content

    title, content = dv.format_push_message(signals)
    title = f'【历史回放 {replay_date}】{title}'
    content = (
        f'<p style="color:#b45309;font-weight:700">历史数据回放，仅用于验证推送样式，不是实时信号。</p>'
        + content
    )
    return title, content


def main():
    print('== 通道配置自检 ==')
    print('PushPlus token :', '已配置' if dv.PUSHPLUS_TOKEN else '未配置')
    print('Server酱 SendKey:', '已配置' if dv.SCT_SENDKEY else '未配置')
    print('邮箱 SMTP       :', '已配置' if (os.getenv('SMTP_USER') and os.getenv('SMTP_PASS')) else '未配置')
    print()

    replay_date = os.getenv('REPLAY_DATE', '').strip()
    if replay_date:
        if replay_date.lower() == 'yesterday':
            replay_date = (dv.now().date() - timedelta(days=1)).isoformat()
        else:
            try:
                replay_date = datetime.strptime(replay_date, '%Y-%m-%d').date().isoformat()
            except ValueError:
                print('REPLAY_DATE 格式错误，应为 YYYY-MM-DD 或 yesterday')
                return 2
        print(f'历史信号回放日期: {replay_date}')
        title, content = build_replay_message(replay_date)
    else:
        title = '【自检】A股埋伏追踪推送测试'
        content = ('<div style="padding:12px;background:#f7f8fa;border-radius:8px">'
                   '这是一条推送通道测试消息，收到即表示该通道正常。</div>')

    ok = dv.push_all(title, content)
    print()
    print('结果:', '至少一个通道送达' if ok else '全部通道失败')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
