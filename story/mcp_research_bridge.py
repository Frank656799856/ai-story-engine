#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
外源世界观资料桥 · MCP 桥接器（8.16 配套，2026-09-26）

把 story 系统（默认 http://127.0.0.1:5004）的两个资料接收端点包装成
标准 stdio MCP 工具，供有搜索能力的外部 AI 工具（Claude Desktop /
Cursor / Trae 等）安装后直接调用：

  - submit_story_research(job_id, research_json)  提交检索资料，校验通过即开始造城
  - skip_story_research(job_id)                   跳过检索，直接造城

设计要点：
  - stdio 直连，story 服务不新增端口；本脚本只是一个 HTTP 客户端
  - 仅用标准库 urllib 发请求，除 mcp 外零第三方依赖
  - 校验失败时 story 端返回 200 + {ok:false, errors, repair_prompt}，
    工具原样回传，外部 AI 可按补查提示词自行修正后重新提交

安装（在运行本脚本的 Python 环境执行一次）：
  pip install mcp

配置示例（Claude Desktop / Cursor / Trae 的 MCP 配置 JSON 通用格式）：
  {
    "mcpServers": {
      "story-research": {
        "command": "C:\\\\Users\\\\Administrator\\\\AppData\\\\Local\\\\Programs\\\\Python\\\\Python313\\\\python.exe",
        "args": ["E:\\\\APP\\\\python_AI\\\\pro_AI2\\\\story\\\\mcp_research_bridge.py"]
      }
    }
  }
  story 服务不在 5004 端口时，在节点里加：
  "env": { "STORY_BASE_URL": "http://127.0.0.1:<端口>" }
"""

import json
import os
import urllib.error
import urllib.request

try:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _MCPServer
except ModuleNotFoundError:  # mcp 2.x：FastMCP 更名 MCPServer（tool()/run(transport="stdio") 用法不变）
    from mcp.server.mcpserver import MCPServer as _MCPServer

BASE_URL = os.environ.get("STORY_BASE_URL", "http://127.0.0.1:5004").rstrip("/")

mcp = _MCPServer("story-research")


def _post(path: str, payload: dict) -> str:
    """向 story 服务发 POST，返回状态码 + 响应体文本（供外部 AI 直接阅读）。"""
    req = urllib.request.Request(
        BASE_URL + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", "replace")
            return f"HTTP {resp.status}\n{body}"
    except urllib.error.HTTPError as e:
        # 404（任务不存在/类型不对）、409（状态不对/锁被占）等走这里
        body = e.read().decode("utf-8", "replace")
        return f"HTTP {e.code}\n{body}"
    except Exception as e:
        return f"请求失败：{e}（请确认 story 服务已在 {BASE_URL} 运行）"


@mcp.tool()
def submit_story_research(job_id: str, research_json: str) -> str:
    """提交外源检索资料给剧情副本系统，校验通过后立即开始造城生成副本。

    Args:
        job_id: 检索任务 id，来自检索提示词里的 /api/story/research/<job_id> 路径
        research_json: 符合契约的完整 JSON 文本，格式：
            {"source_model": "工具名/模型名",
             "entities": [{"name": "规范写法", "type": "人物/地点/组织/物品/术语",
                           "note": "一句话定义"}],
             "topics": [{"topic": "主题名", "facts": ["已查实的事实"],
                         "uncertain": ["没查实的内容"], "relations": ["A—关系—B"]}],
             "notes": "补充说明，可省略"}
            entities 为可选专名规范表（8.25b）：先整理再查证，全文专名一律用规范写法；
            每个主题的 facts 与 uncertain 不可同时为空；
            建议总字数控制在 6000 字以内；没查实的内容必须写进 uncertain。
    """
    try:
        data = json.loads(research_json)
    except json.JSONDecodeError as e:
        return f"research_json 不是合法 JSON：{e}。请修正后重新提交。"
    return _post(f"/api/story/research/{job_id}", data)


@mcp.tool()
def skip_story_research(job_id: str) -> str:
    """跳过外源检索，让剧情副本系统不等资料直接开始造城。"""
    return _post(f"/api/story/research/{job_id}/skip", {})


if __name__ == "__main__":
    mcp.run(transport="stdio")
