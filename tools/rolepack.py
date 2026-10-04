#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
角色包（rolepack）导出/导入工具 — PC 与安卓版共用

角色包 = 一个 NPC 身份的全部资料打包成 zip：
  - PC 上填表建好的角色 → 导出 zip → 手机端导入使用（安卓版的主要角色分发方式）
  - 也可用于 PC 之间备份/迁移角色

包结构（详见 docs/android-app-plan.md §5.1）：
    {角色名}/
    ├── identity.json      # 身份注册条目（name/persona/created_at）
    ├── memory/            # data/memory/{角色名}/ 全部内容（各层记忆、index.json、history.json）
    └── game_db.json       # 可选，data/game_db/{角色名}.json；无游戏库的角色不含此文件
"""

import io
import json
import logging
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath

from memory.store import MEMORY_BASE
from tools.game_db import GAME_DB_DIR
from agent.pipeline import load_identities, save_identities

logger = logging.getLogger("rolepack")

IDENTITY_ENTRY = "identity.json"


def export_rolepack(identity_name: str) -> tuple[bytes, str]:
    """导出角色包。返回 (zip 字节流, 建议文件名)。身份不存在或无记忆目录时抛 ValueError。"""
    identities = load_identities()
    if identity_name not in identities:
        raise ValueError(f"身份 '{identity_name}' 不存在")
    memory_dir = MEMORY_BASE / identity_name
    if not memory_dir.is_dir():
        raise ValueError(f"身份 '{identity_name}' 没有记忆数据目录，无可导出")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # 1) 身份注册条目
        zf.writestr(f"{identity_name}/{IDENTITY_ENTRY}",
                    json.dumps(identities[identity_name], ensure_ascii=False, indent=2))
        # 2) 全部记忆（各层 + archived + index.json + history.json）
        for path in sorted(memory_dir.rglob("*")):
            if path.is_file():
                arcname = f"{identity_name}/memory/{path.relative_to(memory_dir).as_posix()}"
                zf.write(path, arcname)
        # 3) 游戏数据库（可选）
        game_db_file = GAME_DB_DIR / f"{identity_name}.json"
        if game_db_file.exists():
            zf.write(game_db_file, f"{identity_name}/game_db.json")

    filename = f"{identity_name}_rolepack.zip"
    logger.info(f"角色包已导出: {filename}（{buf.tell()} 字节）")
    return buf.getvalue(), filename


def import_rolepack(zip_bytes: bytes, source_filename: str = "") -> dict:
    """
    导入角色包。写入记忆/游戏库并注册身份。
    身份已存在时抛 ValueError（拒绝静默覆盖）。返回导入摘要。
    """
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = zf.namelist()

        # ── 定位 identity.json，确定包内前缀（支持有/无顶层文件夹两种包）──
        prefix = None
        for n in names:
            parts = PurePosixPath(n).parts
            if len(parts) == 2 and parts[1] == IDENTITY_ENTRY:
                prefix = parts[0] + "/"
                break
            if len(parts) == 1 and parts[0] == IDENTITY_ENTRY:
                prefix = ""
                break
        if prefix is None:
            raise ValueError(f"包内未找到 {IDENTITY_ENTRY}，不是有效的角色包")

        # ── 读取身份条目 ──
        entry = json.loads(zf.read(prefix + IDENTITY_ENTRY).decode("utf-8"))
        name = str(entry.get("name", "")).strip()
        if not name:
            raise ValueError("identity.json 缺少 name 字段")
        persona = entry.get("persona") or {}

        # ── 冲突校验：拒绝静默覆盖 ──
        identities = load_identities()
        if name in identities:
            raise ValueError(f"身份 '{name}' 已存在，请先删除或重命名后再导入")
        if (MEMORY_BASE / name).exists():
            raise ValueError(f"记忆目录 '{name}' 已存在，导入中止")

        # ── 解包：memory/ → 记忆目录；game_db.json → 游戏库；其余忽略 ──
        memory_files = 0
        has_game_db = False
        for info in zf.infolist():
            arc = info.filename
            if arc.endswith("/") or arc == prefix + IDENTITY_ENTRY:
                continue
            rel = arc[len(prefix):] if prefix and arc.startswith(prefix) else arc
            parts = PurePosixPath(rel).parts
            if any(p in ("..", "") for p in parts) or "\\" in rel:
                raise ValueError(f"包内路径不安全，导入中止: {arc}")

            if parts[0] == "memory" and len(parts) >= 2:
                target = MEMORY_BASE / name / Path(*parts[1:])
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(zf.read(arc))
                memory_files += 1
            elif rel == "game_db.json":
                GAME_DB_DIR.mkdir(parents=True, exist_ok=True)
                (GAME_DB_DIR / f"{name}.json").write_bytes(zf.read(arc))
                has_game_db = True
            # 其余未知文件跳过（向前兼容）

    # ── 注册身份 ──
    identities[name] = {
        "name": name,
        "persona": persona,
        "created_at": entry.get("created_at") or datetime.now().isoformat(),
        "imported_from": source_filename or None,
    }
    save_identities(identities)
    logger.info(f"角色包已导入: {name}（记忆文件 {memory_files} 个，游戏库={has_game_db}）")
    return {"identity": name, "memory_files": memory_files, "game_db": has_game_db}
