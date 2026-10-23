#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
玩家消息与系统剪贴板内容比对 — 防止误粘贴无关内容

当玩家要发送的消息与系统剪贴板当前内容高度相似（大段重合），
判定为"疑似误粘贴剪贴板内容"，前端据此弹窗确认。

实现：
  - 用 pyperclip 读取系统剪贴板文本
  - 用最长公共子序列（LCS）计算相似度/重合度
  - 重合片段超过阈值长度 → 判定为疑似粘贴
"""

import logging

logger = logging.getLogger("clipboard_check")

# 判定阈值：消息与剪贴板的最长公共子串 ≥ 该长度（字符）即视为疑似粘贴
MIN_COMMON_LEN = 30


def _read_clipboard() -> str:
    """读取系统剪贴板文本；读取失败返回空串"""
    try:
        import pyperclip
        return (pyperclip.paste() or "").strip()
    except Exception as e:
        logger.warning(f"读取剪贴板失败: {e}")
        return ""


def _longest_common_substring(a: str, b: str) -> str:
    """求两个字符串的最长公共子串（连续），返回该子串"""
    if not a or not b:
        return ""
    # 为控制内存，用较短的一边做窗口；一般玩家消息/剪贴板都不长
    len_a, len_b = len(a), len(b)
    # DP 记录以 i,j 结尾的最长公共子串长度
    dp = [0] * (len_b + 1)
    best_len, best_end = 0, 0
    for i in range(1, len_a + 1):
        prev = 0
        for j in range(1, len_b + 1):
            cur = dp[j]
            if a[i - 1] == b[j - 1]:
                dp[j] = prev + 1
                if dp[j] > best_len:
                    best_len = dp[j]
                    best_end = i
            else:
                dp[j] = 0
            prev = cur
    return a[best_end - best_len:best_end]


def check_clipboard_similarity(message: str) -> dict:
    """比对玩家消息与剪贴板内容，返回判定结果"""
    msg = (message or "").strip()
    if not msg:
        return {"suspect": False, "reason": "", "common": "", "common_len": 0}

    clip = _read_clipboard()
    if not clip:
        return {"suspect": False, "reason": "剪贴板为空", "common": "", "common_len": 0}

    common = _longest_common_substring(msg, clip)
    common_len = len(common)

    suspect = common_len >= MIN_COMMON_LEN
    reason = ""
    if suspect:
        # 重合占比也作为一个参考（防止极短消息但重合长的情况过度判定）
        ratio = common_len / max(len(msg), 1)
        reason = (f"消息与剪贴板内容重合 {common_len} 字"
                  f"（占消息 {ratio:.0%}），疑似误粘贴剪贴板中的无关内容")
        logger.warning(f"[剪贴板比对] 疑似误粘贴: {reason}")

    return {
        "suspect": suspect,
        "reason": reason,
        "common": common[:60],
        "common_len": common_len,
    }
