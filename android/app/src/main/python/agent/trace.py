#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
推理日志追踪器 (Trace Collector)

两种模式：
  - 缩略模式：每个 Agent 干了什么（一句话）
  - 详细模式：每次 LLM 调用的完整输入输出

不持久化，仅内存存储。
"""

import logging
import time
from typing import Optional

logger = logging.getLogger("trace")


class TraceEntry:
    """单条追踪记录"""

    def __init__(self, agent: str, step: str, detail: str = "",
                 llm_input: str = None, llm_output: str = None,
                 llm_type: str = "", llm_time_ms: float = 0):
        self.agent = agent
        self.step = step
        self.detail = detail
        self.llm_input = llm_input
        self.llm_output = llm_output
        self.llm_type = llm_type
        self.llm_time_ms = llm_time_ms
        self.timestamp = time.time()

    def to_dict(self, detailed: bool = False) -> dict:
        d = {
            "agent": self.agent,
            "step": self.step,
            "detail": self.detail,
            "time": self._fmt_time(),
        }
        if detailed:
            d["llm_input"] = self.llm_input[:2000] if self.llm_input else ""
            d["llm_output"] = self.llm_output[:2000] if self.llm_output else ""
            d["llm_type"] = self.llm_type
            d["llm_time_ms"] = round(self.llm_time_ms, 1)
        return d

    def _fmt_time(self) -> str:
        t = time.localtime(self.timestamp)
        return f"{t.tm_hour:02d}:{t.tm_min:02d}:{t.tm_sec:02d}.{int((self.timestamp % 1) * 1000):03d}"


class TraceCollector:
    """全局追踪收集器"""

    _instance: Optional["TraceCollector"] = None

    def __init__(self):
        self.entries: list[TraceEntry] = []
        self.enabled = True

    @classmethod
    def get(cls) -> "TraceCollector":
        if cls._instance is None:
            cls._instance = TraceCollector()
        return cls._instance

    def add(self, agent: str, step: str, detail: str = "",
            llm_input: str = None, llm_output: str = None,
            llm_type: str = "", llm_time_ms: float = 0):
        if not self.enabled:
            return
        entry = TraceEntry(
            agent=agent, step=step, detail=detail,
            llm_input=llm_input, llm_output=llm_output,
            llm_type=llm_type, llm_time_ms=llm_time_ms,
        )
        self.entries.append(entry)
        logger.debug(f"[TRACE] {entry.agent}/{entry.step}: {entry.detail[:80]}")

    def add_llm(self, agent: str, step: str, llm_input: str, llm_output: str,
                llm_type: str = "chat", llm_time_ms: float = 0):
        """LLM 调用专用追踪"""
        self.add(
            agent=agent, step=step,
            detail=f"LLM {llm_type} 调用 ({llm_time_ms:.0f}ms)",
            llm_input=llm_input, llm_output=llm_output,
            llm_type=llm_type, llm_time_ms=llm_time_ms,
        )

    def get_summary(self) -> list[dict]:
        """获取缩略模式（仅 Agent 步骤）"""
        return [e.to_dict(detailed=False) for e in self.entries]

    def get_detailed(self) -> list[dict]:
        """获取详细模式（含 LLM 输入输出）"""
        return [e.to_dict(detailed=True) for e in self.entries]

    def clear(self):
        self.entries = []

    def stats(self) -> dict:
        agents = {}
        llm_count = 0
        llm_total_ms = 0
        for e in self.entries:
            agents[e.agent] = agents.get(e.agent, 0) + 1
            if e.llm_input:
                llm_count += 1
                llm_total_ms += e.llm_time_ms
        return {
            "total_entries": len(self.entries),
            "llm_calls": llm_count,
            "llm_total_time_ms": round(llm_total_ms, 1),
            "by_agent": agents,
        }


# 全局单例
trace = TraceCollector.get()