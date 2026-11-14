"""TikHub 适配只用 mock 响应，不请求 api.tikhub.io。"""

from __future__ import annotations

import json
import os
import signal
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
STUDIO = Path(os.environ.get("RAIL_STUDIO_ROOT") or "/tmp/refs/rail-content-studio")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import tikhub_feed.adapter as adapter


def aweme(**overrides):
    item = {
        "aweme_id": "7300000000000000001",
        "desc": "Sleep the night train from London to Scotland",
        "create_time": 1735689600,
        "author": {"unique_id": "rail.notes", "nickname": "Rail Notes"},
        "statistics": {"play_count": 1200, "digg_count": 80, "comment_count": 4},
    }
    item.update(overrides)
    return item


def envelope(*rows, key="search_item_list"):
    return {"code": 200, "data": {key: [{"aweme_info": row} for row in rows]}}


def pin_clock(moment: datetime):
    adapter.clock = lambda: moment


class MappingTest(unittest.TestCase):
    def setUp(self):
        self.original = adapter.transport
        self.original_clock = adapter.clock
        self.original_token_file = adapter.token_file_override
        self.env = {
            key: os.environ.get(key)
            for key in ("TIKHUB_API_KEY", "TIKHUB_TOP_N", "VIDEO_TRENDS_TIKTOK_TOKEN")
        }
        os.environ["TIKHUB_API_KEY"] = "unit-test-secret-value"
        os.environ.pop("TIKHUB_TOP_N", None)
        os.environ.pop("VIDEO_TRENDS_TIKTOK_TOKEN", None)
        adapter.token_file_override = Path("/tmp/tikhub-token-absent-for-unit-tests")
        # 现有夹具的 create_time 是 2025-01-01。把时钟钉在当天中午，1 天和 7 天窗口都盖得住。
        pin_clock(datetime(2025, 1, 1, 12, tzinfo=timezone.utc))

    def tearDown(self):
        adapter.transport = self.original
        adapter.clock = self.original_clock
        adapter.token_file_override = self.original_token_file
        for key, value in self.env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_maps_search_item_and_keeps_missing_metrics_null(self):
        calls = {}

        def transport(url, headers, timeout):
            calls["url"] = url
            calls["headers"] = headers
            calls["timeout"] = timeout
            return envelope(aweme())

        adapter.transport = transport
        result = adapter.search("train travel", "gb", 7, 25)
        item = result["items"][0]
        self.assertEqual(item["url"], "https://www.tiktok.com/@rail.notes/video/7300000000000000001")
        self.assertEqual(item["author"], "rail.notes")
        self.assertEqual(item["metrics"]["views"], 1200)
        self.assertIsNone(item["metrics"]["shares"])
        self.assertTrue(item["published_at"].startswith("2025-01-01"))
        self.assertIn("count=20", calls["url"])
        self.assertIn("publish_time=7", calls["url"])
        self.assertIn("region=GB", calls["url"])
        self.assertIn("sort_type=1", calls["url"])
        self.assertEqual(calls["headers"]["Authorization"], "Bearer unit-test-secret-value")
        self.assertEqual(calls["headers"]["User-Agent"], adapter.USER_AGENT)
        self.assertFalse(calls["headers"]["User-Agent"].startswith("Python-urllib/"))
        self.assertNotIn("unit-test-secret-value", calls["url"])
        self.assertEqual(calls["timeout"], 15)

    def test_cloudflare_1010_is_not_described_as_a_quota_error(self):
        blocked = adapter._tikhub_error(403, b"error code: 1010")
        self.assertEqual(blocked.status, 403)
        self.assertEqual(str(blocked), "TikHub 前置防火墙拦截了请求（Cloudflare 1010）。")
        still_blocked = adapter._tikhub_error(403, b'{"title":"Error 1010: Access denied"}')
        self.assertIn("Cloudflare 1010", str(still_blocked))
        quota = adapter._tikhub_error(403, b'{"detail":"forbidden"}')
        self.assertIn("额度", str(quota))
        self.assertNotIn("1010", str(quota))

    def test_truncates_when_app_v3_returns_more_than_count(self):
        """2026-11-14：count=5 时 search_item_list 实际有 12 条，且每条在 aweme_info 下。"""
        rows = []
        for index in range(12):
            rows.append(aweme(
                aweme_id=f"73000000000000000{index:02d}",
                desc=f"Train travel clip {index}",
                author={"unique_id": f"rail{index}", "nickname": f"Rail {index}"},
                statistics={"play_count": 1200 - index, "digg_count": 80 - index},
            ))
        calls = {}

        def transport(url, headers, timeout):
            calls["url"] = url
            return {"code": 200, "data": {"search_item_list": [{"aweme_info": row} for row in rows]}}

        adapter.transport = transport
        result = adapter.search("train travel", "GB", 7, 5)
        self.assertEqual(len(result["items"]), 5)
        self.assertEqual([item["metrics"]["views"] for item in result["items"]], [1200, 1199, 1198, 1197, 1196])
        self.assertEqual([item["metrics"]["likes"] for item in result["items"]], [80, 79, 78, 77, 76])
        self.assertEqual(result["items"][0]["title"], "Train travel clip 0")
        self.assertNotIn("7300000000000000011", json.dumps(result))
        self.assertIn("/api/v1/tiktok/app/v3/fetch_video_search_result?", calls["url"])
        self.assertNotIn("fetch_search_video", calls["url"])
        # 上游固定要 20 条，滤掉窗口外的视频后仍按调用方的 5 条截断。
        for part in ("keyword=train+travel", "count=20", "offset=0", "sort_type=1", "publish_time=7", "region=GB"):
            self.assertIn(part, calls["url"])

    def test_reads_aweme_list_and_skips_rows_without_a_handle(self):
        adapter.transport = lambda url, headers, timeout: {
            "code": 200,
            "data": {"aweme_list": [aweme(author={"nickname": "no handle"}), aweme(aweme_id="7300000000000000002")]},
        }
        result = adapter.search("train", "GB", 1, 10)
        self.assertEqual([item["url"].rsplit("/", 1)[-1] for item in result["items"]], ["7300000000000000002"])

    def test_missing_key_does_not_call_tikhub(self):
        os.environ.pop("TIKHUB_API_KEY", None)
        called = []
        adapter.transport = lambda *args, **kwargs: called.append(True) or {}
        with self.assertRaises(adapter.TikHubError) as caught:
            adapter.search("train", "GB", 7, 10)
        self.assertEqual(caught.exception.status, 401)
        self.assertFalse(called)

    def test_bad_days_rejected_before_transport(self):
        adapter.transport = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("不应请求"))
        with self.assertRaises(adapter.TikHubError):
            adapter.search("train", "GB", 14, 10)

    def test_server_returns_standard_json(self):
        adapter.transport = lambda url, headers, timeout: envelope(aweme())
        server = ThreadingHTTPServer(("127.0.0.1", 0), adapter.Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urlopen(f"http://127.0.0.1:{port}/videos?q=train+travel&region=GB&days=7&limit=25", timeout=5) as response:
                body = json.load(response)
            self.assertEqual(body["items"][0]["metrics"]["shares"], None)
        finally:
            server.shutdown()
            server.server_close()

    def test_server_unauthorized_without_key(self):
        os.environ.pop("TIKHUB_API_KEY", None)
        server = ThreadingHTTPServer(("127.0.0.1", 0), adapter.Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with self.assertRaises(HTTPError) as caught:
                urlopen(f"http://127.0.0.1:{port}/videos?q=train&region=GB&days=7", timeout=5)
            self.assertEqual(caught.exception.code, 401)
        finally:
            server.shutdown()
            server.server_close()


class CacheTest(unittest.TestCase):
    def setUp(self):
        if not (STUDIO / "studio.py").is_file():
            self.skipTest(f"需要先克隆工作台：{STUDIO}")
        self.output = ROOT / ".run" / "tikhub-test"
        self.output.mkdir(parents=True, exist_ok=True)
        self.original_clock = adapter.clock
        pin_clock(datetime(2025, 1, 1, 12, tzinfo=timezone.utc))
        self.previous_output = os.environ.get("RAIL_OUTPUT_DIR")
        cache = self.output / "video-trends.json"
        if cache.exists():
            cache.unlink()
        os.environ["RAIL_STUDIO_ROOT"] = str(STUDIO)
        os.environ["RAIL_OUTPUT_DIR"] = str(self.output)
        os.environ["TIKHUB_API_KEY"] = "unit-test-secret-value"
        self.original = adapter.transport
        import mcp_server.studio_bridge as bridge
        self.bridge = bridge

    def tearDown(self):
        adapter.transport = self.original
        adapter.clock = self.original_clock
        os.environ.pop("TIKHUB_API_KEY", None)
        if self.previous_output is None:
            os.environ.pop("RAIL_OUTPUT_DIR", None)
        else:
            os.environ["RAIL_OUTPUT_DIR"] = self.previous_output
        import shutil
        shutil.rmtree(self.output, ignore_errors=True)

    def test_collect_writes_workbench_cache(self):
        adapter.transport = lambda url, headers, timeout: envelope(aweme())
        result = self.bridge.collect_video_trends({
            "query": "train travel", "region": "GB", "days": 7, "platforms": ["TikTok"], "tiktok_count": 10,
        })
        self.assertNotIn("unit-test-secret-value", json.dumps(result))
        self.assertEqual(result["count"], 1)
        self.assertIsNone(result["items"][0]["metrics"]["shares"])
        saved = json.loads((self.output / "video-trends.json").read_text(encoding="utf-8"))
        row = saved["items"][0]
        self.assertEqual(row["platform"], "TikTok")
        self.assertEqual(row["source"], "TikHub")
        self.assertEqual(row["id"], "tiktok:7300000000000000001")
        self.assertIsNone(row["metrics"]["shares"])
        self.assertEqual(saved["runs"]["TikTok"]["count"], 1)
        import video_trends
        normalized = video_trends.normalize("TikTok", {
            "title": row["title"], "url": row["url"], "author": row["author"],
            "published_at": row["published_at"], "metrics": row["metrics"],
        }, datetime.now(timezone.utc), "TikHub")
        self.assertEqual(normalized["video_id"], "7300000000000000001")

    def test_cache_keeps_only_requested_count_when_tikhub_returns_twelve(self):
        rows = [aweme(aweme_id=f"73000000000000000{index:02d}", desc=f"Train {index}",
                      author={"unique_id": f"rail{index}"},
                      statistics={"play_count": 100 - index, "digg_count": 10 - index}) for index in range(12)]
        adapter.transport = lambda url, headers, timeout: {
            "code": 200, "data": {"search_item_list": [{"aweme_info": row} for row in rows]},
        }
        result = self.bridge.collect_video_trends({
            "query": "train travel", "region": "GB", "days": 7, "platforms": ["TikTok"], "tiktok_count": 5,
        })
        self.assertEqual(len(result["items"]), 5)
        saved = json.loads((self.output / "video-trends.json").read_text(encoding="utf-8"))
        self.assertEqual(len(saved["items"]), 5)
        self.assertEqual(saved["runs"]["TikTok"]["count"], 5)
        self.assertEqual(saved["items"][0]["metrics"]["views"], 100)
        self.assertEqual(saved["items"][0]["metrics"]["likes"], 10)

    def test_second_tiktok_collect_inside_thirty_seconds_does_not_call_again(self):
        calls = []
        adapter.transport = lambda url, headers, timeout: calls.append(url) or envelope(aweme())
        self.bridge.collect_video_trends({"query": "train travel", "platforms": ["TikTok"]})
        with self.assertRaises(self.bridge.ToolError) as caught:
            self.bridge.collect_video_trends({"query": "train travel", "platforms": ["TikTok"]})
        self.assertIn("30 秒", str(caught.exception))
        self.assertEqual(len(calls), 1)

    def test_missing_key_is_recorded_without_a_request(self):
        os.environ.pop("TIKHUB_API_KEY", None)
        calls = []
        adapter.transport = lambda *args, **kwargs: calls.append(True)
        result = self.bridge.collect_video_trends({"query": "train travel", "platforms": ["TikTok"]})
        self.assertFalse(calls)
        tiktok = next(row for row in result["platforms"] if row["name"] == "TikTok")
        self.assertIn("TIKHUB_API_KEY", tiktok["error"])
        self.assertNotIn("unit-test-secret-value", json.dumps(result))


class WindowTest(unittest.TestCase):
    def setUp(self):
        self.original = adapter.transport
        self.original_clock = adapter.clock
        self.env = {key: os.environ.get(key) for key in ("TIKHUB_API_KEY", "TIKHUB_TOP_N")}
        os.environ["TIKHUB_API_KEY"] = "unit-test-secret-value"
        os.environ.pop("TIKHUB_TOP_N", None)
        self.now = datetime(2026, 11, 14, 12, tzinfo=timezone.utc)
        pin_clock(self.now)

    def tearDown(self):
        adapter.transport = self.original
        adapter.clock = self.original_clock
        for key, value in self.env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def at(self, delta: timedelta, index: int, plays: int, desc: str):
        return aweme(
            aweme_id=f"7300000000000000{index:03d}",
            desc=desc,
            create_time=int((self.now + delta).timestamp()),
            author={"unique_id": f"rail{index}"},
            statistics={"play_count": plays, "digg_count": 1},
        )

    def test_drops_outside_window_then_keeps_highest_plays(self):
        calls = []

        def transport(url, headers, timeout):
            calls.append(url)
            return envelope(
                self.at(timedelta(days=-2), 1, 99999, "old but huge"),
                self.at(timedelta(hours=-1), 2, 10, "new small"),
                self.at(timedelta(hours=-3), 3, 5000, "new big"),
                self.at(timedelta(hours=-20), 4, 800, "new mid"),
                self.at(timedelta(minutes=6), 5, 7000, "too far ahead"),
                aweme(aweme_id="7300000000000000006", desc="no time", author={"unique_id": "rail6"},
                      statistics={"play_count": 6000}, create_time=None),
            )

        adapter.transport = transport
        os.environ["TIKHUB_TOP_N"] = "2"
        result = adapter.search("train travel", "GB", 1, 25)
        self.assertEqual([item["title"] for item in result["items"]], ["new big", "new mid"])
        self.assertEqual([item["metrics"]["views"] for item in result["items"]], [5000, 800])
        self.assertEqual(len(calls), 1)
        self.assertIn("count=20", calls[0])
        self.assertIn("publish_time=1", calls[0])
        self.assertNotIn("offset=20", calls[0])

    def test_keeps_the_window_edge_and_five_minute_skew(self):
        adapter.transport = lambda url, headers, timeout: envelope(
            self.at(timedelta(days=-1), 1, 3, "exactly one day"),
            self.at(timedelta(days=-1, seconds=-1), 2, 9, "one second too old"),
            self.at(timedelta(minutes=5), 3, 4, "five minutes ahead"),
            self.at(timedelta(minutes=5, seconds=1), 4, 8, "just past skew"),
        )
        result = adapter.search("train", "GB", 1, 10)
        self.assertEqual([item["title"] for item in result["items"]], ["five minutes ahead", "exactly one day"])

    def test_top_n_never_exceeds_the_requested_limit(self):
        rows = [self.at(timedelta(hours=-1), index, 1000 - index, f"clip {index}") for index in range(12)]
        adapter.transport = lambda url, headers, timeout: envelope(*rows)
        os.environ["TIKHUB_TOP_N"] = "10"
        result = adapter.search("train", "GB", 1, 4)
        self.assertEqual([item["metrics"]["views"] for item in result["items"]], [1000, 999, 998, 997])

    def test_default_top_n_is_ten_when_the_workbench_asks_for_twenty_five(self):
        rows = [self.at(timedelta(hours=-1), index, index, f"clip {index}") for index in range(15)]
        adapter.transport = lambda url, headers, timeout: envelope(*rows)
        result = adapter.search("train", "GB", 7, 25)
        self.assertEqual(len(result["items"]), 10)
        self.assertEqual(result["items"][0]["metrics"]["views"], 14)


class TokenAndHttpsTest(unittest.TestCase):
    def setUp(self):
        self.original = adapter.transport
        self.original_clock = adapter.clock
        self.original_token_file = adapter.token_file_override
        self.env = {
            key: os.environ.get(key)
            for key in ("TIKHUB_API_KEY", "VIDEO_TRENDS_TIKTOK_TOKEN", "TIKHUB_TOP_N")
        }
        os.environ["TIKHUB_API_KEY"] = "unit-test-secret-value"
        os.environ.pop("VIDEO_TRENDS_TIKTOK_TOKEN", None)
        os.environ.pop("TIKHUB_TOP_N", None)
        self.tmpdir = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmpdir.name)
        adapter.token_file_override = self.directory / "missing-token"
        pin_clock(datetime(2025, 1, 1, 12, tzinfo=timezone.utc))
        adapter.transport = lambda url, headers, timeout: envelope(aweme())

    def tearDown(self):
        adapter.transport = self.original
        adapter.clock = self.original_clock
        adapter.token_file_override = self.original_token_file
        for key, value in self.env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmpdir.cleanup()

    def test_local_cert_is_only_for_loopback_and_is_reused(self):
        cert, key = adapter.ensure_local_cert(self.directory)
        again, again_key = adapter.ensure_local_cert(self.directory)
        self.assertEqual(cert.read_bytes(), again.read_bytes())
        self.assertEqual(key.read_bytes(), again_key.read_bytes())
        self.assertEqual(stat.S_IMODE(key.stat().st_mode), 0o600)
        self.assertNotIn(b"PRIVATE KEY", cert.read_bytes())
        text = subprocess.check_output(["openssl", "x509", "-in", str(cert), "-noout", "-text"], text=True)
        san = text.split("Subject Alternative Name", 1)[1]
        self.assertIn("127.0.0.1", san)
        self.assertIn("localhost", san)

    def test_wrong_bearer_is_401_and_does_not_call_tikhub(self):
        calls = []
        adapter.transport = lambda url, headers, timeout: calls.append(url) or envelope(aweme())
        os.environ["VIDEO_TRENDS_TIKTOK_TOKEN"] = "local-test-token"
        server = adapter.build_server("127.0.0.1", 0, self.directory)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        context = ssl.create_default_context(cafile=str(self.directory / "tikhub-local.crt"))
        url = f"https://127.0.0.1:{port}/videos?q=train&region=GB&days=7&limit=25"
        try:
            with self.assertRaises(URLError) as untrusted:
                urlopen(url, timeout=5)
            self.assertIsInstance(untrusted.exception.reason, ssl.SSLError)
            for header in (None, "Bearer wrong-token", "Basic local-test-token"):
                headers = {} if header is None else {"Authorization": header}
                with self.assertRaises(HTTPError) as caught:
                    urlopen(Request(url, headers=headers), timeout=5, context=context)
                self.assertEqual(caught.exception.code, 401)
            with urlopen(Request(url, headers={"Authorization": "Bearer local-test-token"}), timeout=5, context=context) as response:
                body = json.load(response)
            self.assertEqual(body["items"][0]["url"], "https://www.tiktok.com/@rail.notes/video/7300000000000000001")
            self.assertEqual(len(calls), 1)
        finally:
            server.shutdown()
            server.server_close()

    def test_workbench_partner_feed_reads_the_local_https_feed(self):
        studio = Path(os.environ.get("RAIL_STUDIO_ROOT") or "/tmp/refs/rail-content-studio")
        system_cas = Path("/etc/ssl/certs/ca-certificates.crt")
        if not (studio / "video_trends.py").is_file() or not system_cas.is_file():
            self.skipTest("需要工作台克隆和系统 CA 包")
        if str(studio) not in sys.path:
            sys.path.insert(0, str(studio))
        import video_trends
        os.environ["VIDEO_TRENDS_TIKTOK_TOKEN"] = "local-test-token"
        previous_ssl = os.environ.get("SSL_CERT_FILE")
        server = adapter.build_server("127.0.0.1", 0, self.directory)
        port = server.server_address[1]
        bundle = self.directory / "ca-bundle.pem"
        bundle.write_bytes(system_cas.read_bytes() + b"\n" + (self.directory / "tikhub-local.crt").read_bytes())
        os.environ["SSL_CERT_FILE"] = str(bundle)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            now = datetime(2025, 1, 1, 12, tzinfo=timezone.utc)
            rows = video_trends.partner_feed("TikTok", {
                "VIDEO_TRENDS_TIKTOK_FEED_URL": f"https://127.0.0.1:{port}/videos",
                "VIDEO_TRENDS_TIKTOK_TOKEN": "local-test-token",
            }, "train travel", "GB", 7, now)
        finally:
            if previous_ssl is None:
                os.environ.pop("SSL_CERT_FILE", None)
            else:
                os.environ["SSL_CERT_FILE"] = previous_ssl
            server.shutdown()
            server.server_close()
        self.assertEqual(rows[0]["platform"], "TikTok")
        self.assertEqual(rows[0]["metrics"]["views"], 1200)
        self.assertEqual(rows[0]["video_id"], "7300000000000000001")

    def test_serve_reads_token_file_when_env_is_empty(self):
        token_path = self.directory / "tikhub-local.token"
        token_path.write_text("file-token\n", encoding="utf-8")
        adapter.token_file_override = token_path
        adapter._load_token_from_file()
        self.assertEqual(os.environ.get("VIDEO_TRENDS_TIKTOK_TOKEN"), "file-token")


class StartupScriptTest(unittest.TestCase):
    def test_start_script_exports_https_feed_and_drops_the_http_warning(self):
        text = (ROOT / "scripts" / "start.sh").read_text(encoding="utf-8")
        self.assertIn('LOCAL_FEED_URL="https://127.0.0.1:${TIKHUB_ADAPTER_PORT}/videos"', text)
        self.assertIn('VIDEO_TRENDS_TIKTOK_FEED_URL="$LOCAL_FEED_URL"', text)
        self.assertIn("export VIDEO_TRENDS_TIKTOK_FEED_URL", text)
        self.assertIn("export VIDEO_TRENDS_TIKTOK_TOKEN", text)
        self.assertIn("tikhub-local-adapter", text)
        self.assertIn("已接入工作台选题页", text)
        self.assertNotIn("不能把这个 http 地址写入", text)
        self.assertNotIn("工作台只接受 HTTPS 数据地址", text)

    def test_start_script_makes_the_workbench_trust_the_local_feed(self):
        if (ROOT / "config.env").exists():
            self.skipTest("本机 config.env 会改变启动结果")
        if not Path("/etc/ssl/certs/ca-certificates.crt").is_file():
            self.skipTest("这个环境没有系统 CA 包，无法代替 macOS 钥匙串做信任链检查")
        with tempfile.TemporaryDirectory() as tmp:
            studio = Path(tmp) / "studio"
            studio.mkdir()
            (studio / "studio.py").write_text("raise SystemExit\n", encoding="utf-8")
            captured = Path(tmp) / "result.txt"
            stub = Path(tmp) / "dsh"
            stub.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, ssl, time\n"
                "from urllib.error import HTTPError, URLError\n"
                "from urllib.request import Request, urlopen\n"
                "url = os.environ['VIDEO_TRENDS_TIKTOK_FEED_URL'] + '?q=train&region=GB&days=7'\n"
                "ctx = ssl.create_default_context()\n"
                "last = 'adapter did not answer'\n"
                "for _ in range(50):\n"
                "    try:\n"
                "        urlopen(Request(url, headers={'Authorization': 'Bearer wrong'}), timeout=2, context=ctx)\n"
                "        last = 'missing 401'\n"
                "    except HTTPError as exc:\n"
                "        open(os.environ['STUB_RESULT'], 'w', encoding='utf-8').write(json.dumps({\n"
                "            'code': exc.code,\n"
                "            'url': os.environ.get('VIDEO_TRENDS_TIKTOK_FEED_URL'),\n"
                "            'ssl': os.environ.get('SSL_CERT_FILE'),\n"
                "            'token': os.environ.get('VIDEO_TRENDS_TIKTOK_TOKEN', ''),\n"
                "        }))\n"
                "        raise SystemExit(0 if exc.code == 401 else 1)\n"
                "    except URLError as exc:\n"
                "        last = str(exc.reason)\n"
                "        time.sleep(0.1)\n"
                "open(os.environ['STUB_RESULT'], 'w', encoding='utf-8').write(last)\n"
                "raise SystemExit(1)\n",
                encoding="utf-8",
            )
            stub.chmod(0o755)
            env = os.environ.copy()
            for key in ("SSL_CERT_FILE", "VIDEO_TRENDS_TIKTOK_FEED_URL", "VIDEO_TRENDS_TIKTOK_TOKEN", "TIKHUB_TOP_N"):
                env.pop(key, None)
            env.update({
                "RAIL_STUDIO_ROOT": str(studio),
                "DSH_BIN": str(stub),
                "DSH_NO_OPEN": "1",
                "TIKHUB_API_KEY": "unit-test-secret-value",
                "TIKHUB_ADAPTER_PORT": "8799",
                "STUB_RESULT": str(captured),
            })
            for _ in range(2):
                proc = subprocess.Popen(
                    ["bash", str(ROOT / "scripts" / "start.sh")],
                    cwd=ROOT, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    start_new_session=True,
                )
                try:
                    stdout, stderr = proc.communicate(timeout=30)
                finally:
                    try:
                        os.killpg(proc.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(proc.pid, signal.SIGKILL)
                        proc.wait(timeout=5)
                result_text = captured.read_text(encoding="utf-8") if captured.exists() else ""
                log_path = ROOT / ".run" / "tikhub.log"
                log_text = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
                self.assertEqual(proc.returncode, 0, (stdout or "") + (stderr or "") + result_text + log_text)
                saved = json.loads(result_text)
                self.assertEqual(saved["code"], 401)
                self.assertEqual(saved["url"], "https://127.0.0.1:8799/videos")
                token_file = ROOT / ".run" / "tikhub-local.token"
                self.assertEqual(saved["token"], token_file.read_text(encoding="utf-8").strip())
                self.assertIn("ca-bundle.pem", saved["ssl"])
                bundle = Path(saved["ssl"]).read_text(encoding="utf-8")
                self.assertEqual(bundle.count("# tikhub-local-adapter"), 1)
                self.assertGreater(bundle.count("BEGIN CERTIFICATE"), 1)
                self.assertEqual(stat.S_IMODE((ROOT / ".run" / "tikhub-local.key").stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(token_file.stat().st_mode), 0o600)


class SkillTest(unittest.TestCase):
    def test_skill_covers_topic_script_and_facts(self):
        text = (ROOT / ".dsh/skills/rail-overseas-studio/SKILL.md").read_text(encoding="utf-8")
        for phrase in ("近 7 天", "怎么买最便宜", "圣诞", "春节", "暑假", "大家好", "最低价", "claims", "三版"):
            self.assertIn(phrase, text)
