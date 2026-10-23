# AI-NPC-Forgemind 安卓版设计方案（折中 C 路线）

> 状态：P1/P2/P3 已完成（2026-09-11），P4 真机待测、P5 图标打包待做。
> 目标用户：仅作者本人自用（打包不需上架级严谨，开发者模式安装即可）。

## 1. 一句话目标

把现有 pro_AI2 后端（Flask + A1-A4 流水线）原样打进一个安卓 APK：原生壳只做「配置页 + 服务开关页」两个界面，Python 在本机跑 Flask，手机浏览器打开 `localhost:5003` 复用现有网页聊天 UI。

## 2. 路线决策（已定）

| 项 | 决策 | 理由 |
|---|---|---|
| 技术路线 | **Chaquopy**（Gradle 插件，Python 嵌入安卓工程） | Windows + Android Studio 原生打包，**无需 WSL2 / Buildozer**；原生壳+嵌入 Python 正是它的标准用法 |
| UI 方案 | 原生壳两个界面 + 浏览器访问现有网页 UI | 界面工作量最小，聊天界面零开发 |
| App 名 | AI-NPC-Forgemind | 与仓库名一致（用户已拍板） |
| 端口 | 127.0.0.1:5003 | 与 PC 版一致 |
| 最低安卓 | minSdk 26（Android 8），target 34 | 自用机 ColorOS 14 = Android 14，向下兼容留余量 |
| Python 端依赖（安卓包内） | flask、requests、jieba | flask-socketio 未被 app.py 使用；ddgs/beautifulsoup4 为搜索功能，按约束已停用且排除以瘦身 APK |

## 3. 总体架构

```
┌─ 手机 APK：AI-NPC-Forgemind ──────────────────┐
│ 原生壳（Kotlin，两个 Activity）                 │
│  ① 配置页：读写 config.txt（PROVIDER/API_KEY/   │
│     TEMPERATURE/MAX_TOKENS/TIMEOUT），格式同 PC │
│  ② 开关页：启动/停止服务 + 打开浏览器按钮         │
│ ──────────────────────────────────────────── │
│ 前台服务 ForegroundService（通知栏常驻）         │
│  └ Chaquopy Python 运行时                       │
│     └ 现有 pro_AI2 后端（app.py 精简版）         │
│        Flask @ 127.0.0.1:5003                  │
└──────────────────────────────────────────────┘
        ↓ 手机浏览器 http://localhost:5003
   现有 static/index.html 聊天 UI（原样复用）
```

数据目录映射：PC 的 `pro_AI2/data/` → 安卓 `files/data/`；`config.txt` → 安卓 `files/config.txt`（首次启动生成默认模板）。后端代码中所有相对路径改为基于该根目录解析，**PC 版行为不变**（根目录 = 项目根）。

## 4. ColorOS 14（Android 14）适配要点

1. **前台服务必须声明类型**：`android:foregroundServiceType="dataSync"`，并加 FOREGROUND_SERVICE_DATA_SYNC 权限。
2. **通知运行时权限**：Android 13+ 需动态申请 POST_NOTIFICATIONS，配置页放一个「通知权限」入口。
3. **ColorOS 独有**（写成配置页里的「保活指引」一段话，引导用户手动开）：
   - 设置 → 电池 → 本应用 → 允许后台运行/不优化
   - 最近任务下拉本应用卡片 → 锁定
   - 自启动管理允许（用于开机自启，可选）

## 5. 角色包（zip）规范 — 已确认

手机零内置角色，全部通过角色包安装；角色在 PC 上填表创建。

### 5.1 包结构
```
{角色名}_rolepack.zip
└── {角色名}/
    ├── identity.json      # identities.json 中该角色的条目（name/persona 等）
    ├── memory/            # 该角色 data/memory/{角色名}/ 下全部内容（各层记忆、history.json、索引）
    └── game_db.json       # 可选，data/game_db/{角色名}.json；无则缺省
```

### 5.2 两端 API（新增到 app.py，PC/安卓共用）
- `GET /api/rolepack/export?identity={name}` → 返回 zip 下载（PC 网页 UI 加「导出角色包」按钮）。
- `POST /api/rolepack/import`（multipart 上传 zip）→ 校验结构 → 写入 data 目录 → 合并 identities.json → 返回结果。身份已存在时**拒绝并报错**（不静默覆盖），提示先删除或改名。
- 网页 UI 同步加「导入角色包」上传按钮（用户已确认：导入入口放网页 UI，原生壳保持最薄）。

## 6. 原生壳细节

- **配置页**：读取 files/config.txt 渲染表单；保存时写回，保持 `KEY=VALUE` 注释格式与 PC 一致；提供「测试连接」按钮（调一次最小 LLM 请求验证 KEY）。
- **开关页**：大按钮启动/停止前台服务；状态徽标（运行中端口 5003 / 已停止）；「打开聊天页面」按钮 → Intent 打开浏览器 `http://localhost:5003`。
- **开机自启（可选开关）**：RECEIVE_BOOT_COMPLETED，默认关。
- **图标**：用文生图生成一张 1024px 主视觉（主题：齿轮+大脑/记忆拼图的 Forge+Mind 概念），再脚本切 mipmap-mdpi~xxxhdpi（48/72/96/144/192）+ 自适应图标前景层。

## 7. 实施阶段

| 阶段 | 内容 | 验证方式 |
|---|---|---|
| P1 角色包双端 ✅ | app.py 增 export/import 端点 + 网页两按钮（2026-09-11 完成） | 回环测试通过：导出可莉 83KB/57 条目 → 改名导入 → 记忆 55 文件+游戏库落盘 → 重复导入正确拦截 → 清理 |
| P2 壳工程 ✅ | Android Studio Kotlin 工程 + Chaquopy 插件 + 前台服务 + 两界面 + config 读写（2026-09-11 完成骨架，代码就绪待 AS 编译） | 首次编译需 Android Studio 装入后 Sync；真机 Run 验证服务启停、浏览器访问 localhost:5003 |
| P3 后端嵌入 ✅ | pro_AI2 后端以独立副本放入 android/app/src/main/python，data_paths.py 路径虚拟化（NPC_DATA_BASE 覆盖），剪贴板/web 等 PC 专属功能已由既有 try/except 与懒加载优雅降级（2026-09-11 完成，本地 NPC_DATA_BASE 冒烟验证 import+路径重定向通过） | 真机聊天一轮，记忆正常写入 files/data |
| P4 角色包实机 | 真机导入可莉角色包 | 手机聊可莉，召回旧记忆 |
| P5 图标打包 | 文生图图标 + 切图 + release APK 出包 | 开发者模式安装自用 |

## 8. 风险与对策

| 风险 | 对策 |
|---|---|
| ColorOS 杀后台 | 前台服务 + 保活指引（§4.3）；可接受自用手动白名单 |
| Chaquopy 打包个别纯 C 依赖失败 | 本项目安卓侧仅纯 Python 依赖（flask/requests/jieba），无 C 扩展，风险低 |
| 剪贴板误粘贴检测在安卓不可用 | import 处 try/except 优雅跳过，不影响主链路 |
| jieba 首次分词初始化慢 | 首次进入聊天页预预热一次，无感化 |

## 9. 待办登记

已登记 architecture.md 待办区 T-006，实施按本文件 §7 阶段推进。
