"""MCP 工具定义。人工选用和终审没有可以自动通过的参数。"""

from __future__ import annotations

import mcp_server.studio_bridge as studio

_PHRASE = "必须与用户刚刚原样说出的确认语一致。不要代填。"


def _tool(name: str, description: str, properties: dict, required: list[str] | None = None) -> dict:
    schema: dict = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        schema["required"] = required
    return {"name": name, "description": description, "inputSchema": schema}


def _string(description: str) -> dict:
    return {"type": "string", "description": description}


TOOLS = [
    _tool("studio_status", "查看工作台路径、页面地址、密钥是否已配置（不返回密钥）和最近一次终审记录。无密钥时也可调用。", {}),
    _tool(
        "read_video_hotspots",
        "读取本地视频热点缓存，以及仓库里已经发布的 TikTok 今日灵感。不访问付费采集接口。",
        {},
    ),
    _tool(
        "collect_video_trends",
        "手动采集视频热点。YouTube 走工作台官方接口；TikTok 走 TikHub，按请求计费，每次只发一页。只在用户明确要求采集时调用。不下载视频，不发布。",
        {
            "query": _string("1–100 字搜索词，例如 train travel。"),
            "region": _string("两位大写国家代码，默认 GB。"),
            "days": {"type": "integer", "enum": [1, 7, 30], "description": "近 1、7 或 30 天。"},
            "platforms": {
                "type": "array",
                "items": {"type": "string", "enum": ["YouTube", "TikTok", "Instagram"]},
                "description": "要采集的平台。缺省只采集 YouTube。选 TikTok 时使用 TIKHUB_API_KEY。",
            },
            "tiktok_count": {"type": "integer", "minimum": 1, "maximum": 20, "description": "TikTok 条数，默认 10，最大 20。"},
        },
        ["query"],
    ),
    _tool(
        "read_topic_feed",
        "读取本机 AIHOT 的近期和历史视频条目。只读，不启动采集或模型任务。服务未运行时返回错误说明。",
        {},
    ),
    _tool(
        "generate_scripts",
        "调用工作台生成三版脚本并写入脚本库。不会选用任何一版，也不会开始配音。需要脚本模型密钥。",
        {
            "topic": _string("选题，最多 80 字。"),
            "market": _string("目标市场，默认英国。"),
            "platform": _string("平台，默认 TikTok。"),
            "brief": _string("创作要求，最多 2000 字。可省略。"),
        },
        ["topic"],
    ),
    _tool("list_scripts", "列出脚本库摘要和历史版本入口。不选用脚本。", {"query": _string("按选题名称筛选，可省略。")}),
    _tool(
        "read_script",
        "读取一份脚本及其同选题历史版本。不改变选用状态。",
        {"session_id": _string("脚本 id。省略则读取库里记录的当前一份，仍然不会选用它。")},
    ),
    _tool(
        "rewrite_script",
        "按修改意见调用工作台的 AI 改写，只返回预览。不保存，不选用。需要脚本模型密钥。",
        {
            "session_id": _string("要改写的脚本 id。"),
            "variant": _string("要改写的版本 id，例如 A。"),
            "instruction": _string("修改意见，最多 2000 字。"),
        },
        ["session_id", "variant", "instruction"],
    ),
    _tool(
        "save_script_revision",
        "把用户确认过的修改稿另存为历史版本。保存后选用状态清空，必须重新人工选用才能制作。",
        {
            "session_id": _string("最新一版的脚本 id。"),
            "variant": _string("被修改的版本 id。"),
            "expected_head": _string("该选题最新一版的 id，须与 session_id 相同。"),
            "title": _string("选题名称。省略则沿用原标题。"),
            "action": {"type": "string", "enum": ["手动编辑", "AI 改写"], "description": "写入历史的动作名称。"},
            "content": {"type": "object", "description": "通过工作台校验的一版脚本，含 idea、lines、claims。"},
        },
        ["session_id", "variant", "expected_head", "content"],
    ),
    _tool(
        "choose_script_version",
        "记录用户选定的脚本版本。不能自行挑选版本。" + _PHRASE + f"确认语是「{studio.CHOOSE}」。选定后仍不会制作或发布。",
        {
            "session_id": _string("用户指定的脚本 id。"),
            "variant": _string("用户指定的版本 id。"),
            "human_confirmation": _string(f"用户原话：{studio.CHOOSE}"),
        },
        ["session_id", "variant", "human_confirmation"],
    ),
    _tool(
        "search_materials",
        "按名称、地点和标签搜索本地素材库，规则与工作台素材搜索一致。不访问外网。",
        {
            "query": _string("搜索词，可省略。"),
            "include_rules": {"type": "boolean", "description": "为 true 时附带剪辑规则和来源。"},
        },
    ),
    _tool(
        "produce_rough_cut",
        "按已经人工选用的版本调用 pipeline.py 做配音、素材匹配和剪映草稿。未选用时会拒绝。不会重新生成脚本，也不会发布。"
        + _PHRASE + f"确认语是「{studio.PRODUCE}」。",
        {
            "session_id": _string("已选用的脚本 id。"),
            "variant": _string("必须与已选用版本相同。"),
            "human_confirmation": _string(f"用户原话：{studio.PRODUCE}"),
        },
        ["session_id", "human_confirmation"],
    ),
    _tool(
        "write_jianying_draft",
        "把当前镜头替换写回剪映工程，调用工作台的 redraft。演示数据会拒绝。不打开剪映，不发布。"
        + _PHRASE + f"确认语是「{studio.WRITE_DRAFT}」。",
        {
            "swaps": {"type": "object", "description": "句子序号到镜头序号再到素材编号。空对象表示按当前镜头重写。"},
            "human_confirmation": _string(f"用户原话：{studio.WRITE_DRAFT}"),
        },
        ["human_confirmation"],
    ),
    _tool(
        "export_roughcut",
        "用工作台 roughcut_export 导出当前预览的粗剪 MP4。不调用模型，不发布。"
        + _PHRASE + f"确认语是「{studio.EXPORT}」。",
        {
            "run_id": _string("要导出的 run_id，须与当前粗剪一致。"),
            "swaps": {"type": "object", "description": "尚未写入剪映的镜头替换。可省略。"},
            "human_confirmation": _string(f"用户原话：{studio.EXPORT}"),
        },
        ["human_confirmation"],
    ),
    _tool(
        "review_finished_video",
        "调用工作台 video_review.review_video 评测成片。需要评测模型密钥。评测通过不等于终审，不会解锁发布。",
        {"path": _string("本机 MP4、MOV 或 M4V 的绝对路径。")},
        ["path"],
    ),
    _tool(
        "read_review",
        "读取数据复盘结果。默认读已保存的 review.json，不调用模型。数据是模拟记录，不是平台实时数据。",
        {
            "refresh": {"type": "boolean", "description": "为 true 时按工作台规则重算并写回 review.json。"},
            "use_ai": {"type": "boolean", "description": "重算时是否生成复测建议。缺密钥会拒绝，规则复盘不受影响。"},
        },
    ),
    _tool(
        "read_production_result",
        "读取最近一次粗剪摘要。仓库演示数据会标明 preview_only，不能当成可写回的工程。",
        {},
    ),
    _tool(
        "record_final_review",
        "记录用户作出的终审。不能代替工作台页面上的终审登记，不会解锁发布登记，也没有任何发布动作。"
        + _PHRASE + f"确认语是「{studio.FINAL_REVIEW}」。",
        {
            "reviewer": _string("终审人，最多 40 字。"),
            "decision": {"type": "string", "enum": ["通过", "退回修改"]},
            "comment": _string("终审意见，最多 2000 字。"),
            "human_confirmation": _string(f"用户原话：{studio.FINAL_REVIEW}"),
        },
        ["reviewer", "decision", "comment", "human_confirmation"],
    ),
]

HANDLERS = {
    "studio_status": studio.studio_status,
    "read_video_hotspots": studio.read_video_hotspots,
    "collect_video_trends": studio.collect_video_trends,
    "read_topic_feed": studio.read_topic_feed,
    "generate_scripts": studio.generate_scripts,
    "list_scripts": studio.list_scripts,
    "read_script": studio.read_script,
    "rewrite_script": studio.rewrite_script,
    "save_script_revision": studio.save_script_revision,
    "choose_script_version": studio.choose_script_version,
    "search_materials": studio.search_materials,
    "produce_rough_cut": studio.produce_rough_cut,
    "write_jianying_draft": studio.write_jianying_draft,
    "export_roughcut": studio.export_roughcut,
    "review_finished_video": studio.review_finished_video,
    "read_review": studio.read_review,
    "read_production_result": studio.read_production_result,
    "record_final_review": studio.record_final_review,
}


def list_tools() -> list[dict]:
    return TOOLS


def call_tool(name: str, arguments: dict | None) -> dict:
    handler = HANDLERS.get(name)
    if handler is None:
        return {"content": [{"type": "text", "text": f"未知工具：{name}"}], "isError": True}
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return {"content": [{"type": "text", "text": "工具参数必须是 JSON 对象。"}], "isError": True}
    try:
        payload = handler(arguments)
    except studio.ToolError as exc:
        return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
    except Exception as exc:
        sys_text = "工具内部错误：" + studio.redact(exc)
        return {"content": [{"type": "text", "text": sys_text}], "isError": True}
    return {
        "content": [{"type": "text", "text": _dump(payload)}],
        "isError": False,
    }


def _dump(payload: dict) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False)
