#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A1 — 记忆搜索 Agent（多身份支持）

每个 A1 实例绑定一个身份，使用独立的 store/index/fusion。
"""

import json
import logging
import re

from memory.retrieval import get_fusion, tokenize, LAYER_HALFLIFE
from agent.ai_client import client
from agent import prompts as PROMPTS

logger = logging.getLogger("a1_search")

MAX_SUMMARIES_FOR_LLM = 30


class A1SearchAgent:
    def __init__(self, store, index):
        self.store = store
        self.index = index
        self.fusion = get_fusion(index=index, store=store)

    def search(self, user_message: str, mode: str = "chat", max_candidates: int = 20, max_tokens: int = 3000, recent_history: list[dict] = None) -> dict:
        keywords = tokenize(user_message)
        logger.info(f"A1 关键词 [{self.store.identity_name}]: {keywords}")

        core_memories = self._load_core_memories()

        # 构建上下文感知的查询：当用户消息简短时，结合最近对话推断意图
        enriched_query = self._enrich_query(user_message, recent_history)

        mode_weight = 0.5 if mode == "knowledge" else 1.0
        candidates = self.fusion.search(
            query=enriched_query, layer=None, limit=max_candidates,
            max_tokens=int(max_tokens * mode_weight), mode=mode,
        )

        # 方案A：关键词召回不足时，补一个"兜底候选池"并交给 LLM 语义重排，
        # 让"关键词不重合但语义相关"的记忆也能被捞回来
        # （原逻辑：关键词 0 命中 → candidates 为空 → LLM 语义检索分支直接被跳过，
        #   导致 45% 的语义权重从未真正生效）
        pool_budget = 15
        if len(candidates) < 5:
            candidates = self._merge_fallback_pool(candidates, enriched_query, pool_budget)

        llm_reranked = False
        candidates_with_summaries = [c for c in candidates if c.get("summary")]
        have_no_summaries = all(not c.get("summary") for c in candidates)

        if have_no_summaries:
            logger.info("A1: 候选记忆无摘要，跳过 LLM 语义检索")
        elif candidates_with_summaries:
            candidates = self._llm_semantic_rerank(enriched_query, candidates, mode)
            llm_reranked = True

        total_tokens = self._estimate_tokens(core_memories, candidates)
        history_count = len(recent_history) if recent_history else 0

        result = {
            "core_memories": core_memories,
            "candidates": candidates,
            "keywords": keywords,
            "llm_reranked": llm_reranked,
            "keyword_count": len(keywords),
            "candidate_count": len(candidates),
            "total_tokens_est": total_tokens,
            "mode": mode,
            "recent_history": recent_history or [],
            "history_count": history_count,
        }
        logger.info(f"A1 完成 [{self.store.identity_name}]: 核心={len(core_memories)}, 候选={len(candidates)}, LLM重排={llm_reranked}, 历史={history_count}条")
        return result

    def _merge_fallback_pool(self, found: list[dict], query: str, budget: int) -> list[dict]:
        """
        方案A：把关键词命中的 candidates（可能很少或为空）加进一个"兜底候选池"，
        再交给 LLM 语义重排。兜底池来自各层最近记忆 + 全部核心记忆，按时间倒序取 budget 条。

        这样即使玩家换了个说法、关键词 0 命中，也能靠 LLM 语义相关性召回相关记忆。
        """
        existing_ids = {c.get("memory_id") for c in found}
        merged = list(found)

        # 1) 最近记忆（各层混合，时间倒序）作为主要兜底来源
        recent = self.index.query_recent(limit=budget, layer=None)
        for e in recent:
            mid = e.get("memory_id")
            if mid not in existing_ids:
                base = dict(e)
                base["_score"] = 0.5  # 兜底记忆给中性初始分，靠后续 LLM 重排区分
                merged.append(base)
                existing_ids.add(mid)

        # 2) 若还缺，补核心记忆（优先级高，LLM 会对无关核心给低分）
        if len(merged) < budget:
            core_entries = self._load_core_memories()
            core_budget = budget - len(merged)
            for e in core_entries[:core_budget]:
                mid = e.get("memory_id")
                if mid not in existing_ids:
                    base = dict(e)
                    base["_score"] = 0.6
                    base["pinned"] = True
                    merged.append(base)
                    existing_ids.add(mid)

        logger.info(f"A1: 关键词召回不足，补兜底候选池 -> 原{len(found)}条, 兜底后{len(merged)}条")
        return merged

    def _enrich_query(self, user_message: str, recent_history: list[dict] = None) -> str:
        """当用户消息过短时，结合最近对话上下文增强查询"""
        if not recent_history or len(user_message) >= 10:
            return user_message

        # 提取最近 2 轮对话作为上下文
        recent = recent_history[-4:] if len(recent_history) >= 4 else recent_history
        context_parts = []
        for r in recent:
            role = "玩家" if r.get("role") == "user" else "NPC"
            content = r.get("content", "")[:80]
            context_parts.append(f"{role}: {content}")
        context = " | ".join(context_parts)

        return f"上文: {context} | 当前: {user_message}"

    def _llm_semantic_rerank(self, user_message: str, candidates: list[dict], mode: str = "chat") -> list[dict]:
        summaries = []
        for i, c in enumerate(candidates):
            summary = c.get("summary", "") or (c.get("content", "") or "")[:80]
            summaries.append((i, summary))
        if len(summaries) > MAX_SUMMARIES_FOR_LLM:
            summaries = summaries[:MAX_SUMMARIES_FOR_LLM]

        summaries_text = "\n".join(f"  [{idx}] {s}" for idx, s in summaries)
        prompt = f"""用户查询："{user_message}"

以下是候选记忆的摘要列表，请判断每条摘要与用户查询的语义相关性，给出 0-1 的分数。

{summaries_text}

请只输出 JSON 格式：
{{"scores": {{"0": 0.85, "1": 0.3, ...}}}}"""

        try:
            response = client.chat(
                system_prompt=PROMPTS.A1_SEMANTIC_RERANK_SYSTEM,
                user_message=prompt, temperature=0.3, max_tokens=1000,
            )
            scores = self._parse_llm_scores(response, len(candidates))
            if not scores:
                return candidates
            for idx_str, llm_score in scores.items():
                try:
                    idx = int(idx_str)
                    if 0 <= idx < len(candidates):
                        original = candidates[idx].get("_score", 0)
                        candidates[idx]["_llm_score"] = round(llm_score, 4)
                        candidates[idx]["_score"] = round(llm_score * 0.6 + original * 0.4, 6)
                except (ValueError, IndexError):
                    continue
            candidates.sort(key=lambda x: x.get("_score", 0), reverse=True)
            logger.info(f"A1: LLM 语义重排完成，处理了 {len(scores)} 条摘要")
            return candidates
        except Exception as e:
            logger.warning(f"A1: LLM 语义检索失败: {e}")
            return candidates

    def _parse_llm_scores(self, response: str, total: int) -> dict:
        try:
            data = json.loads(response)
            return data.get("scores", {})
        except json.JSONDecodeError:
            pass
        match = re.search(r'\{[\s\S]*\}', response)
        if match:
            try:
                return json.loads(match.group()).get("scores", {})
            except json.JSONDecodeError:
                pass
        return {}

    def _load_core_memories(self) -> list[dict]:
        core_entries = self.index.query_by_layer("core")
        core_entries.sort(key=lambda e: (not e.get("pinned", False), e.get("timestamp", "")))
        return core_entries

    def _estimate_tokens(self, core_memories: list, candidates: list) -> int:
        total_chars = 0
        for m in core_memories:
            total_chars += len(m.get("summary", "")) + 100
        for c in candidates:
            total_chars += len(c.get("summary", "")) + 200
        return int(total_chars / 1.5)

    def format_for_a2(self, search_result: dict) -> str:
        lines = []
        lines.append("【核心记忆 - 强制注入】")
        if search_result["core_memories"]:
            for i, mem in enumerate(search_result["core_memories"], 1):
                summary = mem.get("summary", "") or mem.get("content", "")[:50]
                lines.append(f"  [{i}] {summary}")
        else:
            lines.append("  (无核心记忆)")
        lines.append(f"\n【候选记忆 - 共{search_result['candidate_count']}条{' (LLM语义重排)' if search_result.get('llm_reranked') else ''}】")
        if search_result["candidates"]:
            for i, mem in enumerate(search_result["candidates"], 1):
                summary = mem.get("summary", "") or mem.get("content", "")[:50]
                score = mem.get("_score", 0)
                llm_s = mem.get("_llm_score")
                layer = mem.get("layer", "working")
                pinned = "📌" if mem.get("pinned") else ""
                llm_info = f" LLM:{llm_s:.2f}" if llm_s is not None else ""
                lines.append(f"  [{i}] [{layer}]{pinned} (总分:{score:.4f}{llm_info}) {summary}")
        else:
            lines.append("  (无候选记忆)")
        lines.append(f"\n【提取关键词】{' '.join(search_result['keywords'])}")
        return "\n".join(lines)

    def format_for_a3(self, search_result: dict, filtered_ids: set = None) -> list[dict]:
        result = []
        for mem in search_result["core_memories"]:
            result.append({"id": mem.get("memory_id"), "layer": "core", "summary": mem.get("summary", ""), "content": mem.get("content", ""), "score": 1.0, "pinned": True})
        for mem in search_result["candidates"]:
            mid = mem.get("memory_id")
            if filtered_ids and mid not in filtered_ids:
                continue
            result.append({"id": mid, "layer": mem.get("layer", "working"), "summary": mem.get("summary", ""), "content": mem.get("content", ""), "score": mem.get("_score", 0), "pinned": mem.get("pinned", False)})
        return result