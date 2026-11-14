"""调用已克隆的轨道工作台，不重写选题、脚本、粗剪和评测规则。"""

from __future__ import annotations

import importlib
import json
import os
import re
import subprocess
import sys
import threading
from typing import Any

CHOOSE = "确认选用此版"
PRODUCE = "确认开始制作"
WRITE_DRAFT = "确认写入剪映"
EXPORT = "确认导出粗剪"
FINAL_REVIEW = "确认终审"

PHRASES = (CHOOSE, PRODUCE, WRITE_DRAFT, EXPORT, FINAL_REVIEW)


class ToolError(Exception):
    """返回给模型的可操作错误。不含密钥。"""


_MUTATION = threading.Lock()
_SECRET = re.compile(r"(sk-[A-Za-z0-9_\-]{8,}|Bearer\s+\S{8,})", re.I)


def studio_root() -> str:
    raw = os.environ.get("RAIL_STUDIO_ROOT") or os.path.join(
        os.path.expanduser("~"), "Documents", "loop", "rail-content-studio"
    )
    return os.path.abspath(os.path.expanduser(raw))


def output_dir() -> str:
    raw = os.environ.get("RAIL_OUTPUT_DIR")
    if raw and raw.strip():
        return os.path.abspath(os.path.expanduser(raw))
    return os.path.join(studio_root(), "output")


def studio_url() -> str:
    explicit = (os.environ.get("RAIL_STUDIO_URL") or "").strip()
    if explicit:
        return explicit
    port = (os.environ.get("RAIL_STUDIO_PORT") or "18770").strip() or "18770"
    return f"http://127.0.0.1:{port}"


def draft_root() -> str:
    raw = (os.environ.get("RAIL_DRAFT_ROOT") or "").strip()
    return os.path.abspath(os.path.expanduser(raw)) if raw else ""


def redact(value: object) -> str:
    text = _SECRET.sub("[redacted]", str(value))
    for key, env_value in os.environ.items():
        if env_value and len(env_value) >= 8 and re.search(r"KEY|TOKEN|SECRET|PASSWORD", key, re.I):
            text = text.replace(env_value, "[redacted]")
    return text[:2000]


def ensure_studio() -> str:
    root = studio_root()
    if not os.path.isfile(os.path.join(root, "studio.py")):
        raise ToolError(f"找不到工作台：{root}。请设置 RAIL_STUDIO_ROOT 指向 rail-content-studio 克隆。")
    if root not in sys.path:
        sys.path.insert(0, root)
    return root


def load(name: str) -> Any:
    ensure_studio()
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as exc:
        missing = exc.name or name
        raise ToolError(
            f"无法导入工作台模块 {name}（缺少 {missing}）。"
            "请在工作台目录安装 requirements.txt，并用该虚拟环境的 Python 启动本服务。"
        ) from exc


def _mutate(action):
    if not _MUTATION.acquire(blocking=False):
        raise ToolError("已有写入任务正在进行，请等它结束后再试。")
    try:
        return action()
    finally:
        _MUTATION.release()


def _call(action):
    try:
        return action()
    except ToolError:
        raise
    except (ValueError, RuntimeError, OSError) as exc:
        raise ToolError(redact(exc)) from exc
    except Exception as exc:
        raise ToolError("工作台调用失败：" + redact(exc)) from exc


def require_phrase(args: dict, expected: str) -> None:
    given = args.get("human_confirmation")
    if given != expected:
        raise ToolError(
            f"这一步必须由人确认。请让用户在对话里原样输入「{expected}」。"
            "不要代填、不要翻译、不要省略。未确认时工作台状态保持不变。"
        )


def _text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ToolError(f"{label}必须是字符串。")
    return value


def _session(library: dict, session_id: str | None) -> dict:
    sessions = library.get("sessions") or []
    target = session_id or library.get("current")
    if not target:
        raise ToolError("脚本库是空的。")
    found = next((item for item in sessions if item.get("id") == target), None)
    if found is None:
        raise ToolError("找不到这份脚本，请先 list_scripts。")
    return found


def _history(library: dict, session: dict) -> list[dict]:
    project = session.get("project_id") or session["id"]
    rows = []
    for item in library.get("sessions") or []:
        if (item.get("project_id") or item.get("id")) != project:
            continue
        rows.append({
            "id": item.get("id"),
            "project_id": item.get("project_id"),
            "revision": item.get("revision"),
            "action": item.get("action"),
            "title": item.get("title"),
            "created_at": item.get("created_at"),
            "chosen": (item.get("scripts") or {}).get("chosen"),
        })
    return rows


def _summary_result(result: dict) -> dict:
    scripts = result.get("scripts") or {}
    segments = result.get("segments") or []
    return {
        "run_id": result.get("run_id"),
        "title": result.get("title"),
        "country": result.get("country"),
        "platform": result.get("platform"),
        "total_s": result.get("total_s"),
        "preview_only": bool(result.get("preview_only")),
        "mode": result.get("mode"),
        "chosen": scripts.get("chosen"),
        "draft": result.get("draft"),
        "score": result.get("score"),
        "segment_count": len(segments),
        "rules": [
            {"id": row.get("id"), "name": row.get("name"), "ok": row.get("ok"), "msg": row.get("msg")}
            for row in (result.get("rules") or [])
        ],
        "log": (result.get("log") or [])[-15:],
    }


def _hot_item(item: dict) -> dict:
    title = str(item.get("title") or "")
    return {
        "id": item.get("id"),
        "platform": item.get("platform"),
        "title": title[:180],
        "author": item.get("author"),
        "original": item.get("original") or item.get("url"),
        "published_at": item.get("published_at"),
        "metrics": item.get("metrics"),
    }


def studio_status(_args: dict | None = None) -> dict:
    root = studio_root()
    exists = os.path.isfile(os.path.join(root, "studio.py"))
    payload: dict[str, Any] = {
        "studio_root": root,
        "studio_root_exists": exists,
        "output_dir": output_dir(),
        "studio_url": studio_url(),
        "draft_root": draft_root() or None,
        "keys": None,
        "models": None,
        "has_local_result": False,
        "final_review": _read_final_review_file(),
    }
    if not exists:
        payload["note"] = "工作台目录不存在。本服务已启动，但工具还不能读取工作台。"
        return payload
    llm = load("llm")
    trends = load("video_trends").config()
    payload["keys"] = {
        "script": bool(llm.model_config("script").get("api_key")),
        "video": bool(llm.model_config("video").get("api_key")),
        "minimax": bool(llm.has("MINIMAX_API_KEY")),
        "youtube": bool(trends.get("YOUTUBE_API_KEY")),
        "tiktok_feed": bool(trends.get("VIDEO_TRENDS_TIKTOK_FEED_URL")),
        "tikhub": bool(os.environ.get("TIKHUB_API_KEY")),
        "instagram_feed": bool(trends.get("VIDEO_TRENDS_INSTAGRAM_FEED_URL")),
    }
    payload["models"] = llm.models_in_use()
    payload["has_local_result"] = os.path.isfile(os.path.join(studio_root(), "output", "result.json"))
    payload["note"] = "密钥只报告是否已配置，不会返回密钥本身。未配置时只读工具仍可使用。"
    return payload


def read_video_hotspots(_args: dict | None = None) -> dict:
    def run():
        cached = load("video_trends").read(output_dir())
        feed_path = os.path.join(studio_root(), "data", "tiktok-feed.json")
        inspiration = None
        if os.path.isfile(feed_path):
            with open(feed_path, encoding="utf-8") as handle:
                raw = json.load(handle)
            items = raw.get("items") or []
            inspiration = {
                "updated_at": raw.get("updated_at"),
                "schedule": raw.get("schedule"),
                "queries": raw.get("queries"),
                "region": raw.get("region"),
                "count": len(items),
                "items": [_hot_item(item) for item in items[:15] if isinstance(item, dict)],
            }
        rows = [item for item in (cached.get("items") or []) if isinstance(item, dict)]
        return {
            "trends": {
                "platforms": cached.get("platforms"),
                "last_request": cached.get("last_request"),
                "checked_at": cached.get("checked_at"),
                "count": len(rows),
                "items": [_hot_item(item) for item in rows[:20]],
            },
            "daily_inspiration": inspiration,
            "note": "只读本地采集缓存和仓库里已发布的今日灵感。刷新或读取不会启动付费采集。",
        }

    return _call(run)


def collect_video_trends(args: dict) -> dict:
    def run():
        days = args.get("days", 7)
        if isinstance(days, bool) or not isinstance(days, int):
            raise ToolError("days 只能是 1、7 或 30。")
        platforms = args.get("platforms") or ["YouTube"]
        if not isinstance(platforms, list) or not platforms:
            raise ToolError("请选择支持的平台。")
        count = args.get("tiktok_count", 10)
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 20:
            raise ToolError("tiktok_count 只能是 1 到 20。TikTok 每次只请求一页，避免重复计费。")
        body = {
            "query": args.get("query"),
            "region": args.get("region") or "GB",
            "days": days,
            "platforms": [name for name in platforms if name != "TikTok"],
        }
        result = None
        if body["platforms"]:
            result = load("video_trends").collect(output_dir(), body)
        if "TikTok" in platforms:
            result = _merge_tiktok(body["query"], body["region"], days, count, skip_cooldown=result is not None)
        items = [item for item in (result.get("items") or []) if isinstance(item, dict)]
        errors = [f"{row.get('name')}：{row.get('error')}" for row in (result.get("platforms") or []) if row.get("error")]
        note = "YouTube 走工作台官方接口。TikTok 走 TikHub，每次只发 1 个请求并按次计费；先丢掉不在天数窗口内的视频，再按播放量保留前若干条（默认 10，不超过 tiktok_count）。失败的平台保留旧数据。不下载视频，也不发布。"
        if errors:
            note += " " + "；".join(errors)
        return {
            "platforms": result.get("platforms"),
            "last_request": result.get("last_request"),
            "count": len(items),
            "items": [_hot_item(item) for item in items[:20]],
            "note": note,
        }

    return _mutate(lambda: _call(run))


def _merge_tiktok(query, region, days, count, skip_cooldown: bool) -> dict:
    """写入工作台 output/video-trends.json，记录格式与 video_trends.collect 相同。"""
    from datetime import datetime, timezone

    import tikhub_feed.adapter as tikhub

    trends = load("video_trends")
    out = output_dir()
    if not trends.LOCK.acquire(blocking=False):
        raise ToolError("已有视频采集进行中，请稍后刷新。")
    try:
        try:
            tikhub._query(query if isinstance(query, str) else "", region, days, count)
        except tikhub.TikHubError as exc:
            raise ToolError(str(exc)) from exc
        now = datetime.now(timezone.utc)
        data = trends.load(out, now)
        last = data.get("last_request") or {}
        last_at = trends.date(last.get("at"))
        if not skip_cooldown and last_at and (now - last_at).total_seconds() < 30:
            raise ToolError("请间隔 30 秒后再次采集，刷新列表不会消耗接口额度。")
        data["last_request"] = {"query": query.strip(), "region": region, "days": days, "at": now.isoformat()}
        previous = data["runs"].get("TikTok", {})
        run = {**previous, "attempted_at": now.isoformat(), "query": query.strip() if isinstance(query, str) else ""}
        try:
            payload = tikhub.search(query, region, days, count)
            rows = [trends.normalize("TikTok", item, now, "TikHub") for item in payload.get("items") or []]
            by_id = {row["id"]: row for row in data["items"]}
            for row in rows:
                old = by_id.get(row["id"])
                if not old or trends.date(row["metrics_at"]) >= trends.date(old["metrics_at"]):
                    by_id[row["id"]] = row
            data["items"] = sorted(by_id.values(), key=lambda row: row["collected_at"], reverse=True)[:500]
            run.update(succeeded_at=now.isoformat(), count=len({row["id"] for row in rows}), error=None)
        except tikhub.TikHubError as exc:
            run["error"] = redact(exc)[:300]
        except trends.FeedError as exc:
            run["error"] = redact(exc)[:300]
        data["runs"]["TikTok"] = run
        trends.save(out, data)
        return trends.present(data, trends.config(), now)
    finally:
        trends.LOCK.release()


def read_topic_feed(_args: dict | None = None) -> dict:
    def run():
        feed = load("topic_feed").read_feed()
        return {
            "feed": feed,
            "note": "只读取已运行的 AIHOT，不启动采集或模型任务。地址可用 AIHOT_API_URL / AIHOT_SITE_URL 或工作台 .env 修改。",
        }

    return _call(run)


def generate_scripts(args: dict) -> dict:
    def run():
        topic = _text(args.get("topic") or "", "选题").strip()[:80]
        if not topic:
            raise ToolError("请先输入选题。")
        market = _text(args.get("market") or "英国", "市场").strip()[:40] or "英国"
        platform = _text(args.get("platform") or "TikTok", "平台").strip()[:40] or "TikTok"
        brief_raw = args.get("brief") or ""
        if not isinstance(brief_raw, str):
            raise ToolError("创作要求必须是字符串。")
        llm = load("llm")
        if not llm.model_config("script").get("api_key"):
            raise ToolError("未配置脚本模型密钥。只读工具仍可用。请在工作台 .env 填写 DEEPSEEK_API_KEY 或 SCRIPT_API_KEY。")
        brief = load("script_edit").text(brief_raw, "创作要求", 2000, False) if brief_raw else ""
        session = load("pipeline").generate_script_session(topic, market, platform, brief)
        session.setdefault("scripts", {})["chosen"] = None
        saved = load("script_store").add(output_dir(), session)
        saved.setdefault("scripts", {})["chosen"] = None
        return {
            "session": saved,
            "note": "已生成三版并写入脚本库，chosen 仍为空。请向用户展示三版，等人选定后再调用 choose_script_version。",
        }

    return _mutate(lambda: _call(run))


def list_scripts(args: dict) -> dict:
    def run():
        query = str(args.get("query") or "").strip().lower()
        rows = load("script_store").summaries(output_dir())
        if query:
            rows = [row for row in rows if query in str(row.get("title") or "").lower()]
        return {"count": len(rows), "sessions": rows[:50], "truncated": len(rows) > 50}

    return _call(run)


def read_script(args: dict) -> dict:
    def run():
        library = load("script_store").read(output_dir())
        session_id = args.get("session_id")
        if session_id is not None and not isinstance(session_id, str):
            raise ToolError("session_id 必须是字符串。")
        session = _session(library, session_id)
        return {
            "session": session,
            "history": _history(library, session),
            "current_id": library.get("current"),
            "note": "读取不会选用版本，也不会把这份脚本标成当前制作稿。",
        }

    return _call(run)


def rewrite_script(args: dict) -> dict:
    def run():
        llm = load("llm")
        if not llm.model_config("script").get("api_key"):
            raise ToolError("未配置脚本模型密钥，不能 AI 改写。手动编辑可直接 save_script_revision。")
        session_id = args.get("session_id")
        variant = args.get("variant")
        if not isinstance(session_id, str) or not isinstance(variant, str):
            raise ToolError("请提供 session_id 和 variant。")
        library = load("script_store").read(output_dir())
        session = _session(library, session_id)
        preview = load("script_edit").rewrite(session, variant, args.get("instruction"))
        return {
            "preview": preview,
            "saved": False,
            "chosen": (session.get("scripts") or {}).get("chosen"),
            "note": "这只是改写预览，没有写入脚本库，也没有选用。请把对照交给用户，确认后再 save_script_revision。",
        }

    return _call(run)


def save_script_revision(args: dict) -> dict:
    def run():
        session_id = args.get("session_id")
        variant_id = args.get("variant")
        expected = args.get("expected_head")
        content = args.get("content")
        if not all(isinstance(item, str) and item for item in (session_id, variant_id, expected)):
            raise ToolError("请提供 session_id、variant 和 expected_head。expected_head 是该选题最新一版的 id。")
        if session_id != expected:
            raise ToolError("只能在最新版本上保存。请先 read_script，用最新 id 作为 session_id 和 expected_head。")
        action = args.get("action") or "手动编辑"
        if action not in ("手动编辑", "AI 改写"):
            raise ToolError("action 只能是「手动编辑」或「AI 改写」。")
        store = load("script_store")
        library = store.read(output_dir())
        session = _session(library, session_id)
        edit = load("script_edit")
        variant = edit.validate(content, variant_id)
        title = edit.text(args.get("title") or session.get("title") or "", "选题名称", 80)
        saved = store.revise(output_dir(), session, variant, title, expected, action)
        return {
            "session_id": saved.get("id"),
            "project_id": saved.get("project_id"),
            "revision": saved.get("revision"),
            "action": saved.get("action"),
            "chosen": (saved.get("scripts") or {}).get("chosen"),
            "note": "已另存一版。选用状态已清空，必须重新人工选用后才能制作。",
        }

    return _mutate(lambda: _call(run))


def choose_script_version(args: dict) -> dict:
    def run():
        require_phrase(args, CHOOSE)
        session_id = args.get("session_id")
        variant = args.get("variant")
        if not isinstance(session_id, str) or not isinstance(variant, str):
            raise ToolError("请提供用户指定的 session_id 和 variant，不要自行挑「最好的一版」。")
        saved = load("script_store").open_session(output_dir(), session_id, variant)
        return {
            "session_id": saved.get("id"),
            "title": saved.get("title"),
            "chosen": (saved.get("scripts") or {}).get("chosen"),
            "note": "已记录人工选用。还没有配音，也没有发布。",
        }

    return _mutate(lambda: _call(run))


def search_materials(args: dict) -> dict:
    def run():
        path = os.path.join(ensure_studio(), "data", "library.json")
        with open(path, encoding="utf-8") as handle:
            library = json.load(handle)
        query = str(args.get("query") or "").strip().lower()
        matched = []
        for clip in library.get("clips") or []:
            if not isinstance(clip, dict):
                continue
            hay = " ".join([
                str(clip.get("id") or ""),
                str(clip.get("title") or ""),
                str(clip.get("location") or ""),
                " ".join(clip.get("places") or []),
                " ".join(clip.get("tags") or []),
            ]).lower()
            if query and query not in hay:
                continue
            matched.append({key: clip.get(key) for key in ("id", "title", "tags", "places", "location", "quality", "issues", "file")})
        rules = None
        if args.get("include_rules"):
            skill_path = os.path.join(studio_root(), "data", "skill.json")
            with open(skill_path, encoding="utf-8") as handle:
                skill = json.load(handle)
            rules = {"name": skill.get("name"), "from": skill.get("from"), "rules": skill.get("rules")}
        return {
            "count": len(matched),
            "clips": matched[:30],
            "truncated": len(matched) > 30,
            "rules": rules,
            "note": "与工作台素材搜索相同：按名称、地点和标签在本地素材库里做包含匹配，不访问外网。",
        }

    return _call(run)


def produce_rough_cut(args: dict) -> dict:
    def run():
        require_phrase(args, PRODUCE)
        session_id = args.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise ToolError("请提供已选用的 session_id。")
        library = load("script_store").read(output_dir())
        session = _session(library, session_id)
        chosen = (session.get("scripts") or {}).get("chosen")
        if not chosen:
            raise ToolError("尚未人工选用脚本版本，不能制作。请先让用户确认并调用 choose_script_version。")
        variant = args.get("variant") or chosen
        if variant != chosen:
            raise ToolError(f"已选用 {chosen}。不能在制作时改用 {variant}。更换版本需要用户重新确认。")
        root = ensure_studio()
        out = output_dir()
        os.makedirs(out, exist_ok=True)
        production_file = os.path.join(out, "production-script.json")
        load("script_store").atomic_json(production_file, session)
        command = [sys.executable, "-u", os.path.join(root, "pipeline.py"), "--script-file", production_file, "--variant", chosen]
        if draft_root():
            command.extend(["--draft-root", draft_root()])
        timeout = int(os.environ.get("RAIL_PRODUCE_TIMEOUT") or "600")
        try:
            proc = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise ToolError(f"制作超时（{timeout} 秒）。已有脚本仍保留。") from exc
        if proc.returncode != 0:
            detail = redact((proc.stderr or proc.stdout or "制作失败")[-800:])
            raise ToolError("制作失败：" + detail)
        summary = None
        try:
            summary = _summary_result(load("studio").read_result())
        except (OSError, ValueError, ToolError) as exc:
            summary = {"warning": redact(exc)}
        return {
            "ok": True,
            "chosen": chosen,
            "stdout": redact(proc.stdout)[-500:],
            "result": summary,
            "note": "已按选定版本制作。流水线不会再次调用脚本模型。剪映里的精修和平台发布仍由人完成。",
        }

    return _mutate(lambda: _call(run))


def write_jianying_draft(args: dict) -> dict:
    def run():
        require_phrase(args, WRITE_DRAFT)
        result = load("studio").read_result()
        if result.get("preview_only") or not os.path.isfile(os.path.join(studio_root(), "output", "result.json")):
            raise ToolError("当前还没有本机生成的粗剪。请先制作，再写入剪映。随仓库附带的演示不能写入。")
        swaps = args.get("swaps") or {}
        if not isinstance(swaps, dict):
            raise ToolError("swaps 必须是对象：句子序号到镜头序号再到素材编号。")
        root = ensure_studio()
        out = os.path.join(root, "output")
        os.makedirs(out, exist_ok=True)
        swaps_path = os.path.join(out, "swaps.json")
        with open(swaps_path, "w", encoding="utf-8") as handle:
            json.dump(swaps, handle, ensure_ascii=False)
        target = draft_root() or os.path.dirname((result.get("draft") or {}).get("path") or "") or os.path.expanduser(
            "~/Movies/JianyingPro/User Data/Projects/com.lveditor.draft"
        )
        load("pipeline").redraft(swaps_path, target)
        updated = _summary_result(load("studio").read_result())
        return {
            "ok": True,
            "draft": updated.get("draft"),
            "score": updated.get("score"),
            "note": "已按工作台的重写草稿逻辑写回剪映工程。剪映客户端里的打开和精修仍由人完成。",
        }

    return _mutate(lambda: _call(run))


def export_roughcut(args: dict) -> dict:
    def run():
        require_phrase(args, EXPORT)
        root = ensure_studio()
        if not os.path.isfile(os.path.join(root, "output", "result.json")):
            raise ToolError("没有本机粗剪结果，不能导出。")
        result = load("studio").read_result()
        run_id = args.get("run_id") or result.get("run_id")
        if run_id != result.get("run_id"):
            raise ToolError("粗剪已更新，请先 read_production_result 再导出当前 run_id。")
        swaps = args.get("swaps") or {}
        if not isinstance(swaps, dict):
            raise ToolError("swaps 必须是对象。")
        rendered = load("roughcut_export").render(root, os.path.join(root, "output"), result, swaps)
        filename = rendered.get("filename")
        path = os.path.join(root, "output", "exports", filename) if filename else None
        return {
            "export": rendered,
            "path": path,
            "studio_url": studio_url().rstrip("/") + str(rendered.get("url") or ""),
            "note": "文件在工作台 output/exports。右侧浏览器的沙箱可能拦截下载，可以直接使用 path。",
        }

    return _mutate(lambda: _call(run))


def review_finished_video(args: dict) -> dict:
    def run():
        path = args.get("path")
        if not isinstance(path, str) or not path.strip():
            raise ToolError("请提供成片文件路径。")
        path = os.path.abspath(os.path.expanduser(path))
        llm = load("llm")
        cfg = llm.model_config("video")
        if not cfg.get("api_key"):
            raise ToolError("未配置成片评测密钥。请在工作台 .env 填写 DEEPSEEK_API_KEY 或 VIDEO_API_KEY。")
        draft = None
        result_path = os.path.join(studio_root(), "output", "result.json")
        if os.path.isfile(result_path):
            with open(result_path, encoding="utf-8") as handle:
                draft = json.load(handle)
        review = load("video_review").review_video(path, cfg["api_key"], cfg["model"], draft, cfg["base_url"])
        if isinstance(review, dict):
            usage = review.get("usage")
            if isinstance(usage, dict):
                usage["provider"] = cfg.get("provider")
                usage.pop("api_key", None)
        return {
            "review": review,
            "page_publish_unlocked": False,
            "note": "评测结论不是终审，也不会解锁工作台的发布登记，更不会发布到平台。",
        }

    return _call(run)


def read_review(args: dict) -> dict:
    def run():
        review = load("review")
        path = os.path.join(review.DATA, "review.json")
        note = "播放和发布记录来自工作台里的模拟 CSV，不是平台实时数据。复盘不会改写脚本，也不能代替终审或发布。"
        if args.get("refresh"):
            use_ai = bool(args.get("use_ai"))
            if use_ai and not load("llm").model_config("script").get("api_key"):
                raise ToolError("未配置脚本模型密钥，不能生成复测建议。可以把 use_ai 设为 false 只跑规则。")
            data = review.build(use_ai=use_ai)
            return {"review": data, "source": "rebuilt", "note": note}
        if not os.path.isfile(path):
            data = review.build(use_ai=False)
            return {"review": data, "source": "built-without-ai", "note": note}
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return {"review": data, "source": "review.json", "note": note}

    return _call(run)


def _final_review_path() -> str:
    return os.path.join(output_dir(), "final-review.json")


def _read_final_review_file() -> dict | None:
    path = _final_review_path()
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {"error": "终审记录无法读取。"}
    return data if isinstance(data, dict) else None


def record_final_review(args: dict) -> dict:
    def run():
        require_phrase(args, FINAL_REVIEW)
        reviewer = args.get("reviewer")
        decision = args.get("decision")
        comment = args.get("comment")
        if not isinstance(reviewer, str) or not reviewer.strip() or len(reviewer.strip()) > 40:
            raise ToolError("请填写终审人姓名，不超过 40 字。")
        if decision not in ("通过", "退回修改"):
            raise ToolError("decision 只能是「通过」或「退回修改」。")
        if not isinstance(comment, str) or not comment.strip() or len(comment.strip()) > 2000:
            raise ToolError("请填写终审意见，不超过 2000 字。")
        from datetime import datetime, timezone

        record = {
            "reviewer": reviewer.strip(),
            "decision": decision,
            "comment": comment.strip(),
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "publishes": False,
            "page_publish_unlocked": False,
        }
        os.makedirs(output_dir(), exist_ok=True)
        path = _final_review_path()
        temporary = path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(record, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
        if decision == "通过":
            nxt = "终审人仍需在工作台页面完成终审登记。之后由运营到平台手动发布。本工具没有发布能力。"
        else:
            nxt = "退回修改。请回到脚本、剪映精修或重新制作，不能进入发布。"
        return {"record": record, "path": path, "next": nxt}

    return _mutate(lambda: _call(run))


def read_production_result(_args: dict | None = None) -> dict:
    def run():
        result = load("studio").read_result()
        return {
            "result": _summary_result(result),
            "note": "预览数据来自仓库演示时 preview_only 为 true，不能把它写成剪映工程。",
        }

    return _call(run)
