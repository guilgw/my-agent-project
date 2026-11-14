"""stdio MCP 服务。日志只写 stderr，stdout 只输出 JSON-RPC。"""

from __future__ import annotations

import json
import sys

from mcp_server import __version__
from mcp_server.tools import call_tool, list_tools
import mcp_server.studio_bridge as studio

SUPPORTED_PROTOCOLS = (
    "2024-11-05",
    "2025-03-26",
    "2025-06-18",
    "2025-11-25",
    "2026-07-28",
)
LATEST_PROTOCOL = SUPPORTED_PROTOCOLS[-1]


def instructions() -> str:
    url = studio.studio_url()
    phrases = "、".join(f"「{item}」" for item in studio.PHRASES)
    return f"""你是出海火车票短视频团队的制作助手。工具来自本机「轨道」工作台，业务规则以工作台代码为准，不要另写一套脚本、召回或评分规则。

工作台页面：{url}
在 DeepSeek Harness 里，工具名带前缀 mcp__rail_studio__。流程是：看热点和复盘，与用户确定选题；generate_scripts 产出三版并展示；用户指定版本且原样说出确认语后才 choose_script_version；用户再说确认语后才 produce_rough_cut；换镜头、导出粗剪同样要对应确认语；review_finished_video 只是评测。评测后如需修改，退回剪映或脚本，不能发布。终审由人给出审稿人、结论和意见，并说出确认语后才可 record_final_review。该记录不会打开页面上的发布登记。没有发布工具。运营到 TikTok 等平台手动发布。

确认语必须逐字匹配：{phrases}。用户没说出来就不要调用对应工具，也不要把确认语写进参数。无密钥时说明只读工具可用，不要编造脚本、评测或采集结果。事实以工作台事实库为准，时刻、价格和班次标成待核对。复盘数据是模拟记录。

更完整的岗位流程在技能 rail-overseas-studio。请先加载该技能再执行制作。"""


def _write(message: dict) -> None:
    sys.stdout.buffer.write(json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()


def _log(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def _result(message_id, result: dict) -> None:
    _write({"jsonrpc": "2.0", "id": message_id, "result": result})


def _error(message_id, code: int, message: str) -> None:
    _write({"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}})


def handle(message: dict) -> None:
    method = message.get("method")
    message_id = message.get("id", None)
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    if not isinstance(method, str):
        if message_id is not None:
            _error(message_id, -32600, "Invalid Request")
        return
    if method == "initialize":
        requested = params.get("protocolVersion")
        version = requested if requested in SUPPORTED_PROTOCOLS else LATEST_PROTOCOL
        _result(message_id, {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "rail-studio", "version": __version__},
            "instructions": instructions(),
        })
        return
    if method in ("notifications/initialized", "notifications/cancelled"):
        return
    if method == "ping":
        _result(message_id, {})
        return
    if method == "tools/list":
        if params.get("cursor"):
            _result(message_id, {"tools": []})
        else:
            _result(message_id, {"tools": list_tools()})
        return
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        if not isinstance(name, str):
            _error(message_id, -32602, "Invalid params")
            return
        _result(message_id, call_tool(name, arguments))
        return
    if method == "resources/list":
        _result(message_id, {"resources": []})
        return
    if method == "resources/templates/list":
        _result(message_id, {"resourceTemplates": []})
        return
    if method == "prompts/list":
        _result(message_id, {"prompts": []})
        return
    if method == "logging/setLevel":
        _result(message_id, {})
        return
    if message_id is None:
        return
    _error(message_id, -32601, f"Method not found: {method}")


def main() -> None:
    for line in sys.stdin.buffer:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            message = json.loads(stripped)
        except json.JSONDecodeError:
            _error(None, -32700, "Parse error")
            continue
        if not isinstance(message, dict):
            _error(None, -32600, "Invalid Request")
            continue
        try:
            handle(message)
        except Exception as exc:
            _log("rail-studio mcp: " + studio.redact(exc))
            if message.get("id") is not None:
                _error(message.get("id"), -32603, "Internal error")


if __name__ == "__main__":
    main()
