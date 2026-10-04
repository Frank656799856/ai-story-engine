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

    def append(self, role: str, content: str, mode: str = "chat", **extra):
        """追加一条对话记录。

        8.13 扩展字段（仅剧情副本私聊/群场使用，普通身份不传即行为不变）：
          speaker       NPC 侧记录说话 NPC 的 id
          speaker_name  NPC 侧记录说话 NPC 的显示名（上下文标注用）
          private       True = 私聊消息（其他 NPC 上下文构建时跳过）
          to            玩家私聊消息的接收 NPC id
        """
        record = {
            "role": role,
            "content": content,
            "mode": mode,
            "timestamp": datetime.now().isoformat(),
        }
        record.update(extra)
        self._records.append(record)
        self.save()

    def get_all(self) -> list[dict]:
        return self._records

    def get_recent(self, count: int = RECENT_HISTORY_COUNT,
                   for_npc: str = None, public_only: bool = False) -> list[dict]:
        """获取最近 N 条对话（供 A1 上传给 AI）。

        8.13 过滤规则（隔离靠过滤不靠演技）：
          for_npc      为该 NPC 构建上下文：跳过其他 NPC 的私聊记录
                       （user 私聊消息 to=对方、npc 私聊回复 speaker=对方）
          public_only  只要公开记录（群场 A1/A2 检索用，排除全部私聊内容）
        """
        records = self._records
        if for_npc is not None:
            records = [
                r for r in records
                if not r.get("private") or r.get("to") == for_npc or r.get("speaker") == for_npc
            ]
        elif public_only:
            records = [r for r in records if not r.get("private")]
        return records[-count:] if records else []

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
        # 剧情副本导演（仅副本身份由 app.py 在首聊前装配；普通身份恒为 None，行为不变）
        self.director = None
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

    def chat(self, user_message: str, npc_id: str = None) -> dict:
        """npc_id：剧情副本模式下当前对话的 NPC（普通身份忽略，保持原行为）。
        剧情副本走 _story_chat（群场/私聊），普通身份走原流程。"""
        if self.director is not None:
            return self._story_chat(user_message, npc_id)

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
            story_section=None,
        )
        trace.add("A3-输出", "完成",
                   f"模式={a3_result['mode']}, 回复长度={len(a3_result['reply'])}, "
                   f"游戏核对={a3_result.get('game_db_used', False)}, "
                   f"校验={a3_result.get('game_db_checked', False)}, "
                   f"修正={a3_result.get('game_db_revised', False)}")

        # 保存 NPC 回复
        self.history.append("npc", a3_result["reply"], a2_result["mode"])

        a4_stats = self._run_a4(a3_result, self.persona)

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

    def _run_a4(self, a3_result: dict, persona: dict, story_brief: str = None,
                director=None, allow_secret_proposals: bool = True) -> dict:
        """A4 记忆管理（群场按 NPC 各跑一次；失败不阻塞主流程）。

        8.24d：story_brief 非空时 A4 升级为剧情状态总分析（分区输出），
        尾区 JSON 提议交 director.apply_ai_proposals 白名单消费——
        A3 卸下打标包袱，状态提议主通道移至 A4（AI 提议、程序裁决不变）。
        """
        if not a3_result.get("snapshot"):
            return {}
        a4 = A4ManagerAgent(self.store, self.index)
        try:
            # 8.24f：把导演的白名单键清单传给 A4，作为 tool calling 的参数枚举约束
            hints = None
            if director is not None:
                try:
                    hints = director.allowed_flag_keys()
                except Exception:
                    hints = None
            stats = a4.manage(a3_result["snapshot"], persona,
                              story_brief=story_brief, state_hints=hints)
            trace.add("A4-管理", "完成",
                       f"摘要={stats.get('summaries_generated', '?')}, "
                       f"事件={stats.get('events_detected', '?')}, "
                       f"创建={stats.get('memories_created', '?')}, "
                       f"升级={stats.get('promoted', '?')}, "
                       f"降级={stats.get('demoted', '?')}")
            proposals = stats.get("proposals")
            if proposals and director is not None:
                changes = director.apply_ai_proposals(
                    proposals, allow_secrets=allow_secret_proposals)
                trace.add("A4-状态提议", "消费", f"生效 {len(changes)} 项")
            return stats
        except Exception as e:
            logger.warning(f"A4 失败: {e}")
            trace.add("A4-管理", "失败", str(e))
            return {}

    def _story_chat(self, user_message: str, npc_id: str = None) -> dict:
        """剧情副本对话（8.13 群场广播 + 私聊模式）。

        群场（默认）：与玩家同处一地的全部 NPC 各跑一次 A3、各出一份回答
        （点名 NPC 排最前）；私聊模式（director.state.private_with）：仅该 NPC 应答，
        私聊消息打 private 标记，其他 NPC 上下文构建时跳过（隔离靠过滤不靠演技）。
        """
        d = self.director
        logger.info(f"副本流水线 [{self.identity_name}]: {user_message[:50]}...")
        trace.clear()
        trace.add("Pipeline", "开始", f"身份={self.identity_name}, 消息={user_message[:60]}")

        notices: list[str] = []      # 程序固定旁白（地点移动/私聊断开等）
        changes_all: list[str] = []  # 状态变更汇总（调试）

        # ── 响应者集合 ──
        private_id = d.state.get("private_with")
        if private_id and private_id not in d.presence_ids():
            # 防御：私聊对象已不在同地点（如历史遗留状态）→ 自动解除
            notices.append(d.clear_private(reason="私聊对象已不在同一地点，私聊自动结束。"))
            private_id = None
        presence = d.presence_ids()
        if private_id:
            responders = [private_id]
        else:
            responders = list(presence)
            if npc_id and npc_id in responders:
                responders.remove(npc_id)
                responders.insert(0, npc_id)  # 点名的排前
            elif npc_id:
                try:
                    far_name = d.get_npc(npc_id).get("name", npc_id)
                    notices.append(f"（{far_name} 不在这里，没有听到你的话）")
                except ValueError:
                    pass

        # ── 记录玩家消息（私聊打标记）──
        self.history.append("user", user_message, private=bool(private_id),
                            to=private_id if private_id else None)

        # ── 无人在场：不调 LLM，玩家可通过地点切换移动 ──
        if not responders:
            notices.append("四周静悄悄的，这里没有其他人。")
            changes, events = d.finalize()
            notices.extend(d.take_quest_notices())
            d.save()
            return self._story_result([], notices, changes_all + changes, [], d,
                                      mode="chat", deep_think=False, debug={})

        # ── A1/A2 每轮只跑一次（全 responder 共享检索结果，省 token）──
        if private_id:
            hist_for_ai = self.history.get_recent(RECENT_HISTORY_COUNT, for_npc=private_id)
        else:
            hist_for_ai = self.history.get_recent(RECENT_HISTORY_COUNT, public_only=True)
        a1_result = self.a1.search(user_message, recent_history=hist_for_ai)
        trace.add("A1-搜索", "完成",
                   f"关键词={a1_result['keywords']}, 候选={a1_result['candidate_count']}条, "
                   f"LLM重排={a1_result.get('llm_reranked', False)}, 历史={a1_result.get('history_count', 0)}条")
        a2_result = self.a2.process(user_message, a1_result, identity_name=self.identity_name)
        trace.add("A2-思考", "完成",
                   f"模式={a2_result['mode']}, 深度思考={'是' if a2_result['deep_think_used'] else '否'}, "
                   f"筛选={len(a2_result['filtered_ids'])}条, "
                   f"教程命中={len(a2_result.get('tutorial_hits', []) or [])}条")
        a3_memories = self.a1.format_for_a3(a1_result, a2_result["filtered_ids"])

        # ── 8.24 群场剧场模式（≥2 NPC 在场且非私聊）：单次调用整场剧本 ──
        # 私聊/单聊维持原逐 NPC 路径：各自人设 + 各自权限门注入 + 各自一份回答
        replies: list[dict] = []
        private_requests: list[dict] = []
        if not private_id and len(responders) >= 2:
            story_section = d.build_stage_injection(responders)
            trace.add("导演", "权限门",
                      f"剧场模式, 在场={len(responders)}, 注入段长度={len(story_section)}")
            # 剧场调用以「演出者」身份驱动，各角色人设走注入段（行格式由 A3_STAGE_SCRIPT_RULE 约束）
            stage_persona = {
                "name": "群像剧场演出者",
                "personality": "你本轮同时扮演注入中列出的全部在场角色，按各角色性格切换口吻",
                "world_setting": d.story.get("setting", {}).get("world_desc", ""),
            }
            a3_result = self.a3.generate(
                user_message, a2_result, a3_memories, stage_persona,
                recent_history=hist_for_ai,
                search_text=a2_result.get("search_text", ""),
                story_section=story_section,
                director_reminder=d.end_prompt_reminder(),
                skip_fact_check=True,
                time_override=d.clock_text(),
            )
            trace.add("A3-输出", "完成",
                      f"剧场整场剧本, 原始长度={len(a3_result['reply'])}")
            # 逐行校验分发（不在场裁行/标签按行角色分发/节奏硬限裁切）
            staged = d.apply_stage_reply(a3_result["reply"], responders)
            replies = staged["replies"]
            private_requests = staged["private_requests"]
            notices.extend(staged["notices"])
            changes_all.extend(staged["changes"])
            # 同步清洗 A3 内部对话历史（整场剧本带说话人与状态标签，防回流后续提示词）
            if self.a3.conversation_history and \
                    self.a3.conversation_history[-1].get("role") == "assistant":
                self.a3.conversation_history[-1]["content"] = "\n".join(
                    f"{it['name']}: {it['reply']}" for it in replies)
            # 逐行：历史记录 + A4 记忆（快照按行角色手工构造）
            for item in replies:
                item["stage"] = True  # 前端标记：纯文本渲染 + 间隔弹出
                self.history.append("npc", item["reply"], a2_result["mode"],
                                    speaker=item["npc_id"], speaker_name=item["name"])
                snap = {
                    "user_message": user_message,
                    "npc_reply": item["reply"],
                    "summary_hint": (
                        f"用户: {user_message[:60]} | NPC: {item['name']}: {item['reply'][:60]}"
                    ),
                    "mode": a2_result["mode"],
                    "relevance": a2_result.get("relevance", "world_related"),
                    "memories_used": [],
                    "time_context": d.clock_text(),
                }
                self._run_a4({"snapshot": snap}, d.persona_for(item["npc_id"]),
                             story_brief=d.state_brief_for_ai(),
                             director=d, allow_secret_proposals=False)
        else:
            for rid in responders:
                npc = d.get_npc(rid)
                persona = d.persona_for(rid)
                story_section = d.build_injection(rid)
                trace.add("导演", "权限门", f"NPC={rid}, 注入段长度={len(story_section)}")
                # 私聊时全场只有该 NPC，历史过滤等价；群场按 NPC 过滤他人私聊
                recent_history = (hist_for_ai if private_id
                                  else self.history.get_recent(RECENT_HISTORY_COUNT, for_npc=rid))
                a3_result = self.a3.generate(
                    user_message, a2_result, a3_memories, persona,
                    recent_history=recent_history,
                    search_text=a2_result.get("search_text", ""),
                    story_section=story_section,
                    director_reminder=d.end_prompt_reminder(),
                    time_override=d.clock_text(),
                )
                trace.add("A3-输出", "完成",
                          f"NPC={rid}, 回复长度={len(a3_result['reply'])}")

                # 后置解析：剥 [STATE:]/[PRIVATE_REQ]/[NOTE:] 标签 → 白名单应用 flags/玩家地点
                parsed = d.apply_reply(a3_result["reply"], npc.get("name", rid), npc_id=rid)
                clean = parsed["clean"]
                changes_all.extend(parsed["changes"])
                notices.extend(parsed["notices"])
                # [NOTE:] 重要信息入冒险手册（含时钟快照，去重后落盘）
                d.add_notes(npc.get("name", rid), parsed.get("notes", []))
                # 8.20c 兜底结算：时间投满的动作任务，与发布人过完任意一轮对话
                # 即由程序确认汇报（AI 本轮主动提议过则跳过），玩家不必反复口头汇报
                notices.extend(d.auto_settle_report(rid))
                # 同步清洗 A3 内部对话历史，防标签回流到后续提示词
                if self.a3.conversation_history and \
                        self.a3.conversation_history[-1].get("role") == "assistant":
                    self.a3.conversation_history[-1]["content"] = clean
                a3_result["reply"] = clean
                # snapshot 的 npc_reply 一并替换——A4 记忆摘要绝不进标签
                if isinstance(a3_result.get("snapshot"), dict):
                    a3_result["snapshot"]["npc_reply"] = clean
                    # 8.24d 双时间修复：A4 摘要用游戏内时间，不再泄漏现实日期
                    a3_result["snapshot"].setdefault("time_context", d.clock_text())

                replies.append({"npc_id": rid, "name": npc.get("name", rid), "reply": clean})

                # 记录 NPC 回复（私聊打 private + speaker 标记）
                self.history.append(
                    "npc", clean, a2_result["mode"],
                    speaker=rid, speaker_name=npc.get("name", rid),
                    private=bool(private_id),
                    to=private_id if private_id else None,
                )

                if parsed["private_requests"] and not private_id:
                    private_requests.append({"npc_id": rid, "name": npc.get("name", rid)})

                # 8.24d：A4 升级为状态总分析（主区玩家状态 + 尾区 JSON 提议交导演白名单）
                self._run_a4(a3_result, persona,
                             story_brief=d.state_brief_for_ai(),
                             director=d)

        # ── 8.24b 兜底「出过台词即相识」：本轮真的对玩家说过话的 NPC，程序直接记相识 ──
        # （单聊/剧场两路共用；相识是照面级别的确定事实，不依赖 AI 打标）
        d.auto_meet([r["npc_id"] for r in replies])

        # ── 程序判定：任务 accept/done flags → 里程碑 → 多终局（先满足先生效）──
        changes, events = d.finalize()
        changes_all.extend(changes)
        # 任务跃迁系统提示（接取/可完成/失败）拼进 notices 给玩家看
        notices.extend(d.take_quest_notices())
        d.save()

        # 旁白只在剧情推进（里程碑触发）时生成；任何失败静默降级为空串
        narration = ""
        if events:
            try:
                from story import narrator
                narration = narrator.narrate(events, d.story)
            except Exception as e:
                logger.warning(f"旁白生成失败（降级为空）: {e}")
                narration = ""

        return self._story_result(
            replies, notices, changes_all, private_requests, d,
            mode=a2_result["mode"], deep_think=a2_result.get("deep_think_used", False),
            narration=narration,
            debug={
                "a1_candidates": a1_result["candidate_count"],
                "a1_keywords": a1_result["keywords"],
                "a2_mode": a2_result["mode"],
                "a2_filtered": len(a2_result["filtered_ids"]),
                "responders": [r["npc_id"] for r in replies],
                "changes": changes_all,
            },
        )

    def _story_result(self, replies: list, notices: list, changes: list,
                      private_requests: list, director, mode: str, deep_think: bool,
                      narration: str = "", debug: dict = None) -> dict:
        """剧情副本统一返回结构（replies 多说话人；reply 为首条兼容字段）。"""
        story_state = director.get_public_state()
        return {
            "reply": replies[0]["reply"] if replies else "",
            "replies": replies,
            "notices": notices,
            "changes": changes,
            "private_requests": private_requests,
            "narration": narration,
            "mode": mode,
            "deep_think_used": deep_think,
            "story_state": story_state,
            "ended": bool(story_state.get("ended")),
            "debug": debug or {},
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