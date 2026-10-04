#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
剧情副本存储引擎（story 新系统专用）

数据落盘位置（与原系统物理隔离）：
  story/data/story/{identity}.json          — 剧本（生成后不可变）
  story/data/story/{identity}_state.json    — 运行时状态（flags/scene/ended）
  story/data/story/_current.json            — 当前游玩副本指针
  story/data/identities.json                — 身份注册表（与原系统 pipeline.py 共用同一文件）

所有写操作均为原子写：先写临时文件再 os.replace，避免半截文件。
"""

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger("story_store")

# pro_AI2/story/story/store.py → 向上两级 = pro_AI2/story（新系统根，与
# agent/pipeline.py 的 IDENTITIES_FILE、memory/tools 的 PROJECT_DIR 同一根）
STORY_DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "story"

IDENTITIES_FILE = Path(__file__).resolve().parent.parent / "data" / "identities.json"

_CURRENT_FILE = "current"
_STATE_SUFFIX = "_state"


# ==================== 原子写工具 ====================

def _atomic_write_json(path: Path, obj) -> None:
    """原子写 JSON：先写同目录临时文件，再 os.replace 覆盖目标。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _read_json(path: Path, default=None):
    """读 JSON 文件；不存在或损坏时返回 default。"""
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"JSON 读取失败 [{path.name}]: {e}")
        return default


# ==================== 剧本（不可变） ====================

def _story_path(identity: str) -> Path:
    return STORY_DATA_DIR / f"{identity}.json"


def _state_path(identity: str) -> Path:
    return STORY_DATA_DIR / f"{identity}{_STATE_SUFFIX}.json"


def save_story(identity: str, d: dict) -> None:
    """保存剧本（生成后视为不可变，仅 builder 首次落盘时调用）。"""
    _atomic_write_json(_story_path(identity), d)
    logger.info(f"剧本已保存: {identity}")


def load_story(identity: str):
    """加载剧本；不存在返回 None。"""
    return _read_json(_story_path(identity), None)


# ==================== 运行时状态 ====================

def save_state(identity: str, s: dict) -> None:
    """保存运行时状态（flags/scene/ended）。"""
    _atomic_write_json(_state_path(identity), s)


def load_state(identity: str):
    """加载运行时状态；不存在返回 None。"""
    return _read_json(_state_path(identity), None)


# ==================== 副本列表 ====================

def list_stories() -> list:
    """列出全部副本的公开摘要（不含 secrets）。

    Returns:
        [{"identity", "title", "world_name"}, ...]
    """
    if not STORY_DATA_DIR.exists():
        return []
    result = []
    for f in sorted(STORY_DATA_DIR.glob("*.json")):
        name = f.name
        if name.startswith("_") or name.endswith(f"{_STATE_SUFFIX}.json"):
            continue
        d = _read_json(f, None)
        if not isinstance(d, dict):
            continue
        setting = d.get("setting", {}) if isinstance(d.get("setting"), dict) else {}
        result.append({
            "identity": f.stem,
            "title": d.get("title", ""),
            "world_name": setting.get("world_name", ""),
        })
    return result


# ==================== 当前副本指针 ====================

def _current_path() -> Path:
    return STORY_DATA_DIR / f"_{_CURRENT_FILE}.json"


def set_current(identity: str) -> None:
    """记录当前游玩的副本 identity（builder 生成完成 / 前端进入游玩时调用）。"""
    _atomic_write_json(_current_path(), {"identity": identity})


def get_current_identity():
    """读取当前副本 identity；未设置返回 None。"""
    d = _read_json(_current_path(), None)
    if isinstance(d, dict):
        return d.get("identity")
    return None


def clear_current() -> None:
    """清除当前副本指针（终局归档 / 重开时调用）。"""
    path = _current_path()
    if path.exists():
        path.unlink()
