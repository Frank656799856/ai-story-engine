#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI NPC 记忆系统 — Flask 应用入口（多身份支持）

启动方式：
  python app.py
  浏览器打开 http://localhost:5000

API 端点：
  POST /api/chat          — 发送消息
  GET  /api/stats         — 获取统计
  POST /api/clear         — 清空对话历史
  POST /api/identities    — 创建新身份
  GET  /api/identities    — 列出所有身份
  DELETE /api/identities/<name> — 删除身份
  POST /api/switch        — 切换当前身份
  GET  /api/rolepack/export?identity= — 导出角色包 zip
  POST /api/rolepack/import           — 导入角色包 zip（multipart, 字段 file）
"""

import io
import json
import logging
import os
import sys

from flask import Flask, request, jsonify, send_from_directory, send_file

from agent.ai_client import token_stats

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent.pipeline import (
    NPCPipeline, load_identities, save_identities,
    create_identity, delete_identity,
)
from tools.npc_state import get_npc_state, set_npc_state
from tools.clipboard_check import check_clipboard_similarity
from tools.rolepack import export_rolepack, import_rolepack

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("app")

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
    return send_from_directory("static", "index.html")


# ==================== 聊天 API ====================

@app.route("/api/chat", methods=["POST"])
def chat():
    logger.info("收到 /api/chat 请求")
    try:
        data = request.get_json(force=True)
        message = data.get("message", "").strip()
        if not message:
            return jsonify({"error": "消息不能为空"}), 400

        identity = data.get("identity", current_identity)
        logger.info(f"开始处理 [{identity}]: {message[:50]}")
        pipe = get_pipeline(identity)
        result = pipe.chat(message)
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


# ==================== 启动 ====================

if __name__ == "__main__":
    logger.info("AI NPC 记忆系统（多身份）启动中...")
    logger.info("打开浏览器访问 http://localhost:5003")
    # 注意：debug=False 关闭 auto-reloader（避免文件变化导致服务重启中断请求）
    # threaded=True 支持并发请求，避免单请求阻塞导致后续请求排队
    app.run(host="0.0.0.0", port=5003, debug=False, threaded=True)