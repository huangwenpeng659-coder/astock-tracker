"""Fetch and store a full-market daily limit-up recap."""
import json
import math
import os
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import akshare as ak
import pandas as pd

if __package__:
    from .concept_data import get_stock_concepts, load_concept_boards
else:
    from concept_data import get_stock_concepts, load_concept_boards

try:
    from zoneinfo import ZoneInfo
    CN_TZ = ZoneInfo("Asia/Shanghai")
except Exception:
    CN_TZ = timezone(timedelta(hours=8))

CONFIG_KEY = "limitup_recap:latest"
MIN_CORE_CONCEPT_LIMITUPS = 2
LIMITUP_COLUMNS = {
    "代码", "名称", "连板数", "涨停统计", "首次封板时间",
    "封板资金", "成交额", "换手率", "流通市值", "所属行业",
}


def _number(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if math.isfinite(result) else 0.0


def _text(value, fallback=""):
    if value is None or pd.isna(value):
        return fallback
    result = str(value).strip()
    return result if result and result.lower() != "nan" else fallback


def _code(value):
    result = re.sub(r"\.0$", "", str(value).strip())
    return result.zfill(6) if result.isdigit() else ""


def _format_time(value):
    raw = _text(value)
    if not raw:
        return "--:--"
    if ":" in raw:
        return raw[:5]
    if re.fullmatch(r"\d+\.0", raw):
        raw = raw[:-2]
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return "--:--"
    digits = digits.zfill(6)[-6:]
    return f"{digits[:2]}:{digits[2:4]}"


def latest_trade_date(calendar, today):
    if calendar is None or calendar.empty or "trade_date" not in calendar.columns:
        raise ValueError("交易日历缺少 trade_date，无法确定复盘日期")
    parsed = pd.to_datetime(calendar["trade_date"], errors="coerce").dropna()
    eligible = [value.date() for value in parsed if value.date() <= today]
    if not eligible:
        raise ValueError(f"交易日历中没有不晚于 {today} 的交易日")
    return max(eligible).strftime("%Y%m%d")


def resolve_trade_date(today=None, override=None):
    if override:
        try:
            return datetime.strptime(override, "%Y%m%d").strftime("%Y%m%d")
        except ValueError as exc:
            raise ValueError("RECAP_DATE 必须使用 YYYYMMDD 格式") from exc
    today = today or datetime.now(CN_TZ).date()
    return latest_trade_date(ak.tool_trade_date_hist_sina(), today)


def _normalise_limitups(frame):
    if frame is None or frame.empty:
        raise ValueError("涨停池为空；不会覆盖已有复盘数据")
    missing = LIMITUP_COLUMNS.difference(frame.columns)
    if missing:
        raise ValueError(f"涨停池缺少字段: {', '.join(sorted(missing))}")

    stocks = {}
    for _, row in frame.iterrows():
        code = _code(row["代码"])
        if not code:
            continue
        industry = _text(row["所属行业"], "未分类")
        seal_fund = _number(row["封板资金"])
        amount = _number(row["成交额"])
        streak = max(1, int(_number(row["连板数"])))
        stocks[code] = {
            "code": code,
            "name": _text(row["名称"], code),
            "streak": streak,
            "board_label": "首板" if streak == 1 else f"{streak}连板",
            "board_stat": _text(row["涨停统计"], "--"),
            "first_seal_time": _format_time(row["首次封板时间"]),
            "seal_fund_yi": round(seal_fund / 100_000_000, 2),
            "seal_to_amount": round(seal_fund / amount, 2) if amount > 0 else None,
            "turnover_pct": round(_number(row["换手率"]), 2),
            "float_cap_yi": round(_number(row["流通市值"]) / 100_000_000, 2),
            "industry": industry,
        }
    if not stocks:
        raise ValueError("涨停池没有有效股票代码；不会覆盖已有复盘数据")
    return list(stocks.values())


def _broken_codes(frame):
    if frame is None or frame.empty:
        return set()
    if "代码" not in frame.columns:
        raise ValueError("炸板池缺少代码字段")
    return {code for code in (_code(value) for value in frame["代码"]) if code}


def fetch_market_concepts(codes, max_workers=6):
    load_concept_boards(strict=True, tries=2, timeout=10)

    def fetch_one(code):
        try:
            return code, get_stock_concepts(
                code,
                strict=True,
                tries=2,
                timeout=10,
            )
        except Exception as exc:
            raise RuntimeError(f"获取 {code} 的概念标签失败: {exc}") from exc

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        return dict(executor.map(fetch_one, codes))


def _apply_concepts(stocks, concepts_by_code):
    names_by_code = {}
    for stock in stocks:
        names = []
        for value in concepts_by_code.get(stock["code"], []):
            name = _text(value)
            if name and name not in names:
                names.append(name)
        names_by_code[stock["code"]] = names

    limitup_counts = Counter(
        concept
        for names in names_by_code.values()
        for concept in names
    )
    for stock in stocks:
        names = names_by_code[stock["code"]]
        peak_count = max((limitup_counts[name] for name in names), default=0)
        stock["concepts"] = [
            {
                "name": name,
                "limitup_count": limitup_counts[name],
                "is_core": (
                    limitup_counts[name] == peak_count
                    and peak_count >= MIN_CORE_CONCEPT_LIMITUPS
                ),
            }
            for name in sorted(names, key=lambda item: (-limitup_counts[item], item))
        ]


def build_recap(limitups, broken, trade_date, updated_at, concepts_by_code=None):
    stocks = _normalise_limitups(limitups)
    _apply_concepts(stocks, concepts_by_code or {})
    limitup_codes = {stock["code"] for stock in stocks}
    broken_codes = _broken_codes(broken) - limitup_codes
    groups = {}
    for stock in stocks:
        groups.setdefault(stock["industry"], []).append(stock)

    group_items = []
    for industry, items in groups.items():
        items.sort(key=lambda stock: (
            -stock["streak"],
            stock["first_seal_time"],
            stock["code"],
        ))
        group_items.append({
            "industry": industry,
            "count": len(items),
            "seal_fund_total_yi": round(sum(item["seal_fund_yi"] for item in items), 2),
            "items": items,
        })
    group_items.sort(key=lambda group: (-group["count"], group["industry"]))

    first_board_count = sum(stock["streak"] == 1 for stock in stocks)
    non_first_count = len(stocks) - first_board_count
    broken_count = len(broken_codes)
    denominator = len(stocks) + broken_count

    return {
        "schema_version": 2,
        "trade_date": trade_date,
        "updated_at": updated_at,
        "summary": {
            "limitup_count": len(stocks),
            "first_board_count": first_board_count,
            "non_first_count": non_first_count,
            "max_streak": max(stock["streak"] for stock in stocks),
            "industry_count": len(group_items),
            "broken_count": broken_count,
            "break_rate_pct": round(broken_count / denominator * 100, 1) if denominator else 0,
            "seal_fund_total_yi": round(sum(stock["seal_fund_yi"] for stock in stocks), 2),
        },
        "groups": group_items,
        "reason_note": (
            "大类按行情源所属行业归类。核心概念按东财概念归属中当日涨停股聚集度最高的标签标注"
            "（至少2只）；仅表示题材关联，不代表公告或新闻已核实的直接涨停原因。"
        ),
    }


def main():
    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_SERVICE_KEY")
    if not supabase_url or not supabase_key:
        raise RuntimeError("缺少 SUPABASE_URL / SUPABASE_SERVICE_KEY")

    trade_date = resolve_trade_date(override=os.getenv("RECAP_DATE"))
    print(f"获取 {trade_date} 全市场涨停与炸板数据...")
    limitups = ak.stock_zt_pool_em(date=trade_date)
    broken = ak.stock_zt_pool_zbgc_em(date=trade_date)
    stocks = _normalise_limitups(limitups)
    print(f"获取 {len(stocks)} 只涨停股的东财概念标签...")
    concepts_by_code = fetch_market_concepts([stock["code"] for stock in stocks])
    recap = build_recap(
        limitups,
        broken,
        f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}",
        datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        concepts_by_code=concepts_by_code,
    )

    from supabase import create_client
    client = create_client(supabase_url, supabase_key)
    client.table("config").upsert(
        {
            "key": CONFIG_KEY,
            "value": json.dumps(recap, ensure_ascii=False),
        },
        on_conflict="key",
    ).execute()

    summary = recap["summary"]
    print(
        f"复盘已保存：{recap['trade_date']}，封板 {summary['limitup_count']} 只，"
        f"非首板 {summary['non_first_count']} 只，行业分组 {summary['industry_count']} 条，"
        f"炸板率 {summary['break_rate_pct']}%"
    )


if __name__ == "__main__":
    main()
