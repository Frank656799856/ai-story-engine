# AI 游戏 NPC 记忆系统

一个本地、私有、可无限自定义的 AI 游戏 NPC 对话系统。通过四子 Agent 流水线（A1-A4）实现**分层记忆**、**游戏数据库约束**、**反事实校验**与**成本控制（前缀缓存）**，让 NPC 记住玩家、遵守世界设定、不凭空编造，并能在沉浸式角色扮演与严格设定之间自由调节。

## ✨ 核心特性

- **四子 Agent 流水线**：A1 记忆搜索 → A2 深度思考 → A3 最终输出 → A4 记忆管理，封装成单个 NPC Agent。
- **分层记忆**：核心（core）/ 场景（episodic）/ 工作（working）三层存储，自动摘要、事件分割、升降级。
- **游戏数据库**：食物 / 道具 / 怪物 / 友好生物 / 地形 / 地点 / 世界观设定，按身份隔离。
- **反事实校验（程序标记 + 模型裁决）**：LLM 抽取词汇、Python 精确匹配数据库，拦截"凭空编造的设定"；对参考游戏角色自动放宽。
- **成本优化**：稳定前缀注入 + 前缀缓存，显著降低重复 token 开销。
- **多身份隔离**：每个 NPC 拥有独立的记忆目录与游戏数据库，互不干扰。
- **玩家状态清单**：NPC 记住"你这个具体的玩家"（武器 / 好感 / 约定），跨会话不失忆。
- **角色当前状态**：前端手动填写"可莉现在在禁闭室"这类即时时，注入对话。
- **误粘贴检测**：发送前比对该消息与系统剪贴板内容（最长公共子串），避免误发无关内容。
- **多厂商模型兼容**：DeepSeek / 豆包 / GPT / GLM / 通义 / Kimi 等，仅需改 config.txt。

## 🚀 快速开始

### 环境要求

- Python 3.9+（开发于 3.13）
- 一个或多个人工智能 API Key

### 安装与启动

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 额外依赖（可选功能）
pip install pyperclip   # 误粘贴检测需要

# 3. 配置 API
#    编辑 config.txt，填入 PROVIDER 和 API_KEY

# 4. 启动
python app.py
```

启动后浏览器打开 **http://localhost:5003** ，在网页左侧身份下拉框切换 NPC 即可开始对话。

> 推理详情（缓存命中率 / 各 Agent 调用链路 / token 统计）可访问 **http://localhost:5003/trace**。

## ⚙️ 配置

编辑 `config.txt`：

```
PROVIDER=deepseek     # 厂商简称：deepseek / doubao / glm / openai / qwen / moonshot / minimax / yi
API_KEY=sk-xxxx       # 你的 API Key
TEMPERATURE=0.8       # 温度（0-2，越高越随机）
MAX_TOKENS=4096       # 最大输出 token
TIMEOUT=60            # 请求超时秒数
```

## 🧠 架构总览

```
浏览器 (index.html)  ←→  Flask (app.py)
                               │
                         NPCPipeline
                        ┌────┴───────────────┐
                        ▼                     ▼
                   用户输入消息          NPC 最终回复
                        │                     ▲
                        ▼                     │
  ┌─────────┐   ┌──────────┐   ┌─────────────────────┐
  │ A1 搜索  │──▶│ A2 思考  │──▶│  A3 输出 + 反事实校验 │
  └─────────┘   └──────────┘   │  (正/重生成)         │
                                     ▲   ▲
                                记忆库 │   │ 游戏数据库
                                     │   │
                               ┌─────┴───┴───────────┐
                               │       A4 记忆管理     │
                               │  摘要/事件/升降级     │
                               └─────────────────────┘
```

### 目录结构

```
pro_AI2/
├── app.py               # Flask 入口（HTTP / 静态页面 / 各 API）
├── config.txt           # API 配置
├── requirements.txt     # 依赖
├── agent/               # 四子 Agent 流水线
│   ├── pipeline.py      # NPCPipeline 编排 + 对话历史
│   ├── a1_search.py     # A1 记忆搜索
│   ├── a2_deep_think.py # A2 深度思考
│   ├── a3_output.py     # A3 生成 + 反事实校验/重生成
│   ├── a4_manager.py    # A4 记忆管理 + 玩家状态
│   ├── ai_client.py     # LLM 调用封装（缓存/重试）
│   └── prompts.py       # 所有 system prompt 集中地
├── memory/              # 记忆存储层
│   ├── store.py         # MemoryStore
│   ├── index.py         # MemoryIndex 索引
│   └── retrieval.py     # 检索
├── tools/               # 工具层
│   ├── game_db.py       # 游戏数据库（按身份加载）
│   ├── npc_state.py     # 角色当前状态
│   ├── clipboard_check.py # 误粘贴检测（LCS）
│   └── time_utils.py    # 时间上下文
├── static/              # 前端页面
│   ├── index.html       # 聊天页
│   └── trace.html       # 推理详情页
└── data/                # 运行期数据
    ├── identities.json  # NPC 身份注册表
    ├── memory/{身份}/   # 每个 NPC 的记忆
    ├── game_db/{身份}.json # 每个 NPC 的游戏数据库
    └── npc_state/npc_state.json # 各角色当前状态
```

## 🆕 创建新 NPC

系统支持两种录入方式（详见 [data/AI代填NPC提示词.md](data/AI代填NPC提示词.md)）：

1. **自动录入**：网页左侧「创建身份」，填入 name / personality / world_setting，可选填 30 句对话样本；首次启动自动提炼角色画像。
2. **手动建库**：在 `data/game_db/名称.json` 手动写游戏数据库，在 `data/memory/名称/` 放核心记忆。

格式与字段详解、含参考大型游戏角色（如原神·可莉）的完整示例，参见 [data/AI代填NPC提示词.md](data/AI代填NPC提示词.md)。

## 📚 文档

- [architecture.md](architecture.md) — 完整架构、数据存储结构、校验机制、业界对比、规划与待办。
- [data/AI代填NPC提示词.md](data/AI代填NPC提示词.md) — 给外部 AI 用的 NPC 资料代填提示词与字段详解。

## 🔌 HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/chat` | 发送消息 |
| GET | `/api/stats` | 获取统计 |
| POST | `/api/clear` | 清空对话历史 |
| GET | `/api/history` | 获取对话历史 |
| GET/POST | `/api/npc_state` | 读取 / 保存角色当前状态 |
| POST | `/api/check_clipboard` | 误粘贴检测（比对剪贴板） |
| GET | `/api/trace` | 获取推理详情（`detailed=1` 返回全量链路） |
| POST | `/api/trace/clear` | 清空推理日志 |
| GET | `/api/identities` | 列出所有身份 |
| POST | `/api/identities` | 创建新身份 |
| DELETE | `/api/identities/<name>` | 删除身份 |
| POST | `/api/switch` | 切换当前身份 |

## 📋 规划与待办

见 [architecture.md §12](architecture.md)（含 T-001 玩家状态、T-002 AI 代填等迭代记录）。

## ⚠️ 说明

- 本项目为个人学习 / 定制化角色扮演用途，所有游戏数据基于公开资料整理，不对版权作任何主张。
- AI 依赖外部大模型厂商 API，token 消耗费用由调用方承担。