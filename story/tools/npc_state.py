#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NPC 角色当前状态 — 持久化存储

玩家可手动为某个 NPC 身份填写"当前状态"（如：可莉现在在禁闭室）。
- 一条覆盖写入：同一身份只保留一份最新状态
- 持久存储：保存后重启仍在
- AI 全程不参与：内容由玩家决定，A4 不会改动
- 注入位置：随当前时间一起拼到 user 消息末尾（动态区），不影响 system prompt 缓存命中
"""

import json
import logging
import os

logger = logging.getLogger("npc_state")

# 状态文件目录（与记忆库 data/memory 同级，方便统一管理）
STATE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "npc_state")
STATE_FILE = os.path.join(STATE_DIR, "npc_state.json")


def _load_all() -> dict:
    """读取全部身份的状态（{identity: "状态文本"}）"""
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning(f"读取 npc_state.json 失败: {e}")
    return {}


def _save_all(data: dict):
    """整体写回状态文件（原子写入，避免半截文件）"""
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        logger.error(f"写入 npc_state.json 失败: {e}")


def get_npc_state(identity: str) -> str:
    """读取某身份的当前状态（无则返回空字符串）"""
    return (_load_all().get(identity) or "").strip()


def set_npc_state(identity: str, state_text: str) -> None:
    """保存某身份的当前状态（一条覆盖写入；state_text 为空表示清空）"""
    data = _load_all()
    state_text = state_text.strip()
    if state_text:
        data[identity] = state_text
    else:
        data.pop(identity, None)  # 空内容 → 移除该状态
    _save_all(data)
    logger.info(f"NPC 当前状态已保存 [{identity}]: {state_text[:50]}")
