#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
时间工具 — 为 AI 提供时间感

两个层次：
  1. get_now_full()  — 精确时间（年/月/日/星期/时/分），注入 A3 提示词
  2. get_time_period() — 概括性时段（凌晨/早上/上午/中午/下午/傍晚/晚上/深夜），
                         用于记忆存储时让 AI 概括性地记住时间
"""

from datetime import datetime

# 中文星期
_WEEKDAYS = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]

# 时段划分（小时 -> 时段词）
_PERIOD_BOUNDS = [
    (0, "凌晨"),
    (5, "早上"),
    (9, "上午"),
    (11, "中午"),
    (13, "下午"),
    (17, "傍晚"),
    (19, "晚上"),
    (23, "深夜"),
]


def get_time_period(now: datetime = None) -> str:
    """根据当前小时返回概括性时段词"""
    now = now or datetime.now()
    hour = now.hour
    period = "深夜"
    for bound, name in _PERIOD_BOUNDS:
        if hour >= bound:
            period = name
        else:
            break
    return period


def get_now_full(now: datetime = None) -> str:
    """返回精确到分钟的时间描述，带星期"""
    now = now or datetime.now()
    weekday = _WEEKDAYS[now.weekday()]
    return f"{now.year}年{now.month}月{now.day}日 {now.hour:02d}:{now.minute:02d}（{weekday}）"


def get_now_datetime_str(now: datetime = None) -> str:
    """返回 ISO 格式时间戳字符串"""
    now = now or datetime.now()
    return now.isoformat()


def build_time_context(now: datetime = None) -> str:
    """组合精确时间 + 时段，供 A3 注入提示词使用"""
    now = now or datetime.now()
    return f"现在是 {get_now_full(now)}，{get_time_period(now)}。"


# 全局可用（方便测试注入）
if __name__ == "__main__":
    print("精确时间:", get_now_full())
    print("时段:", get_time_period())
    print("时间上下文:", build_time_context())
