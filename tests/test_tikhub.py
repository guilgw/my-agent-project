"""TikHub 适配只用 mock 响应，不请求 api.tikhub.io。"""

from __future__ import annotations

import json
import os
import sys
import threading
import unittest
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

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


class MappingTest(unittest.TestCase):
    def setUp(self):
        self.original = adapter.transport
        os.environ["TIKHUB_API_KEY"] = "unit-test-secret-value"

    def tearDown(self):
        adapter.transport = self.original
        os.environ.pop("TIKHUB_API_KEY", None)

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
        self.assertNotIn("unit-test-secret-value", calls["url"])
        self.assertEqual(calls["timeout"], 15)

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
        for part in ("keyword=train+travel", "count=5", "offset=0", "sort_type=1", "publish_time=7", "region=GB"):
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


class SkillTest(unittest.TestCase):
    def test_skill_covers_topic_script_and_facts(self):
        text = (ROOT / ".dsh/skills/rail-overseas-studio/SKILL.md").read_text(encoding="utf-8")
        for phrase in ("近 7 天", "怎么买最便宜", "圣诞", "春节", "暑假", "大家好", "最低价", "claims", "三版"):
            self.assertIn(phrase, text)
