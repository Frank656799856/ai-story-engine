#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A3 — 最终输出 Agent

职责：
  1. 结合核心记忆（L1） + A2筛选后的记忆 + 用户消息，生成 NPC 回复
  2. 闲聊模式：自然丰富，NPC 人设主导，输出与主流大模型对齐
  3. 知识模式：严谨准确，但保留 NPC 语气
  4. 支持多轮对话上下文（conversation_history）
"""

import json
import logging
import re
from typing import Optional

from agent.ai_client import client
from agent import prompts as PROMPTS
from tools.time_utils import build_time_context
from tools.npc_state import get_npc_state

logger = logging.getLogger("a3_output")


class A3OutputAgent:
    """A3 最终输出 Agent"""

    def __init__(self, identity_name: str = "default"):
        self.identity_name = identity_name
        self.conversation_history: list[dict] = []

    def generate(
        self,
        user_message: str,
        a2_result: dict,
        a3_memories: list[dict],
        persona: dict = None,
        recent_history: list[dict] = None,
        search_text: str = "",
    ) -> dict:
        mode = a2_result.get("mode", "chat")
        need_external = a2_result.get("need_external_knowledge", False)
        # 三档相关性（A2 分类附带）：world_related / chitchat / irrelevant
        relevance = a2_result.get("relevance", "world_related")

        # 全量游戏库（整段作为稳定前缀注入；仅当该身份有游戏数据库时非空）
        game_db_section = self._build_game_db_section()

        # ── T-004 教程：仅 knowledge 模式注入；命中(或兜底检索到)才启用教程分支 ──
        tutorial_active = False
        tutorial_section = ""
        if mode == "knowledge":
            tutorial_text = self._build_tutorial_text(a2_result)
            if tutorial_text:
                tutorial_active = True
                tutorial_section = tutorial_text

        system_prompt = self._build_system_prompt(
            persona=persona,
            a3_memories=a3_memories,
            mode=mode,
            need_external=need_external,
            search_text=search_text,
            game_db_section=game_db_section,
            tutorial_section=tutorial_section if tutorial_active else "",
            relevance=relevance,
        )
        user_prompt = self._build_user_prompt(user_message, recent_history)

        try:
            reply = client.chat(
                system_prompt=system_prompt,
                user_message=user_prompt,
                # T-004: 教程分支降温度（0.5 → 0.2），忠实照搬教程原话；chat/非教程 knowledge 照旧
                temperature=0.2 if tutorial_active else (0.8 if mode == "chat" else 0.5),
                max_tokens=4096,
            )
        except Exception as e:
            logger.error(f"A3: LLM调用失败: {e}")
            reply = "（抱歉，我现在有点走神了...）"

        # ── 游戏数据库生成后校验（仅当该身份接入了数据库） ──
        # 生成后先校验；若出现反数据库事实，将「原文 + 必要提示词 + 修改建议」
        # 三者合并，让 LLM 重新生成一次。
        # T-004: 知识模式教程分支不跑反事实校验（经评估幻觉不严重，靠强提示词兜底）。
        game_db_checked = False
        game_db_revised = False
        if self._has_game_db() and not tutorial_active:
            try:
                revised = self._verify_and_revise(reply, system_prompt, user_message)
                game_db_checked = True
                if revised is not None and revised != reply:
                    logger.info(f"A3: 检测到反数据库事实，已重新生成修正回复")
                    reply = revised
                    game_db_revised = True
            except Exception as e:
                logger.warning(f"A3: 游戏数据库校验失败，保留原回复: {e}")

        self.conversation_history.append({"role": "user", "content": user_message})
        self.conversation_history.append({"role": "assistant", "content": reply})
        if len(self.conversation_history) > 20:
            self.conversation_history = self.conversation_history[-20:]

        summary_hint = f"用户: {user_message[:60]} | NPC: {reply[:60]}"
        snapshot = {
            "user_message": user_message,
            "npc_reply": reply,
            "summary_hint": summary_hint,
            "mode": mode,
            "relevance": relevance,
            "memories_used": [
                {"id": m.get("id"), "summary": m.get("summary", "")}
                for m in a3_memories[:10]
            ],
        }

        logger.info(f"A3 输出完成: 模式={mode}, 回复长度={len(reply)}, 游戏核对={bool(game_db_section)}")
        return {
            "reply": reply,
            "mode": mode,
            "snapshot": snapshot,
            "game_db_used": bool(game_db_section),
            "game_db_checked": game_db_checked,
            "game_db_revised": game_db_revised,
        }

    def _build_system_prompt(
        self,
        persona: dict,
        a3_memories: list[dict],
        mode: str,
        need_external: bool,
        search_text: str = "",
        game_db_section: str = "",
        tutorial_section: str = "",
        relevance: str = "world_related",
    ) -> str:
        parts = []

        # ── 固定前缀区（身份/风格/模式基础，保持稳定以命中厂商前缀缓存） ──

        # NPC 人设（详细描述）
        if persona:
            name = persona.get("name", "NPC")
            personality = persona.get("personality", "友好")
            world = persona.get("world_setting", "")
            parts.append(
                PROMPTS.A3_IDENTITY_TEMPLATE.format(
                    name=name,
                    personality=personality,
                    world=world if world else "一个充满冒险与奇幻的世界",
                )
            )
        else:
            parts.append(PROMPTS.A3_IDENTITY_DEFAULT)

        # 说话语言（档案 language 字段，缺省一律中文——旧档案/安卓已装数据没有该字段时同样生效）
        lang = (persona.get("language") or "").strip() if persona else ""
        parts.append(PROMPTS.A3_LANGUAGE_RULE.format(language=lang or "中文"))

        # 输出风格（核心改进）
        parts.append(PROMPTS.A3_FORMAT_AND_STYLE)

        # 模式基础指令（同一模式下固定不变，放在固定前缀区）
        if mode == "knowledge":
            parts.append(PROMPTS.A3_MODE_KNOWLEDGE_BASE)
        else:
            parts.append(PROMPTS.A3_MODE_CHAT)

        # 时间感引导（固定内容；具体当前时间由 _build_user_prompt 追加在 user 末尾）
        parts.append(PROMPTS.A3_TIME_SECTION_TEMPLATE)

        # 全量游戏库（同一身份的库静态不变，作为稳定前缀放在动态区之前，可命中缓存）
        if game_db_section:
            parts.append(game_db_section)

        # ── 动态区（记忆/搜索等易变内容全部后置，避免打断前缀缓存） ──

        # 核心记忆 + 筛选记忆
        if a3_memories:
            core_mems = [m for m in a3_memories if m.get("layer") == "core"]
            other_mems = [m for m in a3_memories if m.get("layer") != "core"]

            parts.append("## 你的记忆\n\n")
            if core_mems:
                parts.append("**核心设定（必须遵守）：**\n")
                for m in core_mems:
                    parts.append(f"- {m.get('summary', '')}\n")
                parts.append("\n")

            if other_mems:
                parts.append("**相关经历（可以参考融入对话）：**\n")
                for m in other_mems:
                    parts.append(f"- {m.get('summary', '')}\n")

        # 搜索/链接内容（知识模式动态段）
        if mode == "knowledge":
            if search_text:
                parts.append(
                    PROMPTS.A3_MODE_KNOWLEDGE_SEARCH.format(search_text=search_text)
                )
            elif need_external:
                parts.append(PROMPTS.A3_MODE_KNOWLEDGE_NO_MEMORY)

        # 教程段（T-004，仅 knowledge 模式有教程命中时；追加在既有提示词后，不删任何既有提示词）
        if tutorial_section:
            parts.append(PROMPTS.A3_TUTORIAL_SECTION.format(tutorial_text=tutorial_section))
            parts.append(PROMPTS.A3_TUTORIAL_STYLE)

        # 无关输入三档分流（教程命中时教程段优先，不注入分流引导）
        if not tutorial_section:
            if relevance == "irrelevant":
                parts.append(PROMPTS.A3_IRRELEVANT_GUIDE)
            elif relevance == "chitchat":
                parts.append(PROMPTS.A3_CHITCHAT_GUIDE)

        return "".join(parts)

    def _get_identity_name(self) -> str:
        """返回当前身份名（用于加载游戏数据库）"""
        return self.identity_name

    def _build_game_db_section(self) -> str:
        """
        构建注入 A3 主生成的游戏库提示段。

        策略（方案 B 变体）：
          - 直接注入**全量游戏库**（同一身份库静态不变，整段作为稳定前缀放入
            固定前缀区，可命中厂商前缀缓存；且不再依赖 A1 打标签决定带哪部分）。
          - 不含数据库时返回空字符串（不注入）。
        """
        try:
            from tools.game_db import get_game_db, CATEGORIES
            db = get_game_db(self._get_identity_name())
        except Exception as e:
            logger.debug(f"A3: 游戏数据库不可用: {e}")
            return ""
        if not db.available:
            return ""

        # 全量取回：每个分类都取全部条目（手写小库，量级很小）
        entries = db.search_by_categories(CATEGORIES, max_per_cat=1000)
        world = db.get_world_setting()
        entries_text = db.format_entries(entries) if entries else ""
        return PROMPTS.A3_GAME_DB_SECTION.format(
            world_name=world.get("world_name", "未知世界"),
            world_desc=world.get("world_description", ""),
            entries_text=entries_text or "（暂无条目）",
        )

    def _build_tutorial_text(self, a2_result: dict) -> str:
        """构建教程注入文本（T-004）。

        优先用 A2 程序侧命中的教程条目（tutorial_hits）；若 A2 未命中，
        在 knowledge 模式下用 A3 对「教程」分类做兜底检索（防 A2 漏判）。
        无可用教程时返回 ""（不启用教程分支）。
        """
        try:
            from tools.game_db import get_game_db
            db = get_game_db(self._get_identity_name())
        except Exception:
            return ""
        if not db.available:
            return ""

        hits = list(a2_result.get("tutorial_hits", []) or [])
        if not hits:
            # 兜底：knowledge 模式补检教程分类（受限于 max_per_cat）
            hits = db.search_by_categories(["教程"], max_per_cat=5)
        if not hits:
            return ""

        lines = []
        for t in hits:
            name = t.get("name", "")
            desc = t.get("description", "")
            lines.append(f"[教程] {name}: {desc}")
        return "\n".join(lines)

    # ── 游戏数据库生成后校验与反事实重生成 ──────────────

    def _has_game_db(self) -> bool:
        """该身份是否接入了游戏数据库"""
        try:
            from tools.game_db import get_game_db
            return get_game_db(self._get_identity_name()).available
        except Exception:
            return False

    def _build_validation_whitelist(self, db, categories: list[str]) -> str:
        """
        构建反事实校验的"已确认合理"白名单文本（方案 A + 方案 B）。

        白名单包含三类，这些在抽取/匹配时永远不算编造：
          1. 游戏库内已记载的所有条目名（方案 A 的核心）
          2. 角色自身名字（方案 B：NPC 自称不算编造）
          3. reference.source_game 参考来源提到的专名（方案 B：参考游戏内的设定当背景）
        """
        if not db.available:
            return ""
        lines = []
        # 1) 库内条目，按分类列出
        all_entries = db.search_by_categories(categories, max_per_cat=1000)
        by_cat: dict[str, list[str]] = {}
        for e in all_entries:
            by_cat.setdefault(e.get("category", "其他"), []).append(e["name"])
        for cat, names in by_cat.items():
            lines.append(f"- [{cat}] {', '.join(names)}")
        # 2) 角色自身名字
        if self.identity_name:
            lines.append(f"- 角色自身: {self.identity_name}")
        # 3) 参考来源（方案 B）
        try:
            ref = db.get_reference()
            src = (ref.get("source_game") or "").strip()
        except Exception:
            src = ""
        if src:
            lines.append(f"- 参考来源（视为世界背景设定）: {src}")
        return "\n".join(lines)

    def _check_game_db_facts(self, reply: str, user_message: str) -> Optional[dict]:
        """基于程序匹配的生成后校验。

        流程（LLM 只负责抽取，命中判定交给 Python）：
          1. LLM 从 NPC 回复中抽取所有游戏相关词汇/实体（A3_GAME_TERM_EXTRACT_SYSTEM）
          2. Python 逐个用游戏数据库匹配：
               - 命中的实体（重合）：记录数量与名称
               - 未命中的实体（库中不存在）：视为"疑似编造/未知事物"
          3. 返回 {"ok": bool, "issues": [...], "suggestions": str,
                    "matched": [...], "missing": [...]}

        Args:
            reply: NPC 生成的回复
            user_message: 玩家原始消息

        Returns:
            校验结果 dict，或 None（无游戏库/解析失败）
        """
        try:
            from tools.game_db import get_game_db
            db = get_game_db(self._get_identity_name())
        except Exception:
            return None
        if not db.available:
            return None

        # ── 放宽模式：参考游戏角色 ─────────────────────────────
        # 若数据库填了 reference.source_game（模仿自某个真实游戏/角色），
        # 该角色靠大模型内部对该游戏的了解来演，反而比手写小库更全，
        # 词级反事实校验会误拦真实设定（如「社奉行」「八重堂」）。
        # 因此这类角色一律跳过词级反事实校验，直接放行。
        try:
            if db.get_reference().get("source_game"):
                logger.info(
                    f"A3: [{self.identity_name}] 为参考游戏角色"
                    f"（source_game={db.get_reference().get('source_game')}），"
                    f"跳过词级反事实校验（放宽模式）"
                )
                return {
                    "ok": True,
                    "issues": [],
                    "suggestions": "",
                    "matched": [],
                    "missing": [],
                    "relaxed": True,
                }
        except Exception:
            pass

        # 第 1 步：LLM 抽取"疑似编造的重要实体"（方案 A：带已知白名单）
        #   白名单 = 库内所有条目名 + 身份名 + 参考来源（这些永远不算编造）
        from tools.game_db import CATEGORIES as DB_CATEGORIES
        whitelist = self._build_validation_whitelist(db, DB_CATEGORIES)
        extract_prompt = PROMPTS.A3_GAME_TERM_EXTRACT_SYSTEM.format(whitelist=whitelist or "（名单为空）")
        response = client.chat(
            system_prompt=extract_prompt,
            user_message=f"NPC 回复：\n{reply}",
            temperature=0.1,
            max_tokens=800,
            cache_pad=True,
        )
        extracted = self._parse_entities(response)
        if extracted is None:
            return None

        # 第 2 步：Python 程序去数据库匹配
        matched, missing = self._match_terms_in_db(db, extracted)

        # 玩家自己提到的词不算 NPC 编造（玩家引入的词汇）
        missing_without_player = [
            item for item in missing
            if item.get("name", "") not in (user_message or "")
        ]

        ok = len(missing_without_player) == 0
        if ok:
            return {"ok": True, "issues": [], "suggestions": "", "matched": matched, "missing": []}

        # 校验发现错误：打印原始回复 + 匹配明细，便于排查可疑规则
        logger.warning(
            f"[校验拦截] 原始回复全文: {reply}"
        )
        logger.warning(
            f"[校验拦截] 玩家消息: {user_message}"
        )
        logger.warning(
            f"[校验拦截] 重合={[m.get('name') for m in matched]}"
        )
        logger.warning(
            f"[校验拦截] 不存在但玩家提过(豁免)={[m.get('name') for m in missing if m.get('name') in (user_message or '')]}"
        )
        logger.warning(
            f"[校验拦截] 不存在且非玩家提出(判为编造)={[m.get('name') for m in missing_without_player]}"
        )

        issues = []
        for item in missing_without_player:
            issues.append(f"回复中提到「{item['name']}」，但游戏数据库中无此记载")
        suggestions = (
            "NPC 回复中提到了以下数据库中不存在的事物："
            f"{'、'.join(i['name'] for i in missing_without_player)}。"
            "若玩家问到的是你并不了解的设定，应坦诚表示自己不知道或所知有限"
            "（\"这超出了我的知识范围\"或\"我只听过模糊传闻\"），不要编造细节、不要凭空补全设定。"
        )
        logger.info(
            f"A3: 程序匹配游戏库: 重合={len(matched)}, 不存在={len(missing_without_player)}, "
            f"共抽取={len(extracted)}"
        )
        return {
            "ok": False,
            "issues": issues,
            "suggestions": suggestions,
            "matched": matched,
            "missing": missing_without_player,
        }

    def _parse_entities(self, text: str) -> Optional[list]:
        """解析抽取器输出的 JSON 数组 [{name, category}]，失败返回 None"""
        if not text:
            return None
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        try:
            data = json.loads(text)
            return data if isinstance(data, list) else None
        except json.JSONDecodeError:
            match = re.search(r"\[[\s\S]*\]", text)
            if match:
                try:
                    data = json.loads(match.group())
                    return data if isinstance(data, list) else None
                except json.JSONDecodeError:
                    return None
            return None

    def _match_terms_in_db(self, db, extracted: list) -> tuple[list, list]:
        """把抽取的词汇与数据库匹配。

        Returns:
            (matched, missing)
            matched: 库中命中的 [{name, category}]
            missing: 库中不存在的疑似编造 [{name, category}]
        """
        matched = []
        missing = []
        seen = set()
        # 取出库内全部实体名，用于子串双向包含匹配（含关系命中）
        known_names = []
        if db is not None and hasattr(db, "_entity_index"):
            known_names = list(db._entity_index.keys())
        for item in extracted or []:
            name = (item.get("name") or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            if self._contains_match(name, known_names):
                matched.append({"name": name, "category": item.get("category", "")})
            else:
                missing.append({"name": name, "category": item.get("category", "")})
        return matched, missing

    @staticmethod
    def _contains_match(name: str, known_names: list) -> bool:
        """子串双向包含匹配：词汇是某库名的子串，或库名是词汇的子串，都算命中已知设定。

        例如抽取到「黑火」而库内有「黑火案」→ 命中；抽取到「蹦蹦炸弹精」库内有「蹦蹦炸弹」→ 命中。
        避免把库内已知实体的简称/全称/扩展说法的偏差误判为编造。
        """
        if not name:
            return False
        for known in known_names:
            if not known:
                continue
            if name == known or name in known or known in name:
                return True
        return False

    def _verify_and_revise(self, reply: str, system_prompt: str, user_message: str) -> Optional[str]:
        """
        生成后校验 + 反事实重生成链路。

        流程：
          1. 抽取 NPC 回复中的游戏词汇，由 Python 去数据库匹配
          2. 全部命中 → 返回 None（无需修正）
          3. 存在库中不存在的事物 → 将「原文 + 必要提示词(原 system prompt) + 修改建议」
             三者合并，让 LLM 重新生成一次，返回修正后的回复

        Returns:
            修正后的回复，或 None（未校验/无需修正/无法修订）
        """
        check = self._check_game_db_facts(reply, user_message)
        if check is None or check.get("ok"):
            return None

        logger.info(f"A3: 反数据库事实校验未通过，共 {len(check.get('issues', []))} 处问题")
        # 建议式：把被标记的新名词以清单形式列出，由模型自行判断去留
        marked = [m.get("name") for m in check.get("missing", [])]
        if not marked:
            return None
        suggestions = "、".join(marked)
        revise_user = PROMPTS.A3_GAME_DB_REVISE_USER.format(
            original=reply,
            suggestions=suggestions,
        )
        # 必要提示词（原 system_prompt）+ 修正指令 合并为新的 system prompt
        revised = client.chat(
            system_prompt=system_prompt + PROMPTS.A3_GAME_DB_REVISE_SYSTEM,
            user_message=revise_user,
            temperature=0.6,
            max_tokens=4096,
        )
        revised = revised.strip()
        return revised if revised else None

    def _build_user_prompt(self, user_message: str, recent_history: list[dict] = None) -> str:
        # 使用持久化的历史记录
        history = recent_history or self.conversation_history
        if len(history) <= 4:
            base = user_message
        else:
            recent = history[-6:]
            history_text = "\n".join(
                f"{'玩家' if m.get('role') == 'user' else 'NPC'}: {m.get('content', '')[:200]}"
                for m in recent
            )
            base = f"最近对话：\n{history_text}\n\n玩家: {user_message}"
        # 动态上下文（易变内容放 user 消息末尾，保持 system prompt 稳定以命中缓存）：
        # 1) 当前时间（背景参考，降权处理：system 引导「勿强调」，此处措辞用「时间参考」）
        # 2) 角色当前状态（玩家手动填写，如"可莉现在在禁闭室"）
        dynamic = [f"（时间参考：{build_time_context()}）"]
        npc_state = get_npc_state(self.identity_name)
        if npc_state:
            dynamic.append(f"（角色当前状态：{npc_state}）")
        return f"{base}\n\n" + "\n".join(dynamic)

    def clear_history(self):
        self.conversation_history = []

    def get_snapshot(self) -> dict:
        return {
            "history_length": len(self.conversation_history),
            "last_user": (
                self.conversation_history[-2]["content"][:100]
                if len(self.conversation_history) >= 2 else ""
            ),
            "last_npc": (
                self.conversation_history[-1]["content"][:100]
                if self.conversation_history else ""
            ),
        }


a3 = A3OutputAgent()