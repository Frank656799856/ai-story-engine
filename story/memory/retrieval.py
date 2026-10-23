#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
记忆检索融合引擎（多身份支持）

整合多路检索：
  1. 关键词倒排索引（jieba 分词 → MemoryIndex）
  2. 四因子复合评分（语义 + 近因 + 频率 + 重要性）
  3. 衰减权重计算
  4. 检索截断保护（Token 预算）
"""

import math
import re
import warnings
from datetime import datetime
from typing import Optional
import logging

# 静音 jieba 启动噪音：pkg_resources 弃用警告（须在首次 import jieba 前装上过滤器）
warnings.filterwarnings("ignore", message="pkg_resources is deprecated")

logger = logging.getLogger("retrieval")

RETRIEVAL_WEIGHTS = {
    "semantic": 0.45,
    "recency": 0.25,
    "frequency": 0.10,
    "importance": 0.20,
}

LAYER_HALFLIFE = {
    "core": 0,
    "episodic": 60,
    "working": 7,
}


def tokenize(text: str) -> list[str]:
    try:
        import jieba
        jieba.setLogLevel(logging.WARNING)  # 静音词典构建的 DEBUG 刷屏（仅首次加载）
        words = jieba.lcut(text)
        stop_words = {"的", "了", "是", "在", "我", "有", "和", "就", "不", "人", "都", "一",
                      "一个", "上", "也", "很", "到", "说", "要", "去", "你", "会", "着",
                      "没有", "看", "好", "自己", "这", "他", "她", "它", "们", "那", "吗",
                      "呢", "吧", "啊", "哦", "嗯", "哈", "呀", "嘛",
                      # 功能词 / 动词虚化 / 口语噪声（避免进记忆关键词索引）
                      "给", "给你", "给我", "咱们", "我们", "你们", "什么", "怎么", "为什么",
                      "怎么样", "这个", "那个", "这些", "那些", "这里", "那里", "一下", "一点",
                      "现在", "最近", "马上", "等等", "然后", "还有", "真的", "其实", "知道",
                      "觉得", "感觉", "告诉", "讲讲", "说说", "谈谈", "看看", "听听", "问问",
                      "起来", "过来", "过去", "出来", "出去", "进去", "回来", "上去",
                      "时候", "回事", "事情", "一个", "这种", "那种"}
        keywords = [w for w in words if len(w) >= 2 and w not in stop_words]
        if keywords:
            return keywords
    except ImportError:
        pass
    text = re.sub(r'[^\u4e00-\u9fff\w]', '', text)
    if len(text) <= 1:
        return [text] if text else []
    return [text[i:i+2] for i in range(len(text)-1)]


def calc_recency_factor(last_accessed: str, halflife_days: float, pinned: bool = False) -> float:
    if halflife_days <= 0 or pinned:
        return 1.0
    try:
        dt = datetime.fromisoformat(last_accessed)
        days = max(0.0, (datetime.now() - dt).total_seconds() / 86400.0)
    except (ValueError, TypeError):
        days = 30.0
    return 2.0 ** (-days / halflife_days)


def calc_frequency_factor(access_count: int) -> float:
    if access_count <= 0:
        return 0.0
    return math.log(1 + access_count) / math.log(1 + max(access_count, 10))


def calc_importance_factor(importance: float, layer: str = "working", pinned: bool = False) -> float:
    layer_bonus = {"core": 0.3, "episodic": 0.15, "working": 0.0}
    pin_bonus = 0.2 if pinned else 0.0
    return min(1.0, importance + layer_bonus.get(layer, 0.0) + pin_bonus)


def calc_composite_score(keyword_score: float, recency: float, frequency: float, importance: float, weights: dict = None) -> float:
    w = weights or RETRIEVAL_WEIGHTS
    return (
        w["semantic"] * keyword_score + w["recency"] * recency +
        w["frequency"] * frequency + w["importance"] * importance
    )


class RetrievalFusion:
    def __init__(self, index, store=None):
        self.index = index
        self.store = store

    def search(self, query: str, layer: str = None, limit: int = 20, max_tokens: int = 3000, mode: str = "chat") -> list[dict]:
        keywords = tokenize(query)
        if not keywords:
            return []
        candidates = self.index.query_by_keywords(keywords, layer=layer)
        if len(candidates) < 5 and layer:
            extra = self.index.query_by_layer(layer)
            existing_ids = {c["memory_id"] for c in candidates}
            for e in extra:
                if e["memory_id"] not in existing_ids:
                    candidates.append(e)
                    existing_ids.add(e["memory_id"])
        if not candidates:
            return []
        scored = []
        for entry in candidates:
            keyword_score = self._calc_keyword_match(keywords, entry)
            halflife = LAYER_HALFLIFE.get(entry.get("layer", "working"), 7)
            recency = calc_recency_factor(entry.get("last_accessed", ""), halflife, entry.get("pinned", False))
            frequency = calc_frequency_factor(entry.get("access_count", 0))
            importance = calc_importance_factor(entry.get("importance", 0.5), entry.get("layer", "working"), entry.get("pinned", False))
            weights = dict(RETRIEVAL_WEIGHTS)
            if mode == "knowledge":
                weights["semantic"] *= 0.5
                weights["importance"] *= 1.3
            composite = calc_composite_score(keyword_score, recency, frequency, importance, weights)
            scored.append({**entry, "_keyword_score": round(keyword_score, 4), "_recency": round(recency, 4), "_frequency": round(frequency, 4), "_importance": round(importance, 4), "_score": round(composite, 6)})
        scored.sort(key=lambda x: x["_score"], reverse=True)
        return self._truncate_by_tokens(scored, max_tokens, limit)

    def _calc_keyword_match(self, keywords: list[str], entry: dict) -> float:
        entry_kws = [k.lower() for k in entry.get("keywords", [])]
        summary = entry.get("summary", "").lower()
        if not keywords:
            return 0.0
        hit_count = 0
        for kw in keywords:
            kw_lower = kw.lower()
            if kw_lower in entry_kws or kw_lower in summary:
                hit_count += 1
        return hit_count / len(keywords)

    def _truncate_by_tokens(self, scored: list[dict], max_tokens: int, limit: int) -> list[dict]:
        result = []
        token_used = 0
        for item in scored:
            if len(result) >= limit:
                break
            summary = item.get("summary", "")
            estimated_tokens = (len(summary) + 200) / 1.5
            if token_used + estimated_tokens > max_tokens and result:
                continue
            token_used += estimated_tokens
            result.append(item)
        return result


def get_fusion(index, store=None):
    return RetrievalFusion(index=index, store=store)