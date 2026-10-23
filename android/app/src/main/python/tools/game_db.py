#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
游戏数据库（Game Database）

每个 NPC 身份可拥有一个手动创建的游戏数据库 JSON 文件：
  data/game_db/{identity}.json

作用：
  - 存储游戏世界中的 食物/道具/怪物/友好生物/地形/地点/世界观设定
  - A1 根据用户消息检测提到哪些实体（打标签）
  - A3 根据标签检索数据库条目，核对最终输出是否与数据库相悖

默认 NPC 没有数据库文件时，此功能整条分支跳过，不影响普通聊天。
"""

import json
import logging
from pathlib import Path

logger = logging.getLogger("game_db")

PROJECT_DIR = Path(__file__).resolve().parent.parent
try:
    from data_paths import DATA_DIR
    GAME_DB_DIR = DATA_DIR / "game_db"
except ImportError:
    GAME_DB_DIR = PROJECT_DIR / "data" / "game_db"

# 数据库中的分类（「教程」为 T-004 新增：步骤化教程条目，仅 knowledge 模式动态注入）
CATEGORIES = ["食物", "道具", "怪物", "友好生物", "地形", "地点", "世界观设定", "教程"]


class GameDB:
    """游戏数据库（按身份加载）"""

    def __init__(self, identity_name: str):
        self.identity_name = identity_name
        self.file_path = GAME_DB_DIR / f"{identity_name}.json"
        self.data = self._load()
        # 构建实体索引: 实体名 -> 条目
        self._entity_index: dict[str, dict] = {}
        self._build_index()

    # ── 加载 ──────────────────────────────────────────

    def _load(self) -> dict:
        if not self.file_path.exists():
            logger.info(f"游戏数据库不存在 [{self.identity_name}]，跳过游戏数据库功能")
            return {}
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            logger.info(f"游戏数据库已加载 [{self.identity_name}]: {len(data.get('categories', {}))} 个分类")
            return data
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"游戏数据库加载失败 [{self.identity_name}]: {e}")
            return {}

    def _build_index(self):
        """构建实体名 -> 条目的索引"""
        categories = self.data.get("categories", {})
        for cat_name, items in categories.items():
            for item in items:
                name = item.get("name", "")
                if name:
                    self._entity_index[name] = {"category": cat_name, **item}

    # ── 状态 ──────────────────────────────────────────

    @property
    def available(self) -> bool:
        """该身份是否接入了游戏数据库"""
        return bool(self.data)

    # ── 实体检测（A1 打标签用） ─────────────────────────

    def detect_entities(self, text: str) -> list[dict]:
        """
        在文本中检测游戏数据库实体。

        Returns:
            [{"name": "月光花茶", "category": "食物", "description": "..."}, ...]
        """
        if not self.available or not text:
            return []

        hits = []
        seen = set()
        # 优先匹配较长的实体名，避免短名误伤
        for name in sorted(self._entity_index.keys(), key=len, reverse=True):
            if name in text and name not in seen:
                seen.add(name)
                entry = self._entity_index[name]
                hits.append({
                    "name": name,
                    "category": entry.get("category", "未知"),
                    "description": entry.get("description", ""),
                })
        return hits

    # ── 检索（A3 用，按分类过滤，非全量） ───────────────

    def has_entry(self, name: str) -> bool:
        """数据库中是否已存在同名条目（用于去重）"""
        return bool(name) and name in self._entity_index

    def add_entry(self, category: str, name: str, description: str) -> dict:
        """新增一条数据库条目（A4 收录玩家提到的新名词时用）。

        按分类追加到 categories 对应列表，并同步更新实体索引后保存。
        若名称已存在或分类不存在则抛 ValueError。
        """
        name = (name or "").strip()
        category = (category or "").strip()
        if not name:
            raise ValueError("新名词不能为空")
        if category not in CATEGORIES:
            raise ValueError(f"无效分类: {category}")
        if self.has_entry(name):
            raise ValueError(f"数据库中已存在: {name}")

        entry = {"name": name, "description": (description or "").strip()}
        data_cats = self.data.setdefault("categories", {})
        data_cats.setdefault(category, []).append(entry)
        self._entity_index[name] = {"category": category, **entry}
        self._save()
        logger.info(f"游戏数据库新增条目 [{self.identity_name}]: [{category}] {name}")
        return {"name": name, "category": category, "description": entry["description"]}

    def _save(self):
        """写回数据库文件"""
        if not self.file_path:
            return
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.file_path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    def match_tutorial(self, text: str) -> list[dict]:
        """教程关键词匹配（T-004，A2/A3 共用）。

        遍历「教程」分类，检查玩家消息是否包含某条教程的 keywords 或 name 子串。
        Returns:
            命中的教程条目 [{"name", "category": "教程", "description", "keywords"}, ...]
        """
        if not self.available or not text:
            return []
        tutorials = self.data.get("categories", {}).get("教程", []) or []
        hits = []
        for item in tutorials:
            name = item.get("name", "")
            triggers = [name] + list(item.get("keywords") or [])
            if any(t and t in text for t in triggers):
                hits.append({
                    "name": name,
                    "category": "教程",
                    "description": item.get("description", ""),
                    "keywords": item.get("keywords") or [],
                })
        return hits

    def search_by_categories(self, categories: list[str], max_per_cat: int = 3) -> list[dict]:
        """
        按分类检索数据库条目。

        Args:
            categories: 需要的分类列表（如 ["食物", "道具"]）
            max_per_cat: 每个分类最多返回条数

        Returns:
            [{"name": ..., "category": ..., "description": ...}, ...]
        """
        if not self.available:
            return []

        data_cats = self.data.get("categories", {})
        results = []
        for cat in categories:
            items = data_cats.get(cat, [])
            for item in items[:max_per_cat]:
                results.append({
                    "name": item.get("name", ""),
                    "category": cat,
                    "description": item.get("description", ""),
                })
        return results

    def get_world_setting(self) -> dict:
        """获取世界观设定（A3 核对世界一致性用）"""
        if not self.available:
            return {}
        return {
            "world_name": self.data.get("world", {}).get("name", ""),
            "world_description": self.data.get("world", {}).get("description", ""),
        }

    # ── 参考来源（录入新 NPC 用） ─────────────────────

    def get_reference(self) -> dict:
        """
        获取游戏数据库顶层的 reference 字段：
          {
            "source_game": "原神·可莉",        # 可选，参考的资料来源
            "dialogue_samples": ["...", "..."], # 可选，角色与他人交流的对话样本（如30句）
            "summary": "..."                    # 首次启动提炼出的角色画像摘要（生成后写入）
          }
        返回空 dict 表示没有填参考字段。
        """
        ref = self.data.get("reference", {}) or {}
        if not ref:
            return {}
        return {
            "source_game": ref.get("source_game", ""),
            "dialogue_samples": ref.get("dialogue_samples", []) or [],
            "summary": ref.get("summary", ""),
        }

    def write_reference_summary(self, summary: str):
        """把提炼出的角色画像摘要写回 reference.summary（同时作为『已完成』标记）。"""
        ref = self.data.setdefault("reference", {})
        ref["summary"] = summary
        self._save()

    # ── 格式化 ────────────────────────────────────────

    def format_entries(self, entries: list[dict]) -> str:
        """把条目格式化为 LLM 可读文本"""
        if not entries:
            return ""
        lines = []
        for e in entries:
            lines.append(f"- [{e['category']}] {e['name']}: {e['description']}")
        return "\n".join(lines)


# 缓存：避免每个 pipeline 都重新读文件
_db_cache: dict[str, GameDB] = {}


def get_game_db(identity_name: str) -> GameDB:
    """获取（缓存的）游戏数据库实例"""
    if identity_name not in _db_cache:
        _db_cache[identity_name] = GameDB(identity_name)
    return _db_cache[identity_name]
