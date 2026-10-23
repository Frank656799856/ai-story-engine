#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据路径统一解析（安卓版副本专用）

背景：安卓端通过 Chaquopy 嵌入 Python，`__file__` 指向 APK 内的只读
python 目录。所有需要写入 data/ 的地方都必须重定向到可写的应用私有
目录（Android 的 files/，等价 PC 版的项目根 /data）。

方案：优先读取环境变量 NPC_DATA_BASE 作为数据根；未设置时回退为
本文件所在目录的上一级（即 PC 版默认行为，方便本机冒烟测试）。

其余模块应通过本模块拿到 PROJ 根/数据根，而不是自己用
Path(__file__).parent.parent 计算，从而保证两端行为一致。
"""

import os
from pathlib import Path

# 本文件目录 = .../src/main/python
_SELF_DIR = Path(__file__).resolve().parent

# 数据根：环境变量 NPC_DATA_BASE 优先，否则回退到本 python 目录
# （安卓由 Java/Kotlin 在启动 Python 前设置 NPC_DATA_BASE=<files 目录>；
#  未设置时仅用于本副本的本地冒烟调试，数据落到 python 目录内，与 PC 端隔离）
PROJECT_DIR = Path(os.environ.get("NPC_DATA_BASE", str(_SELF_DIR)))

CONFIG_FILE = PROJECT_DIR / "config.txt"
DATA_DIR = PROJECT_DIR / "data"