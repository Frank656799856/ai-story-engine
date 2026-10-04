#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
记忆索引管理（多身份支持）

每个身份独立的 index.json：
  data/memory/{identity}/index.json
"""

import json
from pathlib import Path
from datetime import datetime
from typing import Optional
import logging

logger = logging.getLogger("memory_index")

PROJECT_DIR = Path(__file__).resolve().parent.parent
MEMORY_BASE = PROJECT_DIR / "data" / "memory"


class MemoryIndex:
    """记忆索引管理器（支持多身份）"""

    def __init__(self, identity_name: str = "default"):
        self.identity_name = identity_name
        self.base_dir = MEMORY_BASE / identity_name
        self.index_path = self.base_dir / "index.json"
        self._entries: dict[str, dict] = {}
        self._keyword_index: dict[str, set] = {}
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.load()

    def load(self):
        if not self.index_path.exists():
            logger.info(f"索引文件不存在 [{self.identity_name}]，将创建新索引")
            self._entries = {}
            self._keyword_index = {}
            self._save()
            return
        with open(self.index_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self._entries = {}
        self._keyword_index = {}
        for entry in data.get("entries", []):
            mid = entry["memory_id"]
            self._entries[mid] = entry
            for kw in entry.get("keywords", []):
                kw_lower = kw.lower()
                if kw_lower not in self._keyword_index:
                    self._keyword_index[kw_lower] = set()
                self._keyword_index[kw_lower].add(mid)
        logger.info(f"索引已加载 [{self.identity_name}]: {len(self._entries)} 条记忆")

    def _save(self):
        data = {
            "updated_at": datetime.now().isoformat(),
            "total": len(self._entries),
            "entries": list(self._entries.values()),
        }
        with open(self.index_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def save(self):
        self._save()

    def add(self, memory: dict):
        mid = memory["memory_id"]
        old_entry = self._entries.get(mid)
        if old_entry:
            for kw in old_entry.get("keywords", []):
                kw_lower = kw.lower()
                if kw_lower in self._keyword_index:
                    self._keyword_index[kw_lower].discard(mid)
        entry = {
            "memory_id": mid,
            "layer": memory.get("layer", "working"),
            "summary": memory.get("summary", ""),
            "keywords": memory.get("keywords", []),
            "timestamp": memory.get("timestamp", ""),
            "last_accessed": memory.get("last_accessed", ""),
            "access_count": memory.get("access_count", 0),
            "importance": memory.get("importance", 0.5),
            "decay_weight": memory.get("decay_weight", 1.0),
            "pinned": memory.get("pinned", False),
            "tags": memory.get("tags", []),
            "mode": memory.get("mode", "chat"),
            "archived": memory.get("archived", False),
        }
        self._entries[mid] = entry
        for kw in entry["keywords"]:
            kw_lower = kw.lower()
            if kw_lower not in self._keyword_index:
                self._keyword_index[kw_lower] = set()
            self._keyword_index[kw_lower].add(mid)
        self._save()

    def remove(self, memory_id: str):
        entry = self._entries.pop(memory_id, None)
        if entry:
            for kw in entry.get("keywords", []):
                kw_lower = kw.lower()
                if kw_lower in self._keyword_index:
                    self._keyword_index[kw_lower].discard(memory_id)
            self._save()

    def get(self, memory_id: str) -> Optional[dict]:
        return self._entries.get(memory_id)

    def update_field(self, memory_id: str, **kwargs):
        entry = self._entries.get(memory_id)
        if entry is None:
            return
        if "keywords" in kwargs:
            for kw in entry.get("keywords", []):
                kw_lower = kw.lower()
                if kw_lower in self._keyword_index:
                    self._keyword_index[kw_lower].discard(memory_id)
            for kw in kwargs["keywords"]:
                kw_lower = kw.lower()
                if kw_lower not in self._keyword_index:
                    self._keyword_index[kw_lower] = set()
                self._keyword_index[kw_lower].add(memory_id)
        entry.update(kwargs)
        self._save()

    def query_by_keyword(self, keyword: str, layer: str = None) -> list[dict]:
        kw_lower = keyword.lower()
        matched_ids = self._keyword_index.get(kw_lower, set())
        results = []
        for mid in matched_ids:
            entry = self._entries.get(mid)
            if entry is None or entry.get("archived"):
                continue
            if layer and entry.get("layer") != layer:
                continue
            results.append(entry)
        return results

    def query_by_keywords(self, keywords: list[str], layer: str = None) -> list[dict]:
        score_map: dict[str, int] = {}
        for kw in keywords:
            kw_lower = kw.lower()
            for mid in self._keyword_index.get(kw_lower, set()):
                entry = self._entries.get(mid)
                if entry is None or entry.get("archived"):
                    continue
                if layer and entry.get("layer") != layer:
                    continue
                score_map[mid] = score_map.get(mid, 0) + 1
        sorted_ids = sorted(score_map.keys(), key=lambda mid: score_map[mid], reverse=True)
        return [self._entries[mid] for mid in sorted_ids]

    def query_by_layer(self, layer: str, include_archived: bool = False) -> list[dict]:
        return [
            e for e in self._entries.values()
            if e.get("layer") == layer and (include_archived or not e.get("archived"))
        ]

    def query_pinned(self) -> list[dict]:
        return [e for e in self._entries.values() if e.get("pinned") and not e.get("archived")]

    def query_recent(self, limit: int = 20, layer: str = None) -> list[dict]:
        entries = list(self._entries.values())
        if layer:
            entries = [e for e in entries if e.get("layer") == layer]
        entries = [e for e in entries if not e.get("archived")]
        entries.sort(key=lambda e: e.get("timestamp", ""), reverse=True)
        return entries[:limit]

    def query_by_tag(self, tag: str, layer: str = None) -> list[dict]:
        return [
            e for e in self._entries.values()
            if not e.get("archived") and (not layer or e.get("layer") == layer)
            and tag in e.get("tags", [])
        ]

    def stats(self) -> dict:
        total = len(self._entries)
        return {
            "total": total,
            "total_keywords": len(self._keyword_index),
            "core": sum(1 for e in self._entries.values() if e.get("layer") == "core" and not e.get("archived")),
            "episodic": sum(1 for e in self._entries.values() if e.get("layer") == "episodic" and not e.get("archived")),
            "working": sum(1 for e in self._entries.values() if e.get("layer") == "working" and not e.get("archived")),
            "pinned": sum(1 for e in self._entries.values() if e.get("pinned") and not e.get("archived")),
            "archived": sum(1 for e in self._entries.values() if e.get("archived")),
        }

    def rebuild_from_store(self, store):
        self._entries = {}
        self._keyword_index = {}
        for memory in store.list_all(include_archived=True):
            self.add(memory)
        logger.info(f"索引重建完成 [{self.identity_name}]: {len(self._entries)} 条记忆")
        self._save()


# ==================== 全局实例（默认身份） ====================

index = MemoryIndex("default")