#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
旁白（Narrator）— 导演的嘴

把导演产生的"事件"（里程碑 narration_brief 等）转写成简短的氛围旁白。
约定：
  - events 为空 → 不调 LLM，直接返回空串（旁白只在剧情推进时出现）
  - 任何 LLM 失败都静默返回空串，绝不抛异常打断对话主流程
"""

import logging

from agent.ai_client import client
from agent import prompts as PROMPTS

logger = logging.getLogger("story_narrator")


def narrate(events: list, story: dict) -> str:
    """把事件列表转写成一段旁白。

    Args:
        events: 导演 apply() 产出的事件字符串列表（非空才调用 LLM）
        story:  剧本 dict（取当前场景与叙事基调）

    Returns:
        旁白文本；无事件或调用失败返回空串。
    """
    if not events:
        return ""
    try:
        setting = story.get("setting", {}) if isinstance(story, dict) else {}
        scene = (story.get("state", {}) or {}).get("scene", "") or setting.get("world_name", "")
        user = PROMPTS.NARRATOR_USER.format(
            events="；".join(str(e) for e in events),
            scene=scene,
            tone=setting.get("tone", ""),
        )
        text = client.chat(
            system_prompt=PROMPTS.NARRATOR_SYSTEM,
            user_message=user,
            temperature=0.9,   # 旁白要文采，温度稍高
            max_tokens=120,    # 2~3 句足够
        )
        return (text or "").strip()
    except Exception as e:
        logger.warning(f"旁白生成失败（静默降级）: {e}")
        return ""
