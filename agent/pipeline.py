#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A1→A2→A3→A4 流水线编排器（多身份支持）

每个 NPCPipeline 实例绑定一个身份。
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from memory.store import MemoryStore, MEMORY_BASE
from memory.index import MemoryIndex
from agent.a1_search import A1SearchAgent
from agent.a2_deep_think import A2DeepThinkAgent
from agent.a3_output import A3OutputAgent
from agent.a4_manager import A4ManagerAgent
from agent.trace import trace
from agent.ai_client import client
from agent import prompts as PROMPTS
from tools.game_db import get_game_db

logger = logging.getLogger("pipeline")

# A1 上传给 AI 的最近对话条数（3 轮 = 6 条，主流大模型常用值）
RECENT_HISTORY_COUNT = 6


class ConversationHistory:
    """对话历史持久化存储"""

    def __init__(self, identity_name: str):
        self.identity_name = identity_name
        self._file = MEMORY_BASE / identity_name / "history.json"
        self._records: list[dict] = []

    def load(self) -> list[dict]:
        if self._file.exists():
            try:
                with open(self._file, "r", encoding="utf-8") as f:
                    self._records = json.load(f)
                logger.info(f"历史记录加载 [{self.identity_name}]: {len(self._records)} 条")
            except (json.JSONDecodeError, IOError):
                self._records = []
        return self._records

    def save(self):
        self._file.parent.mkdir(parents=True, exist_ok=True)
        with open(self._file, "w", encoding="utf-8") as f:
            json.dump(self._records, f, ensure_ascii=False, indent=2)

    def append(self, role: str, content: str, mode: str = "chat"):
        record = {
            "role": role,
            "content": content,
            "mode": mode,
            "timestamp": datetime.now().isoformat(),
        }
        self._records.append(record)
        self.save()

    def get_all(self) -> list[dict]:
        return self._records

    def get_recent(self, count: int = RECENT_HISTORY_COUNT) -> list[dict]:
        """获取最近 N 条对话（供 A1 上传给 AI）"""
        return self._records[-count:] if self._records else []

    def clear(self):
        self._records = []
        self.save()


class NPCPipeline:
    def __init__(self, identity_name: str = "default", persona: dict = None):
        self.identity_name = identity_name
        self.persona = persona or {
            "name": "NPC",
            "personality": "友好",
            "world_setting": "一个普通的世界",
        }
        self.store = MemoryStore(identity_name)
        self.index = MemoryIndex(identity_name)
        self.history = ConversationHistory(identity_name)
        self.history.load()
        self.a1 = A1SearchAgent(self.store, self.index)
        self.a2 = A2DeepThinkAgent()
        self.a3 = A3OutputAgent(identity_name)
        self.ensure_character_profiled()

    # ── 首次启动：从参考对话样本提炼角色画像 ─────────────
    def ensure_character_profiled(self):
        """
        若游戏数据库提供 reference 参考字段：
          - 有 dialogue_samples（对话样本）且 summary 未生成 → 调用 AI 基于样本提炼角色画像，
            （1）写回 game_db 的 reference.summary 作为完成标记；
            （2）写入一条 pinned 核心记忆（角色画像）；
            （3）回填 identities.json 的 persona（personality 若缺失则补为画像）。
          - 仅填了 source_game（参考来源）→ 只作背景信息，不提炼画像。
        """
        game_db = get_game_db(self.identity_name)
        try:
            ref = game_db.get_reference()
        except Exception:
            return
        if not ref:
            return
        samples = ref.get("dialogue_samples") or []
        summary = ref.get("summary") or ""
        # 仅当提供了对话样本且尚未提炼过（summary 为空即完成标记）才触发；
        # 只填了 source_game（参考来源）仅作背景信息，不提炼画像。
        if not samples or summary:
            return

        try:
            sample_text = "\n".join(f"- {s}" for s in samples)
            profile = client.chat(
                system_prompt=PROMPTS.A1_CHARACTER_PROFILE_SYSTEM.format(samples=sample_text),
                user_message="请提炼该角色画像。",
                temperature=0.6,
                max_tokens=500,
            )
            profile = (profile or "").strip()
            if not profile:
                logger.warning(f"角色画像提炼为空 [{self.identity_name}]")
                return
            # （1）写回 reference.summary 作为完成标记
            game_db.write_reference_summary(profile)
            # （2）写入 pinned 核心记忆
            created = self.store.create(
                content=f"【角色画像】{profile}",
                layer="core",
                summary=profile,
                keywords=["角色画像"] + (ref.get("source_game") or "").split("·"),
                importance=1.0,
                pinned=True,
            )
            self.index.add(created)
            # （3）回填 persona
            if not self.persona.get("personality"):
                self.persona["personality"] = profile
                identities = load_identities()
                if self.identity_name in identities:
                    identities[self.identity_name]["persona"] = {
                        **identities[self.identity_name].get("persona", {}),
                        **self.persona,
                    }
                    save_identities(identities)
            logger.info(f"角色画像提炼完成 [{self.identity_name}]: {profile[:50]}...")
        except Exception as e:
            logger.warning(f"角色画像提炼失败 [{self.identity_name}]: {e}")

    def chat(self, user_message: str) -> dict:
        logger.info(f"流水线 [{self.identity_name}]: {user_message[:50]}...")
        trace.clear()  # 每轮对话清空追踪
        trace.add("Pipeline", "开始", f"身份={self.identity_name}, 消息={user_message[:60]}")

        # 保存用户消息
        self.history.append("user", user_message)

        # 获取最近对话记录（供 A1 上传给 AI）
        recent_history = self.history.get_recent(RECENT_HISTORY_COUNT)

        a1_result = self.a1.search(user_message, recent_history=recent_history)
        trace.add("A1-搜索", "完成",
                   f"关键词={a1_result['keywords']}, 候选={a1_result['candidate_count']}条, "
                   f"LLM重排={a1_result.get('llm_reranked', False)}, 历史={a1_result.get('history_count', 0)}条")

        a2_result = self.a2.process(user_message, a1_result, identity_name=self.identity_name)
        trace.add("A2-思考", "完成",
                   f"模式={a2_result['mode']}, 深度思考={'是' if a2_result['deep_think_used'] else '否'}, "
                   f"筛选={len(a2_result['filtered_ids'])}条, 外部知识={a2_result.get('need_external_knowledge', False)}, "
                   f"搜索={a2_result.get('search_used', False)}, "
                   f"教程命中={len(a2_result.get('tutorial_hits', []) or [])}条")

        a3_memories = self.a1.format_for_a3(a1_result, a2_result["filtered_ids"])
        a3_result = self.a3.generate(
            user_message, a2_result, a3_memories, self.persona,
            recent_history=recent_history,
            search_text=a2_result.get("search_text", ""),
        )
        trace.add("A3-输出", "完成",
                   f"模式={a3_result['mode']}, 回复长度={len(a3_result['reply'])}, "
                   f"游戏核对={a3_result.get('game_db_used', False)}, "
                   f"校验={a3_result.get('game_db_checked', False)}, "
                   f"修正={a3_result.get('game_db_revised', False)}")

        # 保存 NPC 回复
        self.history.append("npc", a3_result["reply"], a2_result["mode"])

        a4_stats = {}
        if a3_result.get("snapshot"):
            self.a4 = A4ManagerAgent(self.store, self.index)
            try:
                a4_stats = self.a4.manage(a3_result["snapshot"], self.persona)
                trace.add("A4-管理", "完成",
                           f"摘要={a4_stats.get('summaries_generated', '?')}, "
                           f"事件={a4_stats.get('events_detected', '?')}, "
                           f"创建={a4_stats.get('memories_created', '?')}, "
                           f"升级={a4_stats.get('promoted', '?')}, "
                           f"降级={a4_stats.get('demoted', '?')}")
            except Exception as e:
                logger.warning(f"A4 失败: {e}")
                trace.add("A4-管理", "失败", str(e))

        return {
            "reply": a3_result["reply"],
            "mode": a2_result["mode"],
            "deep_think_used": a2_result["deep_think_used"],
            "reasoning": a2_result.get("reasoning_chain", []),
            "search": {
                "used": a2_result.get("search_used", False),
                "results": a2_result.get("search_results", []),
            },
            "game_db": {
                "active": a3_result.get("game_db_used", False),
                "verified": a3_result.get("game_db_used", False),
            },
            "debug": {
                "a1_candidates": a1_result["candidate_count"],
                "a1_keywords": a1_result["keywords"],
                "a1_llm_reranked": a1_result.get("llm_reranked", False),
                "a2_mode": a2_result["mode"],
                "a2_filtered": len(a2_result["filtered_ids"]),
                "a2_need_external": a2_result.get("need_external_knowledge", False),
                "a3_game_db_used": a3_result.get("game_db_used", False),
                "a3_game_db_checked": a3_result.get("game_db_checked", False),
                "a3_game_db_revised": a3_result.get("game_db_revised", False),
                "a4_stats": a4_stats,
            },
        }

    def get_history(self) -> list[dict]:
        return self.history.get_all()

    def clear_history(self):
        self.a3.clear_history()
        self.history.clear()

    def get_stats(self) -> dict:
        return self.index.stats()

    def init_core_memories(self, memories: list[dict]):
        """批量初始化核心记忆"""
        for mem in memories:
            created = self.store.create(
                content=mem.get("content", ""),
                layer="core",
                summary=mem.get("summary", ""),
                keywords=mem.get("keywords", []),
                importance=1.0,
                pinned=True,
            )
            self.index.add(created)


# ==================== 身份注册表 ====================

import json
from pathlib import Path

IDENTITIES_FILE = Path(__file__).resolve().parent.parent / "data" / "identities.json"


def load_identities() -> dict:
    """加载所有已注册身份"""
    if not IDENTITIES_FILE.exists():
        return {}
    with open(IDENTITIES_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_identities(identities: dict):
    """保存身份注册表"""
    IDENTITIES_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(IDENTITIES_FILE, "w", encoding="utf-8") as f:
        json.dump(identities, f, ensure_ascii=False, indent=2)


def create_identity(name: str, persona: dict, core_memories: list[dict] = None) -> dict:
    """创建新身份"""
    identities = load_identities()
    if name in identities:
        raise ValueError(f"身份 '{name}' 已存在")

    identity_info = {
        "name": name,
        "persona": persona,
        "created_at": __import__("datetime").datetime.now().isoformat(),
    }
    identities[name] = identity_info
    save_identities(identities)

    # 创建流水线并初始化核心记忆
    pipe = NPCPipeline(identity_name=name, persona=persona)
    if core_memories:
        pipe.init_core_memories(core_memories)

    return identity_info


def delete_identity(name: str):
    """删除身份及其所有数据"""
    identities = load_identities()
    if name not in identities:
        raise ValueError(f"身份 '{name}' 不存在")
    del identities[name]
    save_identities(identities)

    # 删除数据目录
    import shutil
    data_dir = Path(__file__).resolve().parent.parent / "data" / "memory" / name
    if data_dir.exists():
        shutil.rmtree(data_dir)