"""Eastmoney concept-board membership lookups shared by daily jobs."""
import threading
import time

import requests

_CONCEPT_BOARDS = None
_CONCEPT_CODE_SET = set()
_CONCEPT_LOCK = threading.Lock()


def http_json(url, params, tries=3, timeout=25):
    last = None
    for attempt in range(tries):
        try:
            response = requests.get(
                url,
                params=params,
                timeout=timeout,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            response.encoding = "utf-8"
            return response.json()
        except Exception as exc:
            last = exc
            time.sleep(1.5 * (attempt + 1))
    print(f"请求失败 {url}: {last}")
    return None


def load_concept_boards(strict=False, tries=3, timeout=25):
    """Return Eastmoney concept-board codes and names."""
    global _CONCEPT_BOARDS, _CONCEPT_CODE_SET
    if _CONCEPT_BOARDS is not None:
        if strict and not _CONCEPT_BOARDS:
            raise RuntimeError("未能加载东财概念板块列表")
        return _CONCEPT_BOARDS

    with _CONCEPT_LOCK:
        if _CONCEPT_BOARDS is not None:
            if strict and not _CONCEPT_BOARDS:
                raise RuntimeError("未能加载东财概念板块列表")
            return _CONCEPT_BOARDS

        payload = http_json(
            "https://push2.eastmoney.com/api/qt/clist/get",
            {
                "pn": "1",
                "pz": "600",
                "po": "1",
                "np": "1",
                "fltt": "2",
                "invt": "2",
                "fid": "f3",
                "fs": "m:90+t:3+f:!50",
                "fields": "f12,f14",
            },
            tries=tries,
            timeout=timeout,
        )
        data = payload.get("data") if isinstance(payload, dict) else None
        if strict and not isinstance(data, dict):
            raise RuntimeError("未能加载东财概念板块列表")
        boards = {}
        for item in ((data or {}).get("diff") or []):
            code = item.get("f12")
            name = item.get("f14")
            if code and name:
                boards[code] = name

        _CONCEPT_BOARDS = boards
        _CONCEPT_CODE_SET = set(boards)
        print(f"加载概念板块 {len(boards)} 个")
        if strict and not boards:
            raise RuntimeError("未能加载东财概念板块列表")
        return boards


def get_stock_concepts(code, strict=False, tries=3, timeout=25):
    """Return concept-board memberships for one stock."""
    boards = load_concept_boards(strict=strict, tries=tries, timeout=timeout)
    if not boards:
        return []

    payload = http_json(
        "https://push2.eastmoney.com/api/qt/slist/get",
        {
            "fltt": "2",
            "invt": "2",
            "fields": "f12,f14",
            "secid": f'{1 if code.startswith("6") else 0}.{code}',
            "pn": "1",
            "pz": "100",
            "po": "1",
            "np": "1",
            "fid": "f62",
            "spt": "3",
        },
        tries=tries,
        timeout=timeout,
    )
    data = payload.get("data") if isinstance(payload, dict) else None
    if strict and (not isinstance(data, dict) or "diff" not in data):
        raise RuntimeError(f"未能获取 {code} 的概念板块")

    diff = ((data or {}).get("diff") or [])
    concepts = []
    for item in diff:
        board_code = item.get("f12")
        if board_code in _CONCEPT_CODE_SET:
            name = boards.get(board_code) or item.get("f14")
            if name and name not in concepts:
                concepts.append(name)
    return concepts[:12]
