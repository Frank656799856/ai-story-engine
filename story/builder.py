#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
造城 AI（Builder）— 从设置表单生成可玩副本

流程（build_story）：
  1. 调 LLM 产出副本 JSON（BUILDER_STORY_SYSTEM 网文方法论 + few-shot 范例引导）
  2. validate_story 校验；失败把错误清单回喂修复重试一次，仍失败抛异常
  3. 审稿循环（CRITIC 打分，score<7 带 issues 修订重生成，最多 2 轮；仍 <7 也接受并记 review_notes）
  4. 逐 NPC 建档入库：人物卡（性格/目标/位置 + 所持秘密写进关系描述）写入 game_db，
     并写入 world 与 story_id 关联
  5. 注册新身份（identities.json，格式与原系统 agent/pipeline.create_identity 一致）
  6. 剧本与初始 state 原子落盘，记录当前副本指针

约束：所有 LLM 调用走 agent/ai_client.py 的全局 client；不改原系统任何文件。
"""

import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path

from agent.ai_client import client
from agent import prompts as PROMPTS
from tools.game_db import GameDB

from . import schema, store

logger = logging.getLogger("story_builder")

# 身份注册表（与 agent/pipeline.py 的 IDENTITIES_FILE 指向同一文件）
IDENTITIES_FILE = Path(__file__).resolve().parent.parent / "data" / "identities.json"


# ==================== JSON 解析工具 ====================

def _extract_json(text: str) -> dict:
    """从模型输出解析 JSON 对象（容忍 ```json 围栏与前后杂文）。"""
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", text)
        if match:
            return json.loads(match.group())
        raise ValueError("无法从模型输出中解析出 JSON 对象")


def _to_float(val, default: float) -> float:
    """宽松地把评分转成 float；失败用 default（解析失败视为通过，不卡流程）。"""
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


# ==================== game_db 建档入库 ====================

def _fill_game_db(story: dict) -> None:
    """把世界观与逐 NPC 人物卡写入该副本身份的 game_db，并关联 story_id。

    条目格式遵循 GameDB 实际接口（add_entry(category, name, description)）：
      - GameDB 固定分类中没有"人物"类目，人物卡统一归入「世界观设定」兜底类目；
      - 秘密原文不单独立条（防被动态检索整段带出），写进持有者人物卡的关系描述里，
        由 director 的权限门负责跨 NPC 隔离。
    """
    story_id = story["story_id"]
    setting = story.get("setting", {})
    db = GameDB(story_id)
    # world 与 story_id 关联（GameDB 保留未知顶层键，读取方按需取用）
    db.data["world"] = {
        "name": setting.get("world_name", ""),
        "description": setting.get("world_desc", ""),
    }
    db.data["story_id"] = story_id

    secrets_by_id = {s.get("id"): s for s in story.get("secrets", [])}
    for npc in story.get("npcs", []):
        # 人物卡描述：性格 / 目标 / 位置；known_secrets 对应实体写进关系描述
        desc = (
            f"人物卡：{npc.get('name', '')}。性格：{npc.get('personality', '')}。"
            f"目标：{npc.get('goal', '')}。所在位置：{npc.get('location', '')}。"
        )
        sec_texts = []
        for sid in npc.get("known_secrets", []):
            sec = secrets_by_id.get(sid)
            if sec:
                sec_texts.append(
                    f"秘密[{sid}]：{sec.get('content', '')}（揭示暗示：{sec.get('reveal_hint', '')}）"
                )
        if sec_texts:
            desc += "其知晓的秘密与关系：" + "；".join(sec_texts)
        try:
            db.add_entry("世界观设定", npc.get("name", ""), desc)
        except ValueError as e:
            # 重名 NPC 等情况不中断建档（提示词已要求 NPC 名唯一）
            logger.warning(f"game_db 入库跳过 [{npc.get('name', '')}]: {e}")
    db._save()  # 确保.world/story_id 等非 add_entry 写入也落盘
    logger.info(f"game_db 建档完成 [{story_id}]")


# ==================== 身份注册 ====================

def _register_identity(story: dict) -> None:
    """向 identities.json 注册副本身份（格式照搬原系统 create_identity 的注册表条目）。

    persona 采用主角视角世界设定（title/tone/world_desc）；
    游玩时 pipeline 会按 npc_id 用 director.persona_for() 热切人设，此处 persona
    仅作为副本身份的兜底人设。
    """
    identities = {}
    if IDENTITIES_FILE.exists():
        try:
            with open(IDENTITIES_FILE, "r", encoding="utf-8") as f:
                identities = json.load(f)
        except (json.JSONDecodeError, IOError):
            identities = {}
    story_id = story["story_id"]
    if story_id in identities:
        raise ValueError(f"身份 '{story_id}' 已存在")
    setting = story.get("setting", {})
    persona = {
        "name": story.get("title") or story_id,
        "personality": setting.get("tone") or "沉浸式剧情演出",
        "world_setting": setting.get("world_desc", ""),
    }
    identities[story_id] = {
        "name": story_id,
        "persona": persona,
        "created_at": datetime.now().isoformat(),
        "kind": "story",       # 标记为剧情副本身份（普通 NPC 身份无此字段）
        "story_id": story_id,
    }
    store._atomic_write_json(IDENTITIES_FILE, identities)  # 复用原子写，保证注册表不写坏
    logger.info(f"副本身份已注册: {story_id}")


# ==================== 外源检索资料桥（8.16） ====================

def make_research_prompt(settings: dict, job_id: str) -> str:
    """生成给外部 AI 的完整检索提示词（8.16 提前搜模式）。

    一次轻量 LLM 调用产出检索需求清单（主题分组，≤12 个；清单质量前置——
    先模拟「按这些设定写副本需要哪些细节」再列清单），再按 RESEARCH_PROMPT_TEMPLATE
    拼装成完整提示词（含 JSON 契约与提交方式）。
    """
    world_desc = (settings.get("world_desc") or "").strip()
    if not world_desc:
        raise ValueError("world_desc（一句话世界观）不能为空")
    genre = (settings.get("genre") or "都市怪谈").strip()
    lore = (settings.get("lore") or "").strip()
    npc_hints = (settings.get("npc_hints") or "").strip()
    supplement = ""
    if lore:
        supplement += f"用户补充设定（检索时一并考虑）：\n{lore}\n"
    if npc_hints:
        supplement += f"用户指定 NPC（这些人名与身份必须按真实设定查证）：\n{npc_hints}\n"
    user = f"世界观一句话：{world_desc}\n故事类型：{genre}\n" + supplement
    raw = client.chat(
        system_prompt=PROMPTS.RESEARCH_BRIEF_SYSTEM,
        user_message=user,
        temperature=0.4,
        max_tokens=1500,
    )
    brief = _extract_json(raw)
    topics = brief.get("topics") if isinstance(brief, dict) else None
    if not isinstance(topics, list):
        raise ValueError("检索需求清单生成失败（模型未返回 topics 列表）")
    lines = []
    for t in topics[:12]:  # 硬上限：控制用户搬运量与注入体积
        if not isinstance(t, dict):
            continue
        topic = str(t.get("topic") or "").strip()
        if not topic:
            continue
        need = str(t.get("need") or "").strip()
        lines.append(f"{len(lines) + 1}. {topic}" + (f"——{need}" if need else ""))
    if not lines:
        raise ValueError("检索需求清单为空（模型未给出有效主题）")
    return PROMPTS.RESEARCH_PROMPT_TEMPLATE.format(
        world_desc=world_desc,
        genre=genre,
        brief_block="\n".join(lines),
        job_id=job_id,
    )


def make_repair_prompt(original_prompt: str, errors: list) -> str:
    """资料校验不通过时的补查提示词（8.16 唯二可能的多趟场景）：问题清单 + 原要求。"""
    head = (
        "⚠️ 你上一趟提交的检索资料未通过校验，问题清单：\n"
        + "\n".join(f"- {e}" for e in errors)
        + "\n请针对上述问题补查后，连同其余主题一起，仍按原要求一次性重新提交完整 JSON。\n\n"
        "———— 以下为原检索要求 ————\n"
    )
    return head + (original_prompt or "")


# ==================== 主流程 ====================

def build_story(settings: dict, on_progress=None, research=None) -> dict:
    """从设置表单构建一个完整可玩的剧情副本。

    Args:
        settings: {world_desc 一句话世界观, genre 故事类型, npc_count 2~4,
                   duration_hint 时长参考, tone 叙事基调,
                   以及选填高级项：title_hint/player_name/player_desc/
                   lore/npc_hints/ending_hint/extra_req}
        on_progress: 阶段回调 on_progress(stage: str, percent: int)，供前端轮询展示进度与进度条
        research: 外源检索资料 dict（8.16，选填）。非空时格式化后注入造城提示词
                  （extra_block 之前的独立「世界资料」块），并整体存入 story["research_summary"]
                  供导演游玩注入；超 6000 字截断说明记入 review_notes。

    Returns:
        完整副本 dict（含 story_id / state / review_notes；有玩家角色时含 player_persona）

    Raises:
        ValueError: 设置缺世界描述
        RuntimeError: 生成结果两次校验都不通过
    """
    if on_progress is None:
        on_progress = lambda stage: None  # noqa: E731 缺省空回调

    world_desc = (settings.get("world_desc") or "").strip()
    if not world_desc:
        raise ValueError("world_desc（一句话世界观）不能为空")
    genre = (settings.get("genre") or "都市怪谈").strip()
    try:
        npc_count = int(settings.get("npc_count") or 3)
    except (TypeError, ValueError):
        npc_count = 3
    npc_count = max(2, min(4, npc_count))  # 与 schema 校验范围一致
    duration_hint = (settings.get("duration_hint") or "约30分钟").strip()
    tone = (settings.get("tone") or "悬疑").strip()

    # 可选高级项（借鉴参考项目 ai-galgame-demo 的创建页；全部选填）
    title_hint = (settings.get("title_hint") or "").strip()
    player_name = (settings.get("player_name") or "").strip()
    player_desc = (settings.get("player_desc") or "").strip()
    lore = (settings.get("lore") or "").strip()
    npc_hints = (settings.get("npc_hints") or "").strip()
    ending_hint = (settings.get("ending_hint") or "").strip()
    extra_req = (settings.get("extra_req") or "").strip()

    # 组装选填附加要求块（每行一条，注入造城提示词末尾；无任何选填项时为空串）
    extra_lines = []
    if title_hint:
        extra_lines.append(f"- 副本名称（必须使用）：{title_hint}")
    if player_name or player_desc:
        extra_lines.append(
            f"- 玩家角色：{player_name or '（名字由你拟定）'}"
            + (f"，{player_desc}" if player_desc else "")
            + "。剧本必须围绕这一玩家身份展开，NPC 对玩家的称呼要符合该身份"
        )
    for ln in lore.splitlines():
        ln = ln.strip()
        if ln:
            extra_lines.append(f"- 世界观补充设定（必须遵守）：{ln}")
    for ln in npc_hints.splitlines():
        ln = ln.strip()
        if ln:
            extra_lines.append(f"- 指定 NPC（必须收录该角色，姓名与身份不得更改，其余细节可润色）：{ln}")
    if ending_hint:
        extra_lines.append(f"- 期望结局方向（可作为最终结局或其中一条分岔终局的灵感）：{ending_hint}")
    if extra_req:
        extra_lines.append(f"- 其他附加要求（必须满足）：{extra_req}")
    extra_block = ""
    if extra_lines:
        extra_block = "\n\n可选附加要求（玩家指定；若与通用规则冲突，以附加要求为准）：\n" + "\n".join(extra_lines)

    # 审稿记录（8.16 起造城前的资料截断说明也记在这里）
    review_notes: list = []

    # ── 0. 外源检索资料（8.16）：格式化 + 防灌爆（约 6000 字上限），插在 extra_block 之前 ──
    research_summary = None
    if isinstance(research, dict) and research.get("topics"):
        research_text, trunc_notes = schema.format_research(research)
        if research_text:
            research_summary = research
            extra_block = PROMPTS.RESEARCH_INJECT.format(research_text=research_text) + extra_block
        for note in trunc_notes:
            review_notes.append({"round": "research", "score": None, "issues": [note]})

    # ── 1. 造城：世界观与秘密网 ──
    on_progress("正在构建世界观与秘密网", 15)
    system = PROMPTS.BUILDER_STORY_SYSTEM + PROMPTS.BUILDER_FEW_SHOT_EXAMPLE
    user = PROMPTS.BUILDER_STORY_USER.format(
        world_desc=world_desc,
        genre=genre,
        npc_count=npc_count,
        duration_hint=duration_hint,
        tone=tone,
        extra_block=extra_block,
    )
    raw = client.chat(system_prompt=system, user_message=user, temperature=0.85, max_tokens=8192)
    story = _extract_json(raw)

    # ── 2. 结构校验：失败回喂修复重试一次 ──
    errors = schema.validate_story(story)
    if errors:
        logger.warning(f"副本首次校验未通过（{len(errors)} 项），回喂修复重试")
        fix_user = PROMPTS.BUILDER_STORY_FIX_USER.format(
            story_json=json.dumps(story, ensure_ascii=False, indent=2),
            problems="\n".join(f"- {e}" for e in errors),
        )
        raw = client.chat(system_prompt=system, user_message=fix_user, temperature=0.4, max_tokens=8192)
        story = _extract_json(raw)
        errors = schema.validate_story(story)
        if errors:
            raise RuntimeError("副本生成校验失败：\n" + "\n".join(errors))

    # ── 3. 审稿循环（最多 2 轮；仍 <7 也接受，记入 review_notes） ──
    for round_no in (1, 2):
        on_progress(f"审稿第 {round_no} 轮", 55 + (round_no - 1) * 15)
        critic_user = PROMPTS.CRITIC_STORY_USER.format(
            story_json=json.dumps(story, ensure_ascii=False, indent=2)
        )
        try:
            verdict = _extract_json(client.chat(
                system_prompt=PROMPTS.CRITIC_STORY_SYSTEM,
                user_message=critic_user,
                temperature=0.3,
                max_tokens=800,
            ))
        except (ValueError, json.JSONDecodeError):
            verdict = {}
        score = _to_float(verdict.get("score"), 10.0)
        issues = verdict.get("issues") if isinstance(verdict.get("issues"), list) else []
        review_notes.append({"round": round_no, "score": score, "issues": issues})
        if score >= 7:
            break
        # 带 issues 修订重生成
        logger.info(f"审稿第 {round_no} 轮得分 {score} < 7，带 issues 修订重生成")
        revise_user = PROMPTS.BUILDER_STORY_FIX_USER.format(
            story_json=json.dumps(story, ensure_ascii=False, indent=2),
            problems="\n".join(f"- {i}" for i in issues) or "-（责编未给出具体问题，请整体润色提升开篇钩子与冲突张力）",
        )
        raw = client.chat(system_prompt=system, user_message=revise_user, temperature=0.7, max_tokens=8192)
        try:
            revised = _extract_json(raw)
        except (ValueError, json.JSONDecodeError):
            logger.warning("修订输出解析失败，保留当前剧本")
            continue
        if schema.validate_story(revised):
            logger.warning("修订结果未通过结构校验，保留当前剧本")
            continue
        story = revised

    # ── 4. 收尾：编号/初始状态/审稿记录 ──
    story["story_id"] = "story_" + time.strftime("%Y%m%d_%H%M%S")
    story["genre"] = genre
    # 玩家角色卡：表单指定 → 名字/身份用表单值（细节保留 AI 生成）；未指定 → 保留 AI 生成的
    if player_name or player_desc:
        ai_persona = story.get("player_persona") if isinstance(story.get("player_persona"), dict) else {}
        story["player_persona"] = {
            "name": player_name or ai_persona.get("name", ""),
            "identity": player_desc or ai_persona.get("identity", ""),
            "background": ai_persona.get("background", player_desc or ""),
            "skills": ai_persona.get("skills", ""),
        }
    # 搜索开关（8.16 资料桥：本次生成是否走了外源检索）
    story["research_enabled"] = bool((settings or {}).get("research_enabled"))
    # 8.26b 副本类型规则：生存型启用血条/饥饿/死亡判定；剧情型零感知（默认）
    story["rules"] = {"survival": bool((settings or {}).get("survival"))}
    # 外源检索资料（8.16）：结构化存档，供导演游玩注入（build_injection 按 NPC/地点过滤预算）
    story["research_summary"] = research_summary
    story["review_notes"] = review_notes
    story["state"] = schema.new_state(story)

    # ── 5. 建档入库 + 注册身份 + 落盘 ──
    on_progress("正在为 NPC 建档入库", 88)
    _fill_game_db(story)
    _register_identity(story)
    store.save_story(story["story_id"], story)
    store.save_state(story["story_id"], story["state"])
    store.set_current(story["story_id"])

    logger.info(f"副本构建完成: {story['story_id']} ({story.get('title', '')})")
    return story
