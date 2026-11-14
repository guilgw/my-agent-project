"""把 TikHub 的 TikTok 视频搜索结果转成工作台接收器要的 JSON。

工作台 `video_trends.partner_feed` 只接受 HTTPS，并且会在地址上追加
`q`、`region`、`days`、`limit`。本服务实现这个查询和返回格式。
本机默认听 `http://127.0.0.1`，不能直接填进 `VIDEO_TRENDS_TIKTOK_FEED_URL`。
放到可信 HTTPS 地址后面之后，工作台页面才能把 FEED_URL 指过来。
MCP 采集不走这个限制，直接调用 `search` 再写入同一份缓存。

接口选用 TikHub App V3 `GET /api/v1/tiktok/app/v3/fetch_video_search_result`：
它带 region，且 publish_time 的 1、7、30 与工作台的天数一致。每次只发一页，
避免翻页再计一次费。
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

API_URL = "https://api.tikhub.io/api/v1/tiktok/app/v3/fetch_video_search_result"
DEFAULT_COUNT = 10
MAX_COUNT = 20
ALLOWED_DAYS = (1, 7, 30)
# 1 = 最多点赞。条数被压得很低，优先留下这段时间里更热的视频。
SORT_MOST_LIKED = 1

# 测试把这个换成返回 dict 的函数，避免真的请求 TikHub。
transport = None


class TikHubError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def api_key() -> str:
    return (os.environ.get("TIKHUB_API_KEY") or "").strip()


def search(query: str, region: str = "GB", days: int = 7, limit: int = DEFAULT_COUNT) -> dict:
    """返回 `{"items": [...]}`。不发起第二次请求，不下载视频。"""
    keyword, region, days, count = _query(query, region, days, limit)
    if not api_key():
        raise TikHubError("请先设置环境变量 TIKHUB_API_KEY。本次没有请求 TikHub。", 401)
    payload = _load(keyword, region, days, count)
    items = []
    seen = set()
    for aweme in _awemes(payload):
        item = _standard_item(aweme)
        if item is None or item["url"] in seen:
            continue
        seen.add(item["url"])
        items.append(item)
        if len(items) >= count:
            break
    return {"items": items}


def serve() -> None:
    port = int(os.environ.get("TIKHUB_ADAPTER_PORT") or "8781")
    print(f"TikHub 适配层：http://127.0.0.1:{port}/videos", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path).path
        if path not in ("/videos", "/videos/"):
            self._send(404, {"error": "not found"})
            return
        qs = parse_qs(urlparse(self.path).query)

        def one(name: str, default: str = "") -> str:
            values = qs.get(name) or []
            return values[-1] if values else default

        try:
            days = int(one("days") or "7")
            limit = int(one("limit") or str(DEFAULT_COUNT))
            body = search(one("q") or one("keyword"), one("region") or "GB", days, limit)
        except TikHubError as exc:
            self._send(exc.status, {"error": str(exc)})
            return
        except (TypeError, ValueError):
            self._send(400, {"error": "查询参数不正确。"})
            return
        self._send(200, body)

    def log_message(self, format, *args):
        return

    def _send(self, code: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


def _query(query: str, region: str, days: int, limit: int) -> tuple[str, str, int, int]:
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 100:
        raise TikHubError("请输入 1–100 字的搜索关键词。")
    if not isinstance(region, str) or not re.fullmatch(r"[A-Za-z]{2}", region.strip()):
        raise TikHubError("地区须为两位国家代码。")
    if isinstance(days, bool) or not isinstance(days, int) or days not in ALLOWED_DAYS:
        raise TikHubError("时间范围仅支持 1、7 或 30 天。")
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise TikHubError("limit 必须是整数。")
    count = min(max(limit, 1), MAX_COUNT)
    return query.strip(), region.strip().upper(), days, count


def _load(keyword: str, region: str, days: int, count: int) -> dict:
    params = urlencode({
        "keyword": keyword,
        "count": count,
        "offset": 0,
        "sort_type": SORT_MOST_LIKED,
        "publish_time": days,
        "region": region,
    })
    url = API_URL + "?" + params
    headers = {"Authorization": "Bearer " + api_key(), "Accept": "application/json"}
    if transport is not None:
        data = transport(url, headers, 15)
        if not isinstance(data, dict):
            raise TikHubError("TikHub 响应不是 JSON 对象。")
        return data
    request = Request(url, headers=headers)
    try:
        with build_opener(_NoRedirect).open(request, timeout=15) as response:
            raw = response.read(2_000_001)
    except HTTPError as exc:
        messages = {401: "TikHub 凭据无效或已过期。", 403: "TikHub 权限不足或额度已用完。", 429: "TikHub 请求过于频繁或额度已用完。"}
        raise TikHubError(messages.get(exc.code, f"TikHub 返回 HTTP {exc.code}。"), exc.code if exc.code in (401, 403, 429) else 502) from None
    except (URLError, TimeoutError, OSError):
        raise TikHubError("无法连接 TikHub，请检查网络后重试。", 502) from None
    if len(raw) > 2_000_000:
        raise TikHubError("TikHub 响应超过 2 MB，已放弃。", 502)
    try:
        data = json.loads(raw)
    except ValueError:
        raise TikHubError("TikHub 响应不是 JSON。", 502) from None
    if not isinstance(data, dict):
        raise TikHubError("TikHub 响应不是 JSON 对象。", 502)
    code = data.get("code")
    if code not in (None, 200):
        raise TikHubError("TikHub 没有返回视频搜索结果。", 502)
    return data


def _awemes(payload: dict):
    block = payload.get("data", payload)
    if isinstance(block, str):
        try:
            block = json.loads(block)
        except ValueError:
            return
    rows = []
    if isinstance(block, list):
        rows = block
    elif isinstance(block, dict):
        for key in ("search_item_list", "aweme_list", "aweme_detail"):
            value = block.get(key)
            if isinstance(value, list):
                rows.extend(value)
            elif isinstance(value, dict):
                rows.append(value)
        business = block.get("business_data")
        if isinstance(business, list):
            for row in business:
                if isinstance(row, dict):
                    inner = row.get("data") if isinstance(row.get("data"), dict) else row
                    rows.append(inner)
    for row in rows:
        yield from _unwrap(row)


def _unwrap(item):
    if not isinstance(item, dict):
        return
    if item.get("aweme_id"):
        yield item
        return
    for key in ("aweme_info", "aweme"):
        if isinstance(item.get(key), dict):
            yield from _unwrap(item[key])
            return
    mix = item.get("aweme_mix_info")
    if isinstance(mix, dict) and isinstance(mix.get("mix_items"), list):
        for child in mix["mix_items"]:
            yield from _unwrap(child)


def _standard_item(aweme: dict) -> dict | None:
    url = _video_url(aweme)
    title = str(aweme.get("desc") or aweme.get("title") or "").strip()
    if url is None or not title:
        return None
    author = aweme.get("author") if isinstance(aweme.get("author"), dict) else {}
    name = str(author.get("unique_id") or author.get("uniqueId") or author.get("nickname") or "").strip()
    stats = aweme.get("statistics") if isinstance(aweme.get("statistics"), dict) else {}
    item = {
        "title": title[:300],
        "url": url,
        "author": name[:200],
        "published_at": _published(aweme.get("create_time")),
        "metrics": {
            "views": _count(stats, "play_count", "playCount"),
            "likes": _count(stats, "digg_count", "diggCount"),
            "comments": _count(stats, "comment_count", "commentCount"),
            "shares": _count(stats, "share_count", "shareCount"),
        },
    }
    return item


def _video_url(aweme: dict) -> str | None:
    video_id = str(aweme.get("aweme_id") or "")
    author = aweme.get("author") if isinstance(aweme.get("author"), dict) else {}
    handle = str(author.get("unique_id") or author.get("uniqueId") or "")
    if not re.fullmatch(r"\d{1,24}", video_id) or not re.fullmatch(r"[\w.-]{1,80}", handle):
        return None
    return f"https://www.tiktok.com/@{handle}/video/{video_id}"


def _published(value) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"\d{10,13}", value):
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    if value > 10_000_000_000:
        value //= 1000
    try:
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _count(stats: dict, *keys: str) -> int | None:
    for key in keys:
        if key not in stats:
            continue
        value = stats[key]
        if value is None or value == "":
            return None
        if isinstance(value, bool):
            return None
        if isinstance(value, int) and 0 <= value <= 10**18:
            return value
        if isinstance(value, str) and re.fullmatch(r"\d{1,18}", value):
            return int(value)
        return None
    return None
