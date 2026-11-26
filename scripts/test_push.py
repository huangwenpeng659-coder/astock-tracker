"""推送通道自检：逐个测试 PushPlus / Server酱 / 邮箱是否可用

用法（在仓库根目录）：
  python scripts/test_push.py
需要的环境变量：
  SUPABASE_URL / SUPABASE_SERVICE_KEY  （可选，用于读取自选股）
  PUSHPLUS_TOKEN                       PushPlus token
  SCT_SENDKEY                          Server酱 SendKey
  SMTP_USER / SMTP_PASS / SMTP_TO      邮箱账号与授权码
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import detect_volume as dv


def main():
    print('== 通道配置自检 ==')
    print('PushPlus token :', '已配置' if dv.PUSHPLUS_TOKEN else '未配置')
    print('Server酱 SendKey:', '已配置' if dv.SCT_SENDKEY else '未配置')
    print('邮箱 SMTP       :', '已配置' if (os.getenv('SMTP_USER') and os.getenv('SMTP_PASS')) else '未配置')
    print()

    title = '【自检】A股埋伏追踪推送测试'
    content = ('<div style="padding:12px;background:#f7f8fa;border-radius:8px">'
               '这是一条推送通道测试消息，收到即表示该通道正常。</div>')
    ok = dv.push_all(title, content)
    print()
    print('结果:', '至少一个通道送达' if ok else '全部通道失败')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
