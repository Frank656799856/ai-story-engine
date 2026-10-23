#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A2 — 深度思考 Agent

主任务：四步推理筛选记忆（相关性初筛→冲突检测→证据空白检测→最终筛选）
副任务：任务分类（闲聊/知识），控制记忆权重
自主决策：根据 A1 候选记忆数据 + 用户问题复杂度，决定是否启用深度思考

工作流程：
  ┌──────────────────────────────────────────────────────────────────┐
  │ 输入: 用户消息 + A1 搜索结果                                       │
  │                                                                  │
  │ ① 任务分类（轻量，始终执行）                                       │
  │    → 判断 闲聊/知识 模式，设置记忆权重                              │
  │                                                                  │
  │ ② 自主决策：是否启用深度思考？                                     │
  │    决策依据：                                                      │
  │    - 候选记忆 > 5条 → 倾向于启用                                   │
  │    - 记忆间存在冲突 → 启用                                         │
  │    - 相关性分数普遍偏低 → 启用                                     │
  │    - 简单闲聊/简单知识 → 跳过                                     │
  │                                                                  │
  │ ③ 深度思考四步（启用时）:                                          │
  │    ① 相关性初筛 → ② 冲突检测 → ③ 证据空白检测 → ④ 最终筛选       │
  │                                                                  │
  │ 输出: 模式标签 + 筛选后记忆ID集合 + 推理链路 → A3                   │
  └──────────────────────────────────────────────────────────────────┘
"""

import json
import logging
import re
from typing import Optional

from agent.ai_client import client
from agent import prompts as PROMPTS

logger = logging.getLogger("a2_deep_think")

# 深度思考触发阈值
DEEP_THINK_MIN_CANDIDATES = 5       # 候选记忆超过此数倾向于启用
DEEP_THINK_LOW_SCORE_THRESHOLD = 0.3  # 平均分低于此值倾向于启用


class A2DeepThinkAgent:
    """A2 深度思考 Agent"""

    def __init__(self):
        self.mode = "chat"           # 当前模式
        self.relevance = "world_related"  # 三档相关性: world_related / chitchat / irrelevant
        self.deep_think_used = False  # 本轮是否启用了深度思考
        self.reasoning_chain = []    # 推理链路

    def process(
        self,
        user_message: str,
        a1_result: dict,
        identity_name: str = "",
    ) -> dict:
        """
        处理本轮对话。

        Args:
            user_message: 用户输入消息
            a1_result: A1 搜索结果
            identity_name: 当前身份名（用于加载游戏库做教程快查，T-004）

        Returns:
            {
                "mode": "chat" | "knowledge",
                "deep_think_used": bool,
                "filtered_ids": set,        # 筛选后的记忆ID集合
                "reasoning_chain": [...],   # 推理链路
                "memory_weights": {"retrieval": 1.0, "write": 1.0},
                "need_external_knowledge": bool,  # 是否需要外部知识
                "tutorial_hits": [...],     # T-004: 命中的游戏库教程条目（透传 A3）
            }
        """
        self.reasoning_chain = []
        self.deep_think_used = False

        candidates = a1_result.get("candidates", [])
        core_memories = a1_result.get("core_memories", [])

        # ── 步骤1: 任务分类（始终执行） ──
        # 教程快查先于 LLM：游戏库教程关键词/名称命中 → 直接判 knowledge（省一次 LLM 调用）
        tutorial_hits = self._match_tutorials(user_message, identity_name)
        if tutorial_hits:
            self.mode = "knowledge"
            self.relevance = "world_related"
            self.reasoning_chain.append(
                f"[分类] 教程命中（{'、'.join(t['name'] for t in tutorial_hits)}），判定为knowledge模式"
            )
        else:
            self.mode, self.relevance = self._classify_task(user_message, a1_result)
            self.reasoning_chain.append(f"[分类] 判定为{self.mode}模式，相关性={self.relevance}")

        # ── 步骤2: 自主决策是否启用深度思考 ──
        should_deep_think = self._decide_deep_think(user_message, a1_result)

        if should_deep_think:
            self.reasoning_chain.append("[决策] 启用深度思考四步推理")
            filtered_ids, need_ext = self._deep_think(user_message, a1_result)
            self.deep_think_used = True
        else:
            self.reasoning_chain.append("[决策] 跳过深度思考，直接放行")
            # 不放行——直接返回所有候选记忆ID
            filtered_ids = {
                c.get("memory_id") for c in candidates
                if c.get("memory_id")
            }
            need_ext = False

        # ── 步骤3: 设置记忆权重 ──
        memory_weights = self._get_memory_weights()

        # ── 步骤4: 链接内容读取（仅当用户提供确切链接时触发，否则不联网） ──
        search_text = ""
        search_results = []
        search_used = False
        if self._has_link(user_message):
            logger.info(f"A2: 检测到用户链接，读取链接内容")
            self.reasoning_chain.append("[链接] 检测到用户提供的链接，读取链接内容")
            try:
                search_results, search_text = self._do_read_link(user_message)
                search_used = True
                self.reasoning_chain.append(f"[链接] 内容读取完成，共 {len(search_results)} 个链接")
            except Exception as e:
                self.reasoning_chain.append(f"[链接] 内容读取失败: {e}")

        result = {
            "mode": self.mode,
            "relevance": self.relevance,
            "deep_think_used": self.deep_think_used,
            "filtered_ids": filtered_ids,
            "reasoning_chain": self.reasoning_chain,
            "memory_weights": memory_weights,
            "need_external_knowledge": need_ext,
            "search_text": search_text,
            "search_results": search_results,
            "search_used": search_used,
            "tutorial_hits": tutorial_hits,
        }

        logger.info(
            f"A2 完成: 模式={self.mode}, "
            f"深度思考={'是' if self.deep_think_used else '否'}, "
            f"筛选后={len(filtered_ids)}条记忆"
        )

        return result

    # ── 链接内容读取（原 Web 搜索已关闭） ──────────────────

    def _has_link(self, text: str) -> bool:
        """判断消息中是否包含完整链接（http/https）"""
        try:
            from tools.web_search import web_search
            return bool(web_search.extract_links(text))
        except ImportError:
            return False

    def _do_read_link(self, message: str) -> tuple:
        """读取消息中的链接内容，返回 (结构化结果列表, LLM 可读文本)

        消息中无链接时返回 ([], "")，不触发任何网络请求。
        """
        try:
            from tools.web_search import web_search
            return web_search.read_links(message)
        except ImportError:
            return [], "（链接内容读取模块未安装：pip install requests beautifulsoup4）"
        except Exception as e:
            return [], f"（链接内容读取失败: {e}）"

    # ── 任务分类 ──────────────────────────────────────────

    def _match_tutorials(self, message: str, identity_name: str) -> list[dict]:
        """教程快查（T-004）：游戏库「教程」关键词/名称子串命中即返回条目。

        程序侧精确匹配，无 LLM 开销；身份未接游戏库或无教程条目时返回空。
        """
        if not identity_name or not message or not message.strip():
            return []
        try:
            from tools.game_db import get_game_db
            db = get_game_db(identity_name)
        except Exception:
            return []
        if not db.available:
            return []
        return db.match_tutorial(message)

    def _classify_task(self, user_message: str, a1_result: dict) -> tuple:
        """判断对话模式与相关性三档

        Returns:
            (mode, relevance):
              mode: "chat" | "knowledge"（下游既有语义，irrelevant 以 chat 兜底）
              relevance: "world_related" 世界观相关 / "chitchat" 日常闲聊 / "irrelevant" 完全无关
        """
        msg = user_message.strip()

        # 快速跳过：极短消息几乎不可能是知识提问
        if len(msg) < 6:
            return "chat", "chitchat"

        # 快速跳过：纯情感/社交类消息
        social_only = [
            "你好", "嗨", "在吗", "再见", "谢谢", "哈哈", "嗯", "哦",
            "晚安", "早安", "好的", "行", "ok", "hi", "hello", "bye",
        ]
        if msg.lower() in social_only:
            return "chat", "chitchat"

        # LLM 语境判断（chat / knowledge / irrelevant 三选一）
        try:
            label = self._llm_classify(msg)
        except Exception as e:
            logger.debug(f"A2: LLM 分类失败，使用规则降级: {e}")
            label = self._rule_classify(msg)

        if label == "irrelevant":
            # 现实工具活/完全无关：mode 以 chat 兜底，relevance 交给 A3/A4 处理
            return "chat", "irrelevant"
        if label == "knowledge":
            return "knowledge", "world_related"
        return "chat", "world_related"

    def _llm_classify(self, message: str) -> str:
        """用 LLM 判断对话类型：chat / knowledge / irrelevant 三选一"""
        prompt = f"""判断以下玩家消息属于哪种类型，只回复一个词：chat、knowledge 或 irrelevant。

判断标准：
- chat：闲聊、打招呼、分享感受、聊剧情、角色扮演、讨论虚构世界观
- knowledge：询问世界观内的事实性知识、请求解释概念、游戏教程向问题
- irrelevant：想让 NPC 干现实世界的工具活（写作业、写代码、翻译、算数、查资料、订票等），
  或消息内容与世界观、剧情、日常问候都完全无关

注意：
- 玩家在聊虚构世界观（比如「魔法是怎么运作的」「龙为什么会喷火」）→ chat
- 玩家问世界观内设定（比如「风元素粒子是什么」）或问怎么玩 → knowledge
- 玩家让 NPC 写作业、翻译、算数 → irrelevant

玩家消息：「{message}」

类型："""

        response = client.chat(
            system_prompt=PROMPTS.A2_CLASSIFY_SYSTEM,
            user_message=prompt,
            temperature=0.1,
            max_tokens=12,
        )

        response = response.strip().lower()
        if "irrelevant" in response:
            return "irrelevant"
        if "knowledge" in response:
            return "knowledge"
        return "chat"

    def _rule_classify(self, message: str) -> str:
        """规则降级分类（仅在 LLM 不可用时使用）"""
        msg_lower = message.lower().strip()

        # 强现实任务信号：这些全是现实世界的工具活，NPC 干不了
        strong_irrelevant = [
            "等于多少", "计算", "怎么算", "公式", "定义",
            "历史上", "公元", "科学", "物理", "化学", "数学",
            "代码", "编程", "python", "java", "算法",
            "翻译", "用英语怎么说", "用日语怎么说",
        ]
        for pattern in strong_irrelevant:
            if pattern in msg_lower:
                return "irrelevant"

        # 有问号且长度 > 10 字，可能偏知识（但权重降低）
        if "?" in message or "？" in message:
            if len(message) > 15:
                return "knowledge"

        return "chat"

    # ── 深度思考决策 ──────────────────────────────────────

    def _decide_deep_think(self, user_message: str, a1_result: dict) -> bool:
        """自主决定是否启用深度思考四步推理"""
        candidates = a1_result.get("candidates", [])

        # 候选记忆太少，不值得深度思考
        if len(candidates) <= 2:
            return False

        # 用户消息很短（< 5字），可能是简单闲聊
        if len(user_message.strip()) < 5:
            return False

        # 候选记忆超过阈值 → 启用
        if len(candidates) >= DEEP_THINK_MIN_CANDIDATES:
            return True

        # 平均分偏低 → 记忆质量不高，需要深度筛选
        scores = [c.get("_score", 0) for c in candidates if "_score" in c]
        if scores and sum(scores) / len(scores) < DEEP_THINK_LOW_SCORE_THRESHOLD:
            return True

        return False

    # ── 深度思考四步推理 ──────────────────────────────────

    def _deep_think(self, user_message: str, a1_result: dict) -> tuple:
        """
        四步推理：
        ① 相关性初筛 → ② 冲突检测 → ③ 证据空白检测 → ④ 最终筛选

        Returns:
            (filtered_ids: set, need_external_knowledge: bool)
        """
        # 如果 LLM 不可用，降级为规则筛选
        try:
            return self._llm_deep_think(user_message, a1_result)
        except Exception as e:
            logger.warning(f"A2: LLM 深度思考失败，降级为规则筛选: {e}")
            return self._rule_based_filter(a1_result)

    def _llm_deep_think(self, user_message: str, a1_result: dict) -> tuple:
        """使用 LLM 执行四步推理"""
        candidates = a1_result.get("candidates", [])
        core_memories = a1_result.get("core_memories", [])

        # 构建记忆清单
        memory_list = []
        for i, c in enumerate(candidates):
            mid = c.get("memory_id", f"unknown_{i}")
            summary = c.get("summary", "") or c.get("content", "")[:80]
            layer = c.get("layer", "working")
            score = c.get("_score", 0)
            memory_list.append(f"  [{i}] ID={mid} [{layer}] 得分={score:.4f} {summary}")

        memory_text = "\n".join(memory_list[:20])  # 最多 20 条

        prompt = f"""用户消息："{user_message}"

候选记忆列表：
{memory_text}

请按以下四步进行推理，最后输出 JSON：

① 相关性初筛：逐条判断是否与用户消息相关，标记无关的
② 冲突检测：检查相关记忆之间是否有矛盾，如有矛盾标记优先级
③ 证据空白检测：判断当前记忆是否足够支撑回答，是否需要外部知识
④ 最终筛选：输出最终保留的记忆ID列表

请只输出 JSON：
{{
  "step1_relevance": {{"0": true, "1": false, ...}},
  "step2_conflicts": [{{"id_a": "0", "id_b": "1", "resolution": "优先采用id_a"}}],
  "step3_gap": {{"has_gap": false, "need_external": false, "reason": ""}},
  "step4_final_ids": ["mem_xxx_001", "mem_xxx_002"],
  "reasoning": "简短推理说明"
}}"""

        system_prompt = PROMPTS.A2_DEEP_THINK_SYSTEM

        response = client.chat(
            system_prompt=system_prompt,
            user_message=prompt,
            temperature=0.3,
            max_tokens=1500,
        )

        parsed = self._parse_deep_think_response(response, candidates)
        if parsed is None:
            return self._rule_based_filter(a1_result)

        filtered_ids = parsed["filtered_ids"]
        need_ext = parsed.get("need_external", False)
        self.reasoning_chain.append(parsed.get("reasoning", "[深思] 四步推理完成"))

        return filtered_ids, need_ext

    def _parse_deep_think_response(self, response: str, candidates: list) -> Optional[dict]:
        """解析 LLM 深度思考返回的 JSON"""
        try:
            data = json.loads(response)
        except json.JSONDecodeError:
            match = re.search(r'\{[\s\S]*\}', response)
            if match:
                try:
                    data = json.loads(match.group())
                except json.JSONDecodeError:
                    return None
            else:
                return None

        final_ids = set(data.get("step4_final_ids", []))
        need_ext = data.get("step3_gap", {}).get("need_external", False)
        reasoning = data.get("reasoning", "")

        return {
            "filtered_ids": final_ids,
            "need_external": need_ext,
            "reasoning": reasoning,
        }

    def _rule_based_filter(self, a1_result: dict) -> tuple:
        """规则降级筛选：保留得分 > 0.2 的记忆"""
        candidates = a1_result.get("candidates", [])
        filtered = {
            c.get("memory_id") for c in candidates
            if c.get("_score", 0) > 0.2 and c.get("memory_id")
        }
        self.reasoning_chain.append("[降级] 规则筛选，保留得分>0.2的记忆")
        return filtered, False

    # ── 记忆权重 ──────────────────────────────────────────

    def _get_memory_weights(self) -> dict:
        """根据模式返回记忆权重"""
        if self.mode == "knowledge":
            return {"retrieval": 0.5, "write": 0.4}
        return {"retrieval": 1.0, "write": 1.0}


# ==================== 全局实例 ====================

a2 = A2DeepThinkAgent()