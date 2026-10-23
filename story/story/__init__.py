#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
剧情副本系统核心包（story 新系统专用）

职责划分：
  - schema    副本 JSON 校验 + 初始运行时状态生成
  - store     剧本（不可变）/ 运行时状态 / 当前副本指针的原子化落盘
  - builder   造城 AI：设置 → 副本 JSON（校验 + 审稿循环）→ 建档入库 → 注册身份
  - director  导演：权限门注入（跨 NPC 秘密隔离）+ [STATE:] 标签解析 + 里程碑/终局
  - narrator  旁白：把导演产生的"事件"转写成氛围文字（导演的嘴）

调用约定：所有 LLM 调用一律走 agent/ai_client.py 的全局 client。
"""
