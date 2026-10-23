# AI-NPC-Forgemind — 安卓版工程

> 状态：P2 壳工程骨架已完成（代码就绪），P3 后端嵌入已就绪。
> 首次编译需用 Android Studio（本仓库不含 gradle-wrapper.jar 二进制，由 AS 自动补齐）。

## 目录结构

```
android/
├── settings.gradle / build.gradle / gradle.properties
├── gradle/wrapper/gradle-wrapper.properties   # Gradle 8.10.2（wrapper jar 由 AS 生成）
└── app/
    ├── build.gradle            # Chaquopy 17 配置 + pip 依赖(flask/requests/jieba)
    ├── proguard-rules.pro
    └── src/main/
        ├── AndroidManifest.xml # 前台服务(dataSync) + 通知 + 网络权限
        ├── java/com/forgemind/npc/
        │   ├── MainActivity.kt        # 开关页 + 打开浏览器 + 通知权限 + 保活指引
        │   ├── SettingsActivity.kt    # 配置页：写 files/config.txt
        │   ├── NpcForegroundService.kt# 前台服务：Chaquopy 启动 Flask @127.0.0.1:5003
        │   ├── BootReceiver.kt        # 开机自启（可选）
        │   └── AppPaths.kt            # 数据根 + assets/static 解包
        ├── python/              # ⚠️ 后端副本（与 PC 版 pro_AI2 隔离）
        │   ├── app.py           # Flask 应用（静态目录优先 DATA_DIR/static）
        │   ├── npc_launcher.py  # 安卓专用启动器（设 NPC_DATA_BASE + 起 Flask）
        │   ├── data_paths.py    # 路径统一解析（NPC_DATA_BASE 覆盖）
        │   ├── agent/  memory/  tools/   # 已做路径虚拟化
        ├── assets/static/       # 网页 UI（index.html 手机版 / trace.html）
        └── res/                 # 布局 + 自适应图标
```

## 关键机制

1. **数据隔离**：Kotlin 启动 Python 前设 `NPC_DATA_BASE=<filesDir>`，后端所有
   `data_paths` 读/写落到 `files/data`，与 PC 版 `pro_AI2/data` 完全隔离、互不干扰。
2. **静态资源**：`assets/static` 首次启动解包到 `files/static`，供 Flask 托管。
3. **原生壳**：原生壳只有两个界面（开关页 / 配置页），聊天复用浏览器访问 `http://127.0.0.1:5003`。
4. **依赖裁剪**：仅打包 flask/requests/jieba（ddgs/bs4 搜索已停用，排除瘦身）。

## 如何首次编译

前置：Windows + 已装 **Android Studio**（含 SDK）+ 本机 **Python 3.8~3.13**（匹配 Chaquopy，供 buildPython 探测）。

1. Android Studio → Open → 选本 `android` 文件夹（让 AS 生成 wrapper、SDK 等）。
2. 若提示 Gradle wrapper 缺失，AS 会自动创建；或同步一下项目（Sync）。
3. 若 `python` 探测失败，在 `android/app/build.gradle` 的 `python { buildPython "你的 python.exe 路径" }` 显式指定。
4. 真机 USB 调试 → Run（装 arm64-v8a）。

> 提示：ABI 仅 `arm64-v8a` + `x86_64`。若需 32 位老机，去掉 minSdk 提升并加 `armeabi-v7a`。

## 留给 P5 的事项
- 图标：`res/drawable/ic_launcher_foreground.xml` 现为占位向量，P5 用文生图生成正式图标替换。
- 小程序包体积：每 ABI 数 MB + flask/jieba，属正常。

## 与 PC 版的关系
本目录是一份**独立副本**，最终编译只依赖 `android/app/src/main/python/` 下的副本，
PC 版根目录的代码改动不会自动同步到安卓（镜像关系）。若要跟随 PC 版更新，需重新复制一份（或后续做同步脚本）。