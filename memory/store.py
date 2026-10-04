#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
JSON 文件存储引擎（多身份支持）

每个身份独立的目录：
  data/memory/{identity}/core/      — 核心记忆（永久）
  data/memory/{identity}/episodic/  — 情景记忆（中期）
  data/memory/{identity}/working/   — 工作记忆（短期）

事件溯源：Append-only，永不物理删除，旧文件移到 archived/ 子目录。
"""

import json
import os
import uuid
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional
import logging

logger = logging.getLogger("memory_store")

# ==================== 路径配置 ====================

PROJECT_DIR = Path(__file__).resolve().parent.parent
MEMORY_BASE = PROJECT_DIR / "data" / "memory"

LAYER_NAMES = ["core", "episodic", "working"]


# ==================== 工具函数 ====================

def _gen_memory_id() -> str:
    """生成唯一记忆 ID: mem_YYYYMMDD_UUID8"""
    date_str = datetime.now().strftime("%Y%m%d")
    short_uuid = uuid.uuid4().hex[:8]
    return f"mem_{date_str}_{short_uuid}"


# ==================== 存储引擎 ====================

class MemoryStore:
    """JSON 文件记忆存储引擎（支持多身份）"""

    def __init__(self, identity_name: str = "default"):
        """
        Args:
            identity_name: 身份名称，对应 data/memory/{identity_name}/ 目录
        """
        self.identity_name = identity_name
        self.base_dir = MEMORY_BASE / identity_name
        self.layer_dirs = {
            name: self.base_dir / name
            for name in LAYER_NAMES
        }
        self._ensure_dirs()

    def _ensure_dirs(self):
        """确保所有层级目录存在"""
        for layer, dir_path in self.layer_dirs.items():
            dir_path.mkdir(parents=True, exist_ok=True)
            (dir_path / "archived").mkdir(exist_ok=True)

    def _validate_layer(self, layer: str):
        if layer not in self.layer_dirs:
            raise ValueError(f"无效的记忆层级: {layer}，可选: {list(self.layer_dirs.keys())}")

    # ── 创建记忆 ──────────────────────────────────────────

    def create(
        self,
        content: str,
        layer: str = "working",
        summary: str = "",
        keywords: list = None,
        importance: float = 0.5,
        pinned: bool = False,
        emotional_tag: str = "",
        event_id: str = "",
        tags: list = None,
        mode: str = "chat",
        time_period: str = "",
    ) -> dict:
        self._validate_layer(layer)
        memory_id = _gen_memory_id()
        now = datetime.now().isoformat()

        memory = {
            "memory_id": memory_id,
            "layer": layer,
            "content": content,
            "summary": summary or "",
            "keywords": keywords or [],
            "timestamp": now,
            "last_accessed": now,
            "access_count": 0,
            "importance": max(0.0, min(1.0, importance)),
            "decay_weight": 1.0,
            "pinned": pinned,
            "emotional_tag": emotional_tag,
            "event_id": event_id,
            "tags": tags or [],
            "mode": mode,
            "time_period": time_period,
            "archived": False,
        }

        file_path = self.layer_dirs[layer] / f"{memory_id}.json"
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(memory, f, ensure_ascii=False, indent=2)

        logger.info(f"记忆已创建: {memory_id} [{self.identity_name}/{layer}]")
        return memory

    # ── 读取记忆 ──────────────────────────────────────────

    def read(self, memory_id: str) -> Optional[dict]:
        file_path = self._find_file(memory_id)
        if file_path is None:
            return None
        with open(file_path, "r", encoding="utf-8") as f:
            memory = json.load(f)
        memory["last_accessed"] = datetime.now().isoformat()
        memory["access_count"] = memory.get("access_count", 0) + 1
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(memory, f, ensure_ascii=False, indent=2)
        return memory

    def read_raw(self, memory_id: str) -> Optional[dict]:
        file_path = self._find_file(memory_id)
        if file_path is None:
            return None
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)

    # ── 更新记忆 ──────────────────────────────────────────

    def update(self, memory_id: str, **kwargs) -> Optional[dict]:
        file_path = self._find_file(memory_id)
        if file_path is None:
            return None
        with open(file_path, "r", encoding="utf-8") as f:
            memory = json.load(f)
        layer = memory.get("layer", "working")
        archive_dir = self.layer_dirs[layer] / "archived"
        archive_path = archive_dir / f"{memory_id}_v{int(datetime.now().timestamp())}.json"
        shutil.copy2(file_path, archive_path)

        allowed_fields = {
            "content", "layer", "summary", "keywords", "importance",
            "pinned", "emotional_tag", "event_id", "tags", "mode",
            "decay_weight", "archived", "time_period",
        }
        for key, value in kwargs.items():
            if key in allowed_fields:
                memory[key] = value
        memory["last_accessed"] = datetime.now().isoformat()

        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(memory, f, ensure_ascii=False, indent=2)

        logger.info(f"记忆已更新: {memory_id} [{self.identity_name}]")
        return memory

    def archive(self, memory_id: str) -> bool:
        return self.update(memory_id, archived=True) is not None

    # ── 列表查询 ──────────────────────────────────────────

    def list_all(self, layer: str = None, include_archived: bool = False) -> list[dict]:
        memories = []
        layers = [layer] if layer else self.layer_dirs.keys()
        for lyr in layers:
            self._validate_layer(lyr)
            dir_path = self.layer_dirs[lyr]
            if not dir_path.exists():
                continue
            for f in sorted(dir_path.glob("*.json")):
                try:
                    with open(f, "r", encoding="utf-8") as fh:
                        mem = json.load(fh)
                        if include_archived or not mem.get("archived", False):
                            memories.append(mem)
                except (json.JSONDecodeError, KeyError):
                    continue
        return memories

    def list_by_layer(self, layer: str) -> list[dict]:
        self._validate_layer(layer)
        return self.list_all(layer=layer)

    def list_recent(self, limit: int = 20, layer: str = None) -> list[dict]:
        memories = self.list_all(layer=layer)
        memories.sort(key=lambda m: m.get("timestamp", ""), reverse=True)
        return memories[:limit]

    def list_pinned(self) -> list[dict]:
        return [m for m in self.list_all() if m.get("pinned")]

    def search_by_keyword(self, keyword: str, layer: str = None) -> list[dict]:
        results = []
        kw = keyword.lower()
        for mem in self.list_all(layer=layer):
            content = mem.get("content", "").lower()
            summary = mem.get("summary", "").lower()
            mem_keywords = [k.lower() for k in mem.get("keywords", [])]
            if kw in content or kw in summary or kw in " ".join(mem_keywords):
                results.append(mem)
        return results

    def search_by_tag(self, tag: str, layer: str = None) -> list[dict]:
        return [m for m in self.list_all(layer=layer) if tag in m.get("tags", [])]

    def search_by_event(self, event_id: str) -> list[dict]:
        return [m for m in self.list_all() if m.get("event_id") == event_id]

    def stats(self) -> dict:
        all_memories = self.list_all()
        stats = {"total": len(all_memories)}
        for layer in self.layer_dirs:
            layer_mems = [m for m in all_memories if m.get("layer") == layer]
            stats[f"{layer}_count"] = len(layer_mems)
            stats[f"{layer}_pinned"] = sum(1 for m in layer_mems if m.get("pinned"))
        stats["archived"] = sum(1 for m in all_memories if m.get("archived"))
        return stats

    def _find_file(self, memory_id: str) -> Optional[Path]:
        for layer, dir_path in self.layer_dirs.items():
            file_path = dir_path / f"{memory_id}.json"
            if file_path.exists():
                return file_path
        return None


# ==================== 全局实例（默认身份） ====================

store = MemoryStore("default")