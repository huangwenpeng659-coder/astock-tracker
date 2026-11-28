"""Poll intraday limit-up events and rank related concept stocks."""
import json
import math
import os
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone

import akshare as ak
import pandas as pd

if __package__:
    from .concept_data import get_stock_concepts
else:
    from concept_data import get_stock_concepts

try:
    from zoneinfo import ZoneInfo
    CN_TZ = ZoneInfo("Asia/Shanghai")
except Exception:
    CN_TZ = timezone(timedelta(hours=8))

LATEST_KEY = "limitup_radar:latest"
STATE_KEY = "limitup_radar:state"
TREND_KEY_PREFIX = "limitup_radar:trend:"
MAX_ACTIVE_THEMES = 6
MAX_TREND_FETCH_PER_RUN = 80
MAX_CONCEPT_CANDIDATES = 10
MIN_HISTORY_BARS = 25
MAX_QUOTE_GAP_MINUTES = 12


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
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return "--:--"
    digits = digits.zfill(6)[-6:]
    return f"{digits[:2]}:{digits[2:4]}"


def _decode_json(value, fallback):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Supabase JSON 配置格式无效: {exc}") from exc
    return value if value is not None else fallback


def _is_market_session(now):
    minute = now.hour * 60 + now.minute
    return 9 * 60 + 30 <= minute <= 11 * 60 + 30 or 13 * 60 <= minute <= 15 * 60


def _trade_dates(calendar):
    if calendar is None or calendar.empty or "trade_date" not in calendar.columns:
        raise ValueError("交易日历缺少 trade_date")
    parsed = pd.to_datetime(calendar["trade_date"], errors="coerce").dropna()
    return sorted({value.date() for value in parsed})


def _normalize_pool(frame):
    required = {
        "代码", "名称", "连板数", "首次封板时间", "所属行业",
    }
    if frame is None:
        raise ValueError("涨停池接口没有返回数据")
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"涨停池缺少字段: {', '.join(sorted(missing))}")

    stocks = {}
    for _, row in frame.iterrows():
        code = _code(row["代码"])
        if not code:
            continue
        streak = max(1, int(_number(row["连板数"])))
        stocks[code] = {
            "code": code,
            "name": _text(row["名称"], code),
            "industry": _text(row["所属行业"], "未分类"),
            "streak": streak,
            "board_label": "首板" if streak == 1 else f"{streak}连板",
            "first_seal_time": _format_time(row["首次封板时间"]),
        }
    return list(stocks.values())


def _broken_codes(frame):
    if frame is None or frame.empty:
        return set()
    if "代码" not in frame.columns:
        raise ValueError("炸板池缺少代码字段")
    return {code for code in (_code(value) for value in frame["代码"]) if code}


def _kv_get(client, key):
    response = client.table("config").select("value").eq("key", key).limit(1).execute()
    if not response.data:
        return None
    return _decode_json(response.data[0].get("value"), None)


def _kv_set(client, key, value):
    client.table("config").upsert(
        {"key": key, "value": json.dumps(value, ensure_ascii=False)},
        on_conflict="key",
    ).execute()


def _load_state(client, trade_date):
    state = _kv_get(client, STATE_KEY)
    if state is None:
        return {
            "trade_date": trade_date.isoformat(),
            "events": {},
            "quote_history": {},
            "initialized": False,
        }
    if not isinstance(state, dict):
        raise ValueError("涨停雷达状态格式无效")
    if state.get("trade_date") != trade_date.isoformat():
        return {
            "trade_date": trade_date.isoformat(),
            "events": {},
            "quote_history": {},
            "initialized": False,
        }
    if not isinstance(state.get("events"), dict) or not isinstance(state.get("quote_history"), dict):
        raise ValueError("涨停雷达状态缺少 events 或 quote_history")
    return state


def _fetch_concepts(codes):
    found = {}
    errors = {}
    if not codes:
        return found, errors

    def fetch_one(code):
        return code, get_stock_concepts(code, strict=True, tries=1, timeout=8)

    with ThreadPoolExecutor(max_workers=10) as executor:
        pending = {executor.submit(fetch_one, code): code for code in codes}
        for future in as_completed(pending):
            code = pending[future]
            try:
                fetched_code, concepts = future.result()
                found[fetched_code] = concepts
            except Exception as exc:
                errors[code] = str(exc)
                print(f"获取 {code} 概念标签失败: {exc}")
    return found, errors


def _update_events(pool, broken_codes, state, now):
    events = state["events"]
    initialized = bool(state.get("initialized"))
    previous_active = {
        code for code, event in events.items() if event.get("status") == "封板"
    }
    active_codes = {stock["code"] for stock in pool}
    new_codes = active_codes - previous_active if initialized else set()

    for code, event in events.items():
        if code not in active_codes:
            if code in broken_codes:
                event["status"] = "开板"
            elif event.get("status") == "封板":
                event["status"] = "离开涨停池"

    missing_concepts = [
        stock["code"]
        for stock in pool
        if not events.get(stock["code"], {}).get("concept_fetch_done")
    ]
    fetched, errors = _fetch_concepts(missing_concepts)

    for stock in pool:
        code = stock["code"]
        event = events.get(code, {})
        was_seen = bool(event)
        was_active = event.get("status") == "封板"
        event.update(stock)
        event["status"] = "封板"
        event["last_seen_at"] = now.isoformat()
        if not was_seen:
            event["first_seen_at"] = now.isoformat()
            event["seal_cycles"] = 1
        elif not was_active and code in new_codes:
            event["seal_cycles"] = int(event.get("seal_cycles", 1)) + 1

        if code in fetched:
            event["concepts"] = fetched[code]
            event["concept_fetch_done"] = True
            event.pop("concept_error", None)
        elif code in errors:
            event["concept_error"] = errors[code]
            event["concept_fetch_done"] = False
        events[code] = event

    state["events"] = events
    state["initialized"] = True
    current_events = [events[code] for code in active_codes]
    current_events.sort(key=lambda event: (event["first_seal_time"], event["code"]))
    recent_new = [
        event for event in current_events
        if event["code"] in new_codes
    ]
    return current_events, recent_new, new_codes, errors


def _fetch_theme_stats():
    frame = ak.stock_board_concept_name_em()
    required = {"板块名称", "涨跌幅"}
    if frame is None or frame.empty or not required.issubset(frame.columns):
        raise ValueError("概念板块行情缺少板块名称或涨跌幅")
    stats = {}
    for _, row in frame.iterrows():
        name = _text(row["板块名称"])
        if name:
            stats[name] = {
                "change_pct": round(_number(row["涨跌幅"]), 2),
                "up_count": int(_number(row.get("上涨家数", 0))),
            }
    return stats


def _select_active_themes(events, theme_stats, limit=MAX_ACTIVE_THEMES):
    counts = Counter(
        concept
        for event in events
        for concept in event.get("concepts", [])
    )
    ranked = sorted(
        counts,
        key=lambda name: (
            -counts[name],
            -theme_stats.get(name, {}).get("change_pct", 0),
            name,
        ),
    )[:limit]
    return [
        {
            "name": name,
            "trigger_count": counts[name],
            "change_pct": theme_stats.get(name, {}).get("change_pct", 0),
            "up_count": theme_stats.get(name, {}).get("up_count", 0),
        }
        for name in ranked
    ]


def _normalize_theme_members(frame, theme):
    required = {"代码", "名称", "最新价", "涨跌幅", "成交量", "成交额", "换手率"}
    if frame is None or frame.empty or not required.issubset(frame.columns):
        raise ValueError(f"{theme} 成分股接口缺少行情字段")

    members = {}
    for _, row in frame.iterrows():
        code = _code(row["代码"])
        price = _number(row["最新价"])
        name = _text(row["名称"], code)
        if not code or price <= 0 or re.search(r"ST|退", name, re.IGNORECASE):
            continue
        members[code] = {
            "code": code,
            "name": name,
            "price": price,
            "change_pct": round(_number(row["涨跌幅"]), 2),
            "volume": _number(row["成交量"]),
            "amount_yi": round(_number(row["成交额"]) / 100_000_000, 3),
            "turnover_pct": round(_number(row["换手率"]), 2),
            "themes": [theme],
        }
    return members


def _update_volume_history(code, volume, now, old):
    interval_ratio = None
    intervals = []
    if old and old.get("at"):
        try:
            previous_at = datetime.fromisoformat(old["at"])
            elapsed = (now - previous_at).total_seconds() / 60
            previous_volume = float(old["volume"])
            if 1 <= elapsed <= MAX_QUOTE_GAP_MINUTES and volume >= previous_volume:
                delta = volume - previous_volume
                intervals = [float(item) for item in old.get("intervals", [])][-6:]
                if len(intervals) >= 2 and sum(intervals) > 0:
                    interval_ratio = delta / (sum(intervals) / len(intervals))
                intervals = (intervals + [delta])[-6:]
        except (TypeError, ValueError):
            intervals = []
    return interval_ratio, {
        "volume": volume,
        "at": now.isoformat(),
        "intervals": intervals,
    }


def _trend_metrics(frame, as_of):
    required = {"收盘", "最高"}
    if frame is None or frame.empty or not required.issubset(frame.columns):
        return {"as_of": as_of, "bars": 0, "trend_ready": False}
    closes = pd.to_numeric(frame["收盘"], errors="coerce").dropna().tolist()
    highs = pd.to_numeric(frame["最高"], errors="coerce").dropna().tolist()
    bars = min(len(closes), len(highs))
    if bars < MIN_HISTORY_BARS:
        return {"as_of": as_of, "bars": bars, "trend_ready": False}

    closes = closes[-bars:]
    highs = highs[-bars:]
    ma20 = sum(closes[-20:]) / 20
    previous_ma20 = sum(closes[-25:-5]) / 20
    return {
        "as_of": as_of,
        "bars": bars,
        "trend_ready": True,
        "ret_5d_pct": round((closes[-1] / closes[-6] - 1) * 100, 2) if closes[-6] else 0,
        "ret_20d_pct": round((closes[-1] / closes[-21] - 1) * 100, 2) if closes[-21] else 0,
        "ma20": round(ma20, 4),
        "ma20_rising": ma20 > previous_ma20,
        "high_20d": round(max(highs[-20:]), 4),
    }


def _load_trend_cache(client, codes, as_of):
    cache = {}
    keys = [f"{TREND_KEY_PREFIX}{code}" for code in codes]
    for offset in range(0, len(keys), 300):
        batch = keys[offset:offset + 300]
        if not batch:
            continue
        response = client.table("config").select("key,value").in_("key", batch).execute()
        for row in response.data or []:
            value = _decode_json(row.get("value"), {})
            if isinstance(value, dict) and value.get("as_of") == as_of:
                cache[row["key"].split(":")[-1]] = value
    return cache


def _fetch_trend(code, start_date, end_date):
    frame = ak.stock_zh_a_hist(
        symbol=code,
        period="daily",
        start_date=start_date,
        end_date=end_date,
        adjust="qfq",
    )
    return _trend_metrics(frame, end_date[:4] + "-" + end_date[4:6] + "-" + end_date[6:])


def _fill_missing_trends(client, candidates, cache, as_of_date):
    as_of = as_of_date.isoformat()
    missing = [
        candidate for candidate in candidates.values()
        if candidate["code"] not in cache
    ]
    missing.sort(
        key=lambda item: (
            -item["amount_yi"],
            -item["turnover_pct"],
            -item["change_pct"],
        )
    )
    selected = missing[:MAX_TREND_FETCH_PER_RUN]
    if not selected:
        return cache, []

    start_date = (as_of_date - timedelta(days=90)).strftime("%Y%m%d")
    end_date = as_of_date.strftime("%Y%m%d")
    errors = []
    new_cache = {}
    with ThreadPoolExecutor(max_workers=8) as executor:
        pending = {
            executor.submit(_fetch_trend, item["code"], start_date, end_date): item["code"]
            for item in selected
        }
        for future in as_completed(pending):
            code = pending[future]
            try:
                trend = future.result()
                new_cache[code] = trend
            except Exception as exc:
                errors.append({"code": code, "error": str(exc)})
                print(f"获取 {code} 日线趋势失败: {exc}")

    rows = [
        {
            "key": f"{TREND_KEY_PREFIX}{code}",
            "value": json.dumps(value, ensure_ascii=False),
        }
        for code, value in new_cache.items()
    ]
    for offset in range(0, len(rows), 500):
        client.table("config").upsert(rows[offset:offset + 500], on_conflict="key").execute()
    cache.update(new_cache)
    return cache, errors


def _score_candidate(candidate, trend, theme, volume_ratio):
    current_pct = candidate["change_pct"]
    theme_pct = theme["change_pct"]
    trigger_count = theme["trigger_count"]
    theme_score = min(25, 10 + min(trigger_count, 5) * 2 + max(0, min(theme_pct, 5)))

    trend_score = 0
    support_score = 0
    reasons = [", ".join(candidate["themes"])]
    risks = []
    trend_ready = bool(trend and trend.get("trend_ready"))
    if trend_ready:
        ret_5d = trend["ret_5d_pct"]
        ret_20d = trend["ret_20d_pct"]
        trend_score += 12 if ret_5d >= 8 else 10 if ret_5d >= 3 else 7 if ret_5d >= 0 else 3 if ret_5d >= -3 else 0
        trend_score += 10 if ret_20d >= 15 else 8 if ret_20d >= 5 else 5 if ret_20d >= 0 else 2 if ret_20d >= -5 else 0
        current_vs_ma20 = (candidate["price"] / trend["ma20"] - 1) * 100 if trend["ma20"] else -100
        trend_score += 5 if trend.get("ma20_rising") else 0
        trend_score += 3 if current_vs_ma20 >= 0 else 0
        if ret_5d > 0:
            reasons.append(f"近5日 {ret_5d:+.1f}%")
        if ret_20d > 0:
            reasons.append(f"近20日 {ret_20d:+.1f}%")
        if current_vs_ma20 >= 0:
            reasons.append(f"站上MA20 {current_vs_ma20:+.1f}%")
        elif current_vs_ma20 >= -2:
            reasons.append("回踩MA20附近")
        else:
            risks.append(f"低于MA20 {current_vs_ma20:.1f}%")
        if trend.get("ma20_rising"):
            reasons.append("MA20向上")
        drawdown = (candidate["price"] / trend["high_20d"] - 1) * 100 if trend["high_20d"] else 0
        support_score += 8 if drawdown >= -10 else 6 if drawdown >= -20 else 3 if drawdown >= -30 else 0
        if drawdown <= -20:
            risks.append(f"距20日高点 {drawdown:.1f}%")
        support_score += 12 if 0 <= current_vs_ma20 <= 5 else 8 if 5 < current_vs_ma20 <= 10 else 6 if -2 <= current_vs_ma20 < 0 else 3 if current_vs_ma20 > 10 else 0
    else:
        reasons.append("历史趋势数据补采中")
        risks.append("暂无完整日线趋势")

    intraday_score = 10 if 0.5 <= current_pct <= 3 else 8 if 0 < current_pct < 0.5 else 7 if 3 < current_pct <= 6 else 3 if 6 < current_pct <= 8 else 0
    if volume_ratio is not None:
        intraday_score += 10 if volume_ratio >= 2 else 8 if volume_ratio >= 1.5 else 5 if volume_ratio >= 1 else 0
        reasons.append(f"5分钟量比 {volume_ratio:.1f}x")
    else:
        risks.append("盘中量能基线积累中")
    intraday_score += 5 if candidate["turnover_pct"] >= 1 else 3 if candidate["turnover_pct"] >= 0.3 else 0
    if candidate["amount_yi"] >= 0.5:
        reasons.append(f"成交额 {candidate['amount_yi']:.2f}亿")
    if candidate["turnover_pct"] >= 1:
        reasons.append(f"换手 {candidate['turnover_pct']:.1f}%")
    if current_pct > 7:
        risks.append("盘中涨幅偏高，注意追高风险")
    if current_pct < 0:
        risks.append("盘中走势偏弱")

    total = max(0, min(100, round(theme_score + trend_score + intraday_score + support_score)))
    if not trend_ready:
        tier = "趋势待补"
    elif total >= 75:
        tier = "强势联动"
    elif total >= 60:
        tier = "趋势观察"
    else:
        tier = "观察"

    return {
        **candidate,
        "score": total,
        "tier": tier,
        "volume_ratio_5m": round(volume_ratio, 2) if volume_ratio is not None else None,
        "ret_5d_pct": trend.get("ret_5d_pct") if trend_ready else None,
        "ret_20d_pct": trend.get("ret_20d_pct") if trend_ready else None,
        "ma20": trend.get("ma20") if trend_ready else None,
        "trend_ready": trend_ready,
        "reasons": reasons,
        "risks": risks,
    }


def build_radar(themes, members_by_theme, trends, quote_history, now, active_codes, broken_codes):
    candidate_map = {}
    next_quote_history = {}
    for theme in themes:
        for code, candidate in members_by_theme.get(theme["name"], {}).items():
            if code in active_codes or code in broken_codes:
                continue
            volume_ratio, history = _update_volume_history(
                code,
                candidate["volume"],
                now,
                quote_history.get(code),
            )
            next_quote_history[code] = history
            if code not in candidate_map:
                candidate_map[code] = {**candidate, "volume_ratio_5m": volume_ratio}
            else:
                candidate_map[code]["themes"].extend(
                    name for name in candidate["themes"]
                    if name not in candidate_map[code]["themes"]
                )
                if volume_ratio is not None:
                    candidate_map[code]["volume_ratio_5m"] = volume_ratio

    theme_rows = []
    for theme in themes:
        ranked = []
        for candidate in candidate_map.values():
            if theme["name"] not in candidate["themes"]:
                continue
            scored = _score_candidate(
                candidate,
                trends.get(candidate["code"], {}),
                theme,
                candidate.get("volume_ratio_5m"),
            )
            ranked.append(scored)
        ranked.sort(key=lambda item: (-item["score"], -item["amount_yi"], item["code"]))
        theme_rows.append({
            **theme,
            "candidate_count": len(ranked),
            "candidates": ranked[:MAX_CONCEPT_CANDIDATES],
        })
    return theme_rows, next_quote_history


def _paused_payload(status, message, trade_date, now):
    return {
        "schema_version": 1,
        "status": status,
        "message": message,
        "trade_date": trade_date.isoformat() if trade_date else None,
        "updated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "active_count": 0,
        "new_count": 0,
        "themes": [],
        "active_events": [],
        "errors": [],
    }


def _save_paused_status(client, status, message, trade_date, now):
    previous = _kv_get(client, LATEST_KEY)
    if isinstance(previous, dict) and previous.get("trade_date") == (
        trade_date.isoformat() if trade_date else None
    ):
        payload = dict(previous)
        payload["status"] = status
        payload["message"] = message
        payload["checked_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
    else:
        payload = _paused_payload(status, message, trade_date, now)
    _kv_set(client, LATEST_KEY, payload)
    return payload


def run_radar(client, now=None):
    now = now or datetime.now(CN_TZ)
    today = now.date()
    calendar = ak.tool_trade_date_hist_sina()
    trading_dates = _trade_dates(calendar)
    previous_dates = [trade_day for trade_day in trading_dates if trade_day < today]
    previous_trade_date = max(previous_dates) if previous_dates else None

    if today not in trading_dates:
        return _save_paused_status(
            client,
            "non_trading_day",
            "今天不是交易日",
            previous_trade_date,
            now,
        )
    if not _is_market_session(now):
        return _save_paused_status(
            client,
            "outside_session",
            "当前不在连续竞价时段",
            today,
            now,
        )
    if previous_trade_date is None:
        raise ValueError("交易日历中没有可用的前一交易日")

    trade_date = today.strftime("%Y%m%d")
    pool_frame = ak.stock_zt_pool_em(date=trade_date)
    pool = _normalize_pool(pool_frame)
    broken_frame = ak.stock_zt_pool_zbgc_em(date=trade_date)
    broken_codes = _broken_codes(broken_frame)
    state = _load_state(client, today)
    active_events, new_events, new_codes, concept_errors = _update_events(
        pool,
        broken_codes,
        state,
        now,
    )

    errors = [
        {"code": code, "error": message, "stage": "concepts"}
        for code, message in concept_errors.items()
    ]
    themes = []
    if pool:
        theme_stats = _fetch_theme_stats()
        themes = _select_active_themes(active_events, theme_stats)

    members_by_theme = {}
    for theme in themes:
        try:
            frame = ak.stock_board_concept_cons_em(symbol=theme["name"])
            members_by_theme[theme["name"]] = _normalize_theme_members(frame, theme["name"])
        except Exception as exc:
            errors.append({"theme": theme["name"], "error": str(exc), "stage": "members"})
            print(f"获取 {theme['name']} 成分股失败: {exc}")

    all_candidates = {}
    for members in members_by_theme.values():
        for code, candidate in members.items():
            if code not in all_candidates:
                all_candidates[code] = candidate
            else:
                existing = all_candidates[code]
                existing["themes"].extend(
                    theme for theme in candidate["themes"]
                    if theme not in existing["themes"]
                )
    codes = list(all_candidates)
    trend_cache = _load_trend_cache(client, codes, previous_trade_date.isoformat())
    trend_cache, trend_errors = _fill_missing_trends(
        client,
        all_candidates,
        trend_cache,
        previous_trade_date,
    )
    errors.extend(
        {"code": item["code"], "error": item["error"], "stage": "trend"}
        for item in trend_errors
    )

    theme_rows, quote_history = build_radar(
        themes,
        members_by_theme,
        trend_cache,
        state.get("quote_history", {}),
        now,
        {event["code"] for event in active_events},
        broken_codes,
    )
    state["quote_history"] = quote_history
    state["updated_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
    _kv_set(client, STATE_KEY, state)

    payload = {
        "schema_version": 1,
        "status": "partial" if errors else "live",
        "trade_date": today.isoformat(),
        "updated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "active_count": len(active_events),
        "new_count": len(new_events),
        "active_events": active_events[:100],
        "new_events": new_events[:50],
        "themes": theme_rows,
        "trend_fetch_limit": MAX_TREND_FETCH_PER_RUN,
        "errors": errors[:30],
        "score_note": (
            "综合分用于候选排序，不是收益概率或买入指令。资金承接仅以成交额、换手和相邻轮询量能作参考；"
            "历史胜率需积累并回测足够样本后再展示。"
        ),
    }
    _kv_set(client, LATEST_KEY, payload)
    print(
        f"盘中雷达已更新 {payload['trade_date']} {payload['updated_at']}，"
        f"封板 {payload['active_count']} 只，新触发 {payload['new_count']} 只，"
        f"扫描概念 {len(theme_rows)} 个，异常 {len(errors)} 项"
    )
    return payload


def main():
    from supabase import create_client

    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_SERVICE_KEY")
    if not supabase_url or not supabase_key:
        raise RuntimeError("缺少 SUPABASE_URL / SUPABASE_SERVICE_KEY")
    client = create_client(supabase_url, supabase_key)
    run_radar(client)


if __name__ == "__main__":
    main()
