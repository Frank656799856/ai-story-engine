# 教程功能设计方案（并入现有对话链）

> 状态：**只写方案，未实现**（待评审后再动手写码）
> 目标：让 NPC 除了闲聊，还能基于 `游戏数据库` 输出**精准、可查证的教程**（如"如何炸鱼""怎么合成""某个材料去哪找"），同时把幻觉风险降到最低。
> 关键决策（用户已拍板，**此处以本清单为最终定论，不再并入 architecture.md**）：
> 1. **接入方式**：并入现有 A1→A2→A3 对话链，不另建独立旁路。
> 2. **触发方式（仅知识模式触发）**：教程只在 `knowledge` 模式时注入与触发；`chat` 模式**完全不碰教程**，避免 token 激增。
> 3. **A2 识别**：游戏环境（身份接入了游戏库）+ 教程向提问 → 归入 `knowledge` 模式；A2 命中教程后把命中结果透传 A3。
> 4. **A3 加工方式**：A3 在 `knowledge` 模式检索/使用命中教程，**尽量引用原话**，只做轻度人设化加工。
> 5. **知识模式不加反事实校验**：教程回答走 knowledge 时不跑 `_verify_and_revise`，经评估模型幻觉不严重。
> 6. **知识模式用强提示词**：A3 在 knowledge 模式注入强校验提示词（照搬教程原话、不改步骤、不编库外细节）。
> 7. **缓存命中优先**：A3 把之前（既有的）那些提示词保持在**最前面**，追加的强教程提示词排在其后，**不删除任何既有提示词**，维持稳定前缀以命中厂商前缀缓存。
> 8. **教程分支动态降温度**：平时用配置文件里的 `TEMPERATURE`；当命中教程、走 knowledge 教程分支时，把 A3 生成温度**降一档（0.5 → 0.2）**，抑制自由发挥、提高对教程原话的遵循度。闲聊（chat）不受影响，仍用配置默认。

---

## 1. 现状梳理

### 1.1 A2 现有分类逻辑（`agent/a2_deep_think.py`）
- `_classify_task()` 把所有输入分成两类：`chat`（闲聊/角色扮演/讨论虚构世界观）和 `knowledge`（事实性知识/概念解释）。
- 分类走 LLM（`A2_CLASSIFY_SYSTEM`），失败降级为规则（`_rule_classify`）。
- 当前 `knowledge` 触发偏向"通用知识"（公式/历史/科学/翻译），**并不感知游戏库**，也不会把"游戏内操作教程"当作 knowledge。

### 1.2 A3 现有游戏库注入（`agent/a3_output.py`）
- `_build_game_db_section()` **全量注入**游戏库所有分类条目（`search_by_categories(CATEGORIES, max_per_cat=1000)`），作为稳定前缀进 system prompt，目的是命中厂商前缀缓存。
- 生成后会跑**反事实校验**（`_verify_and_revise`）：LLM 抽词 → Python 回数据库匹配 → 匹配不到就建议式重生成。
- 对填了 `reference.source_game` 的"参考游戏角色"走**放宽模式**，跳过词级校验。

### 1.3 游戏库结构与检索（`tools/game_db.py`）
- 数据库 JSON：`data/game_db/{identity}.json`，顶层 `categories` 内按分类存条目，每条 `{name, description}`。
- 固定分类：`食物/道具/怪物/友好生物/地形/地点/世界观设定`（`CATEGORIES`，**[game_db.py L27](e:/APP/python_AI/pro_AI2/tools/game_db.py#L27)**）。
- 已有：`detect_entities()`（A1 打标签）、`search_by_categories()`、`format_entries()`。

---

## 2. 设计目标

1. **教程準确**：教程类回答尽量"照着库里的话说"，避免大模型自由发挥编错步骤。
2. **幻觉可控（低成本路线）**：教程条目是**手写**的确定性内容；经评估教程场景模型幻觉不严重，**知识模式不再额外跑反事实校验**，改用强提示词引导"照搬原话"。
3. **省 token 优先**：教程**只在 `knowledge` 模式**注入与触发；`chat` 模式完全不涉及教程，避免每轮对话 token 激增。
4. **不破坏现有链路，且保缓存命中**：聊天/角色扮演照旧；教程仅在"知识模式 + 教程向提问"增强；A3 的既有提示词**原样保留并置顶**，新教程提示词追加其后，维持稳定前缀以命中厂商前缀缓存。

---

## 3. 方案设计

### 3.1 游戏库新增「教程」分类

在 `CATEGORIES` 末尾追加 `"教程"`（不动现有分类，[game_db.py L27](e:/APP/python_AI/pro_AI2/tools/game_db.py#L27) 同步更新）。

录入示例（`data/game_db/可莉.json`）：

```json
"教程": [
  {
    "name": "炸鱼教程",
    "description": "① 选没人的水域；② 站远一点；③ 丢出蹦蹦炸弹；④ 观察湖面带焦痕后再靠近捞鱼。琴团长说：先征得同意再炸。",
    "keywords": ["炸鱼", "怎么炸鱼", "鱼怎么弄", "打渔"]
  },
  {
    "name": "蹦蹦炸弹使用教程",
    "description": "拔保险→读秒→扔出→后退三步→捂耳朵。禁止在蒙德城内/室内/禁闭室附近使用。",
    "keywords": ["炸弹", "蹦蹦", "怎么用炸弹", "引爆"]
  }
]
```

设计说明：
- 每条教程 `description` 用**步骤化、口语化、可直接复述**的句式写，方便 A3"引用原话"。
- 每条可带 `keywords`（触发用），可选；不写 keywords 也能被 detect/search 检索到。
- 教程与"世界观设定""道具"的分类边界：**教步骤类→教程**；**实体属性类→道具**。如"蹦蹦炸弹是什么"属于道具，"怎么用蹦蹦炸弹"属于教程（同一物品可两条并存，靠提问侧自动分流）。

### 3.2 A2：识别"教程向提问"并归入 knowledge

**改动点：[a2_deep_think.py](e:/APP/python_AI/pro_AI2/agent/a2_deep_think.py) `_classify_task()` 的 LLM 提示与规则降级**

思路：现在的 LLM 分类只问"chat 还是 knowledge"。方案改为在判断前注入"游戏库教程命中"信号：

1. **新增快速规则（低成本，先于 LLM）**：
   - 若该身份**接入了游戏库**（`get_game_db(identity).available`）且消息长度正常，先用 `detect_entities()` / 新增的教程关键词匹配，把命中教程的玩家提问直接判为 `knowledge`。
   - 关键词匹配：遍历游戏库「教程」分类，检查玩家消息是否包含某条教程的 `keywords` 或 `name` 子串。命中→`knowledge`，并把命中教程名随 A2 结果传出。
2. **LLM 分类提示升级**：把 `A2_CLASSIFY_SYSTEM` 补充一句："若玩家询问**游戏内可玩系统/操作/材料获取**等教程向问题，视为 knowledge"。（沿用现有 chat/knowledge 二分类，不新增第三模式，保证 A3 侧不用大改。）
3. **规则降级同步**：`_rule_classify()` 增加教程命中分支（游戏库 + 关键词命中 → knowledge）。

输出：在 `a2_result` 增加字段 `tutorial_hits: [...]`（命中的教程条目），透传给 A3。

> 说明：判断标准仍是"教程向提问"。角色扮演问"你觉得这鱼好吃吗"仍是 chat，不会被误判成教程。

### 3.3 A3：knowledge 模式下注入命中教程 + 轻度加工（引用原话）

**改动点：[a3_output.py](e:/APP/python_AI/pro_AI2/agent/a3_output.py)**

- **仅 `mode == "knowledge"` 时走教程段**；`chat` 模式直接跳过，不检索、不注入教程（省 token）。
- 在 `_build_system_prompt()` 的**动态区**（记忆之后、既有提示词保持最前不变）追加一段"**玩家可能想要的教程**"：
  - 内容来自 `a2_result.tutorial_hits`（A2 命中的那几条），加上 A3 `search_by_categories(["教程"], ...)` 做兜底检索（防 A2 漏判）。
  - 用 `PROMPTS.A3_TUTORIAL_SECTION` 模板注入：`"玩家可能在问教程：\n[教程] 名称: 步骤全文..."`。
- **强加工提示词**（新增 `A3_TUTORIAL_STYLE`），**追加在既有提示词之后、不删既有提示词**：
  > 回答教程类问题时，**优先照搬上面给出的教程步骤原话**，你可以加一点自己作为角色的语气词/俏皮话当开头结尾，但**不要篡改步骤顺序、不要补充库外细节**；若库里没有覆盖到，就坦诚说"可莉也不会/没试过"，不要编造操作。
- **不进入反事实校验**：knowledge 教程回答**不再额外跑 `_verify_and_revise`**（经评估教程场景模型幻觉不严重，走强提示词引导即可）。
- **动态降温度**：A3 主生成温度现为 `0.8 if chat else 0.5`（**[a3_output.py L62](e:/APP/python_AI/pro_AI2/agent/a3_output.py#L62)**）。当命中教程、走 knowledge 教程分支时，把温度从 `0.5` 降到 `0.2`，让模型更忠实于教程原话；chat 及非教程的 knowledge 仍沿用配置默认/原有取值。

为什么不用单独的旁路/开关：因为你选了"并入对话链"，好处是触发、语气、记忆全部复用；教程只是在 knowledge 模式下给 A3 多喂几条"可查证资料并指示照搬"。

---

## 4. 幻觉控制点（重点回应你的顾虑）

| 风险 | 对策 | 落点 |
|---|---|---|
| A3 自由发挥编错步骤 | 强提示词"优先照搬教程原话、不补库外细节" | A3_TUTORIAL_STYLE |
| 教程幻觉严重度 | 经评估不算严重 → **知识模式不跑反事实校验**，靠强提示词兜底，省一次 LLM 调用 | a3_output.py（knowledge 分支跳过 `_verify_and_revise`） |
| A2 误判（把闲聊当教程） | 教程先经关键词命中再看 LLM；角色扮演/主观评价不算；`chat` 模式完全不碰教程 | a2_deep_think.py |
| 教程库本身遗漏/过时 | 教程为手写慢热数据；可后续做"AI 半自动填教程"（呼应你既有的"支持 AI 对新 NPC 填表"规划） | 后续迭代 |
| 教程全量注入撑爆上下文 / token 激增 | 教程**只在 knowledge 模式**按命中/检索注入，chat 不注入；避免 token 膨胀 | a3_output.py |

---

## 5. 落地改动清单（评审通过后才写）

1. `tools/game_db.py`：`CATEGORIES` 增 `"教程"`；可选加 `match_tutorial(text)`（用 keywords+name 做子串匹配，返回命中条）。
2. `agent/a2_deep_think.py`：`_classify_task` 规则提前命中教程→knowledge；透传 `tutorial_hits`。
3. `agent/prompts.py`：
   - `A2_CLASSIFY_SYSTEM` 补充教程向判定；
   - 新增 `A3_TUTORIAL_SECTION`（模板）、`A3_TUTORIAL_STYLE`（强加工提示词）；**既有提示词不删、保持置顶**。
4. `agent/a3_output.py`：**仅 knowledge 模式**动态区注入命中教程 + 引用原话强提示词（追加在既有提示词后）；**knowledge 教程分支不进 `_verify_and_revise`**；命中教程分支温度 `0.5 → 0.2`。
5. `agent/pipeline.py`：把 `a2_result.tutorial_hits` 传进 `a3.generate()`（若 A2→A3 之间未透传该字段）。
6. 数据：为现有身份（如可莉）补几条示例教程，验证全链路。

---

## 6. 验证方式

- 用 curl 或前端发"怎么炸鱼"，确认 A2 归到 knowledge、A3 在 knowledge 下回复基本引用教程原话，且**知识模式未触发反事实校验**（少一次 LLM 调用）。
- 发"你觉得鱼好吃吗"确认仍走 chat，完全不涉及教程、token 不膨胀。
- 发一个库里没有的教程问题，确认 NPC 坦诚"不知道/没试过"，不编步骤。
- 观察提示词顺序：既有提示词仍在最前，教程提示词追加其后，便于校验缓存命中未被破坏。

---

## 7. 归档与待办（已定论，无需再确认）

- 本文档 **`docs/tutorial-plan.md`** 作为**独立设计文档**归档，**不并入 architecture.md**。
- **architecture.md 待办区新增 T-004，仅一条引用**指向本文档，不在 architecture.md 内重复展开方案内容。