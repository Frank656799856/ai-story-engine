#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
剧情副本系统（story）— Flask 应用入口

启动方式：
  python app.py
  浏览器打开 http://localhost:5004

API 端点（原有）：
  POST /api/chat          — 发送消息（body 可带 npc_id，副本身份自动走导演流水线）
  GET  /api/stats         — 获取统计
  POST /api/clear         — 清空对话历史
  POST /api/identities    — 创建新身份
  GET  /api/identities    — 列出所有身份
  DELETE /api/identities/<name> — 删除身份
  POST /api/switch        — 切换当前身份
  GET  /api/rolepack/export?identity= — 导出角色包 zip
  POST /api/rolepack/import           — 导入角色包 zip（multipart, 字段 file）

API 端点（剧情副本）：
  POST /api/story/create        — 设置表单 → 后台线程造城，返回 job_id
  GET  /api/story/create/<job_id> — 轮询生成进度（generating/critic_round2/done/error）
  GET  /api/story/list          — 存档选择：全部副本摘要（并行模式，按最近游玩排序）
  POST /api/story/enter         — 进入指定副本存档（设当前指针 + 返回公开视图）
  GET  /api/story/current       — 当前副本公开视图（不含 secrets 原文）
  POST /api/story/npc           — 校验 npc_id 是否存在于当前剧本
  POST /api/story/finish        — 终局归档（置 ended + 清除当前副本指针）
  POST /api/story/quest/complete — 玩家点「完成」：completable→done + 扣时推进时钟
  POST /api/story/private       — 建立/解除私聊模式（同地点硬校验；npc_id 空 = 解除）
  POST /api/story/location      — 玩家切换地点（白名单校验；私聊对象不在场自动断开）
  GET  /api/story/debug         — 开发者面板：全量 flags/秘密状态/审稿记录/玩家状态
"""

import datetime
import io
import json
import logging
import os
import sys
import threading
import uuid

from flask import Flask, request, jsonify, send_from_directory, send_file

from agent.ai_client import token_stats

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent.pipeline import (
    NPCPipeline, load_identities, save_identities,
    create_identity, delete_identity,
)
from memory.store import MEMORY_BASE
from tools.npc_state import get_npc_state, set_npc_state
from tools.clipboard_check import check_clipboard_similarity
from tools.rolepack import export_rolepack, import_rolepack
# 剧情副本核心包（story/ 为运行根：story 包 = story/story/ 目录）
from story import store as story_store
from story import schema as story_schema
from story import builder as story_builder
from story import director as story_director

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)

# ── 全局文件日志（8.24b）：所有 logger 的输出统一落盘到 data/logs/，按日期划分文件 ──
# 每次 LLM 调用（ai_client 埋点）与每次状态变化（director 等既有埋点）都会写进当天文件；
# 当天文件为 story.log，跨天滚动时自动归档为 story_YYYY-MM-DD.log，保留 30 天。
from logging.handlers import TimedRotatingFileHandler

_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "logs")
os.makedirs(_LOG_DIR, exist_ok=True)
_file_handler = TimedRotatingFileHandler(
    os.path.join(_LOG_DIR, "story.log"),
    when="midnight", backupCount=30, encoding="utf-8",
)


def _roll_namer(default_name: str) -> str:
    """滚动归档改名：story.log.2026-09-26 → story_2026-09-26.log"""
    import re as _re
    m = _re.match(r"(.*)\.(\d{4}-\d{2}-\d{2})$", default_name)
    return f"{m.group(1)}_{m.group(2)}.log" if m else default_name


_file_handler.namer = _roll_namer
_file_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
))
logging.getLogger().addHandler(_file_handler)

logger = logging.getLogger("story_app")

app = Flask(__name__, static_folder="static", static_url_path="")

# 多身份流水线管理
pipelines: dict[str, NPCPipeline] = {}
current_identity = "default"


def get_pipeline(name: str = None) -> NPCPipeline:
    """获取或创建身份流水线"""
    name = name or current_identity
    if name not in pipelines:
        identities = load_identities()
        persona = identities.get(name, {}).get("persona", {"name": name, "personality": "友好"})
        pipelines[name] = NPCPipeline(identity_name=name, persona=persona)
    return pipelines[name]


# 初始化默认身份
identities = load_identities()
if "default" not in identities:
    create_identity("default", {"name": "默认NPC", "personality": "友好", "world_setting": "一个普通的世界"})
get_pipeline("default")


# ==================== 页面 ====================

@app.route("/")
def index():
    # story.html 位于 story/ 根（由前端任务创建）；send_file 运行时才解析路径，
    # 文件暂缺只影响该请求本身，不影响应用导入与启动。
    return send_file("story.html")


# ==================== 聊天 API ====================

def _ensure_story_director(identity: str, pipe: NPCPipeline) -> None:
    """若身份是剧情副本，则（重新）装配导演。

    8.20c 修复：必须每次聊天都从磁盘重建——调时/移动/完成等接口各自新建 Director
    落盘，若这里缓存旧实例，聊天结束 save_state 会把内存里的旧状态（旧位置、
    旧任务状态）覆盖回磁盘，造成"切场景无效、任务完成状态回退"。
    """
    info = load_identities().get(identity) or {}
    if info.get("kind") != "story":
        return
    try:
        pipe.director = story_director.Director(identity)
        logger.info(f"副本身份已装配导演: {identity}")
    except FileNotFoundError as e:
        logger.error(f"导演装配失败: {e}")


@app.route("/api/chat", methods=["POST"])
def chat():
    logger.info("收到 /api/chat 请求")
    try:
        data = request.get_json(force=True)
        message = data.get("message", "").strip()
        if not message:
            return jsonify({"error": "消息不能为空"}), 400

        # 剧情副本优先：未显式指定身份时取当前副本指针；指针为空回退原逻辑
        identity = data.get("identity") or story_store.get_current_identity() or current_identity
        npc_id = data.get("npc_id")
        logger.info(f"开始处理 [{identity}]: {message[:50]}")
        pipe = get_pipeline(identity)
        _ensure_story_director(identity, pipe)
        result = pipe.chat(message, npc_id=npc_id)
        logger.info(f"处理完成 [{identity}]，回复长度={len(result.get('reply', ''))}")
        return jsonify(result)
    except Exception as e:
        logger.error(f"聊天失败: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/stats", methods=["GET"])
def stats():
    identity = request.args.get("identity", current_identity)
    pipe = get_pipeline(identity)
    return jsonify(pipe.get_stats())


@app.route("/api/clear", methods=["POST"])
def clear():
    data = request.get_json(silent=True) or {}
    identity = data.get("identity", current_identity)
    pipe = get_pipeline(identity)
    pipe.clear_history()
    return jsonify({"status": "ok"})


# ==================== NPC 当前状态 API ====================

@app.route("/api/npc_state", methods=["GET"])
def get_npc_state_api():
    identity = request.args.get("identity", current_identity)
    return jsonify({"identity": identity, "state": get_npc_state(identity)})


@app.route("/api/npc_state", methods=["POST"])
def set_npc_state_api():
    data = request.get_json(silent=True) or {}
    identity = data.get("identity", current_identity)
    state_text = data.get("state", "").strip()
    set_npc_state(identity, state_text)
    return jsonify({"status": "ok", "identity": identity, "state": state_text})


# ==================== 剪贴板比对 API ====================

@app.route("/api/check_clipboard", methods=["POST"])
def check_clipboard_api():
    data = request.get_json(silent=True) or {}
    message = data.get("message", "").strip()
    return jsonify(check_clipboard_similarity(message))


# ==================== 对话历史 API ====================

@app.route("/api/history", methods=["GET"])
def get_history():
    identity = request.args.get("identity", current_identity)
    pipe = get_pipeline(identity)
    return jsonify({"history": pipe.get_history()})


# ==================== 推理日志 API ====================

@app.route("/api/trace", methods=["GET"])
def get_trace():
    from agent.trace import trace
    detailed = request.args.get("detailed", "0") == "1"
    return jsonify({
        "summary": trace.get_summary(),
        "detailed": trace.get_detailed(),
        "stats": trace.stats(),
        "detailed_mode": detailed,
        "api": token_stats.get_summary(),
    })


@app.route("/api/trace/clear", methods=["POST"])
def clear_trace():
    from agent.trace import trace
    trace.clear()
    return jsonify({"status": "ok"})


@app.route("/trace")
def trace_page():
    return send_from_directory("static", "trace.html")


# ==================== 身份管理 API ====================

@app.route("/api/identities", methods=["GET"])
def list_identities():
    idents = load_identities()
    return jsonify({
        "current": current_identity,
        "identities": idents,
    })


@app.route("/api/identities", methods=["POST"])
def create_identity_api():
    try:
        data = request.get_json(force=True)
        name = data.get("name", "").strip()
        if not name:
            return jsonify({"error": "身份名称不能为空"}), 400

        persona = data.get("persona", {})
        core_memories = data.get("core_memories", [])
        info = create_identity(name, persona, core_memories)

        # 预创建流水线
        get_pipeline(name)

        return jsonify({"status": "ok", "identity": info})
    except ValueError as e:
        return jsonify({"error": str(e)}), 409
    except Exception as e:
        logger.error(f"创建身份失败: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/identities/<name>", methods=["DELETE"])
def delete_identity_api(name):
    try:
        delete_identity(name)
        if name in pipelines:
            del pipelines[name]
        return jsonify({"status": "ok"})
    except ValueError as e:
        return jsonify({"error": str(e)}), 404


@app.route("/api/switch", methods=["POST"])
def switch_identity():
    global current_identity
    data = request.get_json(force=True)
    name = data.get("identity", "default")
    identities = load_identities()
    if name not in identities:
        return jsonify({"error": f"身份 '{name}' 不存在"}), 404
    current_identity = name
    get_pipeline(name)
    return jsonify({"status": "ok", "current": current_identity})


# ==================== 角色包 API（PC 导出 / 安卓导入，共用） ====================

@app.route("/api/rolepack/export", methods=["GET"])
def export_rolepack_api():
    identity = request.args.get("identity", current_identity)
    try:
        zip_bytes, filename = export_rolepack(identity)
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    logger.info(f"导出角色包 [{identity}]: {filename}")
    return send_file(io.BytesIO(zip_bytes), mimetype="application/zip",
                     as_attachment=True, download_name=filename)


@app.route("/api/rolepack/import", methods=["POST"])
def import_rolepack_api():
    f = request.files.get("file")
    if f is None or not f.filename:
        return jsonify({"error": "未收到上传文件"}), 400
    try:
        result = import_rolepack(f.read(), f.filename)
    except ValueError as e:
        return jsonify({"error": str(e)}), 409
    except Exception as e:
        logger.error(f"导入角色包失败: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500
    get_pipeline(result["identity"])  # 预创建流水线
    return jsonify({"status": "ok", **result})


# ==================== 剧情副本 API ====================

# 副本生成任务表：{job_id: {kind, status, stage, percent, story_id, error, ...}}
# kind ∈ build（直接造城）/ research（8.16 先检索后造城）
# build    status ∈ generating（生成中）/ critic_round2（审稿中）/ done / error
# research status ∈ brief_generating（清单生成中）/ waiting（等资料提交）/
#                    generating / critic_round2 / done / error
story_jobs: dict = {}
# 造城锁：同一时间只允许一个生成任务（非阻塞抢锁，抢不到即拒绝）
story_job_lock = threading.Lock()


def _start_builder(job: dict, settings: dict, research=None) -> None:
    """启动造城后台线程（直接生成 / 检索就绪后的生成共用）。造城锁由调用方持有。"""
    def _on_progress(stage: str, percent: int = None):
        # 进度回调：更新内存任务表的阶段文案与百分比；审稿阶段单独标记状态
        job["status"] = "critic_round2" if "审稿" in stage else "generating"
        job["stage"] = stage
        if percent is not None:
            job["percent"] = percent

    def _run():
        try:
            story = story_builder.build_story(settings, on_progress=_on_progress, research=research)
            job["story_id"] = story.get("story_id")
            job["status"] = "done"
            job["stage"] = "副本生成完成"
            job["percent"] = 100
        except Exception as e:
            logger.error(f"副本生成失败: {e}", exc_info=True)
            job["status"] = "error"
            job["error"] = str(e)
            job["stage"] = "生成失败"
        finally:
            story_job_lock.release()

    threading.Thread(target=_run, daemon=True, name="story_builder").start()


@app.route("/api/story/create", methods=["POST"])
def story_create():
    """设置表单 → 后台任务 → 返回 job_id 供前端轮询。

    开启 research_enabled（8.16）时创建检索任务：先一次轻量 LLM 生成检索需求清单
    并拼装给外部 AI 的完整提示词，等资料提交（或跳过检索）后才启动造城。
    """
    data = request.get_json(force=True) or {}
    if not (data.get("world_desc") or "").strip():
        return jsonify({"error": "world_desc（一句话世界观）不能为空"}), 400
    if not story_job_lock.acquire(blocking=False):
        return jsonify({"error": "已有副本正在生成中，请稍候再试"}), 409

    job_id = uuid.uuid4().hex[:12]

    if bool(data.get("research_enabled")):
        # ── 8.16 检索模式：builder 不启动，先出检索提示词 ──
        job = {
            "kind": "research", "status": "brief_generating",
            "stage": "正在生成检索需求清单…", "percent": 5,
            "prompt": "", "settings": data, "story_id": None, "error": None,
        }
        story_jobs[job_id] = job

        def _run_brief():
            try:
                prompt = story_builder.make_research_prompt(data, job_id)
                job["prompt"] = prompt
                job["status"] = "waiting"
                job["stage"] = "等待外源资料：复制提示词 → 外部 AI 检索 → 回粘贴框提交（或跳过）"
            except Exception as e:
                logger.error(f"检索清单生成失败: {e}", exc_info=True)
                job["status"] = "error"
                job["error"] = str(e)
                job["stage"] = "检索清单生成失败"
            finally:
                story_job_lock.release()  # 等资料期间放锁，用户可返回修改重新发起

        threading.Thread(target=_run_brief, daemon=True, name="story_research").start()
        return jsonify({"job_id": job_id, "mode": "research"})

    job = {"kind": "build", "status": "generating", "stage": "正在排队", "percent": 5,
           "story_id": None, "error": None}
    story_jobs[job_id] = job
    _start_builder(job, data, None)
    return jsonify({"job_id": job_id, "mode": "build"})


@app.route("/api/story/create/<job_id>", methods=["GET"])
def story_create_status(job_id):
    """轮询生成/检索进度（research 任务的 prompt 字段供资料检索视图展示）。"""
    job = story_jobs.get(job_id)
    if job is None:
        return jsonify({"error": "任务不存在"}), 404
    pub = {k: v for k, v in job.items() if k not in ("settings", "research")}
    return jsonify({"job_id": job_id, **pub})


@app.route("/api/story/research/<job_id>", methods=["POST"])
def story_research_submit(job_id):
    """8.16 接收端（HTTP 端点即 MCP tool over HTTP）：外部 AI 或粘贴框提交检索结果。

    校验通过 → research job 启动 builder（带资料造城）；不通过 → 返回 200 + ok:false
    与补查提示词（前端展示给用户第二趟），任务保持 waiting。
    """
    job = story_jobs.get(job_id)
    if job is None or job.get("kind") != "research":
        return jsonify({"error": "检索任务不存在"}), 404
    if job.get("status") != "waiting":
        return jsonify({"error": f"任务当前状态为 {job.get('status')}，不接受提交"}), 409
    data = request.get_json(silent=True)
    if data is None:
        return jsonify({"ok": False, "errors": ["请求体不是合法 JSON"], "repair_prompt": ""})
    errors = story_schema.validate_research(data)
    if errors:
        repair = story_builder.make_repair_prompt(job.get("prompt", ""), errors)
        logger.info(f"检索资料校验未通过 [{job_id}]: {len(errors)} 项问题")
        return jsonify({"ok": False, "errors": errors, "repair_prompt": repair})
    if not story_job_lock.acquire(blocking=False):
        return jsonify({"error": "已有副本正在生成中，请稍候再试"}), 409
    job["research"] = data
    job["status"] = "generating"
    job["stage"] = "资料已接收，正在构建世界观与秘密网"
    logger.info(f"检索资料已接收 [{job_id}]，启动造城")
    _start_builder(job, job.get("settings") or {}, data)
    return jsonify({"ok": True})


@app.route("/api/story/research/<job_id>/skip", methods=["POST"])
def story_research_skip(job_id):
    """8.16 跳过检索：不带资料直接启动造城（AI 纯自创）。"""
    job = story_jobs.get(job_id)
    if job is None or job.get("kind") != "research":
        return jsonify({"error": "检索任务不存在"}), 404
    if job.get("status") != "waiting":
        return jsonify({"error": f"任务当前状态为 {job.get('status')}，无法跳过"}), 409
    if not story_job_lock.acquire(blocking=False):
        return jsonify({"error": "已有副本正在生成中，请稍候再试"}), 409
    job["status"] = "generating"
    job["stage"] = "已跳过检索，正在构建世界观与秘密网"
    logger.info(f"跳过检索 [{job_id}]，直接造城")
    _start_builder(job, job.get("settings") or {}, None)
    return jsonify({"ok": True})


@app.route("/api/story/list", methods=["GET"])
def story_list():
    """存档选择界面：全部副本摘要（并行模式，各副本数据独立保存、互不干扰）。

    每项：标题/世界名/类型/NPC数/时钟/地点/任务进度/是否终局/最近游玩时间，
    按最近游玩倒序（刚玩过的排前面）。不含 secrets 原文。
    """
    saves = []
    for item in story_store.list_stories():
        identity = item["identity"]
        story_d = story_store.load_story(identity) or {}
        state = story_store.load_state(identity) or {}
        # 最近游玩时间：状态文件 mtime（从未进入过的副本回退剧本文件 mtime）
        story_path = story_store.STORY_DATA_DIR / f"{identity}.json"
        state_path = story_store.STORY_DATA_DIR / f"{identity}_state.json"
        try:
            mtime = state_path.stat().st_mtime if state_path.exists() else story_path.stat().st_mtime
        except OSError:
            mtime = 0.0
        flags = state.get("flags", {})
        quest_done = sum(1 for k, v in flags.items()
                         if k.startswith("quest_") and k.endswith("_state") and v == "done")
        clock = state.get("clock") or {}
        saves.append({
            "identity": identity,
            "title": story_d.get("title", ""),
            "world_name": (story_d.get("setting", {}) or {}).get("world_name", ""),
            "genre": story_d.get("genre", ""),
            "npc_count": len(story_d.get("npcs", [])),
            "ended": bool(state.get("ended")),
            "clock_text": f"第{clock.get('day', 1)}天 {clock.get('time', '08:00')}" if clock else "",
            "location": (state.get("player", {}) or {}).get("location", ""),
            "quest_total": len(story_d.get("quests", [])),
            "quest_done": quest_done,
            "cover": story_d.get("cover", ""),
            "last_played": datetime.datetime.fromtimestamp(mtime).strftime("%m-%d %H:%M") if mtime else "",
            "_mtime": mtime,
        })
    saves.sort(key=lambda s: s["_mtime"], reverse=True)
    for s in saves:
        s.pop("_mtime", None)
    return jsonify({"saves": saves})


def _story_history_payload(identity: str, limit: int = 200) -> list:
    """8.24i：副本对话历史精简视图（玩家重新进入副本时前端回放）。

    对话历史全量保存于 data/memory/{identity}/history.json（NPC 侧 A1 每轮
    仍只取最近几条喂给 AI——这里是给玩家看的回放，两回事）。返回最近 limit 条：
      {role: "user"|"npc", text, name（npc 的显示名）, private}
    旧格式（role=assistant）按 npc 兼容；系统提示/旁白不入库（notices 不持久化）。
    """
    f = MEMORY_BASE / identity / "history.json"
    if not f.exists():
        return []
    try:
        records = json.loads(f.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, IOError):
        return []
    if not isinstance(records, list):
        return []
    # NPC id → 显示名映射（直接读剧本，不装配 Director，保持轻量）
    npc_names = {}
    try:
        story_d = story_store.load_story(identity) or {}
        for n in story_d.get("npcs", []):
            if n.get("id"):
                npc_names[n["id"]] = n.get("name", n["id"])
    except Exception:
        pass
    out = []
    for r in records[-limit:]:
        role = r.get("role")
        if role not in ("user", "npc", "assistant"):
            continue
        item = {
            "role": "user" if role == "user" else "npc",
            "text": r.get("content", ""),
            "private": bool(r.get("private")),
        }
        if role != "user":
            item["name"] = r.get("speaker_name") or npc_names.get(r.get("speaker"), "")
        out.append(item)
    return out


@app.route("/api/story/enter", methods=["POST"])
def story_enter():
    """进入指定副本存档：设当前指针 + 返回公开视图（与 /api/story/current 同构）。

    已终局的副本也可进入（前端会直接展示终局遮罩）。
    8.24i：返回体带 history（对话历史回放视图）。
    """
    data = request.get_json(silent=True) or {}
    identity = (data.get("identity") or "").strip()
    if not identity or not story_store.load_story(identity):
        return jsonify({"error": "副本存档不存在"}), 404
    try:
        d = story_director.Director(identity)
    except FileNotFoundError:
        return jsonify({"error": "副本剧本不存在"}), 404
    story_store.set_current(identity)
    logger.info(f"进入副本存档: {identity}")
    pub = d.get_public_state()
    pub["active"] = True
    pub["background"] = (story_store.load_story(identity) or {}).get("background", "")
    pub["history"] = _story_history_payload(identity)
    return jsonify(pub)


@app.route("/api/story/current", methods=["GET"])
def story_current():
    """当前副本公开视图（NPC/开场白/任务/场景/ended），绝不含 secrets 原文。

    8.24i：返回体带 history（F5 刷新恢复时同样能回放对话历史）。
    """
    identity = story_store.get_current_identity()
    if not identity:
        return jsonify({"active": False})
    try:
        d = story_director.Director(identity)
    except FileNotFoundError:
        return jsonify({"active": False})
    pub = d.get_public_state()
    pub["active"] = True
    pub["background"] = (story_store.load_story(identity) or {}).get("background", "")
    pub["history"] = _story_history_payload(identity)
    return jsonify(pub)


def _save_story_image(field: str, identity: str, image: str, max_len: int) -> None:
    """把前端压缩后的 dataURL 图片存进副本剧本 JSON（cover/background 共用）。"""
    image = (image or "").strip()
    if not image.startswith("data:image/"):
        raise ValueError("图片格式无效")
    if len(image) > max_len:
        raise ValueError("图片过大，请换一张较小的图")
    story_d = story_store.load_story(identity)
    if not story_d:
        raise FileNotFoundError(f"副本剧本不存在: {identity}")
    story_d[field] = image
    story_store.save_story(identity, story_d)
    logger.info(f"副本 {identity} 已更新 {field}（{len(image)//1024}KB）")


@app.route("/api/story/cover", methods=["POST"])
def story_cover():
    """设置/更新副本存档图标（dataURL，前端已压缩；留空可清除，默认留白）。"""
    data = request.get_json(silent=True) or {}
    identity = (data.get("identity") or "").strip()
    if not identity:
        return jsonify({"error": "缺少 identity"}), 400
    try:
        if not (data.get("image") or "").strip():
            story_d = story_store.load_story(identity)
            if not story_d:
                return jsonify({"error": "副本存档不存在"}), 404
            story_d.pop("cover", None)
            story_store.save_story(identity, story_d)
        else:
            _save_story_image("cover", identity, data.get("image"), 800_000)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 404
    return jsonify({"ok": True})


@app.route("/api/story/background", methods=["POST"])
def story_background():
    """设置/更新游玩界面背景图（dataURL，前端已压缩；identity 缺省 = 当前副本）。"""
    data = request.get_json(silent=True) or {}
    identity = (data.get("identity") or "").strip() or story_store.get_current_identity()
    if not identity:
        return jsonify({"error": "当前没有进入任何副本"}), 400
    try:
        if not (data.get("image") or "").strip():
            story_d = story_store.load_story(identity)
            if not story_d:
                return jsonify({"error": "副本存档不存在"}), 404
            story_d.pop("background", None)
            story_store.save_story(identity, story_d)
        else:
            _save_story_image("background", identity, data.get("image"), 2_500_000)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 404
    return jsonify({"ok": True})


@app.route("/api/story/npc", methods=["POST"])
def story_npc():
    """校验 npc_id 是否存在于当前剧本（前端每次 chat 携带 npc_id，此处仅做校验提示）。"""
    data = request.get_json(silent=True) or {}
    npc_id = (data.get("npc_id") or "").strip()
    identity = story_store.get_current_identity()
    if not identity:
        return jsonify({"error": "当前没有进行中的副本"}), 404
    story_d = story_store.load_story(identity)
    if not story_d:
        return jsonify({"error": "副本剧本不存在"}), 404
    for npc in story_d.get("npcs", []):
        if npc.get("id") == npc_id:
            return jsonify({"ok": True, "name": npc.get("name", npc_id)})
    return jsonify({"ok": False, "error": f"副本中不存在 NPC: {npc_id}"}), 404


def _load_director_or_none():
    """取当前副本的 Director；无进行中的副本或剧本缺失返回 (None, error响应)。"""
    identity = story_store.get_current_identity()
    if not identity:
        return None, (jsonify({"error": "当前没有进行中的副本"}), 404)
    try:
        d = story_director.Director(identity)
    except FileNotFoundError:
        return None, (jsonify({"error": "副本剧本不存在"}), 404)
    return d, None


@app.route("/api/story/finish", methods=["POST"])
def story_finish():
    """终局归档：置当前副本 ended=True + 清除当前副本指针，返回达成的终局文本。"""
    d, err = _load_director_or_none()
    if err:
        return err
    d.state["ended"] = True
    ending_text = d.ending_text()
    d.save()
    story_store.clear_current()
    logger.info(f"副本终局归档: {d.identity}")
    return jsonify({"ok": True, "identity": d.identity,
                    "ending_reached": d.state.get("ending_reached"),
                    "ending_text": ending_text})


@app.route("/api/story/quest/complete", methods=["POST"])
def story_quest_complete():
    """玩家点「完成」：任务 completable → done + 扣 time_cost_minutes 推进时钟。

    判定权在导演：flags 未满足（state != completable）时拒绝完成。
    """
    d, err = _load_director_or_none()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    quest_id = (data.get("quest_id") or "").strip()
    if not quest_id:
        return jsonify({"error": "quest_id 不能为空"}), 400
    result = d.complete_quest(quest_id)
    if not result.get("ok"):
        return jsonify({"error": result.get("error", "任务无法完成")}), 400
    d.save()
    clock = result.get("clock") or {}
    notice = (
        f"「{result.get('quest_name', quest_id)}」完成，时间推进 {result.get('minutes', 0)} 分钟"
        f"（现在 第{clock.get('day', 1)}天 {clock.get('time', '')}）。"
    )
    return jsonify({"ok": True, **result, "notice": notice,
                    "story_state": d.get_public_state()})


@app.route("/api/story/invest", methods=["POST"])
def story_invest():
    """玩家调时投入（8.20）：动作型任务分批投入时间，投入同步推进时钟。

    投入累计达标时程序写 time_ready 标记；跃迁提示（达标/可完成）一并返回。
    """
    d, err = _load_director_or_none()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    quest_id = (data.get("quest_id") or "").strip()
    minutes = data.get("minutes")
    if not quest_id:
        return jsonify({"error": "quest_id 不能为空"}), 400
    result = d.invest_time(quest_id, minutes)
    if not result.get("ok"):
        return jsonify({"error": result.get("error", "无法投入时间")}), 400
    d.save()
    notices = list(result.get("notices") or [])
    notices.extend(d.take_quest_notices())
    return jsonify({"ok": True, "quest_name": result.get("quest_name", quest_id),
                    "invested": result.get("invested", 0), "need": result.get("need", 0),
                    "clock": result.get("clock"), "notices": notices,
                    "story_state": d.get_public_state()})


@app.post("/api/story/report")
def story_report():
    """玩家汇报任务完成（8.20d）：任务面板「汇报完成」按钮。

    汇报 = 系统级指令 + 对话活动：程序写 reported（确定性）→ finalize 跃迁；
    返回代拟汇报消息 draft，前端自动代发给发布人 NPC 演验收反应。
    """
    d, err = _load_director_or_none()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    quest_id = (data.get("quest_id") or "").strip()
    if not quest_id:
        return jsonify({"error": "quest_id 不能为空"}), 400
    result = d.report_quest(quest_id)
    if not result.get("ok"):
        return jsonify({"error": result.get("error", "无法汇报")}), 400
    d.save()
    notices = d.take_quest_notices()
    return jsonify({"ok": True, "npc_id": result.get("npc_id"),
                    "npc_name": result.get("npc_name"), "draft": result.get("draft", ""),
                    "notices": notices, "story_state": d.get_public_state()})


@app.post("/api/story/reveal")
def story_reveal():
    """导演主动揭示秘密（8.22 开发者兜底，与 /api/story/debug 同族）。

    背景（2026-09-26 实测）：AI 会在正文里完整演完秘密揭示（认账/交钥匙），
    但 [STATE: knows_secret_*=true] 标签间歇性漏打，而三条结局全部以
    knows_secret 为前置——漏打即结局不可达。此处走导演侧 reveal_secret
    正规通道兜底，前提是剧情上确实已经揭示（由操作者判断）。
    """
    d, err = _load_director_or_none()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    sid = (data.get("secret_id") or "").strip()
    if not sid:
        return jsonify({"error": "secret_id 不能为空"}), 400
    if not d.reveal_secret(sid):
        return jsonify({"error": f"秘密不存在: {sid}"}), 404
    d.save()
    notices = d.take_quest_notices()
    return jsonify({"ok": True, "secret_id": sid, "notices": notices,
                    "story_state": d.get_public_state()})


@app.route("/api/story/private", methods=["POST"])
def story_private():
    """建立/解除私聊模式（8.13）。npc_id 非空 = 与该 NPC 私聊（同地点硬校验）；空 = 解除。"""
    d, err = _load_director_or_none()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    npc_id = (data.get("npc_id") or "").strip()
    if not npc_id:
        notice = d.clear_private()
        d.save()
        return jsonify({"ok": True, "notice": notice, "story_state": d.get_public_state()})
    result = d.set_private(npc_id)
    if not result.get("ok"):
        return jsonify({"error": result.get("error", "无法建立私聊")}), 400
    d.save()
    npc = d.get_npc(npc_id)
    return jsonify({"ok": True, "story_state": d.get_public_state(),
                    "notice": f"你与{npc.get('name', npc_id)}进入了私聊，其他 NPC 听不到你们的对话。"})


@app.route("/api/story/location", methods=["POST"])
def story_location():
    """玩家切换地点（前端地点下拉；与 AI 提议同一套地点白名单硬校验）。"""
    d, err = _load_director_or_none()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    loc = (data.get("location") or "").strip()
    result = d.set_location(loc)
    if not result.get("ok"):
        return jsonify({"error": result.get("error", "无法移动")}), 400
    d.save()
    return jsonify({"ok": True, "notice": result.get("notice"),
                    "story_state": d.get_public_state()})


@app.route("/api/story/debug", methods=["GET"])
def story_debug():
    """开发者面板：全量 flags、secrets（含揭示状态）、审稿 review_notes。"""
    identity = story_store.get_current_identity()
    if not identity:
        return jsonify({"error": "当前没有进行中的副本"}), 404
    try:
        d = story_director.Director(identity)
    except FileNotFoundError:
        return jsonify({"error": "副本剧本不存在"}), 404
    flags = d.state.get("flags", {})
    secrets = []
    for sec in d.story.get("secrets", []):
        sid = sec.get("id", "")
        secrets.append({
            "id": sid,
            "content": sec.get("content", ""),
            "holders": sec.get("holders", []),
            "reveal_hint": sec.get("reveal_hint", ""),
            "revealed": bool(flags.get(f"knows_secret_{sid}", False)),
        })
    return jsonify({
        "identity": identity,
        "title": d.story.get("title", ""),
        "scene": d.state.get("scene", ""),
        "ended": bool(d.state.get("ended")),
        "ending_reached": d.state.get("ending_reached"),
        "player": d.state.get("player", {}),
        "clock": d.state.get("clock", {}),
        "npc_locations": d.state.get("npc_locations", {}),
        "npc_knowledge": d.state.get("npc_knowledge", {}),   # 8.21a NPC 认知账本
        "npc_attitude": d.state.get("npc_attitude", {}),     # 8.21b 关系/心情慢变量
        "private_with": d.state.get("private_with"),
        "locations": d.locations_whitelist(),
        "flags": flags,
        "secrets": secrets,
        "routes": d.story.get("routes", []),
        "endings": d._endings_list(),
        "review_notes": d.story.get("review_notes", []),
    })


# ==================== 启动 ====================

if __name__ == "__main__":
    logger.info("剧情副本系统（story）启动中...")
    logger.info("打开浏览器访问 http://localhost:5004")
    # 注意：debug=False 关闭 auto-reloader（避免文件变化导致服务重启中断请求）
    # threaded=True 支持并发请求，避免单请求阻塞导致后续请求排队
    app.run(host="0.0.0.0", port=5004, debug=False, threaded=True)