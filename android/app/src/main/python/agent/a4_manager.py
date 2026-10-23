#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A4 — 记忆管理 Agent（多身份支持）

绑定到特定身份，使用独立的 store/index。
"""

import logging
from datetime import datetime
from typing import Optional

from memory.retrieval import LAYER_HALFLIFE, calc_recency_factor
from agent.ai_client import client
from agent import prompts as PROMPTS
from tools.time_utils import get_time_period

logger = logging.getLogger("a4_manager")

# 玩家状态记忆的标记 tag：用于在 core 层识别"整块玩家状态核心记忆"，实现覆盖更新
PLAYER_STATE_TAG = "player_state"


# 玩家状态归属的玩家标识（当前为单玩家场景，固定为 one_player）
PLAYER_ID = "one_player"


def _coarse_period(now: datetime = None) -> str:
    """8 档时段归并为 4 档笼统词（早上/中午/下午/晚上），供记忆摘要文本使用。
    摘要里不写「凌晨/深夜」这类细分时段（用户要求：时间只是参考，粗粒度即可）。"""
    h = (now or datetime.now()).hour
    if 5 <= h < 11:
        return "早上"
    if 11 <= h < 13:
        return "中午"
    if 13 <= h < 19:
        return "下午"
    return "晚上"


EVENT_SIGNALS = {
    "场景切换": ["离开", "进入", "到达", "来到", "出去", "开门", "通过"],
    "新角色": ["遇到", "见到", "来了", "出现", "看见", "打招呼"],
    "物品获得": ["得到", "获得", "拿到", "捡到", "收到", "购买", "赠送"],
    "任务变更": ["任务", "完成", "失败", "接取", "提交", "目标"],
    "情绪剧变": ["震惊", "愤怒", "伤心", "感动", "害怕", "狂喜", "哭泣"],
    "关系变化": ["好感", "信任", "背叛", "和解", "约定", "承诺"],
}


class A4ManagerAgent:
    def __init__(self, store, index):
        self.store = store
        self.index = index
        self.conversation_buffer = []

    def manage(self, a3_snapshot: dict, persona: dict = None) -> dict:
        self.conversation_buffer.append(a3_snapshot)
        stats = {"summaries_generated": 0, "events_detected": 0, "memories_created": 0, "promoted": 0, "demoted": 0, "player_state_updated": 0}

        # 三档相关性：完全无关的消息跳过摘要（省 token + 不污染记忆库），重要性打折
        relevance = a3_snapshot.get("relevance", "world_related")

        time_period = get_time_period()
        if relevance == "irrelevant":
            summary = a3_snapshot.get("summary_hint", "")[:50]
            logger.info("A4: 无关消息，跳过 LLM 摘要")
        else:
            summary = self._generate_summary(a3_snapshot, time_period)
        if summary:
            stats["summaries_generated"] = 1

        events = self._detect_events(a3_snapshot)
        stats["events_detected"] = len(events)

        # T-001 玩家状态：识别本轮玩家汇报的状态，覆盖更新/创建该玩家状态核心记忆
        player_state_updated = self._update_player_state(a3_snapshot)
        stats["player_state_updated"] = player_state_updated

        layer = self._decide_layer(a3_snapshot, events, persona)
        importance = self._calc_importance(a3_snapshot, events)
        if relevance == "irrelevant":
            importance = round(importance * 0.3, 4)  # 无关消息不易升级为长期记忆
        from memory.retrieval import tokenize
        keywords = tokenize(a3_snapshot.get("user_message", "") + " " + a3_snapshot.get("npc_reply", ""))[:8]
        content = f"玩家: {a3_snapshot.get('user_message', '')}\nNPC: {a3_snapshot.get('npc_reply', '')}"
        event_id = events[0] if events else ""
        tags = [a3_snapshot.get("mode", "chat"), time_period]
        if events:
            tags.append("event")

        memory = self.store.create(
            content=content, layer=layer, summary=summary, keywords=keywords,
            importance=importance, event_id=event_id, tags=tags, mode=a3_snapshot.get("mode", "chat"),
            time_period=time_period,
        )
        self.index.add(memory)
        stats["memories_created"] = 1

        if events:
            promoted, demoted = self._check_promotion_demotion(events)
            stats["promoted"] = promoted
            stats["demoted"] = demoted

        if len(self.conversation_buffer) >= 10:
            self._schedule_decay()
            self.conversation_buffer = []

        logger.info(f"A4 完成 [{self.store.identity_name}]: 摘要={stats['summaries_generated']}, 事件={stats['events_detected']}, 创建={stats['memories_created']}, 升级={stats['promoted']}, 降级={stats['demoted']}, 玩家状态更新={stats['player_state_updated']}")
        return stats

    def _generate_summary(self, snapshot: dict, time_period: str = "") -> str:
        user_msg = snapshot.get("user_message", "")
        npc_reply = snapshot.get("npc_reply", "")
        # 注入日期 + 四档笼统时段（不给精确时刻，避免模型把「几点几分」写进摘要）
        today = datetime.now().strftime("%Y年%m月%d日")
        prompt = f"今天：{today}；时段：{_coarse_period()}（仅限早上/中午/下午/晚上四档之一）\n用户: {user_msg}\nNPC: {npc_reply[:500]}"
        try:
            return client.chat(
                system_prompt=PROMPTS.A4_SUMMARY_SYSTEM,
                user_message=prompt, temperature=0.3, max_tokens=260,
                # 频率惩罚：抑制摘要中的重复/口水词，让总结更凝练
                frequency_penalty=0.4,
            ).strip()
        except Exception as e:
            logger.warning(f"A4: 摘要失败: {e}")
            return snapshot.get("summary_hint", "")[:50]

    def _find_player_state_memory(self) -> Optional[dict]:
        """在 core 层查找"玩家状态"核心记忆（通过 PLAYER_STATE_TAG 标记）"""
        core_mems = self.store.list_by_layer("core")
        for mem in core_mems:
            if PLAYER_STATE_TAG in mem.get("tags", []):
                return mem
        return None

    def _update_player_state(self, snapshot: dict) -> int:
        """
        T-001 玩家状态：采用"增量迭代"思路，让 LLM 读取当前玩家状态 + 本轮新增信息，
        产出"整块最新完整版"，再由程序覆盖写回（存在则 update，否则 create 成一条 core+pinned）。
        返回 0=无更新, 1=已更新。
        """
        user_msg = snapshot.get("user_message", "")
        npc_reply = snapshot.get("npc_reply", "")
        if not user_msg.strip():
            return 0

        existing_mem = self._find_player_state_memory()
        existing_text = existing_mem.get("content", "") if existing_mem else ""

        prompt = (
            f"当前已记录的玩家状态（可能为空）：\n{existing_text}\n\n"
            f"玩家本轮消息：{user_msg}\n\n"
            f"NPC本轮回复：{npc_reply}"
        )
        try:
            new_state = client.chat(
                system_prompt=PROMPTS.A4_PLAYER_STATE_SYSTEM,
                user_message=prompt,
                temperature=0.2,
                max_tokens=200,
            ).strip()
        except Exception as e:
            logger.warning(f"A4: 玩家状态增量迭代失败: {e}")
            return 0

        # 模型输出"无"或空 → 无可收录的玩家状态信息
        if not new_state or new_state == "无":
            return 0

        # 程序覆盖写入：命中已有则更新内容，否则新建一条 core+pinned 玩家状态记忆
        if existing_mem:
            self.store.update(existing_mem["memory_id"], content=new_state, importance=1.0)
            logger.info(
                f"A4: 玩家状态已覆盖更新 [{self.store.identity_name}/{PLAYER_ID}]: {new_state[:60]}..."
            )
        else:
            memory = self.store.create(
                content=new_state,
                layer="core",
                pinned=True,
                importance=1.0,
                tags=[PLAYER_STATE_TAG, "player", PLAYER_ID],
                mode="chat",
            )
            self.index.add(memory)
            logger.info(
                f"A4: 玩家状态已收录 [新增] [{self.store.identity_name}/{PLAYER_ID}]: {new_state[:60]}..."
            )
        return 1

    def _detect_events(self, snapshot: dict) -> list[str]:
        combined = snapshot.get("user_message", "") + " " + snapshot.get("npc_reply", "")
        detected = []
        for event_type, signals in EVENT_SIGNALS.items():
            for signal in signals:
                if signal in combined:
                    detected.append(f"evt_{datetime.now().strftime('%Y%m%d')}_{event_type}")
                    break
        return detected

    def _decide_layer(self, snapshot: dict, events: list[str], persona: dict) -> str:
        if events:
            return "episodic"
        if snapshot.get("mode") == "knowledge":
            return "working"
        if persona:
            name = persona.get("name", "")
            combined = snapshot.get("user_message", "") + snapshot.get("npc_reply", "")
            if name and name in combined:
                return "core"
        return "working"

    def _calc_importance(self, snapshot: dict, events: list[str]) -> float:
        score = 0.3
        if events:
            score += 0.4
        if snapshot.get("mode") == "knowledge":
            score -= 0.1
        if len(snapshot.get("user_message", "")) + len(snapshot.get("npc_reply", "")) > 200:
            score += 0.1
        return min(1.0, max(0.0, score))

    def _check_promotion_demotion(self, events: list[str]) -> tuple:
        promoted = 0
        demoted = 0
        for event_id in events:
            related = self.store.search_by_event(event_id)
            working_mems = [m for m in related if m.get("layer") == "working"]
            if len(working_mems) >= 3:
                for mem in sorted(working_mems, key=lambda m: m.get("timestamp", ""))[:2]:
                    self.store.update(mem["memory_id"], layer="episodic")
                    self.index.update_field(mem["memory_id"], layer="episodic")
                    promoted += 1
        episodic_mems = self.store.list_by_layer("episodic")
        for mem in episodic_mems:
            if mem.get("pinned"):
                continue
            halflife = LAYER_HALFLIFE.get("episodic", 60)
            weight = calc_recency_factor(mem.get("last_accessed", ""), halflife, False)
            if weight < 0.15:
                self.store.update(mem["memory_id"], layer="working")
                self.index.update_field(mem["memory_id"], layer="working")
                demoted += 1
        return promoted, demoted

    def _schedule_decay(self):
        all_mems = self.store.list_all()
        updated = 0
        for mem in all_mems:
            if mem.get("pinned") or mem.get("archived"):
                continue
            layer = mem.get("layer", "working")
            halflife = LAYER_HALFLIFE.get(layer, 7)
            if halflife <= 0:
                continue
            weight = calc_recency_factor(mem.get("last_accessed", ""), halflife, False)
            if mem.get("mode") == "knowledge":
                weight = weight ** 1.5
            self.store.update(mem["memory_id"], decay_weight=weight)
            self.index.update_field(mem["memory_id"], decay_weight=weight)
            updated += 1
        logger.info(f"A4: 衰减调度完成 [{self.store.identity_name}]，更新了 {updated} 条")

    def force_maintenance(self):
        self._schedule_decay()
        self.conversation_buffer = []