# AI 游戏 NPC 记忆系统 — 完整架构与操作指南

> 本文档基于对当前磁盘代码的**重新审视**编写，反映最新状态。
> 最后一次核对时间：2026-08-29（本次改动删除"A4 动态改游戏库"功能、时间上下文移至 user 消息尾部）。

---

## 目录

1. [项目概览](#1-项目概览)
2. [整体架构图](#2-整体架构图)
3. [核心数据存储](#3-核心数据存储)
4. [四个子 Agent 详解](#4-四个子-agent-详解)
5. [提示词系统（prompts.py）](#5-提示词系统promptspy)
6. [缓存优化机制](#6-缓存优化机制)
7. [游戏数据库（静态/只读）](#7-游戏数据库静态只读)
8. [常见问题排查](#8-常见问题排查)
9. [新建一个游戏 NPC（操作指南）](#9-新建一个游戏-npc操作指南)
10. [仿照大型游戏角色建 NPC 的流程示例](#10-仿照大型游戏角色建-npc-的流程示例)

---

## 1. 项目概览

本地私人化的**单 NPC 记忆聊天系统**，是"AI 自动化游戏"的一个子模块。核心目标是解决游戏 NPC 长对话中**"聊久了忘事、人设崩"**的记忆问题。参考了「万忆中枢 wanyimem」的记忆思想，但代码**完全自建**（`wanyimem-1.0.6/` 目录仅供借鉴，不参与运行）。

关键技术栈：

| 项 | 值 |
|---|---|
| 后端 | Python + Flask + Flask-SocketIO |
| 模型调用 | `agent/ai_client.py` 统一封装（多厂商探测） |
| 前端 | `static/index.html`（聊天页）、`static/trace.html`（推理日志页） |
| 数据 | 纯 JSON 文件持久化（无数据库中间件） |
| 端口 | 5003 |

---

## 2. 整体架构图

```
                    浏览器 (static/index.html)
                              │  POST /api/chat {message, identity}
                              ▼
                 ┌────────────────────────────┐
                 │        app.py (Flask)      │
                 │  · 身份注册表加载/创建/删除 │
                 │  · 每身份一个 NPCPipeline   │
                 │  · switch 切换当前身份      │
                 └─────────────┬──────────────┘
                               │ 按 identity 取/建 NPCPipeline
                               ▼
        ┌────────────────  NPCPipeline.chat() ────────────────┐
        │ 保存 user → 取最近6条历史 → 依次调用 A1→A2→A3→A4        │
        │ trace.add() 记录每阶段结果                           │
        └───────────────┬─────────────────────────────────────┘
                        │
   ┌────────────┬───────┴────────┬───────────────┬─────────────┐
   ▼            ▼                ▼               ▼
  A1 搜索      A2 思考          A3 输出         A4 管理
  ─────────   ─────────        ─────────       ─────────
  多路检索     任务分类(chat/    组 system prompt 摘要/事件分割
  语义重排     knowledge)        生成回复         决定记忆层级
  游戏实体打标  四步推理筛选        游戏库校验+      分类归档
                             反事实重生成       记忆升降级
   │            │                │               │
   └──── 记忆库 ┴──── 记忆库 ─────┴─── snapshot ──┴─→ 写回记忆库
        (memory/…)              │
                                ▼
                    tools/game_db （只读参考，A1打标/A3校验）
```

### 一次请求的生命周期（8 次 LLM 调用之内）

```
用户输入
  │ ① 保存到 history.json
  ▼
A1. 记忆搜索
  │   · 关键词检索 + 语义重排（可能1次LLM）
  │   · detect_game_entities()（游戏库实打标，纯规则）→ game_entities
  │   · 输出：candidate memories + game_entities + game_db_active
  ▼
A2. 深度思考
  │   · LLM 分类：chat / knowledge
  │   · 四步推理：初筛→查冲突→找空白→筛选
  │   · 输出：mode、filtered_ids（用哪几条记忆）
  ▼
A3. 最终输出（可能是 2~4 次 LLM）
  │   · 组装 system prompt（固定前缀区 + 动态区）
  │   · 调 LLM 生成初始回复
  │   · 若有游戏库：_verify_and_revise() 校验
  │       未通过 → 带"修改建议"重生成一次（反事实修正）
  │   · 输出：reply + snapshot（给A4）
  ▼
A4. 记忆管理
  │   · 生成摘要（1次LLM）
  │   · 检测事件、决定层级、算重要性
  │   · 创建记忆 + 写索引
  │   · 阈值触发时记忆降级
  ▼
返回给玩家 reply
```

---

## 3. 核心数据存储

所有数据都在 `data/` 下，纯 JSON。

```
data/
├── identities.json        # 身份注册表（每个NPC有一行 persona）
├── memory/                # 记忆库
│   ├── {identity}/        #   每个身份独立目录
│   │   ├── core/          #     核心记忆（永久，pinned）
│   │   ├── episodic/      #     情景记忆（按事记叙）
│   │   ├── working/       #     工作记忆（短期，易衰减）
│   │   ├── history.json   #     对话历史（含 role/content/mode/timestamp）
│   │   └── index.json     #     记忆索引（供检索）
│   └── index.json         #   （memory 层全局，历史遗留，实际按身份用）
├── game_db/               # 游戏数据库（手动创建）
│   └── {identity}.json    #   每个身份可选
└── model_cache.json       # 模型探测缓存
```

### 记忆字段（store.py create）

每条记忆是一个 JSON 文件，核心字段：

| 字段 | 含义 |
|---|---|
| `memory_id` | 唯一ID，如 `mem_20260825_0c8556ca` |
| `layer` | `core` / `episodic` / `working`（或 `archived`） |
| `content` | 记忆内容原文 |
| `summary` | 一句话摘要（检索/A3用） |
| `keywords` | 关键词列表（索引用） |
| `importance` | 0~1 重要度 |
| `pinned` | 是否锁定（核心记忆=True） |
| `decay_weight` | 衰减权重（retrieval.py 有半衰期参数） |
| `tags`/`mode`/`time_period` | 分类标签/聊天模式/时间泛化时段 |
| `emotional_tag`/`event_id` | 情绪标签/事件ID |

> 核心记忆示例（艾琳/core/mem_20260825_0c8556ca.json）：
> `content="艾琳最喜欢的饮品是月光花茶"`, `layer="core"`, `importance=1.0`, `pinned=true`

---

## 4. 四个子 Agent 详解

### A1 — 记忆搜索（`agent/a1_search.py`）
- **入口**：`search(user_message, recent_history)`
- **职责**：
  1. 多路检索（关键词 `query_by_keywords` + 语义）
  2. LLM 语义重排 `A1_SEMANTIC_RERANK_SYSTEM`（对候选摘要打分重排）
  3. **游戏实体打标** `detect_game_entities()`：用 `game_db.detect_entities()` 纯规则匹配，返回 `game_entities`
  4. 输出 `game_db_active`（该身份有无游戏库）
- **产物**：候选记忆 + `game_entities` + `game_db_active`
- **平台交互**：pipeline 取 `format_for_a3()` 生成供 A3 用的记忆列表

### A2 — 深度思考（`agent/a2_deep_think.py`）
- **入口**：`process(user_message, a1_result)`
- **职责**：
  1. 任务分类 `A2_CLASSIFY_SYSTEM`：`chat` / `knowledge`（LLM分类 + 规则降级）
  2. 深度思考决策 `A2_DEEP_THINK_SYSTEM`
  3. 四步推理：初筛→查冲突→找知识空白→筛选
  4. 为 A3 准备：`filtered_ids`、`search_text`、`need_external_knowledge`
- **产物**：`{mode, filtered_ids, reasoning_chain, search_text, ...}`

### A3 — 最终输出（`agent/a3_output.py`）
- **入口**：`generate(user_message, a2_result, a3_memories, persona, ...)`
- **职责**：
  1. 组装 system prompt（见 §5）
  2. 调 LLM 生成回复（`cache_pad=True`）
  3. **游戏数据库校验 + 反事实重生成**（详见下）
  4. 生成 `snapshot` 给 A4
- **反事实链路**：
  - `_build_game_db_section()`：按 A1 实体标签分类检索库（每类5条）；无标签时**兜底注入精简全局库**（每类3条）
  - `_verify_and_revise()` → `_check_game_db_facts()`：
    - 校验官 `A3_GAME_DB_CHECKER_SYSTEM` 判断回复是否与权威设定相悖
    - 通过 → 不改；不通过 → 用 `A3_GAME_DB_REVISE_SYSTEM` + `A3_GAME_DB_REVISE_USER` 重生成一次
  - **特殊分支**：玩家问到库中未记载的事物，NPC 应坦诚说"超出知识范围"，不算反事实；库对已记载实体必须照设定回答
- **产物**：`{reply, mode, snapshot, game_db_used, game_db_checked, game_db_revised}`

### A4 — 记忆管理（`agent/a4_manager.py`）
- **入口**：`manage(a3_snapshot, persona)`
- **职责**（当前版本**只做记忆管理，不再改游戏库**）：
  1. 生成对话摘要 `A4_SUMMARY_SYSTEM`（1次LLM）
  2. 检测事件 `_detect_events()`
  3. 决定记忆层级 `_decide_layer()`、算重要性 `_calc_importance()`
  4. 创建记忆 + 写入索引
  5. 事件触发时 `_check_promotion_demotion()`（升降级）
  6. 缓冲区满 10 条时 `_schedule_decay()`（衰减）
- **产物**：stats 字典（摘要/事件/创建/升级/降级计数）

> ⚠️ 注意：历史上 A4 曾有"动态把玩家提到的新名词写入游戏库"（`_update_game_db`/共同提及豁免）功能，**已按需求整体移除**。游戏库现在是**静态/只读**的。

---

## 5. 提示词系统（prompts.py）

所有 system prompt 都集中在 `agent/prompts.py`，常量命名标注用途。关键常量：

| 常量 | 调用方 | 作用 |
|---|---|---|
| `A1_SEMANTIC_RERANK_SYSTEM` | A1 | 记忆候选语义打分 |
| `A2_CLASSIFY_SYSTEM` | A2 | chat/knowledge 二分类 |
| `A2_DEEP_THINK_SYSTEM` | A2 | 深度思考引导 |
| `A3_TIME_SECTION_TEMPLATE` | A3 | 时间感引导（固定内容） |
| `A3_IDENTITY_TEMPLATE` / `A3_IDENTITY_DEFAULT` | A3 | NPC 人设块 |
| `A3_FORMAT_AND_STYLE` | A3 | 输出风格 |
| `A3_MODE_CHAT` / `A3_MODE_KNOWLEDGE_BASE` / `SEARCH` / `NO_MEMORY` | A3 | 模式指令 |
| `A3_GAME_DB_SECTION` | A3 | 游戏库核对段模板（占位 `{world_name}{world_desc}{entries_text}`） |
| `A3_GAME_DB_CHECKER_SYSTEM` | A3 | **校验官**（判断是否反事实） |
| `A3_GAME_DB_REVISE_SYSTEM` / `REVISE_USER` | A3 | 反事实重生成指令 |
| `A4_SUMMARY_SYSTEM` | A4 | 对话摘要 |
| `CACHE_PAD` | ai_client | 缓存填充（见 §6） |

### A3 的 system prompt 结构

```
[固定前缀区 — 字节级稳定，用于命中厂商缓存]
  A3_IDENTITY_TEMPLATE / DEFAULT  (人设)
  A3_FORMAT_AND_STYLE             (风格)
  A3_MODE_CHAT / KNOWLEDGE_BASE   (模式基础)
  A3_TIME_SECTION_TEMPLATE        (时间感引导，固定文案)

[动态区 — 易变，放在前缀之后]
  核心记忆 + 相关经历              (来自A1)
  A3_MODE_KNOWLEDGE_SEARCH        (知识模式搜索内容，如有)
  A3_GAME_DB_SECTION              (游戏库核对段，如有)
```

> **重要**：具体的**当前时间**已从 system prompt **移除**，改由 `_build_user_prompt()` 追加到 **user 消息末尾**（`（当前时间：...）`）。这样 system prompt 前缀不含动态内容，缓存命中率更高；而 NPC 仍保有对时段的自然感知。

---

## 6. 缓存优化机制

### CACHE_PAD（app 级固定前缀）
- `CACHE_PAD` 在 `prompts.py`，由 `ai_client.py` 自动拼到所有 system prompt **最前面**。
- 目的：让**每次调用完全一致**的头部长度超过 DeepSeek 前缀缓存可靠命中线（约 1024 token），命中厂商侧上下文缓存，省重复输入计费。
- **原则：该段必须字节级不变**，任何改动会整体失效。

### 动态内容后置 / 移出 system prompt
- 对所有 A3：`时间` 已挪到 user 消息末尾（不久会验证命中率提升）。
- 记忆 / 搜索 / 游戏库段属动态内容，天然无法缓存命中，放在固定前缀之后。
- 游戏库校验与 A4 的库文本**限量采样**（每类 3~6 条），控制 token 成本。

### 命中率观测
- 运行 `python app.py` 后访问 **`http://localhost:5003/trace`**，页面上"API"卡片显示每次调用的 `缓存命中/未命中 token` 及命中率。

---

## 7. 游戏数据库（静态/只读）

游戏数据库是**手动创建**的 JSON 文件，位于 `data/game_db/{identity}.json`。程序只在 **A1 打标**和 **A3 校验**时读取它，**从不写入**（改动后保持只读）。

### 分类（`CATEGORIES`）
```
食物 / 道具 / 怪物 / 友好生物 / 地形 / 地点 / 世界观设定
```

### JSON 结构示例（`data/game_db/艾琳.json`）

```json
{
  "world": {
    "name": "黑森林",
    "description": "一片被古老魔法笼罩的幽暗森林..."
  },
  "reference": {
    "source_game": "原神·可莉",
    "dialogue_samples": ["...", "..."],
    "summary": "首次启动提炼出的角色画像摘要"
  },
  "categories": {
    "食物": [
      { "name": "月光花茶", "description": "用只在满月之夜绽放的月光花冲泡的茶..." }
    ],
    "道具": [ { "name": "森林护符", "description": "..." } ],
    "怪物": [ { "name": "暗影狼", "description": "..." } ],
    "友好生物": [ { "name": "荧光蝶", "description": "..." } ],
    "地形": [ { "name": "月光花丛", "description": "..." } ],
    "地点": [ { "name": "黑森林", "description": "..." } ],
    "世界观设定": [ { "name": "森林之约", "description": "..." } ]
  }
}
```

#### reference 字段（录入新 NPC 用，可选）
| 字段 | 说明 |
|---|---|
| `source_game` | 参考的资料来源（如「原神·可莉」）。**只作背景信息**，不会触发 AI 提炼画像 |
| `dialogue_samples` | 该角色与他人交流的对话样本（可填约 30 句）。**首次启动**时 AI 据此提炼角色画像（触发提炼的唯一条件） |
| `summary` | 提炼出的画像摘要。**首次启动自动生成**后写回，同时充当「已完成」标记，避免重复总结 |

> 交互流程：录入新 NPC 时，向 `reference.dialogue_samples` 填入该角色的对话样本（约 30 句），
> 首次启动 `NPCPipeline` 时，若发现 `dialogue_samples` 非空且 `summary` 为空，自动调用 AI 生成画像，并同步完成三件事：
>   （1）把 `summary` 写回 game_db（完成标记）；
>   （2）写入一条 `layer=core, pinned` 的角色画像核心记忆；
>   （3）若 `persona.personality` 为空则用画像回填 `identities.json`。
> 仅填 `source_game` 时不触发提炼，只作为游戏库背景信息供 A3 参考。

### 关键方法（`tools/game_db.py`）
| 方法 | 用途 |
|---|---|
| `get_game_db(identity)` | 取（缓存的）实例 |
| `.available` | 该身份是否接入游戏库（有文件=True） |
| `.detect_entities(text)` | A1 打标：文本中匹配实体名 |
| `.search_by_categories(cats, max_per_cat)` | A3 按分类限量检索 |
| `.get_world_setting()` | 取世界观 |
| `.format_entries(entries)` | 格式化为 LLM 可读文本 |
| `.get_reference()` | 读取 reference 字段（source_game/dialogue_samples/summary） |
| `.write_reference_summary(summary)` | 写回画像摘要（作为完成标记） |

> 无游戏库文件的身份（如 `default`）`available=False`，游戏库相关分支整体跳过，不影响普通聊天。

---

## 8. 常见问题排查

| 现象 | 排查思路 |
|---|---|
| 某身份游戏库校验不生效 | 确认 `data/game_db/{identity}.json` 存在且 `world` 段已填；重启服务 |
| 缓存命中率低 | 看 `/trace` 的 API 卡片；确认 system prompt 前缀未含时间/记忆/搜索等动态内容 |
| 打回/反事实太多 | 检查是否该 NPC 的 A3 生成了库中未记载的新实体；核对 `A3_GAME_DB_SECTION` 是否被注入 |
| 记忆不衰减/不升级 | 检查 `data/memory/{identity}/index.json` 正常；A4 缓冲区需满10条才触发衰减 |
| 端口占用 | 默认 5003，改 `app.py` 的 `app.run(port=...)` |

---

## 9. 新建一个游戏 NPC（操作指南）

建 NPC 只需两步：**① 用前端/API 建身份（含核心记忆）** + **② 手动放一个游戏库文件（可选但推荐）**。

### 方式一：通过前端界面（最简单，推荐）

1. 启动服务：`python app.py`，浏览器打开 `http://localhost:5003`
2. 点界面上的**"创建身份"**按钮，填写：
   - **身份名称**（唯一，不能和已有重复）
   - **性格**（如"温和善良"）
   - **世界观**（如"黑森林守护者"）
   - **核心记忆框**：每行一条记忆，直接写人设/事实。例如：
     ```
     艾琳是黑森林的守护者，已守护这片森林三百年
     艾琳最喜欢的饮品是月光花茶
     艾琳性格温和善良，讨厌破坏森林的人
     ```
     （前端会自动把每一行转成一条 `layer=core, importance=1.0, pinned=True` 的核心记忆）
3. 点创建 → 自动切换到新身份 → 开始聊天。

### 方式二：调用 API（适合批量/脚本）

```
POST /api/identities
Content-Type: application/json

{
  "name": "西尔维亚",
  "persona": {
    "name": "西尔维亚",
    "personality": "冷静睿智",
    "world_setting": "晨曦峡谷的守望者"
  },
  "core_memories": [
    { "content": "西尔维亚是晨曦峡谷的守望者", "summary": "守望者身份", "keywords": ["守望者"] }
  ]
}
```

后端 `create_identity()` 会：
- 写入 `data/identities.json`
- 实例化该身份的 `NPCPipeline`（自动建 `data/memory/{identity}/` 各层级目录）
- `init_core_memories()` 把传入的记忆写成核心记忆（pinned）

之后发送消息用 `identity` 指定：
```
POST /api/chat  {"message": "你好", "identity": "西尔维亚"}
```

### ③（推荐）录入游戏数据库

游戏库**不进 API，而是手动编辑文件** `data/game_db/{identity}.json`。照着 [#7 的 JSON 结构](#7-游戏数据库静态只读) 填，注意：
- **它决定 A1 能检测到哪些实体、A3 校验与哪些设定比对**。
- 填写实体名时用词要和你预期玩家会说的称呼一致（A1 是子串匹配）。
- 无该文件则跳过游戏库功能，NPC 退化为纯记忆聊天。

### ④ 验证

1. 重启服务（若已运行）确保新身份就绪。
2. `/trace` 页观察该身份的 A1 是否检测到 `游戏实体`、A3 是否 `游戏核对=True`。
3. 直接用"核心记忆里提过的高频词/游戏库里的实体名"去问，看 NPC 是否表现一致且不编造。

---

## 10. 仿照大型游戏角色建 NPC 的流程示例

假设要仿照某大型游戏的角色（这里以"某峡谷神庙的守关长老"为例，方法论通用）。总流程：**先定概念 → 写 persona → 写核心记忆 → 写游戏库 → 聊天验证**。

### 步骤 1：提炼角色三要素
| 要素 | 示例取值 |
|---|---|
| 身份/名字 | `守界长老` |
| 性格 | 威严、寡言、重誓约 |
| 世界观 | 神庙由他看守数百年，守护一件被封印的古器 |

### 步骤 2：写成 persona
```json
{
  "name": "守界长老",
  "personality": "威严寡言、恪守誓约",
  "world_setting": "远古神庙的守关人，看守被封印的古器，数百年未离一步"
}
```

### 步骤 3：写核心记忆（决定人设一致性）
在"核心记忆框"每行一条，覆盖：身份、性格、在此地的职责、几个关键立场。
```
守界长老是远古神庙的守关人，看守被封印的古器
他誓言终生不离开神庙大门
他寡言少语，说话极为简短，从不闲谈
只有证明来意诚心的人才能觐见古器
若有人强闯，他会唤醒神庙的守护石像
```

### 步骤 4：写游戏数据库 `data/game_db/守界长老.json`
对照角色的世界观设计实体系。例如：
```json
{
  "world": {
    "name": "封神古庙",
    "description": "一座被时间遗忘的远古神庙，正殿封印着古器'启明镜'，长老在此看守数百年。"
  },
  "categories": {
    "道具": [
      { "name": "启明镜", "description": "被封印的古器，据说能映照人心，只有诚心者可触碰。" },
      { "name": "长老手杖", "description": "象征守关人身份的骨杖，杖头刻着一只闭眼蛇。" }
    ],
    "怪物": [
      { "name": "守护石像", "description": "长老唤醒的神庙守卫，由古石雕成，坚硬无比，只服从长老的号令。" }
    ],
    "地点": [
      { "name": "封神古庙", "description": "远古神庙，位于孤峰之巅，只有一条幽径可通。" }
    ],
    "世界观设定": [
      { "name": "守关誓约", "description": "长老毕生不离开神庙，不见来意不良者，违誓者古器封印会松动。" }
    ]
  }
}
```

### 步骤 5：验证
- 问"启明镜是什么" → 长老应按世界观设定回答，不编造额外细节。
- 问库中**没记载**的地区（如"北境的事你听过吗"）→ 长老应坦诚"超出我的知识范围"或"我只守着这座庙"，**不编造**。
- 让他聊一段较长对话 → 检查 `/trace` 里 A1 游戏实体命中数、A3 校验是否正确通过。

---

## 附：本次（最近一轮）代码改动纪要

| 文件 | 改动 |
|---|---|
| `agent/a4_manager.py` | 删除 `_update_game_db`/`_extract_candidates`/`_parse_json_array`/`_game_db_update_enabled`；`manage()` 不再收录游戏库；日志去掉"游戏库新增"；清理 json/math/re 导入 |
| `agent/a3_output.py` | 去掉 `co_mentioned` 链路；`_verify_and_revise` 返回简化；`_build_user_prompt` 末尾加当前时间；system prompt 固定区加回时间感引导 |
| `agent/prompts.py` | 删除 `A4_GAME_DB_EXTRACT_SYSTEM`；校验官提示词删"共同提及豁免"分支；`A3_TIME_SECTION_TEMPLATE` 改为固定文案 |
| `agent/ai_client.py` | 删除 `GAME_DB_UPDATE` 开关及加载逻辑 |
| `agent/pipeline.py` | A4 trace 日志去掉"游戏库新增" |
| `config.txt` | 删除 `GAME_DB_UPDATE=on` |

---

## 11. 与业界方案对比（2026-08 检索整理）

本系统并非从零凭空设计，而是对照业界已落地的 AI NPC 方案后确定架构。整理如下，方便以后回查与决策。

### 11.1 逆水寒手游「智能 NPC」（网易伏羲）

**一句话定位**：国内首个大规模落地（1000+ NPC）的 **AI Agent Harness（代理管控工程）**——给大模型套缰绳，而非无条件放养。

**核心结论（关键一点）**：它不单靠训练也不单靠外置库，而是**「后训练灌设定 + 运行时外置辅助」两条腿走路**：

| 层次 | 做法 | 类型 |
|---|---|---|
| 模型层 | **游戏专属后训练**（把武侠世界观/门派/武功灌进基座模型）；海量玩家用「大模型蒸馏小模型」降本 | 专用训练 |
| 运行时层 | Harness 5 大模块 + 运行时读取身世/属性/好感度等**外置动态数据** | 外置辅助 |

**Harness 5 大模块对照**：

| 逆水寒模块 | 功能 | 本系统对应 |
|---|---|---|
| 身份锚定 | 人设/背景/能力边界/世界观约束，治 OOC | ≈ persona + 核心记忆 |
| 记忆管理 | 短期（当前上下文）/中期（与某玩家历史）/长期（世界知识）分层 | ≈ core/episodic/working 三层 |
| 行为对齐 | NPC 行为绑定玩法/装备/好感度 | —（本系统纯对话） |
| 安全审核 | 内容合规过滤 | 有反事实校验，缺内容合规 |
| 交互编排 | 感知→决策→对话编排 | ≈ A1→A2→A3→A4 |

**对我们的启示**：
- 它的优势在"把知识**训练进模型**"，成本高；我们把知识**在提示词里外挂**（游戏库塞缓存前缀），零训练、改一个 NPC 只改 JSON，更适合个人/小型项目。
- 我们的模块覆盖了它 5 大模块里的 4 个，唯一缺口是「内容安全审核」。

### 11.2 酒馆 SillyTavern / TavernAI（字符卡角色扮演）

**一句话定位**：非游戏导向的纯聊天角色扮演工具，用 **6 层约束**治角色一致性，而非单一提示词。

| 层次 | 说明 | 本系统对应 / 可借鉴 |
|---|---|---|
| 角色卡 | Description/Personality/Scenario 每次生成都发，构成人设骨架 | ≈ persona + 核心记忆 |
| First Message | 只发一次，却定义了回复风格与长度 | 可借鉴：给 NPC 配"示例开场白" |
| **Example Dialogue** | 用对话示例教模型"该角色怎么说话"，比干写性格描述更有效 | ≈ `reference.dialogue_samples`（30 句触发画像提炼）正好是这思路 |
| 正则/场景宏 | 处理动态状态 | — |
| Post-History Instructions (PHI) | 在消息**末尾**再贴关键指令，模型给更高优先级 | ≈ 本系统把当前时间挪到 user 末尾（同理） |
| 世界书（Lorebook） | 可开关的知识条，命中才注入 | ≈ 本系统的游戏数据库（按需检索注入） |

**对我们的启示**：酒馆的 Example Dialogue 与 PHI 两招，本系统已分别用 `dialogue_samples` 和"时间挪末位"隐性落到。

### 11.3 本系统的架构定位

> 逆水寒 = "重部署"：后训练灌知识，保上线质量，但贵、要数据、要训练团队。
> 酒馆 = "纯提示词"：只靠角色卡，灵活但 OOC 失控风险高。
> **本系统 = 中间路线**：不训练模型，但用「游戏数据库 + 分层的记忆 + 反事实/实体校验」做轻量约束——成本最低且能治 OOC。

---

## 附二：本轮 reference 画像提炼功能纪要

| 文件 | 改动 |
|---|---|
| `agent/prompts.py` | 新增 `A1_CHARACTER_PROFILE_SYSTEM`（从对话样本提炼角色画像） |
| `agent/pipeline.py` | 新增 `NPCPipeline.ensure_character_profiled()`：首次启动若 `reference.dialogue_samples` 非空且 `summary` 为空，调用 AI 提炼画像并落地三件事（写回 game_db.summary 标记 / 写 pinned 核心记忆 / 回填 persona） |
| `tools/game_db.py` | 新增 `get_reference()` / `write_reference_summary()` |
| `data/game_db/可莉.json` | 新增 `reference.source_game="原神·可莉"`（仅背景，不触发提炼） |

**规则约定（重要）**：
- 只有填 `reference.dialogue_samples`（对话样本，约 30 句）会**触发首次启动 AI 提炼画像**。
- 只填 `reference.source_game`（参考来源）**仅作背景信息，不触发提炼**。
- 提炼结果由 `reference.summary` 充当"已完成"标记，避免重复提炼。

---

## 12. 规划与待办

> 本节记录已确认但**暂缓实施**的功能设计，便于未来回查，避免重复讨论设计方向。

### T-001：玩家状态清单（暂缓）

**目标**：让 NPC 认知并记住"某个玩家"这个维度的持久状态（如玩家的武器、攻击等级、与 NPC 的关系），使 NPC 对不同玩家产生"熟人感"。

**设计要点（已与用户对齐）**：
- **输入来源**：玩家主动汇报类信息（"我攻击等级升到 60 了""我拿到了流云剑"）。
- **写入动作**：由 **A4 在整理（生成摘要）时识别**——判断信息属于"玩家状态"，打包成**一条 core+pinned 核心记忆**（整个玩家状态合成一条，不拆散）。
- **存储位置**：存放在该 NPC 的核心记忆里（语义上 = "NPC 记得玩家 X 的当前状态"）。
- **注入方式**：这条"玩家状态"核心记忆随 A3 固定前缀一起传给 NPC，让 NPC 知道玩家是谁、当前状态。
- **生成与写盘策略（重点）**：
  - **写盘 = 覆盖整块**：A4 一次性生成这块玩家状态记忆的**完整最新版**，整体覆盖写回（不是逐条 append）。
  - **生成思路 = 增量迭代**：提示词要求 A4 采用"增量迭代"思路——读取当前已有玩家状态 + 本次新增信息，在原有基础上更新/合并出这一块的完整内容。因此**新增或覆盖，由 A4 根据迭代结果裁决**（信息已存在则合并更新，纯新增则补充），但它输出的始终是"这块的整份最新版本"供程序覆盖写入。
  - 一句话：**模型用增量迭代的思路产出完整版，程序按完整版覆盖写入**。由 A4 说了算是新增会合并。

**与现系统关系**：
- 走现成的核心记忆读写通道，**不需要新架构**。
- 需新增：A4 的"玩家状态识别"逻辑 + "单条覆盖写入"（现 A4 是新增记忆，此条需 special-case 为整体覆盖）。

**来源**：参考逆水寒智能 NPC「运行时外置数据」中"身世/属性/好感度"设计（详见 §11.1）。

### T-002：AI 代填新 NPC 资料（list待办，已确认要做的正事）

**目标**：新 NPC 接入时不再需要人为逐项填 persona / 核心记忆 / 游戏数据库（当前手工填可莉那份很费劲），改为**由 AI 自动代填**。

**痛点**：人为填表太耗体力；录入一个新 NPC（人设、核心记忆、游戏库 7 分类条目）要手写大量 JSON。

**设计要点（待细化）**：
- **入口**：给出一个参考来源（如游戏名 + 角色名，或一文档/链接），AI 据此自动生成一份完整的 NPC 资料包（persona + 核心记忆 + `data/game_db/{身份}.json`）。
- **可复用**：与已有的 `reference` 首次启动画像提炼机制衔接；可先自动产出底稿 → 生成后由用户人工复核/微调再落库。
- **落库路径**：走现成身份创建流程（`POST /api/identities`），生成结构符合现有 memory / game_db 格式。
- **边界**：参考来源需是公开资料；无版权/私设内容不编造（沿用系统一贯约束）。

**状态**：已确认要做（正事），具体实现方案未定，暂缓实施，待后续细化。

### T-003：架构横向测评——给主流游戏填库跑分（待办）

**目标**：用"来硬的"方式验证本架构的真实水平——给一款现有知名游戏的 NPC 填入本系统的记忆库 + 游戏数据库，等实际游玩时与游戏官方 AI 板块"跑一下分"，横向对比本架构与官方方案的准确/开放表现。

**计划**：
- 选择目标游戏（已初步确定为《逆水寒》）。
- 为其某个 NPC 手工/半自动录入 persona + 核心记忆 + `data/game_db/{身份}.json`（可复用 T-002 代填能力）。
- 待实际游玩时，就同一个问题分别向本系统 NPC 和官方 AI 提问，对比：准确度（贴合设定/防编造）与开放度。

**状态**：待办，尚未开始。等待实际游玩时机或专项测试。

### T-004：游戏内教程功能（待办，方案已定）

**状态**：方案已完成并定稿，当前为**只写方案、未写码**。

**设计文档**：详见 **`docs/tutorial-plan.md`**（本条目仅作指引，方案内容不在此处重复展开）。

**核心决策（用户已拍板）**：
- 并入现有 A1→A2→A3 对话链，不建独立旁路。
- **仅 knowledge 模式**触发教程；chat 完全不碰，省 token。
- 教程数据写入游戏库「教程」分类，由 A2 自动识别教程向提问归入 knowledge。
- A3 在 knowledge 模式注入命中教程并"照搬原话"轻度加工；**知识模式不跑反事实校验**。
- 为缓存命中：A3 既有提示词原样置顶，教程提示词追加其后，不删任何提示词。

**状态**：待实施方案，未开始写码。

### T-005：向量语义检索（待办，用户已确认本机可跑向量模型）

**背景**：现有检索纯关键词（jieba 分词 + 子串匹配），玩家换措辞/同义/隐喻时关键词零命中 → 相关记忆捞不回来。已先落地**方案 A 兜底**（关键词召回 <5 条时补最近/核心记忆候选池，交 LLM 语义重排，见 `agent/a1_search.py` 的 `_merge_fallback_pool`）；向量检索是**方案 B**，在其之上精进。

**原理备忘（用"衣服尺码向量"类比）**：专门的**嵌入模型**把每句话翻译成一个高维坐标（维度是模型从海量语料自学的"意思暗号"，无名字）；检索时把玩家消息也转坐标，算两点距离，最近的即语义最相关。距离计算纯数学、本地瞬时完成；**唯一额外依赖是嵌入模型本身**（chat 对话模型当不了这个翻译机）。

**计划**：
- 本地嵌入模型选型：优先 `bge-small-zh`（中文、轻量、CPU 可跑），备选其他本地 embedding。
- 入库端：A4 建记忆时同步生成向量并存档（旧记忆批量补算）。
- 检索端：A1 改为**关键词 + 向量混合召回**，向量相似度并入现有融合打分。
- **降级策略**（参考 wanyimem）：向量模型加载失败/超时 → 自动退回纯关键词 + 方案 A 兜底，不阻塞对话。

**状态**：暂缓（2026-09-11 用户决定）。原因：安卓端手机性能跑不动本地向量模型，而检索主力场景将转移到手机端；PC 端可在远期再启。方案 A 兜底（`_merge_fallback_pool` + LLM 语义重排）已上线，当前语义召回暂由它承担。

### T-006：安卓版 App（实施中）

**目标**：把本系统后端打进安卓 APK（折中 C 路线：Chaquopy 嵌入 Python，原生壳仅「配置页 + 服务开关页」，浏览器访问 localhost:5003 复用现有网页 UI）。App 名 AI-NPC-Forgemind，自用开发者模式安装，无需 WSL2。

**设计文档**：详见 **`docs/android-app-plan.md`**（本条目仅作指引，不重复展开）。

**核心决策（用户已拍板）**：
- Chaquopy 路线（Windows + Android Studio 原生打包）；minSdk 26 / target 34（ColorOS 14）。
- 手机零内置角色，角色以 zip 角色包发放；PC 填表建角色 → 导出 zip → 手机导入；导入入口放网页 UI。
- API 配置沿用 config.txt 老格式，配置页读写。
- 前台服务保活（dataSync 类型 + POST_NOTIFICATIONS），附 ColorOS 保活指引。
- 图标由文生图生成 + 多尺寸切图（实现期解决）。

**状态**：实施中。P1 角色包双端已完成（`tools/rolepack.py` + app.py 端点 + 网页导入导出按钮，回环测试通过，2026-09-11）；P2 壳工程待开始（`android/` 目录已建）。后续按设计文档 §7 阶段推进。