"""MCP 协议和工具门槛。模型调用被替换，不访问付费接口。"""

from __future__ import annotations

import json
import os
import select
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STUDIO = Path(os.environ.get("RAIL_STUDIO_ROOT") or "/tmp/refs/rail-content-studio")
SECRET_KEYS = (
    "DEEPSEEK_API_KEY", "SCRIPT_API_KEY", "LLM_API_KEY", "VIDEO_API_KEY",
    "MINIMAX_API_KEY", "YOUTUBE_API_KEY", "TIKHUB_API_KEY",
)
OUTPUT = Path(tempfile.mkdtemp(prefix="rail-mcp-test-"))
SAVED_ENV: dict[str, str] = {}


def setUpModule():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    os.environ["RAIL_STUDIO_ROOT"] = str(STUDIO)
    os.environ["RAIL_OUTPUT_DIR"] = str(OUTPUT)
    os.environ["AIHOT_API_URL"] = "invalid"
    os.environ["AIHOT_SITE_URL"] = "invalid"
    for key in SECRET_KEYS:
        if key in os.environ:
            SAVED_ENV[key] = os.environ.pop(key)


def tearDownModule():
    for key, value in SAVED_ENV.items():
        os.environ[key] = value
    shutil.rmtree(OUTPUT, ignore_errors=True)


def require_studio():
    if not (STUDIO / "studio.py").is_file():
        raise unittest.SkipTest(f"需要先克隆工作台：{STUDIO}")


def script_lines(tag="train"):
    rows = [
        ("Sleep the night from London to Scotland.", "从伦敦睡一夜到苏格兰。", "开头 · 冷知识", "LONDON TO SCOTLAND"),
        ("The evening train leaves London Euston.", "傍晚列车离开伦敦尤斯顿。", "铺垫 · 路线", ""),
        ("Wake beside lochs and green hills.", "在湖和青山边醒来。", "画面 · 情绪", ""),
        ("Split the ticket and save up to 30%.", "分段买票，最多省 30%。", "利益点 · 省钱", "SAVE UP TO 30%"),
        ("Search UK20 in the app before you go.", "出发前在 App 搜 UK20。", "结尾 · 搜码钩子", "SEARCH UK20"),
    ]
    lines = []
    for en, zh, role, on_screen in rows:
        if len(en.split()) > 14:
            raise AssertionError(en)
        item = {"en": en, "zh": zh, "role": role, "tags": [tag], "places": []}
        if on_screen:
            item["on_screen"] = on_screen
        lines.append(item)
    return lines


def three_variants():
    variants = []
    for index, idea in enumerate(("冷知识", "路线", "画面")):
        variants.append({"id": "ABC"[index], "idea": idea, "lines": script_lines(), "claims": []})
    return variants


class Server:
    def __init__(self, env):
        self.stderr_path = OUTPUT / f"stderr-{os.getpid()}-{id(self)}.log"
        self.stderr = open(self.stderr_path, "wb")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "mcp_server"],
            cwd=ROOT,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.stderr,
        )

    def request(self, method, params=None, message_id=1):
        message = {"jsonrpc": "2.0", "id": message_id, "method": method}
        if params is not None:
            message["params"] = params
        assert self.proc.stdin is not None and self.proc.stdout is not None
        self.proc.stdin.write(json.dumps(message, ensure_ascii=False).encode() + b"\n")
        self.proc.stdin.flush()
        ready, _, _ = select.select([self.proc.stdout], [], [], 15)
        if not ready:
            self.stderr.flush()
            detail = self.stderr_path.read_text(encoding="utf-8", errors="replace")[-1000:]
            raise AssertionError(f"{method} 没有在 15 秒内返回。stderr:\n{detail}")
        line = self.proc.stdout.readline()
        if not line:
            raise AssertionError(f"{method} 的 stdout 已关闭")
        return json.loads(line)

    def notify(self, method):
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method}).encode() + b"\n")
        self.proc.stdin.flush()

    def close(self):
        if self.proc.stdin:
            self.proc.stdin.close()
        if self.proc.stdout:
            self.proc.stdout.close()
        self.proc.wait(timeout=5)
        self.stderr.close()


def base_env(**extra):
    env = os.environ.copy()
    env["RAIL_STUDIO_ROOT"] = str(STUDIO)
    env["RAIL_OUTPUT_DIR"] = str(OUTPUT)
    env["AIHOT_API_URL"] = "invalid"
    env["AIHOT_SITE_URL"] = "invalid"
    for key in SECRET_KEYS:
        env.pop(key, None)
    env.update(extra)
    return env


class ProtocolTest(unittest.TestCase):
    def test_lists_tools_without_secrets_and_survives_missing_root(self):
        missing = Server(base_env(RAIL_STUDIO_ROOT=str(OUTPUT / "missing-studio")))
        try:
            init = missing.request("initialize", {
                "protocolVersion": "2026-07-28",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            })
            self.assertEqual(init["result"]["protocolVersion"], "2026-07-28")
            self.assertIn("确认选用此版", init["result"]["instructions"])
            self.assertNotIn("resources", init["result"]["capabilities"])
            missing.notify("notifications/initialized")
            listed = missing.request("tools/list", {}, 2)
            names = [item["name"] for item in listed["result"]["tools"]]
            self.assertIn("studio_status", names)
            self.assertIn("choose_script_version", names)
            self.assertNotIn("publish", names)
            self.assertFalse(any(name.startswith("publish") for name in names))
            status = missing.request("tools/call", {"name": "studio_status", "arguments": {}}, 3)
            body = json.loads(status["result"]["content"][0]["text"])
            self.assertFalse(status["result"]["isError"])
            self.assertFalse(body["studio_root_exists"])
            ping = missing.request("ping", {}, 4)
            self.assertEqual(ping["result"], {})
        finally:
            missing.close()

    def test_real_studio_status_hides_key(self):
        require_studio()
        secret = "unit-test-secret-value"
        proc = Server(base_env(DEEPSEEK_API_KEY=secret))
        try:
            proc.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}})
            proc.notify("notifications/initialized")
            response = proc.request("tools/call", {"name": "studio_status", "arguments": {}}, 2)
        finally:
            proc.close()
        raw = json.dumps(response, ensure_ascii=False)
        self.assertNotIn(secret, raw)
        body = json.loads(response["result"]["content"][0]["text"])
        self.assertTrue(body["keys"]["script"])
        self.assertNotIn("api_key", raw)


class StudioToolTest(unittest.TestCase):
    def setUp(self):
        require_studio()
        import mcp_server.studio_bridge as bridge
        self.bridge = bridge
        self.bridge.ensure_studio()

    def test_read_only_tools_without_key(self):
        hot = self.bridge.read_video_hotspots({})
        self.assertGreater(hot["daily_inspiration"]["count"], 0)
        self.assertIn("不会启动付费采集", hot["note"])
        found = self.bridge.search_materials({"query": "train"})
        self.assertGreater(found["count"], 0)
        review = self.bridge.read_review({})
        self.assertEqual(review["source"], "review.json")
        self.assertIn("模拟", review["note"])
        feed = self.bridge.read_topic_feed({})
        self.assertIn("config", feed["feed"]["errors"])
        with self.assertRaises(self.bridge.ToolError) as caught:
            self.bridge.collect_video_trends({"query": ""})
        self.assertIn("搜索", str(caught.exception))
        with self.assertRaises(self.bridge.ToolError) as review_error:
            self.bridge.review_finished_video({"path": "/tmp/not-a-video.mp4"})
        self.assertIn("密钥", str(review_error.exception))
        result = self.bridge.read_production_result({})
        if not (STUDIO / "output" / "result.json").is_file():
            self.assertTrue(result["result"]["preview_only"])

    def test_generate_rewrite_and_human_gates(self):
        import llm
        os.environ["DEEPSEEK_API_KEY"] = "unit-test-secret-value"
        usage = {"provider": "mock", "model": "mock", "total_tokens": 12, "seconds": 0.1}

        def fake_json(system, user, temperature=0.9):
            payload = json.loads(user)
            if "原稿" in payload:
                tag = payload["素材词表"]["tags"][0]
                return {"variant": {"idea": "改写预览", "lines": script_lines(tag), "claims": []}}, usage
            return {"variants": three_variants()}, usage

        original = llm.script_json
        llm.script_json = fake_json
        original_run = self.bridge.subprocess.run
        try:
            created = self.bridge.generate_scripts({"topic": "伦敦到苏格兰的夜车", "brief": "保留事实库里的优惠"})
            session = created["session"]
            self.assertIsNone(session["scripts"]["chosen"])
            self.assertEqual([item["id"] for item in session["scripts"]["variants"]], ["A", "B", "C"])
            before = len(self.bridge.list_scripts({})["sessions"])
            preview = self.bridge.rewrite_script({
                "session_id": session["id"], "variant": "A", "instruction": "把第一句换个说法",
            })
            self.assertFalse(preview["saved"])
            self.assertEqual(len(self.bridge.list_scripts({})["sessions"]), before)
            with self.assertRaises(self.bridge.ToolError):
                self.bridge.choose_script_version({
                    "session_id": session["id"], "variant": "A", "human_confirmation": "选用 A",
                })
            self.assertIsNone(self.bridge.read_script({"session_id": session["id"]})["session"]["scripts"]["chosen"])
            chosen = self.bridge.choose_script_version({
                "session_id": session["id"], "variant": "B", "human_confirmation": self.bridge.CHOOSE,
            })
            self.assertEqual(chosen["chosen"], "B")
            saved = self.bridge.save_script_revision({
                "session_id": session["id"],
                "variant": "A",
                "expected_head": session["id"],
                "action": "AI 改写",
                "content": preview["preview"]["variant"],
            })
            self.assertIsNone(saved["chosen"])
            self.assertGreater(saved["revision"], 1)
            with self.assertRaises(self.bridge.ToolError) as produce_blocked:
                self.bridge.produce_rough_cut({
                    "session_id": saved["session_id"], "human_confirmation": self.bridge.PRODUCE,
                })
            self.assertIn("尚未人工选用", str(produce_blocked.exception))
            self.bridge.choose_script_version({
                "session_id": saved["session_id"], "variant": "A", "human_confirmation": self.bridge.CHOOSE,
            })
            calls = {}

            def fake_run(command, cwd, capture_output, text, timeout):
                calls["command"] = command
                return subprocess.CompletedProcess(command, 0, stdout="done\n", stderr="")

            self.bridge.subprocess.run = fake_run
            with self.assertRaises(self.bridge.ToolError) as mismatch:
                self.bridge.produce_rough_cut({
                    "session_id": saved["session_id"],
                    "variant": "B",
                    "human_confirmation": self.bridge.PRODUCE,
                })
            self.assertIn("不能", str(mismatch.exception))
            self.assertFalse(calls)
        finally:
            llm.script_json = original
            self.bridge.subprocess.run = original_run
            os.environ.pop("DEEPSEEK_API_KEY", None)

    def test_produce_uses_chosen_variant_only(self):
        import llm
        os.environ["DEEPSEEK_API_KEY"] = "unit-test-secret-value"
        usage = {"provider": "mock", "model": "mock", "total_tokens": 4, "seconds": 0.1}
        original = llm.script_json
        original_run = self.bridge.subprocess.run
        llm.script_json = lambda system, user, temperature=0.9: ({"variants": three_variants()}, usage)
        try:
            created = self.bridge.generate_scripts({"topic": "巴黎到阿姆斯特丹的火车"})
            session_id = created["session"]["id"]
            self.bridge.choose_script_version({
                "session_id": session_id, "variant": "A", "human_confirmation": self.bridge.CHOOSE,
            })
            calls = {}

            def fake_run(command, cwd, capture_output, text, timeout):
                calls["command"] = list(command)
                calls["cwd"] = cwd
                return subprocess.CompletedProcess(command, 0, stdout="done\n", stderr="")

            self.bridge.subprocess.run = fake_run
            result = self.bridge.produce_rough_cut({
                "session_id": session_id, "human_confirmation": self.bridge.PRODUCE,
            })
            self.assertTrue(result["ok"])
            self.assertEqual(result["chosen"], "A")
            variant_at = calls["command"].index("--variant")
            self.assertEqual(calls["command"][variant_at + 1], "A")
            self.assertEqual(calls["cwd"], str(STUDIO))
            self.assertNotIn("unit-test-secret-value", json.dumps(result, ensure_ascii=False))
            self.assertNotIn("DEEPSEEK_API_KEY=", json.dumps(calls["command"]))
        finally:
            llm.script_json = original
            self.bridge.subprocess.run = original_run
            os.environ.pop("DEEPSEEK_API_KEY", None)

    def test_final_review_does_not_publish_or_unlock(self):
        with self.assertRaises(self.bridge.ToolError):
            self.bridge.record_final_review({
                "reviewer": "周舟", "decision": "通过", "comment": "可以发", "human_confirmation": "通过",
            })
        self.assertFalse((OUTPUT / "final-review.json").exists())
        recorded = self.bridge.record_final_review({
            "reviewer": "周舟",
            "decision": "退回修改",
            "comment": "第一句还有价格。",
            "human_confirmation": self.bridge.FINAL_REVIEW,
        })
        self.assertFalse(recorded["record"]["publishes"])
        self.assertFalse(recorded["record"]["page_publish_unlocked"])
        self.assertIn("不能进入发布", recorded["next"])
        self.assertTrue(str(recorded["path"]).startswith(str(OUTPUT)))

    def test_draft_export_refuse_demo(self):
        if (STUDIO / "output" / "result.json").is_file():
            self.skipTest("工作台已有本机粗剪，不覆盖它来做拒绝测试")
        with self.assertRaises(self.bridge.ToolError) as draft_error:
            self.bridge.write_jianying_draft({"human_confirmation": self.bridge.WRITE_DRAFT, "swaps": {}})
        self.assertIn("演示", str(draft_error.exception))
        with self.assertRaises(self.bridge.ToolError) as export_error:
            self.bridge.export_roughcut({"human_confirmation": self.bridge.EXPORT})
        self.assertIn("不能导出", str(export_error.exception))


class ProjectFileTest(unittest.TestCase):
    def test_skill_and_patch_keep_human_gates(self):
        import mcp_server.studio_bridge as bridge
        import mcp_server.server as server
        skill = (ROOT / ".dsh/skills/rail-overseas-studio/SKILL.md").read_text(encoding="utf-8")
        text = server.instructions()
        for phrase in bridge.PHRASES:
            self.assertIn(phrase, skill)
            self.assertIn(phrase, text)
        self.assertIn("name: rail-overseas-studio", skill)
        for relative in ("harness/rail-studio.cordis.yml", "harness/plugins/rail-studio-sidebar/cordis.patch.yml"):
            patch = (ROOT / relative).read_text(encoding="utf-8")
            self.assertIn("serverName: rail_studio", patch)
            self.assertIn("transport: stdio", patch)
            self.assertIn("@deepseek-ai/dsh-mcp-client", patch)
            self.assertIn("ui-sidebar-browser", patch)
            self.assertIn("disabled: false", patch)
            self.assertIn("process.env.DEEPSEEK_API_KEY", patch)
            self.assertIn("process.env.TIKHUB_API_KEY", patch)
            self.assertNotIn("sk-", patch)
        self.assertIn("name: ./plugins/rail-studio-sidebar/index.js", (ROOT / "harness/rail-studio.cordis.yml").read_text(encoding="utf-8"))
        self.assertIn("openTab('browser'", (ROOT / "harness/plugins/rail-studio-sidebar/client.js").read_text(encoding="utf-8"))
        names = [item["name"] for item in __import__("mcp_server.tools", fromlist=["list_tools"]).list_tools()]
        self.assertEqual(len(names), len(set(names)))
        self.assertIn("record_final_review", names)
        self.assertNotIn("publish_video", names)


if __name__ == "__main__":
    unittest.main()
