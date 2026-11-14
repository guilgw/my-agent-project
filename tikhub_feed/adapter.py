"""把 TikHub 的 TikTok 视频搜索结果转成工作台接收器要的 JSON。

工作台 `video_trends.partner_feed` 只接受 HTTPS，并且会在地址上追加
`q`、`region`、`days`、`limit`。本服务听 `https://127.0.0.1`，证书只签给
127.0.0.1 和 localhost。`scripts/start.sh` 会把证书追加进工作台信任的
CA 包，并带上本地 Bearer 令牌。

TikHub 的 `publish_time` 过滤不可靠。这里仍把天数传给它，但拿到结果后按
`create_time` 丢掉窗口外的视频，再按 `play_count` 降序取前 N 条。
N 来自 `TIKHUB_TOP_N`（默认 10），并且不超过调用方的 limit。
上游 `count` 固定为 20，这样滤掉过期视频后仍可能凑够 N 条。每次只发一页。

只请求 App V3 `GET /api/v1/tiktok/app/v3/fetch_video_search_result`。
2026-11-14 实测：Web 的 `GET /api/v1/tiktok/web/fetch_search_video` 对
`keyword=train travel&count=3&offset=0` 返回 400，且未计费，所以不调用、也不做备用。
同一天 App V3 在 `count=5` 时 `data.search_item_list` 返回了 12 条。
App V3 带 region，publish_time 的 1、7、30 与工作台的天数一致。
"""

from __future__ import annotations

import hmac
import json
import os
import re
import ssl
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

API_URL = "https://api.tikhub.io/api/v1/tiktok/app/v3/fetch_video_search_result"
DEFAULT_COUNT = 10
# 上游一页要 20 条。TikHub 不保证少返回，但再大也不会多发一次请求。
FETCH_COUNT = 20
ALLOWED_DAYS = (1, 7, 30)
# 1 = 最多点赞。播放量在本地再排一次，因为这个排序不是按播放。
SORT_MOST_LIKED = 1
# 和工作台 normalize 一样，允许 5 分钟内的时钟误差。
FUTURE_SKEW = timedelta(minutes=5)
CERT_NAME = "tikhub-local.crt"
KEY_NAME = "tikhub-local.key"
TOKEN_NAME = "tikhub-local.token"

# 测试把这个换成返回 dict 的函数，避免真的请求 TikHub。
transport = None
# 测试可以换成返回固定时间的函数。生产环境用当前 UTC。
clock = None
# 测试把令牌文件指到临时路径，避免读到本机 .run 里的真令牌。
token_file_override: Path | None = None


class TikHubError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def api_key() -> str:
    return (os.environ.get("TIKHUB_API_KEY") or "").strip()


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def search(query: str, region: str = "GB", days: int = 7, limit: int = DEFAULT_COUNT) -> dict:
    """返回 `{"items": [...]}`。不发起第二次请求，不下载视频。"""
    keyword, region, days, limit = _query(query, region, days, limit)
    if not api_key():
        raise TikHubError("请先设置环境变量 TIKHUB_API_KEY。本次没有请求 TikHub。", 401)
    payload = _load(keyword, region, days, FETCH_COUNT)
    now = _now()
    items = []
    seen = set()
    for aweme in _awemes(payload):
        if not _in_window(aweme, days, now):
            continue
        item = _standard_item(aweme)
        if item is None or item["url"] in seen:
            continue
        seen.add(item["url"])
        items.append(item)
    items.sort(key=_play_count, reverse=True)
    # 默认留下播放最高的 10 条，但不会超过工作台这次传来的 limit。
    return {"items": items[: min(top_n(), limit)]}


def serve() -> None:
    _load_token_from_file()
    if not local_token():
        print("未配置本地令牌。请用 scripts/start.sh 启动，以免本机其他进程直接消耗 TikHub 额度。", flush=True)
    port = int(os.environ.get("TIKHUB_ADAPTER_PORT") or "8781")
    server = build_server("127.0.0.1", port)
    print(f"TikHub 适配层：https://127.0.0.1:{port}/videos", flush=True)
    server.serve_forever()


def build_server(host: str, port: int, directory: Path | None = None) -> ThreadingHTTPServer:
    """听指定地址，并用只签给本机的证书包上 TLS。"""
    server = ThreadingHTTPServer((host, port), Handler)
    cert, key = ensure_local_cert(directory)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certfile=str(cert), keyfile=str(key))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    return server


def ensure_local_cert(directory: Path | None = None) -> tuple[Path, Path]:
    """生成或复用只含 127.0.0.1 与 localhost 的自签证书。私钥保持 600。"""
    base = Path(directory) if directory is not None else repo_root() / ".run"
    base.mkdir(parents=True, exist_ok=True)
    cert = base / CERT_NAME
    key = base / KEY_NAME
    if _cert_reusable(cert, key):
        os.chmod(key, 0o600)
        return cert, key
    _generate_cert(cert, key)
    os.chmod(key, 0o600)
    if not _cert_reusable(cert, key):
        raise TikHubError("生成的本地证书不能用于 127.0.0.1 和 localhost。", 500)
    return cert, key


def local_token() -> str:
    return (os.environ.get("VIDEO_TRENDS_TIKTOK_TOKEN") or "").strip()


def token_file() -> Path:
    if token_file_override is not None:
        return token_file_override
    return repo_root() / ".run" / TOKEN_NAME


def _load_token_from_file() -> None:
    if local_token():
        return
    path = token_file()
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError:
        return
    if token:
        os.environ["VIDEO_TRENDS_TIKTOK_TOKEN"] = token


def top_n() -> int:
    raw = (os.environ.get("TIKHUB_TOP_N") or "").strip()
    if re.fullmatch(r"[1-9]\d{0,3}", raw):
        return int(raw)
    return DEFAULT_COUNT


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if not self._allowed():
            self._send(401, {"error": "本地令牌不正确。"})
            return
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

    def _allowed(self) -> bool:
        expected = local_token()
        if not expected:
            return True
        header = self.headers.get("Authorization") or ""
        given = header[7:].strip() if header.lower().startswith("bearer ") else ""
        return hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))

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
    # 工作台最多收 100 条。真正留下几条还要再和 TIKHUB_TOP_N 取更小值。
    bounded = min(max(limit, 1), 100)
    return query.strip(), region.strip().upper(), days, bounded


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


def _epoch(value) -> int | None:
    if isinstance(value, str) and re.fullmatch(r"\d{10,13}", value):
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    if value > 10_000_000_000:
        value //= 1000
    return value


def _published(value) -> str | None:
    epoch = _epoch(value)
    if epoch is None:
        return None
    try:
        return datetime.fromtimestamp(epoch, timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _now() -> datetime:
    if clock is not None:
        return clock()
    return datetime.now(timezone.utc)


def _in_window(aweme: dict, days: int, now: datetime) -> bool:
    """TikHub 的 publish_time 会把窗口外的视频也塞进来，这里用 create_time 再剔一次。"""
    epoch = _epoch(aweme.get("create_time"))
    if epoch is None:
        return False
    try:
        published = datetime.fromtimestamp(epoch, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return False
    if published < now - timedelta(days=days):
        return False
    if published > now + FUTURE_SKEW:
        return False
    return True


def _play_count(item: dict) -> int:
    views = item["metrics"]["views"]
    if isinstance(views, int):
        return views
    return -1


def _cert_reusable(cert: Path, key: Path) -> bool:
    if not cert.is_file() or not key.is_file() or cert.stat().st_size == 0 or key.stat().st_size == 0:
        return False
    inspected = subprocess.run(
        ["openssl", "x509", "-in", str(cert), "-noout", "-text", "-checkend", "86400"],
        capture_output=True, text=True, timeout=15,
    )
    if inspected.returncode != 0:
        return False
    text = inspected.stdout
    marker = "Subject Alternative Name"
    if marker not in text:
        return False
    san = text.split(marker, 1)[1][:500]
    if "127.0.0.1" not in san or "localhost" not in san:
        return False
    cert_mod = subprocess.run(
        ["openssl", "x509", "-in", str(cert), "-noout", "-modulus"],
        capture_output=True, text=True, timeout=15,
    )
    key_mod = subprocess.run(
        ["openssl", "rsa", "-in", str(key), "-noout", "-modulus"],
        capture_output=True, text=True, timeout=15,
    )
    return cert_mod.returncode == 0 and key_mod.returncode == 0 and cert_mod.stdout == key_mod.stdout and bool(cert_mod.stdout.strip())


def _generate_cert(cert: Path, key: Path) -> None:
    config = "\n".join([
        "[req]",
        "distinguished_name = dn",
        "x509_extensions = ext",
        "prompt = no",
        "[dn]",
        "CN = 127.0.0.1",
        "[ext]",
        "subjectAltName = IP:127.0.0.1,DNS:localhost",
        "basicConstraints = CA:FALSE",
        "keyUsage = digitalSignature, keyEncipherment",
        "extendedKeyUsage = serverAuth",
        "",
    ])
    with tempfile.TemporaryDirectory() as tmp:
        cnf = Path(tmp) / "openssl.cnf"
        cnf.write_text(config, encoding="utf-8")
        result = subprocess.run(
            [
                "openssl", "req", "-x509", "-newkey", "rsa:2048", "-sha256",
                "-days", "825", "-nodes",
                "-keyout", str(key), "-out", str(cert),
                "-config", str(cnf), "-extensions", "ext",
            ],
            capture_output=True, text=True, timeout=30,
        )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[-300:]
        raise TikHubError("无法生成本地证书。" + (detail and " " + detail), 500)
    if key.is_file():
        os.chmod(key, 0o600)


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
