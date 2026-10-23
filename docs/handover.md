# 项目接手文档（Handover）

> 最后更新：2026-09-24
> 用途：新会话 / 新开发者接手本项目的**入口文档**。
> 阅读顺序：先读本文档 → 需要深入时看 `architecture.md`（详细架构书，核对时间 2026-08-29）和 `feature_plan.md`（早期规划 v5）。

---

## 1. 项目一句话

**AI NPC 记忆系统**：让游戏 NPC 拥有长期记忆、不崩人设的本地聊天系统。
- 双端：PC（Python + Flask，端口 5003）+ 安卓（同一套 Python 代码经 Chaquopy 打包进 APK）
- 单玩家个人使用，数据全部本地 JSON，无外部数据库
- LLM 调用经 `agent/ai_client.py` 统一封装（多厂商探测）

## 2. 核心架构速览

```
玩家消息 ──▶ A1 检索(关键词+游戏库) ──▶ A2 深度思考(筛选+分类) ──▶ A3 输出(人设+记忆+模式) ──▶ 回复
                                              │                            │
                                              └── a2_result(dict)          └── a3_result["snapshot"] ──▶ A4 记忆管理(摘要/事件/归档)
```

- **数据流关键约定**：`a2_result` 整体透传给 A3（pipeline.py 不需要改就能传新字段）；`snapshot` 透传给 A4。新增跨 Agent 字段就往这两个 dict 里塞。
- **三档相关性**：A2 输出 `relevance`（world_related / chitchat / irrelevant），A3 按档注入引导词，A4 对 irrelevant 跳过摘要 + 重要性 ×0.3。
- **三层记忆**：core（核心，含玩家状态整块记忆）/ episodic（情节，事件驱动）/ working（工作层）。
- **教程模式**：A2 分类为 knowledge 且检索命中教程 → A3 走教程分支（低温度忠实照搬，跳过反事实校验），且不注入三档分流引导。

## 3. 双端文件对照表（改任何 agent/memory/tools 代码必须双端同步！）

| 职责 | PC | 安卓 |
|------|----|----|
| Agent 流水线 | `agent/` | `android/app/src/main/python/agent/` |
| 记忆检索/存储 | `memory/` | `android/app/src/main/python/memory/` |
| 工具（角色包/游戏库/时间等） | `tools/` | `android/app/src/main/python/tools/` |
| Flask 入口 | `app.py` | `android/app/src/main/python/app.py` |
| 前端 | `static/index.html` | `android/app/src/main/assets/static/index.html` |
| 提示词 | `agent/prompts.py` | 同构副本 |

**铁律：**
- 项目**没有 git 仓库**，双端同步只能逐文件精确 Edit，**禁止整文件盲覆盖**（两端文件常有行号漂移）。
- `android/app/build/` 下的 python 源码是构建产物，**不要手改**，打包时自动重新生成。
- 每次改完跑冒烟：临时测试脚本放系统 Temp 目录（用完删），用假 client/store/index 打桩，不触网。

## 4. 数据存储布局

- PC：`data/memory/{身份}/`（记忆 JSON）+ `data/game_db/{身份}.json`（游戏库，手动建档，存在才启用）
- 安卓：`{context.filesDir}/data/...`（应用沙箱 `/data/user/0/com.forgemind.npc/files`，系统级隔离）
- 每身份目录：`history.json`（对话历史，页面打开全量加载）、记忆文件、`persona` 存在身份注册表（identities）
- 角色包导入：POST `/api/rolepack/import`（multipart 字段 file）→ 数据落 `{NPC_DATA_BASE}/data/memory/{身份}/`

## 5. 已完成功能清单

| 模块 | 状态 | 说明 |
|------|------|------|
| 四 Agent 流水线 | ✅ 稳定 | A1 检索 → A2 思考分类 → A3 输出 → A4 记忆管理 |
| 三层记忆 + 升降级 | ✅ 稳定 | 事件检测（EVENT_SIGNALS）触发 working→episodic 升级 |
| 玩家状态核心记忆 | ✅ 稳定 | T-001：LLM 增量迭代产出整块状态，程序覆盖写回 core+pinned |
| 多身份 + 角色包 | ✅ 稳定 | 身份隔离、zip 导入导出、安卓「＋导入」按钮（file input 在 body 级） |
| 游戏数据库 + 反事实校验 | ✅ 稳定 | A3 生成后抽词核对，反库事实触发重生成；普通物品豁免 |
| 教程系统 | ✅ 稳定 | T-004：教程命中走忠实照搬分支，优先级高于分流引导 |
| 三档相关性分流 | ✅ 稳定 | 2026-09-19 完成，双端同步，冒烟 32 项通过 |
| language 语言字段 | ✅ 稳定 | 2026-09-24 完成：档案 `language` 字段定语种，缺省一律中文，兼容旧档案/安卓已装数据 |
| 时间系统 | ✅ 稳定 | 时间降权（"时间参考"措辞）+ A4 摘要四档笼统时段（`_coarse_period`），`time_period` 字段保留 8 档供程序检索 |
| 复读机 bug 修复 | ✅ 完成 | 三重修复：PC 装 jieba 0.42.1 + A3 风格铁律第 6 条（严禁复读旧话）+ 历史数据手术 |
| 流水线状态气泡 | ✅ 完成 | pipeline 各阶段打点 + GET `/api/stage` + 前端气泡轮询（PC + 移动端） |

## 6. 已知的坑（接手必读）

1. **PC 必须 jieba 0.42.1**：缺了会 fallback 到 2 字滑窗分词 → 检索关键词碎片化 → 复读机类 bug。pip 安装可能被沙盒拦（需重试）。
2. **存量记忆 keywords 仍是碎词**（9/12 之前存的），检索精度受影响，等 T-005 重建。
3. **旧记忆 `time_period` 含"凌晨/深夜"**：新四档规则只影响新记忆，旧数据不清洗。
4. **irrelevant 消息仍会走一次玩家状态提取 LLM 调用**：提取器对无关内容返回"无"，属正常兜底，不算 bug。
5. **安卓 WebView 的 file input 必须放 body 级**：藏在 display:none 容器里弹不出选择器。
6. **A2 分类 LLM 挂了会降级 `_rule_classify`**：强现实任务词表（代码/翻译/计算等）→ irrelevant，这是故意的兜底。
7. **提示词缓存**：A3 system prompt 分"固定前缀区"（身份/风格/语言规则/游戏库）和"动态区"（记忆/时间），改提示词时注意别把易变内容挪进前缀区。
8. **`agent/prompts.py` 导出方式**：`from agent import prompts as PROMPTS`（模块别名），不是 `from agent.prompts import PROMPTS`。

## 7. 待办任务池

| 编号 | 任务 | 备注 |
|------|------|------|
| T-005 | 向量语义检索 + 存量记忆 keywords 碎词重建 | 检索精度的根治方案 |
| T-002 | AI 代填 NPC 档案（填表逻辑） | 方向已定："关联链填表法"——三层漏斗（核心自身→关联人物→关联物/地/怪）；性格写行为化 300-500 字；对话样本 30 句覆盖 5 情境；game_db 15-25 条带关系。**story 侧已实施（9.5，2026-10-16）**：档案补全轮逐 NPC 产出行为化画像+情境示范台词（主 NPC 15 句/次要 8 句）；原系统侧的手填工作流仍待做 |
| T-003 | 逆水寒横向跑分 | 对标测试 |
| T-011 | BUILDER 加物品获取时序约束 | **已实施（2026-10-16）**：BUILDER 规则加「物品时序铁律」+ CRITIC 审查清单第 6 条加物品时序专项。案例：story_20261007 副本 q2 要求出示"军队徽章"但徽章是 q3 的 reward_items；q3 任务文本已补丁改写（出示徽章问哥哥下落），审稿 AI 当时已警告 |
| T-012 | 审稿警告未清零仍出厂的流程漏洞 | **已实施（2026-10-16）**：builder.py 审稿裁决改 `score >= 7 and not issues`——审稿发现任何问题（哪怕分数够高）至少带 issues 修订一轮，仍受 2 轮上限约束不死循环 |
| T-006 | 模组化架构（演出层舞台化 + 玩法模组挂载） | 远期，剧情系统跑稳后启动；方向已讨论定稿，**未开工**，见 §8.19 |
| **新任务** | **剧情系统**（用户已立项，处于计划阶段，**未开工**） | 见下节 |

## 8. 剧情系统计划（下一步大任务）

**定位**：给现有系统加一个"导演"，NPC 从"被动演员"升级为"活在世界里的人"。
- 现状：NPC 你问才答；剧情系统让"世界自己会动"——时机到了发生事件，NPC 演出来，玩家应对影响走向。

**与现有系统的对接点（全部现成，无需推翻）：**
- 素材库：游戏数据库（人/物/地/怪 + 关系）
- 扳机：A4 的 EVENT_SIGNALS 事件检测（可扩展为剧情触发信号）
- 进度记录：记忆系统（剧情进展写入 episodic）
- 演出：A3 注入"剧情卡"（时间/地点/人物/事件/可选应对）
- 分类：A2 可扩展 story 模式或复用 knowledge 分支

**待定的两个方向性决策（用户尚未选择）：**
1. 素材来源：推荐**混合模式**——库内取材保证不崩设定 + 允许 AI 新编小事件、经反事实校验后回写数据库（越玩库越厚）。备选：纯数据库驱动（最稳但会重复）/ AI 自由现编（最天马行空但可能矛盾）。
2. 触发方式：推荐**双向**——NPC 检测到时机主动开口 + 玩家"然后呢"推动。备选：纯玩家推（省 token）/ 纯 NPC 主动（意外感最强但 token 消耗高）。

**建议分期：**
- **Phase 1（最小可用）**：剧情发生器——事件/间隔触发，生成一张剧情卡注入 A3，NPC 主动开口推进；剧情进度落 `data/memory/{身份}/story.json`
- **Phase 2**：剧情状态机——多步剧情链、玩家选择分支、完成/失败判定
- **Phase 3**：长线剧情 + 世界演化（NPC 关系变化、传闻系统、跨事件影响）

**开工前约定**：先在本文档/专项计划里补齐技术方案（提示词常量进 prompts.py 并注明用途、数据结构、双端同步清单），再动代码。

### 8.1 方向修正（2026-09-25，用户已定）

- **产品形态**：不做视觉小说/Galgame 皮，做**互动游戏**——AI 完全主导剧情生成，玩家输入/选择实时影响走向。
- **范围收窄**：先不做大，首个目标 = **单次流程约 20 分钟的短故事**。
- **明确砍掉**：生成图片、TTS。只保留骨架（剧情生成 + 状态推进 + 玩家交互）。
- **参考仓库**：`jie2807/ai-galgame-demo`（GitHub，AI 视觉小说比赛作品），已下载到 `reference/ai-galgame-demo/`（约 45MB，含 .trae 规格文档、data 数据层、2.2MB 单体 index.html、素材）。
- **许可证结论：无 LICENSE 文件**（GitHub API `license: null`；package.json 声明 ISC 但不构成仓库许可）→ 默认版权保留，**不能直接复制代码片段**，只能提炼思想自行实现。可借鉴的思想：剧本 Schema V2（stateVars 数值状态 + timeline/threshold/random/ending 四类事件 + "变量±数值" impact 表达式解析）、"剧本主控文档"四段式 player 描述（人设→AI叙事指令→状态跟踪→事件系统→语言风格）、AI 三档生成完整 JSON 剧本（含防截断优先级与 JSON 骨架）、lorebook 关键词命中注入、情绪关键词→表情映射、AIServiceHub（请求队列/优先级/LRU 缓存/指数退避重试）。
- **反面教训**：该仓库把全部逻辑塞进 2.2MB 单体 index.html，导致反复"瘦身-回归"死循环；file:// 协议 ES module 失效需做 stub 桥接。本项目剧情系统应模块化，并注意安卓 WebView 的协议限制。

### 8.2 参考仓库架构核验结论（2026-09-25，代码级）

两轮讨论后逐文件核验的结果，供后续直接引用。文件引用相对 `reference/ai-galgame-demo/`。

**整体形态**：纯前端单页应用，**无后端**。`index.html`（2.2MB）内联全部运行时逻辑，`data/*.js` 为静态数据层，LLM 走用户自配的 OpenAI 兼容 API。核心运行时对象：`SimpleChatEngine`（index.html:12308，对话引擎）、`EventEngine`（index.html:14497，事件引擎）、`Live2DRenderer`、`BGMEngine`、`PageManager`（index.html:40680）；`AIServiceHub`（index.html:36320）只服务于"舆论锻造所"CWE 模式，与主玩法无关。

**关键结论：剧本文件里躺着两套东西，运行时只有一套生效。**

- **散文套（真正生效）**：`game-chapters.js` 的 `playerDesc` 是一大段结构化散文，含【AI叙事指令】【AI需跟踪状态】（10 个变量名）【事件系统】（D-28→D-0 共 11 个时间线事件、8 个阈值事件、8 个随机事件、4 种终局条件，全用文字写了一遍）【语言风格】。它整体进提示词，AI 照着办。另见 index.html:12755-12764 的【末世方舟叙事深度融合指令】第 3 条，明确要求 AI「每回合/阶段必须根据剩余天数、关键状态变量以及本路线事件表触发相应事件」。
- **结构化套（未生效）**：`stateVars`（带 key/label/initialValue/min/max）、`timelineEvents`、`thresholdEvents`、`randomEvents`、`endingEvents`（选项带 `impact` 表达式）。**全文检索确认无任何代码读取其数值**，也不把选项渲染成按钮；只用于剧本编辑器的展示/校验（index.html:23374-23414）与"AI 生成剧本"的输出格式。

**引擎真正持有数值的只有三样**：硬编码的 `playerAttributes`（health/fatigue/mood/hunger，index.html:15160，带 UI 面板）、每 NPC 好感度（信任/好感/爱慕三维）、物品栏。

**事件触发有两条互不相通的路径**：

1. **内置三章**：数据格式为 `trigger: {daysRemaining: 28}` + `choices[].impact`，但引擎不读 → 实际靠 `playerDesc` 散文让 AI 自行判断是否触发。
2. **自定义事件**：`EventEngine` 能查的条件仅 8 种——affection / location / time_period / message_count / keyword / player_status / has_item / attribute（index.html:14509-14563）。命中后每轮最多触发 2 个事件，把事件描述塞进 `_customInjection`，下一轮提示词多出一段【世界事件提示】（index.html:12711-12715）。事件源由 `getCustomEvents()`（index.html:22194）从自定义剧本编辑器的 DOM 读取，**内置章节的事件不经过这条路**。

两条路径的字段名完全不同（前者 `trigger/eventName/description/choices/impact`，后者 `conditions/actions/name/once/cooldown/priority`），说明是不同时期写的，未打通。

**可直接复用的规范**：

- `impact` 表达式语法：`"foodDays-2, morale+5, unity-10"` —— 变量名 + 正负数字，逗号分隔；支持中文条目（`医疗库存+大量`）、分支开关（`开启逃亡分支`）、非数值后缀标记（`population-1(风险)`、`morale+5(安全)`）。
- `(风险)` / `(安全)` 标记配合提示词里的【不确定性规则】（index.html:12700-12704），把"该选项是否真的成功"交给 AI 判定，而非写死。

**核心教训**：该仓库把 schema 设计得很完整，但只实现了一半——只做了"把规则讲给 AI 听"，没做"引擎持有并推进数值"。这个缺口正是本项目要补的位置。

**介绍页定位核验（2026-09-25，forum.trae.cn/t/topic/75789）**：作者为大一学生参赛作品，自称"几乎是我和 TRAE 一起聊出来的"。宣传点全部在体验层（Live2D/TTS/BGM/PWA/剧本编辑器），**无多 Agent、无记忆、无状态机宣称——它确实也没有**。`AIServiceHub` 名字像中枢，实为 API 请求队列（并发/优先级/缓存/重试），与 Agent 协作无关。工程定位 = 参赛 Demo，体验层亮眼、架构层无深度；可借鉴的只有思想（impact 表达式、事件四类、剧本主控文档），工程实现无内容可抄。

### 8.3 骨架路线（2026-09-25 已定：路线 B + 城市模型）

- **路线 A·轻提示词**：规则写进提示词，数值由 AI 自己在对话里维护。成本极低，但 AI 会数错天数、忘库存、前后矛盾。即参考仓库的实际做法。**（已否决）**
- **路线 B·真状态机**：引擎持有数值与标志，玩家得知信息/完成任务 → 引擎确定性改状态 → 下轮注入对应内容。即参考仓库设计但未实现的那半段。

**用户定调原话**："整个世界观已经提前生成好了，所有的秘密、伏笔、玩家不知道需要慢慢探索的事情都是提前已经在那儿了。好比已经规划了一个城市，你只是给玩家选一个游览路线……玩家知道某条信息了，或者完成某个任务了，就在状态机中改一下状态。"

→ 结论：生成与演出分离（详见 8.6）。

### 8.4 四个问题的速查答案（2026-09-25）

**① 有几个模块、流程怎么走**
运行时只有 5 块，全在 index.html 里：PageManager（页面切换，index.html:40680）、SimpleChatEngine（对话，:12308）、EventEngine（事件，:14497）、Live2D 渲染 + BGM（表现层，本次要砍）、data/*.js（静态数据）。
一轮流程：玩家输入 → 对话引擎拼提示词（章节 + NPC 人设 + 命中的世界书条目 + 硬编码玩家属性 index.html:12685 + 事件提示）→ 调 LLM 流式返回 → ActionTagParser 解析动作标签 → 情绪检测 → 写入对话历史 → 下一轮 EventEngine 查条件。时间由 advanceTime()（index.html:14769）每轮随机推进 30–90 分钟。

**② 怎么生成世界**
两条路。官方三章的 worldSettings（background / loreEntries / locations / worldContext）是作者手写在 `data/game-chapters.js` 里；自定义章节走剧本编辑器表单，或让 AI 生成——`data/custom-script-prompts.js` 三档提示词，输入一段故事描述即输出完整 JSON 剧本（世界背景、世界关键词、loreEntries、地点+天气权重、2-3 个状态变量、1-2 个 NPC）。世界书即 `{keys:[关键词], content:设定}`，玩家输入命中关键词就把 content 插进提示词。

**③ 怎么生成完整剧情**
**它并不生成剧情**，没有剧情引擎、没有进度记录。所谓"剧情"是章节作者手写在提示词里的一张日程表：30 天内 11 个时间点（D-28→D-0）+ 8 个阈值事件 + 8 个随机事件 + 4 种终局条件，以散文形式进提示词，让 AI「自己看着办」。实际剧情由 AI 每轮自由发挥。

**④ 怎么操控 NPC 回答**
同样全靠提示词组装。NPC 档案的 name/title/description/personality/firstMessage 直接拼进 system prompt；切换 NPC 即换这一块。立绘动作由 AI 输出 `[smile]` 等标签、经 ActionTagParser 正则提取后驱动（属表现层）。好感度（信任/好感/爱慕三维）是引擎真持有的，会影响话术。**没有 RAG、没有检索、没有记忆系统**——NPC 只"记得"对话历史。

**一句话总括**：它的「世界」和「剧情」都不在代码里，只是一份写得很细的提示词。

### 8.5 副本架构设计（2026-09-25，方向已定，未开工）

**与参考项目的根本差别**：参考项目是"一整个 30 天的世界"（作者手写的固定章节）；本项目是"一局一局的副本"（AI 现场生成、有限、会结束、可反复生成），更适合 20 分钟短流程。

**用户已确认两点：**
1. 副本 = **一次性关卡**（有目标和终局，玩完结束、不留档）
2. 多 NPC = **AI 现场生成**（不是预先写死）

**演进路线（用户原话）**："之后我打算做成那种你可以持续玩的那种短副本，但是现在先测试一次性的副本的可行性。就是 AI 现场生成，然后按照我的记忆系统项目规则给他们填表填数据库，然后再把那个项目进行改造一下。" → 先用一次性副本验证可行性，之后再扩为可续玩的短副本。

**副本最小数据结构（草案，待议）**

| 字段 | 内容 |
|------|------|
| `setting` | 世界观一段话 |
| `npcs` | AI 现场生成，每个带 `name` / `personality` / `goal`（新增目标层）/ `known_secrets`（信息差） |
| `milestones` | 2-3 个关键事件点 |
| `ending_condition` | 终局判定（什么条件下副本结束） |

落档：AI 生成的 NPC 按本项目记忆系统规则填表，写入 `data/game_db/{身份}.json`（沿用 T-002 关联链填表法）。

**缺口定义（用户原话："单 NPC 有了中枢，对每个 NPC 的指挥还有任务缺，因为现在 NPC 就是纯闲聊模式"）**

用户不缺记忆，缺的是**导演 + 每个 NPC 的目标**：
- **任务层 = 每个 NPC 的 `goal`**：参考项目 NPC 档案（`data/characters.js`）只有 name/title/gender/description/personality/firstMessage/portrait/live2dModel，**没有 goal 字段** → 那边的 NPC 只能当待命工具人、只会闲聊。本项目需补 `goal`。
- **指挥层 = NPC 之上的导演**：现有中枢只服务"单个 NPC 的一轮对话"；往上长一层即导演——掌握全部 NPC 状态、决定谁主动开口、把里程碑推进指令注入对应 NPC 的 A3 提示词。

**导演层分工（草案）**

| 角色 | 职责 |
|------|------|
| 中枢（现有 A1-A4） | 单 NPC 一轮对话：检索 → 思考 → 输出 → 记忆 |
| 导演（新增） | 副本进度、里程碑判定、决定谁开口、下发本轮 NPC 任务 |
| NPC 目标层 | 每个 NPC 自己的 `goal` + `known_secrets`，由导演注入 A3 |

**NPC 间信息传递拓扑（2026-09-25 定）**：**星型，全部经导演**。导演→NPC：任务/揭示注入（每轮批量一次，进 A3 提示词）；NPC→导演：状态提议（`[STATE:]` 标签）+ 记忆沉淀（A4）；**NPC 之间不直连**——传话必须过导演，防泄密 + 省 token + 可控。注：参考项目无多 Agent 架构可借鉴（见 8.2 介绍页核验），本项目 A1-A4 流水线（a2_result/snapshot 透传约定）本身就是现成的多 Agent 传递骨架。

**有记忆的 NPC 相对参考项目的三条先进性**（备查）：
1. 跨会话记得住事（episodic memory）——那边刷新页面就全忘；
2. 不会自相矛盾（反事实校验）——那边 NPC 过十几轮开始漂；
3. 有知识库可做检索（game_db + A1）——那边没有 RAG，只"记得"对话历史。

一句话："没记忆的 NPC 每次都在演第一集，有记忆的 NPC 才在过日子。"

**开工前约定**：先补齐技术方案（导演层接口、副本 JSON schema、提示词常量入 prompts.py 并注明用途、双端同步清单），再动代码。

### 8.6 城市模型定稿（2026-09-25，用户已定，未开工）

核心原则：**AI 生成与 AI 演出分离**——玩家的描述只发生在开工前；游玩中的 AI 不发明任何新设定，只揭示已埋好的秘密/伏笔。秘密与伏笔在造城阶段全部预置，玩家探索的是"既定真相"，而非 AI 现编。

**两个 AI 各干各的：**

| | 造城 AI（开工前，只跑一次） | 演出 AI（游玩中，每轮） |
|---|---|---|
| 输入 | 玩家一段简短/稍复杂描述 | 玩家本轮发言 + 当前状态 + 本 NPC 档案/goal/known_secrets |
| 输出 | 完整副本 JSON（8.5 结构 + 全部秘密/伏笔/任务预置） | 只做对话演出，可按揭示条件放出已埋内容 |
| 自由度 | 高，随便发挥 | 低，禁止引入新设定（game_db 反事实校验兜底） |

**状态机最小设计（路线 B 落地）：**

- **状态** = 一组布尔标志 + 少量计数：如 `knows_secret_1`（玩家得知秘密1）、`quest_1_state`（未接/进行中/完成）、伏笔揭示开关
- **变更** = 确定性代码：导演层判定条件成立才改标志。AI 只能"提议"（如输出 `[STATE: quest_1=done]`），导演确认后生效——不靠 AI 自觉
- **判定来源**：玩家说出关键信息（keyword / 语义判定）或完成任务；这也是本设计唯一的判定难点（"玩家真的知道了"怎么算数），后续方案阶段细化
- **改完状态** → 导演决定谁开口、把任务/揭示注入对应 NPC 的 A3 提示词 → 下轮演出

**与参考项目 EventEngine 的对照（8.2/前面已核验）**：借它的 conditions/actions 骨架（每轮扫描、once/cooldown、custom 注入通道），补上它缺的两样——**真状态存储**、**milestones 先后依赖**（它的事件是平铺规则，无先后链）。

### 8.7 游戏数据库演进：内存模型（2026-09-25，用户提出，未开工）

**用户原话要点**：不能把硬盘（数据库）里所有数据全部读进对话窗口（内存）——①会泄密：NPC 知道得太多，玩家一问全抖出来 ②注意力权重撑不住 ③浪费 token。检索时"中枢判断 + 分词检索 + 常规检索"都要进行。

**本质 = 操作系统虚拟内存的按需调页**：game_db 是硬盘，对话窗口是内存，检索器是 MMU；**页表 = 每个 NPC 的知识边界（known_secrets/goal）+ 玩家状态标志（knows_secret_X）**，未过权限的条目物理上不进上下文。

**三道门（按顺序过）：**
1. **相关性门（已有）**：A1 分词检索 + 游戏库关键词命中
2. **中枢判断门（已有）**：A2 深度思考筛选，本轮真正需要的才放行
3. **权限门（新增，副本必需）**：
   - NPC 知识边界：条目在不在本 NPC 的 known_secrets/goal 范围内
   - 玩家状态门控：该秘密的揭示条件（state flag）是否已满足
   - 跨 NPC 隔离：NPC A 不知道 NPC B 的私有信息，上下文必须按 NPC 分别组装

**安全原则**：**不进上下文 = 物理上不可能泄密**，远强于提示词里写"别主动说出 X"（后者玩家直接逼问就可能绕过）。

**与现状的差异（改造点，方案阶段核实）**：现有 A3 把游戏库内容放进 system prompt"固定前缀区"（见第 6 节坑 7）= 每轮全量在场。单 NPC 聊天模式够用；副本模式必须改为**每轮动态检索注入**（进动态区），固定前缀区只留身份/风格/语言。副本条目多（几十条），动态检索同时就是省 token 的手段。

**角色定位升级（用户补充，2026-09-25）**：内存模型不只是防泄密的安全机制，更是拟人化的核心——"NPC 也是人，剧情是一群人之间发生的故事，努力做的像人"。NPC 的"不知道"不是提示词装的，是物理事实（真相不在上下文里），因此：
- NPC 与玩家一起探索真相时，它的疑惑、猜测、惊讶都是**真的**——它只能基于自己知道的部分推理，不存在"演不知道"
- NPC 知道 + 玩家不知道 → 隐瞒/暗示/按揭示条件逐步放出
- NPC 不知道 + 玩家知道 → 戏剧反讽，自然成立
- **NPC 的页表本身随剧情变化**：milestone 推进后，导演往该 NPC 的页表加条目（NPC"得知"新真相），行为随之自然转变——不知情 → 起疑 → 得知 → 反应，全程物理保证，无需 AI 自觉

### 8.8 表现层设计（2026-09-25，用户提出，未开工）

**旁白 Agent（用户提出）**：负责输出必要信息，如"你和某 NPC 一起来到了酒馆"这类场景转换/环境描述。
- **实现定位：导演的"嘴"，不建完整 A1-A4 管线**——旁白不需要检索/记忆/反事实校验，就是一个轻量 LLM 调用（输入 = 导演层状态变化：场景切换/NPC 入场/事件触发；输出 = 一两句旁白）
- 只在事件发生时调用，**不是每轮都调**（省 token）
- 前端独立样式呈现（斜体/灰字），与 NPC 对话视觉区分

**玩家视野铁律（用户提出）**：玩家总不能盯着中枢里一堆状态及过程消息发呆——游玩界面只显示 **A3 输出 + 旁白**。中枢内部状态、a2_result、流水线打点（现有 /api/stage 气泡）全部归入开发者面板/调试模式。

### 8.9 剧情生成质量方案：不用微调（2026-09-25，用户疑问，方向已定）

**用户疑虑**：造城 AI 生成的故事不够抓人眼球；AI 到底知不知道网文写法？想到 LoRA 微调但成本高（需 GPU）。

**结论：不上微调。** 理由：
1. 写作吸引力主要来自**内容结构**（冲突、钩子、信息差），不是模型权重；LoRA 微调擅长改"风格/格式"，改不了"会不会讲故事"
2. 迭代成本：提示词改一行立刻重跑；微调一轮 = 收集数据集 + GPU + 训练 + 回归验证，试错慢一个量级
3. 数据集版权风险（网文有版权，爬来做训练集有法律问题）
4. 真要换文风，先试"换底层模型"（API 层面），比微调便宜得多

**AI 知不知道网文写法？知道**——训练语料里有海量网文（黄金三章、钩子、冲突前置等套路都在），只是默认不往那个方向写，需要明确的质量标准注入。且本项目最难的部分（长程结构）已由城市模型解决：预置的伏笔/秘密/信息差 = 结构骨架，AI 只负责局部表达。

**三层质量方案（造城阶段专用）：**
1. **方法论注入**：网文核心规则写成 prompts.py 常量（开篇钩子前置、每幕一个冲突、悬念收尾、Show don't tell 等）
2. **审稿循环（generate-critique-revise）**：造城 AI 出稿 → 审稿 Agent 按清单打分（钩子强度/冲突密度/信息差利用/结尾悬念）→ 不达标退回修订，最多 1-2 轮。多 Agent 架构的天然用法
3. **few-shot 范例**：2-3 段自写的高质量范例文本进提示词（自写保版权安全）

**一句话**：故事质量 = 原料结构 × 表达技巧。前者（goal 冲突、known_secrets 信息差、milestones 节奏）已在架构里，后者用三层方案解决，无需动模型权重。

### 8.10 story/ 独立系统落地现状（2026-09-25，Phase 1 已完成）

**架构定案（用户拍板）**：新系统与原系统完全分离——原系统零改动（已核验），复制到 `story/` 独立开发。详细实施计划：`.trae/documents/story-system-implementation-plan.md`。

- **形态**：独立 Flask 应用（端口 5004），`cd story && python app.py`，前端 `story.html`（浅色主题 #4FC3F7 背景 + 白底蓝边，2026-09-25 用户指定；参考图见会话，布局自定）
- **结构**：`story/story/`（schema/store/director/narrator/builder）+ 复制的 agent/memory/tools（两处集成：A3 前置注入权限门内容；chat() 后置 director.apply 剥 [STATE:] 标签并清洗 A4 输入）+ 7 个 story API
- **副本身份模型**：一个副本 = 一个新 identity（`story_时间戳`），identities.json 条目带 `kind:"story"`；NPC 人设切换 = 对话前替换 pipe.persona
- **冒烟**：核心包 50/50、集成 25/25、前端 DOM 28/28；真实 LLM 全流程测试由用户负责
- **已修的坑**：①`config.txt` 不在复制清单会报 Invalid URL（必须复制到 story/）；②app.py 的 static_url_path="" 只映射 static/，/story.js 会 404，故 JS 内嵌进 story.html（story.js 仅作规范源码副本，需手动同步）
- **生成进度条**：builder 阶段回调带百分比（5 排队→15 构建世界观→55/70 审稿两轮→88 建档→100），前端 2s 轮询，白槽蓝条只进不退
- ** Persona 并发**：热切换为 Phase 1 单人方案，根治留 Phase 2

### 8.11 任务系统 + 多终局 + 游览路线（2026-09-25 第一轮测试反馈，已实施）

**用户反馈三问题**：①终局只有口头一个，需 2-3 个事件驱动终局；②城市模型的"游览路线"没落地；③任务全靠 NPC 口头约定，没有真任务系统，"现在就是单纯 NPC 在聊天"。

**核心思路：三个问题 = 同一件事——把"口头约定"升级成"机器可判的结构"。任务完成的判定权在导演，不在 AI**（AI 的 [STATE:] 提议只是参考，flags 集合满足才算数）。

**改动设计：**

1. **多终局**：schema 单数 `ending_condition` → `endings[]`（2-3 个：id/title/condition_flags/ending_text）。造城 AI 必须设计分岔（如：信任方舟信号→生还；揭穿→内乱；物资耗尽→冻毙）。导演每轮评估全部终局，先满足先生效，终局遮罩显示达成了哪个。旧副本的单字符串兼容保留。
2. **游览路线**：`routes[]`（2-3 条：id/name/brief/steps 有序步骤链，步骤引用 NPC/任务）。造城 AI 生成，作为任务链骨架——路线走得通是副本合法性的隐性标准。**约束力 = 软引导（用户已选）**：当前路线进度注入 NPC 提示词，NPC 主动把话题往下一步拉；玩家偏离不拦截。
3. **真任务系统**：quests 每项加机器可判的 `accept_flags` / `done_flags`（如 done_flags=[knows_secret_s1, met_npc_n2]）。导演每轮 flags 变化后自动判定任务状态，**状态只进不退**：undiscovered→active→done/failed。前端加任务面板（任务名/giver/状态标签），随 /api/story/current 轮询或每轮对话后刷新。

**改动范围**：schema.py（结构+校验）、builder.py（提示词升级：终局分岔+路线+任务 flags，BUILDER_STORY_SYSTEM/FEW_SHOT/CRITIC 清单同步）、director.py（任务自动判定+终局数组评估+路线进度注入）、前端（任务面板+路线清单+终局显示）、/api/story/current（返回 routes/quests/endings 公开视图）。

**状态：已实施（2026-09-25）。** 要点：①schema.py 新增 `endings[]`/`routes[]`/`locations[]` 校验与 quests 的 accept_flags（可空）/done_flags（必填）/time_cost_minutes（1~600）校验，缺 endings 时回退旧单终局必填；②director.finalize 自动判定——accept_flags 满足→active、done_flags 满足→completable（状态只进不退），completable/done 仅程序可判，AI 标签只能提议 active/failed；endings[] 先满足先生效置 ending_reached；routes 注入 NPC 提示词软引导；③BUILDER_STORY_SYSTEM 方法论第6条+JSON 结构扩展、FEW_SHOT 客栈疑云范例升级（4 地点/2 任务/2 路线/3 终局）、CRITIC 新增维度5（系统可判定性）维度6（终局分岔与路线）；④前端任务面板（五色状态标签 q-undiscovered/active/completable/done/failed + 可完成任务"完成（推进 N 分钟）"按钮 + 路线清单 + 可完成数 badge）。与 8.12/8.13 同次交付。

### 8.12 游戏状态层：玩家数值 + 时间 + 地点（2026-09-25 第二批测试需求，已实施）

**用户需求**：血槽/饱食度/现在几点等状态参数；任务固定耗时由 AI 考量；左上角任务弹窗，玩家点"完成"扣对应时间量；地点参数由导演指挥——NPC 邀请"跟我来地下实验室"、玩家明确同意（"好的"）→ 玩家地点换过去；不同地点的 NPC 无法直接对话，chip 变灰。

**已定决策（用户选择）**：
- 数值范围 = **玩家数值（hp/max_hp/hunger）+ NPC 只有地点参数**；NPC 自身血/饱食后置
- **时间只在任务完成时推进**：对话不消耗时间；玩家点"完成"→ 导演扣除该任务 `time_cost_minutes`
- 时钟初始值由 builder 定（如第 1 天 08:00）

**设计**：
1. **Schema 新增**：`player`（hp/max_hp/hunger/location）、`clock`（day/time）、`locations[]`（地点白名单，builder 生成）、`npc_locations`（每 NPC 一个地点）、quests 加 `time_cost_minutes`（AI 考量写入）
2. **导演职责**：flags 满足→任务转"可完成"；玩家点完成→done+扣时+时钟推进；`[STATE: location=xxx]` 经白名单校验后换玩家地点+旁白播报"你们来到了xx"；NPC 可对话性 = `npc_locations[npc] == player.location`
3. **注入**：A3 提示词加玩家状态块（HP/饱食度/第X天时刻/所在地点/当前可对话 NPC 名单），NPC 因此知道玩家在哪、能自然发出"跟我来"类邀请
4. **前端**：顶栏状态区（HP/饱食/时间/地点）、左上角任务面板（8.11，含"可完成"按钮）、NPC chip 灰色禁用（不同地点，8.11 已有 chip UI）
5. **防作弊**：灰 NPC 的对话后端 /api/chat 同步拦截，不只前端装样子
6. **饱食度本阶段只展示+注入**（无死亡机制），后续可挂钩任务耗时自然下降

**改动文件**：与 8.11 相同——schema.py / builder.py（提示词+few-shot+critic 清单同步）/ director.py / 前端 story.html / API（/api/story/current、/api/chat）。

**状态：已实施（2026-09-25，与 8.11 同次）。** 要点：①schema/director 新增 `player`（hp/max_hp/hunger/location）、`clock`（day+HH:MM，advance_clock 总分钟数计算跨天进位）、`npc_locations` 运行时表、locations 白名单；旧副本 `_migrate_state` 幂等补字段；②新 API `POST /api/story/quest/complete`（complete_quest：completable→done + 扣 time_cost_minutes 推进时钟，notice 文案"「XX」完成，时间推进 N 分钟"）、`POST /api/story/location`（白名单硬校验移动）；③`[STATE: location=xx]` 一字不差过白名单才生效，玩家移动后私聊对象不在场自动断开并旁白提示；可对话性 = npc_locations[npc]==player.location，灰 NPC 后端 /api/chat 同步拦截（不只前端装样子）；④注入：build_injection 玩家状态块（数值/时钟/地点/在场 NPC/可前往地点/白名单）+ 任务行带预计耗时；⑤前端顶栏状态行（体力/饱食/时钟/地点）+ 任务面板完成按钮 + chip 灰色；⑥**UI 逃生通道**：顶栏地点下拉（changeLocation）——防玩家走到无人地点后无 NPC 应答 [STATE: location] 也无人触发导致的死锁，与 AI 提议同一套白名单硬校验；⑦饱食度本阶段只展示+注入，无死亡机制。

### 8.13 场景广播 + 私聊模式（2026-09-25 第三批测试需求，已实施）

**用户需求**：玩家在某地点说话，在场所有 NPC 同时听见并各自回答（不是定向单聊，玩家收到多份回答）；另可开启"私聊模式"——只有该 NPC 应答，其他 NPC 的信息也不更新。

**设计**：
1. **群场对话（默认）**：在场 = `npc_locations[npc] == player.location` 的全部 NPC（复用 8.12）。玩家一句 → 在场每个 NPC 各跑一次 A3 各出一份回答（各自 known_secrets/goal/页表独立，知情与不知情的回答天然不同——内存模型的直接效果）。前端按 NPC 分条显示（头像+名字），点名的排前。
2. **私聊模式**：chip 加"私聊"开关 → 仅该 NPC 应答，界面标注"私聊中，仅你与 XX 可见"；隔离靠**过滤不靠演技**——私聊消息打 `private` 标记，其他 NPC 上下文构建时跳过。私聊中的 `[STATE: location=xx]` 照常生效（跟随进入某地后同地 NPC 自动变在场）。
3. **记忆归属（Phase 1 决策）**：单一副本记忆 + speaker/private 标注——副本 identity 仍是一个记忆目录，历史消息标注"谁说的/是否私聊"，注入按 NPC 过滤。每 NPC 独立记忆目录（复用原系统多身份）留 Phase 2，Phase 1 不动 A4。
4. **成本**：N 个 NPC 同场 = 每句 N 次 A3 + N 次记忆调用。Phase 1 按全员作答实现；后续可加节流（被点名者完整回答、其他人短反应）。
5. **私聊参数与地点校验（用户补充，2026-09-25）**：私聊是"玩家↔某 NPC"的成对参数，**双方都可发起**——玩家点 chip 私聊开关；NPC 可在回复中输出 `[PRIVATE_REQ]` 标签主动请求（导演拦截该标签不展示原文 → 前端弹"XX 想和你单独谈谈"，玩家同意才建立）。**地点校验是硬条件**：双方不同地点不能建立私聊（玩家发起：chip 已灰 + 后端双重校验；NPC 发起：说话必在场，天然满足）。私聊中玩家地点变更而私聊对象不在新地点 → 私聊自动断开（旁白提示"XX 留在了原地"）。

**改动叠加**：director.build_injection 的在场集合从单 active NPC 变为同地点列表；history 消息结构加 speaker/private 字段（store 兼容旧数据）；前端 chip 加私聊开关、消息条目带说话人。其余同 8.11/8.12 文件范围。

**状态：已实施（2026-09-25，与 8.11/8.12 同次）。** 要点：①群场对话：pipeline._story_chat 响应者 = 同地点全部 NPC（private_id 优先、点名排前、点名不在场→旁白提示不加入），每人各跑一次 A3 各答一份；A1/A2 每轮只跑一次，结果与记忆共享给全部响应者，各 NPC 拿各自过滤视图的 recent_history；②私聊隔离靠过滤不靠演技：ConversationHistory 消息加 speaker/speaker_name/private/to 字段（旧数据兼容），get_recent(for_npc/public_only) 过滤——其他 NPC 上下文构建时跳过私聊消息，普通身份调用点零改动；③新 API `POST /api/story/private`（npc_id 空=解除；set_private 同地点硬校验，失败 400）；④NPC 主动发起：A3 输出 `[PRIVATE_REQ]` 标签（每轮与 [STATE:] 二选一），导演剥标签收进 private_requests → 前端弹窗"XX 想和你单独谈谈"队列逐个处理，玩家同意才建立；玩家移动后私聊对象不在场自动断开；⑤记忆归属 Phase 1：单一副本记忆 + 标注过滤，未动 A4；⑥前端：chip 旁"私"字开关（灰=不同地点不可私聊）、私聊横幅（仅你与 XX 可见）、消息带（私聊）标记与虚线边、输入框提示"在场所有人都会听到你说话…"、/api/chat 超时放宽 5 分钟（群场 N 次 A3）。

### 8.14 存档选择界面 + 并行多副本（2026-09-25 第四批需求，已实施）

**用户需求**：进游戏第一眼 = 存档选择界面（仿我的世界存档列表），选择副本进入或点"生成新副本"进入生成界面；每个副本数据独立保存、退出后可再进来继续；退出等必要按钮放暂停菜单里，从暂停菜单回到存档选择。

**实施要点**：
- **后端**：新 API `GET /api/story/list`（全部副本摘要：标题/世界名/类型/NPC数/时钟/地点/任务完成数/是否终局/上次游玩时间，按状态文件 mtime 倒序；任务完成数从 flags 的 quest_{id}_state 统计）、`POST /api/story/enter`（校验副本存在 → set_current 设指针 → 返回 get_public_state 公开视图，与 /api/story/current 同构）。并行模式天然成立：每副本本就独立存 `{identity}.json` + `{identity}_state.json`，指针只是"最近进入的副本"标记；生成完成 builder 自动设指针，生成完直接进入新副本的旧流程不变。
- **前端**：新增视图零 view-select（存档选择首屏，init 不再自动恢复进游玩而是 showSelect）；存档卡片点击 → enterSave → enterPlay（已终局的副本进入后直接展示终局遮罩）；底部"＋ 生成新副本"→ 设置视图，设置视图新增"← 返回存档选择"；游玩页标题行加"菜单"按钮 → 暂停遮罩（继续游戏 / 回到存档选择）；终局遮罩"再来一局"改为回存档选择（原来回设置页）。story.js 已同步。
- **删除副本未做**（用户未要求；后续可加 DELETE /api/story/save）。

**状态：已实施（2026-09-25）。py_compile + node --check 通过，真实测试由用户负责。**

### 8.15 生成设置高级选项（2026-09-25 第五批需求，已实施）

**用户需求**：参考项目 ai-galgame-demo 的剧本创建页选项很多（封面/标签/名称/作者/世界观条目/地区/玩家角色/NPC 卡片/关系网/物品库/事件系统/开场场景/叙事视角/骰子/不确定性等 20 余节），我们只有 5 项，覆盖不了需求，借鉴扩充。

**取舍与实施**（只借鉴适配我们生成引擎的项，不做手填式条目编辑器——我们的定位是"AI 生成为主，玩家给约束"）：
- **新增 7 个选填项**（设置页"高级选项"折叠组）：`title_hint` 副本名称（必须使用）、`player_name`+`player_desc` 玩家角色（身份/背景，剧本围绕其展开）、`lore` 世界观补充设定（每行一条，必须遵守）、`npc_hints` 指定 NPC（每行"名字：身份/性格"，必须收录且姓名身份不得改）、`ending_hint` 期望结局方向（作分岔终局灵感）、`extra_req` 其他附加要求（必须满足）。
- **注入链**：app.py 原样透传表单 → builder.py 组装 extra_block（有内容才注入，格式"可选附加要求（玩家指定；若与通用规则冲突，以附加要求为准）"）→ BUILDER_STORY_USER 新占位符 `{extra_block}`；玩家角色另存 `story.player_persona`，由 director.build_injection 玩家状态块注入"- 玩家身份：XX"让 NPC 按身份称呼玩家。
- **参考页中未采纳的项**：封面图/BGM/TTS 立绘（无多媒体层）、骰子/不确定性等级（无行动判定机制）、物品库/事件系统/关系网/快速回复（这些由造城 AI 全自动生成更优）、作者/叙事视角（单人系统无意义）。后续若需要可在高级选项组内继续加，注入链已就绪。
- story.js 已同步；py_compile + node --check 通过。

**状态：已实施（2026-09-25）。真实测试由用户负责。**

### 8.16 外源世界观资料桥（2026-09-25 第六批需求，2026-09-26 已实施）

**用户需求**：基于已有世界观（如战锤40K）生成副本时，我们没有搜索 API，无法拿到真实设定资料。用户方案：生成阶段界面弹出一段大提示词 → 用户复制到有搜索能力的外部 AI 工具 → 提示词引导外部 AI 搜索完毕后，把结果提交到我们专属的"故事情节 MCP"接收端 → builder 拿到资料后再生成。**硬性要求**：①信息完整准确是第一优先级；②在此前提下尽量减少用户复制趟数（builder 要一次性收集全部所需，避免用户来回搬几趟）。本节只做计划，未写代码。

**核心流程（时序）**：
1. 设置页开启"外部世界观资料检索"开关（高级选项内，默认关）→ 点开始生成
2. 后端创建 research job（独立于现有 builder job），builder 不启动
3. 一次轻量 LLM 调用：基于 world_desc/lore/npc_hints/genre 生成**检索需求清单**（research brief）+ 拼装成给外部 AI 的完整提示词
4. 前端切到"资料检索"视图：展示提示词全文 + 一键复制 + 兜底粘贴框 + "跳过检索直接生成"按钮
5. 用户复制 → 外部 AI 搜索 → 外部 AI 通过 MCP/HTTP 调用我们的接收工具提交 JSON（或用户手动复制 JSON 回粘贴框提交）
6. 接收端校验通过 → research job 置 ready → 前端轮询感知 → 自动进入生成进度视图，builder 带资料启动
7. 校验不通过（缺主题/空字段/JSON 非法）→ 生成"补查提示词"给用户第二趟（这是唯二可能的多趟场景）

**关键设计决策**：
- **一趟式收集**：检索需求清单按主题分组（阵营/地名/人物/术语/科技或力量体系/时间线/可造剧情冲突点），提示词明确要求"全部主题一次查完，在一次回复/一次调用中提交"，并给出输出 JSON 契约；主题数上限 ≤12、每主题事实 ≤10 条，控制用户搬运量与注入体积
- **提前搜 vs builder 实时搜（用户 2026-09-25 追问，已决策：提前搜）**：曾考虑 builder 生成中途实时发搜索任务（查得准，查哪缺哪）。不采纳，理由：①与硬性要求矛盾——实时模式下用户每趟都要手动搬运，一趟生成可能拆出 3~6 趟操作；②现有 builder 是单次 LLM 调用产出完整 JSON，要支持中途暂停等资料必须改成多阶段状态机（骨架→逐 NPC→逐任务），复杂度和不稳定度大增；③critic 审稿拿不到"事后才查到"的资料。提前搜的漏查风险靠两点对冲：清单生成前先让 LLM 模拟"按这些设定写副本需要哪些具体细节"再列清单（清单质量前置）；builder 仍可自创未覆盖细节但不得与已核实资料矛盾。"实时补搜"留作 Phase 2 备选，模式 A 跑通后若漏查率高再加
- **资料注入游玩阶段（用户已定，Phase 1 就做）**：research 结果存入 `story.research_summary`；director.build_injection 新增独立块"### 世界观资料（经检索核实，禁止与之矛盾）"，把 facts 平铺注入当前 NPC（受 6000 字预算约束，超出时只注入与该 NPC/当前地点相关的主题）；与现有 A3 反事实校验（_verify_and_revise）构成双重防矛盾
- **JSON 契约**：`{source_model, topics: [{topic, facts[], uncertain[], relations[]}], notes}`；uncertain 字段强制外部 AI 标注没查实的内容——**准确性优先**的落点
- **接收端形态**：优先在 story/app.py 加 `POST /api/story/research/<job_id>`（HTTP 端点即"MCP tool over HTTP"，外部 Web AI 配置自定义 MCP/插件即可调用）；如需标准协议再包 FastMCP（端口 5005）。粘贴框是必做兜底——不是所有外部 AI 都支持 MCP
- **注入 builder**：research 结果格式化后注入 BUILDER_STORY_USER（extra_block 之前的独立"世界资料"块），措辞："以下为经检索核实的官方设定，必须严格遵守，不得与之矛盾；未覆盖细节可自创但不得冲突"；prompts.py 新增 RESEARCH_BRIEF_SYSTEM / RESEARCH_PROMPT_TEMPLATE / RESEARCH_INJECT 常量
- **防灌爆**：research JSON 总字数上限（约 6000 字），超限截断长主题并记 review_notes

**开放问题（实施前需确认）**：
- 外部 AI 支持度：主流工具（豆包/Claude/ChatGPT）的 MCP/插件配置方式各不相同，提示词模板里给通用引导 + 粘贴兜底双路（已决定兜底必做）
- ~~游玩阶段一致性~~ → 已定：Phase 1 就注入（见关键设计决策）
- ~~提前搜 vs 实时搜~~ → 已定：提前搜（见关键设计决策）

**实施记录（2026-09-26，全部按上述计划落地）**：
- **后端任务机制**：app.py 的 story_jobs 增加 kind=build/research 双轨——`/api/story/create` 开启 research_enabled 时建检索任务（先一次轻量 LLM 生成检索需求清单并拼装完整提示词，builder 不启动）；新增 `POST /api/story/research/<job_id>`（HTTP 端点即 MCP tool 接收端：校验通过→带资料启动造城；不通过→200+ok:false 返回问题清单与补查提示词，任务保持 waiting）与 `POST /api/story/research/<job_id>/skip`（跳过检索直接生成）。造城锁策略：清单生成完即放锁（等资料期间可返回修改重新发起），提交/跳过时重新抢锁
- **校验与防灌爆**：schema.py 新增 `validate_research`（契约校验：topics 非空、每主题 topic 非空、facts/uncertain 不可双空）与 `format_research`（facts 平铺【主题】块、每主题 facts ≤10 条、relations 支持字符串或 {a,relation,b} 对象、uncertain 加「（未查实）」前缀；总字数 6000 上限——超限时若给 focus 关键词（NPC 名/NPC 地点/玩家地点）先按相关性筛主题、仍超再整主题顺序截断；截断说明随返回值交调用方记 review_notes）
- **注入链**：builder.build_story 新增 research 参数——资料格式化后经 RESEARCH_INJECT 注入 BUILDER_STORY_USER 的 extra_block 之前独立「世界资料」块，结构化原文存 `story["research_summary"]`，截断说明记 review_notes（round="research"）；director.build_injection 经 `_research_block` 在 NPC 注入块尾部追加「### 世界观资料（经检索核实，禁止与之矛盾）」（6000 字预算，超预算仅注入与当前 NPC/当前地点相关主题），与 A3 反事实校验构成双重防矛盾
- **prompts.py**：新增 RESEARCH_BRIEF_SYSTEM（清单生成：先模拟「写副本需要哪些细节」再列主题分组清单，≤12 个，只输出 JSON）/ RESEARCH_PROMPT_TEMPLATE（给外部 AI 的完整提示词：全部主题一次查完、准确性第一、uncertain 强制标注没查实、JSON 契约 {source_model, topics[{topic,facts,uncertain,relations}], notes}、提交方式双路=HTTP POST /api/story/research/<job_id> 或复制回粘贴框）/ RESEARCH_INJECT（造城注入块）
- **前端**（story.html 与 story.js 已同步）：设置页 researchSwitch 的 hint 更新为正式说明（8.19 预留位接通）；新增「视图二点五：外源资料检索」——提示词全文展示 + 一键复制（clipboard API 优先、execCommand 兜底）+ 粘贴兜底框 + 校验失败展示问题清单与补查提示词（可一键复制，第二趟）+ 跳过检索按钮 + 返回修改；pollCreate 感知 research job waiting→自动切检索视图；检索视图 3 秒看门狗轮询（外部 AI 走 HTTP 直提时前端也能感知状态跃迁并自动回生成进度视图）
- **校验**：py_compile（app/prompts/schema/builder/director 5 文件）+ 17 项冒烟（契约校验/格式化截断/focus 过滤/模板渲染/路由注册）+ node --check（story.html 内嵌脚本与 story.js）全部通过

- **MCP 桥接器（2026-09-26 追加，应「装到别的 AI 工具」需求）**：story/mcp_research_bridge.py——stdio FastMCP 把两个接收端点包装为 `submit_story_research(job_id, research_json)` / `skip_story_research(job_id)` 两工具；urllib 直调本机 HTTP，story 服务不新增端口，除 mcp 外零依赖；校验失败时补查提示词随工具返回原样回传外部 AI 自查修正。安装：`pip install mcp`（已装入 Python 3.13 全局环境）；MCP 配置 JSON（Claude Desktop / Cursor / Trae 通用）见脚本文件头注释；story 服务不在 5004 端口时用 env `STORY_BASE_URL` 覆盖

**状态：已实施（2026-09-26）。真实测试由用户负责：重启 python app.py（端口 5004）+ Ctrl+F5 强刷。测试链路：①开关开→生成→复制提示词→（粘贴合法 JSON 提交走通全链路 / 粘贴非法 JSON 验证补查提示词 / 点跳过检索）→生成进度→进本游玩，与 NPC 聊设定相关话题验证不与资料矛盾。MCP 路：把提示词粘给装了桥接器的外部 AI→它搜完直接调工具提交，前端看门狗自动切回生成进度。**

### 8.19 角色卡自动生成 + 搜索预留开关 + 冒险手册（2026-09-26 第八批需求，已实施）

**需求**：①"你的角色"不填时 AI 自动生成玩家角色；②高级选项加"外源资料检索"开关（8.16 前端预留位，否=纯自创；是=预留，暂未接通）；③开局获得角色卡片（身份信息）；④冒险手册（仅当前副本，导航栏入口）：身份卡 + 已完成任务 + NPC 交代的重要信息。

**实施**：
- **角色自动生成**：BUILDER_STORY_SYSTEM 的 JSON 结构新增 `player_persona {name, identity, background, skills}` + 硬性要求一条（表单指定必须照用名字身份；未指定则 AI 设计有代入感的普通人角色，不要救世主，要有卷入纷争的理由）；few-shot 范例同步加示例。schema.validate_story 校验 name 必填。builder 收尾：表单指定时名字/身份用表单值、细节保留 AI 生成；未指定保留 AI 生成；旧副本无此字段时公开视图回退空对象（不报错）
- **搜索预留**：设置页高级选项加 `researchSwitch`（否=默认/是），表单透传 `research_enabled` → builder 落盘 `story["research_enabled"]`（8.16 资料桥开工时直接读取）；开关旁注明"暂未接入，即将推出"
- **[NOTE:] 标签**（手册信息来源）：A3_STORY_STATE_RULE 第 6 条——仅当本轮透露重要信息（关键线索/承诺约定/身份来历/任务要点/时间地点约定）才输出 `[NOTE: 一句话]`，可与 [STATE:] 同轮；director 新增 NOTE_RE 剥标签并提取；`add_notes(npc_name, notes)` 带时钟快照（day/time/npc/content）写入 `state.manual_notes`（同 NPC 同内容去重）并落盘；pipeline 每条 NPC 回复解析后调用；_migrate_state 为旧副本补 manual_notes=[]
- **冒险手册 + 角色卡**：play-header 新增"📖 手册"按钮 → overlay 三区块（身份卡/已完成任务（只展示 state==done 的，含奖励）/重要信息（最新在上，含"第X天 HH:MM · NPC"））；公开视图新增 player_persona 与 manual_notes；开局 enterPlay 自动弹角色卡（每副本每会话一次），点"开始冒险"关闭，手册中随时可看
- story.js 已同步；py_compile（6 文件）+ node --check 通过

**状态：已实施（2026-09-26）。真实测试由用户负责。**

### 8.19b 首测反馈修复：任务跃迁系统提示 + undiscovered 任务隐藏 + 任务面板收进导航栏（2026-09-26，已实施）

**用户反馈**：①任务接取成功没有系统提示；②"完成"按钮没看到；③"任务与路线"常驻框占屏幕太大——收进导航栏做成可展开/合闭；④未发现的任务不应显示——玩家自己发现任务，系统提示"已发现任务"；玩家只能看到：已发现任务的信息 + 完成按钮 + 手册内容。

**实施**：
- **任务跃迁系统提示**（解决反馈①②）：director 新增 `_quest_notices` 缓冲 + `_push_quest_notice(q, prev, now)`（undiscovered→active："📌 接取任务「X」（预计耗时 N 分钟，进度见右上角任务面板）"；→completable："✅ 目标达成！可在右上角任务面板点击「完成」（推进 N 分钟）"；→failed："❌ 已失败"）+ `take_quest_notices()`（取走清空）；跃迁的**两条路径**都接入——AI 提议路径（`_apply_flag` quest 分支，新增 `get_npc_quest(qid)` 查任务定义）与程序判定路径（`finalize` 的 accept_flags→active、done_flags→completable）；pipeline 两处 finalize 后 `notices.extend(d.take_quest_notices())` 拼进前端系统提示。反馈②根因：任务在 active 阶段（done_flags 未满足）本来就不显示完成按钮，属预期行为，补上跃迁提示后引导闭环
- **任务保密性**（反馈④）：`get_public_state` 的 quests 过滤——`quest_{id}_state` 为 undiscovered 的任务不下发给前端；面板空状态文案改为"还没有发现任务。与 NPC 交谈、探索地点，可能会遇到任务。"
- **面板收进导航栏**（反馈③）：quest-panel 从常驻 aside 改为 absolute 下拉（`.play-header{position:relative}` + `top:100%;right:12px;width:min(340px,...);max-height:62vh;z-index:60`），header 加 `questToggleBtn`（📋 任务 + 可完成数 badge）；点击 toggle、点击面板/按钮以外区域自动收起；删除旧 questHead/questArrow/collapsed 样式与 HTML
- story.js 已同步（renderQuests 空状态 + init 绑定替换为 questToggleBtn + document 点击外部收起）；py_compile（director/pipeline）+ node --check 通过

**状态：已实施（2026-09-26）。真实测试由用户负责。**

### 8.20 调时系统：动作型任务的时间投入判定 + milestone_* 词表放宽（2026-09-26，已实施）

**需求**（用户设计）：动作任务（"帮我整理xx"这类无法对话验证的）没法用现有 flags 表达，采用"时间投入代替动作验证"：任务设预留时长，玩家自己调时投入，投入够代表活儿干完了；**且必须再口头汇报**（"我整理完了"），双条件缺一不可。事实型任务（告诉秘密/剧情关键）完全走旧系统。调时档位定为 +15/+30/+60 三档。

**核心设计**——双分支统一走 flags 判定，判定权仍在导演：
- 任务结构新增 `judge_mode`（"flags"默认 | "time"），builder 按任务性质决定
- **flags 型**：done_flags 照旧（knows_secret_*/met_npc_*/quest_*_state + **新增自由 milestone_自定义词**）
- **time 型**：done_flags 固定模板 `milestone_{qid}_time_ready==true && milestone_{qid}_reported==true`——`time_ready` 仅程序写（投入累计 ≥ time_cost_minutes 时）；`reported` 由 A3 提议（玩家口头汇报、NPC 认可后 [STATE:] 标签）。两个条件独立推进、缺一不跃迁，finalize 判定逻辑零改动
- **调时记账独立**（state.quest_invested {qid: 分钟}），不从时钟差值倒推（完成任务/其他事件也推钟，倒推会错算）；调时点击时时钟**同步推进**（时间真的在过）；time 型任务点"完成"**不再扣时**（时间已在投入时消耗）
- 防作弊白名单：剧本里程碑 milestone_{mid} 与 time_ready 只许程序写；AI 只能提议 `_ai_reported_flags` 集合内的 reported key（__init__ 按 judge_mode==time 的任务构建）；未预置 flag 的旧副本提议自动被 `key not in flags` 拒绝

**实施**：
- **schema.py**：quest 校验 judge_mode ∈ flags/time；new_state 为 time 型任务预置 `milestone_{qid}_time_ready/reported=False` 两个 flag + state 加 `quest_invested: {}`
- **prompts.py**：BUILDER_STORY_SYSTEM 硬性要求重写任务段（judge_mode 二选一 + time 型 done_flags 固定模板 + flags 型词表放宽 milestone_自定义词）；JSON 结构模板 quest 加 judge_mode；few-shot q2「稳住掌柜」改为 time 型示例；A3_STORY_STATE_RULE 第 1/3 条加 reported 汇报标记提议规则（含"时间投入明显不够就汇报→追问细节不要提议"的分寸）；玩家时间注入行改为"时间只在玩家投入任务时间或完成任务时推进"
- **director.py**：`invest_time(qid, minutes)`（校验 judge_mode/state==active/minutes∈{15,30,60} → 记账+推钟 → 达标首写 time_ready+提示"记得向XX汇报" → finalize 统一跃迁）；complete_quest time 型分支不扣时；_push_quest_notice 按 judge_mode 分文案（接取提示 time 型引导调时+汇报；completable 提示 time 型注明时间已消耗）；_apply_flag 加 reported 白名单分支；build_injection 任务块 time 型注入汇报提议指引（含确切 [STATE: milestone_{qid}_reported=true] 写法）；_migrate_state 补 quest_invested；get_public_state quests 附 judge_mode/invested_minutes
- **app.py**：`POST /api/story/invest`（quest_id + minutes；notices=invest 自身提示 + take_quest_notices 跃迁提示；返回 story_state）
- **story.html/story.js**：renderQuests time 型 active 任务渲染进度条（已投入/预留，0.6s 过渡）+ 三档调时按钮 + 引导文案"投入足够时间后，向发布任务的 NPC 口头汇报即可完成"；completable 完成按钮 time 型只显示"完成"；新增 investQuest()（POST invest → notices 渲染进聊天流 + applyState）；CSS 加 .q-progress/.btn-invest 等
- py_compile（schema/director/prompts/app）+ node --check 通过；story.js 已同步

**注意**：~~旧副本任务无 judge_mode 字段（默认 flags 型），调时 UI 不会出现~~ → **8.20b 已兼容旧副本（见下）**。

**状态：已实施（2026-09-26）。真实测试由用户负责。**

### 8.20b 调时系统二轮反馈：旧副本兼容 + NPC 委婉拒绝 + 防双扣时（2026-09-26，已实施）

**用户反馈**：①长任务需多次调时，检测到调时够了就弹提示（首轮已实现：每次投入弹累计提示，达标那刻额外弹"记得汇报"，`not ready_key` 保证只弹一次）；②时间没投入够就问 NPC → NPC 用委婉的话表示任务还没完成；③兼容旧副本（旧副本也要能调时，不用重新生成）。

**实施**：
- **调时通用化（旧副本兼容）**：`invest_time` 去掉 judge_mode 硬拒——**所有 active 任务都可投入时间**（记账+推钟，时间真的在过）；但 `time_ready` 只在 time 型任务达标时写（flags 型投入不参与完成判定，完成仍以对话达成为准）；旧副本（无 judge_mode，全 flags 型）因此天然可用调时
- **NPC 委婉拒绝有据可依**：build_injection 任务块注入「玩家已投入：X/N 分钟」（NPC 看得到确切进度）；time 型指引按投入状态分支——未达标：「玩家问起或汇报就委婉表示这件事还需要些工夫（如「别急，慢慢来」「这事急不得」），不要提议 reported」；已达标：「玩家口头汇报且判断合理时提议 [STATE: milestone_{qid}_reported=true]」
- **防双扣时**：complete_quest 扣时改为 `max(0, 预留 - 已投入)`——time 型投入必达标扣 0；flags 型未调时全额扣（与 8.12 一致）、调时过只扣剩余；completable 跃迁提示与前端"完成"按钮文案同步按剩余时间显示（扣 0 时只显示「完成」）
- 前端：所有 active 任务显示进度条+三档调时按钮，tip 按 judge_mode 分文案（time 型引导汇报 / flags 型注明"任务完成仍以对话中达成为准"）；story.js 已同步；py_compile + node --check 通过

**状态：已实施（2026-09-26）。真实测试由用户负责。**

**已知问题（8.20b 实测）**：A3 对「汇报确认」标签漏发——即使注入用硬性要求措辞，模型仍会因叙事惯性（历史里奖励已发放）跳过 [STATE: milestone_qX_reported=true] 输出（两次实测均漏）。待办方向：在 pipeline 的 user prompt **末尾**（末位指令遵从率最高）追加导演提醒行：「若玩家本轮在汇报动作任务完成，末尾必须输出对应汇报标记」；story_20260926_004601 副本 q1 已手动置 completable 供用户验证后续流程。

### 8.20c 汇报确认可靠性：导演末位提醒 + 任意对话兜底结算（2026-09-26，已实施）

**背景**：8.20b 实测 AI 两次漏发 reported 标签，用户已连续口头汇报多次陷入尴尬循环。

**实施**：
- **导演末位提醒**：director 新增 `end_prompt_reminder()`——存在「time 型 + active + time_ready 已达标 + 未 reported」的任务时返回硬性提醒文本（含确切标签写法与"没汇报就不要输出"分支），pipeline 传给 a3.generate 新参数 `director_reminder`，拼在 **user prompt 最末尾**（末位指令遵从率最高）；无满足条件任务返回空串零 token
- **任意对话兜底结算**（用户裁定"再发一段消息让 NPC 回复一下就行，别让玩家尬聊汇报"）：director 新增 `auto_settle_report(npc_id)`——time 型任务时间已投满时，玩家与发布人完成**任意一轮对话**（该 NPC 本轮有回复即算），若 AI 本轮仍未提议 reported，程序补写 + 手册记一条「XX确认了任务「X」已完成」+ finalize 跃迁；AI 主动提议过则跳过（原方案优先）；pipeline 每条 NPC 回复解析后调用
- **导演缓存 bug（顺带修复，实为严重）**：app.py `_ensure_story_director` 此前只在首次聊天装配 Director 并**缓存在 pipeline 对象里**，invest/move/complete 各自新建 Director 读盘 → 每次聊天结束时缓存 Director 把**内存旧状态整体覆盖回磁盘**（场景切换被回滚、手动置的任务状态被回退、日志无异常的"静默回档"）。修复：导演改为**每次聊天都从磁盘重建**，与其它接口一致

**状态：已实施（2026-09-26）。py_compile 通过。**（末位提醒实测仍可能被叙事惯性压过 → 8.20d 上程序级按钮，见下）

### 8.20d 汇报按钮：系统级确认 + 自动代发对话（2026-09-26，已实施）

**用户裁定**：汇报这个动作要"既是系统级指令，又是 NPC 对话活动"——点任务面板按钮 → 程序立刻确认状态（确定性，不依赖 AI）→ 前端自动代发一条汇报消息给发布人 → NPC 正常回复演验收反应。

**实施**：
- **director.report_quest(quest_id)**：校验（time 型/active/time_ready/未重复汇报/**发布人与玩家同地点**，否则报"需要到 XX（地点）身边当面汇报"）→ 程序写 reported=true → finalize 跃迁（投满即 completable）→ 返回 {npc_id, npc_name, draft:"我把「X」的活儿干完了，跟你汇报一声。"}
- **app.py `POST /api/story/report`**：quest_id 必填；调 report_quest → d.save() → notices=take_quest_notices → 返回 story_state + draft
- **前端**（story.html/story.js 已同步）：renderQuests 给 time 型 active 任务在时间投满后渲染「📣 汇报完成（当面告诉 XX）」按钮（复用 btn-complete 样式，q.reported 或未达标不显示）；新增 `reportQuest()`——POST report → notices/applyState → 旁白"（你向 XX 汇报了…）"→ 自动把 draft 填入 chatInput 并调 doSend()（走正常聊天链路，NPC 回复为纯演出，不影响状态）；tip 文案改"投入足够时间后，点「汇报完成」向发布任务的 NPC 当面汇报"
- **公开视图**：quests 附 `time_ready`/`reported` 两个字段（按钮显隐依据）
- **旧方案保留**：口头汇报 + AI 提议标签（原方案）、8.20c 末位提醒、auto_settle 任意对话兜底——三条路径并存互不冲突（reported 幂等，先到先得）

**状态：已实施（2026-09-26）。py_compile + node --check 通过，真实测试由用户负责。**

### 8.21 Phase 2 双件套：类型化状态原语 + NPC 关系/心情慢变量（2026-09-26 立项，2026-09-26 已实施）

**范围裁定（用户 2026-09-26）**：8.17 调研候选中实施两项——①类型化状态原语（worldsmith 启发）；②NPC 关系/心情慢变量（MistScale/ai_rpg 启发）。**Git 风格剧情分支存档被用户明确排除**（"分支太多会乱"，等其它分支概念成熟再说）。

#### 8.21a 类型化状态原语（升级 [STATE:] 标签体系）

**设计要点**：
- **mutate 数值原语**：`[STATE: hp+10]` / `[STATE: hunger-20]` / `[STATE: hp=50]` 三种写法（相对增减 / 绝对赋值）
  - 白名单键 Phase 1 只开玩家两个数值：hp（钳 0~max_hp）、hunger（钳 0~100）；NPC 侧数值不开
  - 防作弊：单轮单键变化幅度 ±30 钳制（防 AI 一下清空/拉满）；钳界截断
  - 语义：AI 在叙事中有理由时提议（"吃了面包"→hunger+15），导演白名单校验后应用——写入权仍在导演
- **remember 认知原语**：`[STATE: npc_learned_{nid}=一句话事实]`
  - 落 `state.npc_knowledge {nid: [事实,...]}`（去重、每 NPC 上限 8 条、FIFO 淘汰）
  - build_injection 注入"你已知悉的事实"块——8.7 页表动态扩展的落地（NPC 得知新真相 → 行为自然转变）
  - 权限校验：nid 必须 == 本轮回复的 NPC 自己（群场各写各的，防 A 替 B 认知）
- **实施清单**：director `_apply_flag` 加 mutate/learned 解析分支（STATE_TAG_RE 已兼容 k=v 格式，k 里带 +/- 需单独正则）+ `_migrate_state` 补 `npc_knowledge={}` + build_injection 事实块；prompts.py A3_STORY_STATE_RULE 第 7 条（用法+分寸：数值变化要有叙事理由、learned 只记真正得知的重要事实）；schema 无需改（运行时结构）；pipeline 传回复者 nid 给 apply_reply（已有 npc_name 参数，需确认 id 传递）
- **前端**：无需改动（开发者面板 /api/story/debug 顺带展示即可）

#### 8.21b NPC 关系/心情慢变量

**设计要点**：
- **结构**：`state.npc_attitude {nid: {affinity: 0~100(初始50), trust: 0~100(初始50), mood: 枚举(默认"平静")}}`；_migrate_state 补默认
- **mood 枚举 7 档**：平静/开心/警惕/不满/疲惫/悲伤/愤怒（由 emotion-keywords 中文情绪词表归并）
- **双通道更新（防讨好循环是核心考量）**：
  - **mood**：A3 可提议 `[STATE: mood_{nid}=开心]`（白名单枚举 + 只许本人）——心情是短变量，叙事里自然起伏
  - **affinity/trust**：**只由程序事件驱动，AI 不许碰**（防 AI 刷好感）——事件清单 Phase 1：任务完成（giver +8）、任务 failed（giver -8）、秘密揭示成功（持有人 +10）、私聊请求被玩家接受（发起者 +5）
- **注入**：build_injection NPC 块加"你对玩家的当前态度：好感 X（分档文案：0-20 排斥/21-40 冷淡/41-60 中立/61-80 友好/81-100 亲近）、信任 X、心情 Y"——影响扮演、不暴露数值给玩家台词
- **展示**：Phase 1 只进 /api/story/debug 开发者面板；玩家界面不动（沉浸优先）
- **实施清单**：director（状态结构+迁移+事件钩子+`_apply_flag` mood 分支+注入行）；prompts.py A3 规则补 mood 提议；app.py /api/story/debug 补 attitude 输出；前端零改动
- **实施顺序**：先 8.21a 后 8.21b，分两批交付（py_compile 校验，测试由用户负责）

**实施记录（2026-09-26，26 项冒烟全过，用户实测待做）**：
- **director.py**：`apply_reply` 加 `npc_id` 参数（pipeline 传 rid）；解析链新增四分支——`MUTATE_RE`（hp+10/hunger-20 相对增减）→ location → `npc_learned_{nid}`（只许本人，去重+上限 8 条 FIFO）→ `mood_{nid}`（7 档枚举+只许本人）→ `_MUTABLE_KEYS` 绝对赋值（hp=50，按与当前值差钳 ±30）→ 原 flags 白名单。新方法：`_attitude`/`_adjust_attitude`（程序专属，钳 0~100）/`_mutate_player`/`_npc_learn`/`_set_mood`
- **事件钩子**（AI 无权直写好感/信任）：任务完成 giver+8（complete_quest）、任务 failed giver-8（_apply_flag）、秘密揭示持有人+10（_apply_flag 与 reveal_secret 双入口）、私聊建立发起者+5（set_private）
- **注入**：NPC 块新增「你的 NPC 编号」行 + 态度行（好感+分档文案 0-20排斥/21-40冷淡/41-60中立/61-80友好/81-100亲近、信任、心情）+「你已知悉的事实」块（npc_knowledge，他人隔离）
- **迁移**：_migrate_state 与 schema.new_state 补 `npc_knowledge={}` / `npc_attitude={}`（幂等，旧副本自动补齐）
- **prompts.py**：A3_STORY_STATE_RULE 规则 1/2 扩白名单 + 新增第 7 条（数值有叙事理由、单轮≤30；learned 只记重要事实；mood 7 档）
- **app.py**：/api/story/debug 返回 npc_knowledge + npc_attitude；前端零改动
- **已知边界**：±30 钳制按"单条提议"计（群场多 NPC 同轮各提一条可叠加，Phase 1 接受）；信任值暂无事件来源，恒 50

### 8.22 全流程实测修复：秘密揭示链（2026-09-26，游玩通关「凛冬三十日」时发现并修复）

**发现（阻断级）**：AI 会在正文完整演完秘密揭示（认账/交账本/交钥匙），但 `[STATE: knows_secret_*=true]` 标签间歇性漏打或发明 key（实测：`knows_secret_suwan_ledger`），而全部结局都以 knows_secret 为前置——漏打即三条结局全部不可达。根因有二：①导演注入的「【尚未对玩家揭示】」行只给内容与暗示，从未把确切 key 给 AI（flags 摘要也只给已揭示 id），AI 无从得知 key 名只能编造；②提示词对"正文说破必须同轮打标"无硬约束。

**修复（三件套）**：
- director.build_injection 秘密行补确切 key +「玩家凭证据问破、决定揭示时必须输出 [STATE: knows_secret_sX=true]」指令
- prompts.py A3_STORY_STATE_RULE 新增 3b 铁律：正文说破/承认/默认/实质交付未揭示秘密 → 同轮必须输出确切标签，宁可多打不可漏打（即使自认为"之前已经说过"）
- app.py 新增 `POST /api/story/reveal`：导演侧 reveal_secret 正规通道的开发者兜底端点（与 /api/story/debug 同族；剧情上确已揭示时由操作者调用），防模型失手卡死结局

**实测结果**：修复后 s2/s1 均由模型自发正确打标签落盘（铁律生效）；s3 经兜底端点落盘。副本「凛冬三十日」通关：q1/q2/q3 全 done、s1/s2/s3 全揭示、m1~m3 全触发、终局 **e1「向北」** 达成（endings 按声明序首个条件命中即选）。同轮全流程验证通过：完成按钮+时钟推进、任务接取/跃迁系统提示、同地点硬校验拦截、地点移动播报、AI 提议 quest active/location 落盘、冒险手册 [NOTE:] 入账。

**遗留（记入待办池，未实施；2026-09-27 复盘核实后于 8.23 处理，见下节）**：①NPC 无位置提议 key——AI 剧情带玩家移动时会把玩家单独挪走、NPC 留原地，同地点硬校验随即拦对话（"郑铁山不在这里"）；②mood_* 提议被白名单静默丢弃（flags 无此 key），与规则 1 允许 mood_* 自相矛盾，需对齐；③无 judge_mode 的任务调 invest 返回 400（行为正确，错误文案未随 400 体返回可读信息）。

**状态：已实施（2026-09-26）。py_compile 3 文件通过；真实测试由用户负责：重启 python app.py（端口 5004）+ Ctrl+F5。**

### 8.23 遗留三项落地与复盘修正（2026-09-27）

**复盘结论**：8.22 遗留三项中，②③经复核均为当时误判——
- ②"mood_* 被白名单静默丢弃"不成立：8.21b 起导演 `_apply_proposal` 在 flags 白名单**之前**已有 `mood_` 独立分支（`_set_mood`：7 档枚举校验 + 只许本人提议），提示词规则 1/2/7 亦已写明 key 与取值，链路完整，无需改代码。
- ③"invest 400 无可读错误体"不成立：`invest_time` 的文案（"单次投入只能是 15/30/60 分钟"等）一直随 400 响应体 JSON 返回，当时是测试驱动脚本未打印响应体导致"看不到"。
- ①为真实缺失，本轮实施修复（见下）。

**实施（遗留①：NPC 位置跟随提议通道）**：
- director.py 新增 `_move_npc(nid, loc, reply_nid)`：本人限定（只许 NPC 写自己的位置）+ 地点白名单校验 + 存在性校验；移动到玩家所在地点播报「{名}也来到了{地点}」（跟随语义），移到别处播报「{名}动身去了{地点}，离开了这里」；若移动者是私聊对象且移离玩家 → 私聊自动断开（与 `_move_player` 对称）。
- director.py `_apply_proposal` 新增 `npc_location_*` 解析分支（位于 mood_ 分支之后、flags 白名单之前），移动播报同时进 changes 与 notices。
- director.py `invest_time` 遗留③顺手增强：flags 型任务投入成功时追加提示「（该任务以当面沟通汇报确认为准，投入时间只推进时间。）」，防止玩家对不吃时长的任务白投时间产生困惑。
- prompts.py A3_STORY_STATE_RULE 三处扩充：规则 1 可提议 key 清单加 `NPC 位置 npc_location_*`；规则 2 取值约束（只能取可前往地点确切名称）；规则 7 新增用法 bullet——**你把玩家带去哪，同轮就必须用 npc_location_ 把自己也带过去**（给出 `[STATE: location=地热机房, npc_location_n1=地热机房]` 同轮连用范例），并警告不带自己会被判定不在场。

**实测（内存冒烟，通关存档零污染）**：跟随（移至玩家所在地→「郑铁山也来到了电台室。」）、离开（→「老周动身去了医疗帐篷，离开了这里。」）、非本人提议拒绝、非法地点拒绝四场景全过；端到端 `[STATE: location=围墙哨塔, npc_location_n3=围墙哨塔]` 同轮解析 → 玩家与老周双双抵达哨塔，播报正确。

**状态：已实施（2026-09-27）。py_compile 2 文件通过 + 冒烟 5 项通过；真实测试由用户负责：服务已重启（端口 5004），新开副本时 AI 带路即可验证跟随。**

### 8.24 群场剧场模式：单调用整场剧本（2026-09-27 立项，设计定稿）

**背景**：用户对标原神/崩铁的剧情对话节奏（每轮 1-3 句、短回合插话），要求群场对话剧场化。经调研（ElevenLabs Dialogue v3 / Gemini radio script / sessionmovie screenplay IR / turn-taking 论文 arXiv 2412.04937）与三轮讨论拍板**方案 A 剧场模式**。

**核心设计（定稿，实施以此为准）**：
- **单次调用整场剧本**：群场（无 npc_id、多 NPC 在场）聊天只调一次 LLM，输出整场"带说话人标签的剧本"——每行 `[角色名]台词`，行序即说话顺序（显式数字序号经讨论省略：行序天然即序号，序号冗余且有跳号/重号副作用）。说话顺序全权归模型自排（turn-taking），允许插话、抢话、同一 NPC 连说多句、某 NPC 沉默不接。
- **节奏硬限（结构上不可能长墙）**：每行 1-3 句；行级字数上限（约 80 字，超限整行裁掉并 log，宁缺毋滥）；一场总行数上限（约 12 行）与总字数上限（约 500 字），超限裁尾。禁 markdown（不加粗、不列表、不标题），NPC 台词用中文括号动作（沿用现有约定）。
- **群场/私聊分治（秘密隔离的关键）**：群场剧本调用的注入**一律不含任何【尚未对玩家揭示】的秘密**（谁也不当众说破，泄漏问题不存在）；私聊/单聊（带 npc_id）维持现状完全不动，8.22 秘密揭示链成果全保留。
- **导演逐行校验分发**：说话人不在场白名单 → 整行裁掉；行内 [STATE:] 标签按该行角色的权限校验分发（npc_learned_/mood_ 只许写本人=该行角色；quest/location/hp 等全局沿用现有校验；knows_secret 群场本就无）；[NOTE:] 行内可挂，归属该行角色；[PRIVATE_REQ] 若出现在某 NPC 行末则归属该 NPC（沿用现有弹窗征求玩家同意）。
- **建议说话顺序（软）**：注入保留一句"被玩家点名的 NPC 优先回应"，其余顺序自由——不硬固定轮替。
- **前端**：按行逐条渲染为对应 NPC 的对话气泡（间隔弹出，打字机节奏感），每行不渲染 markdown。
- **风格铁律（全局，单聊同步受益）**：A3 规则加"对话腔"——像人说话不像写作文，每轮 1-3 句短句、禁 markdown 长文输出、口语化。
- **兼容边界**：仅群场（多 NPC 在场且未指定 npc_id）走剧场管线；单聊/私聊链路零改动；旧副本无需迁移。

**实施切分（分两批，各批独立可测）**：第 1 批后端——director.py（剧本解析器 + 逐行校验分发 + 硬限）、prompts.py（剧本格式规则 + 对话腔铁律）、pipeline.py（群场调用路径改为单次剧本调用）；第 2 批前端——story.js/story.html 按行逐条渲染。

**实施记录（2026-09-27，两批已全部完成）**：
- prompts.py：新增 `A3_STAGE_SCRIPT_RULE`（剧本行格式/说话顺序全权自排/行 80 字全场 12 行/禁 markdown/无标签行会被丢弃/标签写在行末且视为该行角色发起）；`A3_FORMAT_AND_STYLE` 对话腔铁律改造——「日常对话像说话一样短句 1-3 句、禁整段排版，markdown 仅限知识/教程解答」（单聊同步受益；知识模式 markdown 指引保留）。
- director.py：新增 `build_stage_injection(responders)`——单次调用全场注入（各角色块=性格/目标/编号/态度/认知账本/自己发布的任务；**未揭示秘密零注入**，只注入【玩家已知】秘密；路线软引导共享；research 按玩家地点筛选）+ `A3_STAGE_SCRIPT_RULE`；新增 `apply_stage_reply(raw, responders)`——逐行解析 `[角色名]台词`（说话人不在场/无标签行裁掉+log，说话人带前后缀做包含匹配兜底），行内标签复用 `apply_reply` 按该行角色权限分发（npc_learned_/mood_/npc_location_ 只许本人=行角色），`[NOTE:]` 归属行角色入手册，`[PRIVATE_REQ]` 归属行 NPC，每行后跑 auto_settle_report 兜底；节奏硬限（行>80 裁行、>12 行裁尾、>500 字裁尾）——**行长只按剥标签后的台词算**（冒烟发现带长标签的合法行会被误裁后修复），行内 markdown 记号清洗（`**`/`__`/标题/列表前缀）。共用逻辑抽取：`_quest_lines_for`（giver 任务行）、`_player_state_block`（玩家状态摘要）；`_research_block` 的 npc_id 改可空；`apply_reply`/`_apply_flag` 新增 `allow_secrets` 参数（默认 True 单聊不受影响；剧场 False 硬禁 knows_secret_* 提议，防模型猜 key 当众说破）。
- a3_output.py：`generate` 新增 `skip_fact_check` 参数（剧场传 True——整场剧本若被反事实重生成会破坏行格式导致被逐行裁空）。
- pipeline.py `_story_chat`：**群场（非私聊且在场 ≥2 NPC）走剧场分支**——单次 a3.generate（persona=「群像剧场演出者」，各角色人设走注入段；skip_fact_check=True）→ apply_stage_reply 逐行分发 → 每行写历史（speaker 标记）+ 按行角色跑 A4（手工快照）；私聊/单聊维持原逐 NPC 循环零改动；debug 增加 stage 标记。
- story.js：`addMsg` 新增 plain 参数（剧场行纯文本渲染不进 miniMd）；doSend 对 `r.stage` 行按行序间隔 500ms 弹出（短回合节奏感），单聊/私聊立即渲染不变。
- 冒烟（内存不落盘）6 项全过：注入零泄漏（未揭示秘密/不在场 NPC 不出现）、行序自排+连说+移动跟随标签、群场秘密提议拦截+心情/认知本人限定、三重硬限、私聊请求按行归属+markdown 清洗、单聊揭示链不受影响。py_compile 4 文件 + node --check 通过。

**状态：已实施（2026-09-27）。真实测试由用户负责：重启服务后新开副本（或进入多 NPC 同处的地点）发一条群场消息即可验证剧场节奏；旧副本无需迁移，单聊/私聊行为不变。**

### 8.24b 星露任务链卡死修复：auto_meet 程序兜底 + 全局文件日志（2026-09-27，已实施）

**背景（8.27 星露寻宝记第一手数据确诊）**：磁盘剧本 quests 完整（3 任务+路线+秘密+结局全在），但运行时 flags 只有 met_npc_n2=True，q1 的 accept_flags=`met_npc_n1==true` 因 n1 漏打 met 标签而永不满足 → q1 永远 undiscovered → q2/q3 依赖链全锁。根因：met_npc 全靠 AI 打标无程序兜底 + 剧场规则第 6 条只点名移动类标签，silent omission 漏打 met。**用户决策**：状态尾块 JSON 分区（改动大）等方案暂缓，只做程序兜底方案一 + 顺带实现全局文件日志。

**实施**：
- **director.py 新增 `auto_meet(npc_ids)`**：本轮真的对玩家出过台词的 NPC，由程序直接写 `met_npc_<id>=true`（只写不退、幂等），插在 pipeline 末尾 finalize 之前，单聊/剧场两路共用。相识是「照过面」级别的确定事实，收归程序判定（同 auto_settle_report 思路）；AI 打标保留为双保险（保上限），程序兜底保下限。pipeline.py 相应加 3 行挂接。
- **app.py 全局文件日志**：logging.handlers.TimedRotatingFileHandler 挂 root logger → `story/data/logs/story.log`，按天滚动（midnight），滚动归档改名 `story_YYYY-MM-DD.log`（namer 自定义），保留 30 天，UTF-8。所有模块既有 `logging.getLogger` 输出（director 状态埋点、pipeline、werkzeug 请求）自动全部落盘，无需逐个改。
- **ai_client.py LLM 调用埋点**：chat() 成功后 `_log_call`（模型/耗时/入出字数/系统提示头 50 字识别调用方 Agent/用户消息尾 80 字，try/except 包裹不影响主流程）；chat_stream() 记流式开始。满足「每一次 LLM 调用状态改变全部记在日志」。
- 冒烟：auto_meet 写入+幂等 PASS、任务链自动解锁（q1 undiscovered→active）PASS；文件日志用真实服务验证（服务启动后 data/logs/story.log 生成，含完整时间戳）。py_compile 4 文件通过。

**状态：已实施（2026-09-27）。星露旧存档无需修复——走一趟月桂树屋跟 n1 说话，met 自动写入，q1 即解锁。测试方式：跟任意 NPC 说一句话 → `/api/story/debug` 看 met_npc 已写 + data/logs/story.log 出现 LLM 调用行。**

### 8.24c flag 注册制 + 完成标记点名：任务永不可完成的结构性修复（2026-09-30，已实施）

**背景（9/28 星露日志确诊，两层根因）**：q1「怀表指路」done_flags=`knows_secret_s2==true && milestone_q1_watch_ready==true`，两个条件全部不可达——①`milestone_q1_watch_ready` 等剧情里程碑键 builder 未预置进 flags 字典，`_apply_flag` 白名单首行「key not in self._flags」无声丢弃 AI 提议（**双门问题**：预置门 + 类型分发门都堵）；②灰羽（s2 持有者）三轮把 s2 内容说破但零打标（silent omission，met_npc 同款）。附带发现：flags 型任务 invest_time 只记账+推时钟（1296 行已有明示提示，前端 investQuest 会弹 notices），玩家 75 分钟投入非白投但易误解。**业界调研背书**（用户要求搜索）：RPG Maker 六十年铁律「flag 先注册后使用」；GroqNPC/Personica/DAQS 均为「AI 建议、系统裁决」，任务推进走程序事件——我们总架构同构，差距即本节两修复。

**实施（全部在 director.py）**：
- **flag 注册制**：新增模块级 `_FLAG_KEY_RE`（只收 knows_secret_/met_npc_/milestone_/quest_*_state 四类键，player_location 天然排除）；`_migrate_state` 末尾从 quests 的 accept/done_flags + milestones 的 trigger 提取全部键，缺失即预置（布尔键 False、任务状态键 "undiscovered"），幂等。旧存档在服务重启后首次装配导演时自动补。
- **剧情里程碑 AI 通道（第二道门）**：`__init__` 新增 `_ai_milestone_flags`（任务 accept/done 条件引用的 milestone_* 键，排除 time_ready/reported 两个专门通道键）；`_apply_flag` 加对应布尔分支。trigger-only 的 milestone_m* 仍只许程序写。
- **完成标记点名（修 2+3）**：`_quest_lines_for` 对 active 任务逐个解析 done_flags，按 8.20b 汇报纪律同款硬指令写法点名确切 key——knows_secret_*「本轮已讲秘密必须输出 [STATE: knows_secret_sX=true]」、milestone_*「所述事实发生必须输出 [STATE: milestone_qX_xxx=true]」。undiscovered 任务不点名（防剧透）。
- 冒烟（假身份 smoke_824c 借星露剧本，不碰真实档，跑完已删）：①补预置键 PASS ②n1 注入行含两个确切 key 点名 PASS ③AI 提议→白名单放行→finalize 判定 q1=completable PASS。py_compile 通过，服务已重启探活 200。

**状态：已实施（2026-09-30）。星露存档续玩即可：跟灰羽聊到把灯圈/点名秘密说破（AI 打标 knows_secret_s2）+ 剧情让怀表就绪（打标 milestone_q1_watch_ready），q1 自动 completable → 点「完成」拿月桂叶奖励。任务面板「完成」按钮出现即验证成功。**

**9/30 续修（全局日志确诊，两处）**：①「沃雅妮莎」是玩家用户名（player_persona.name），被 A3 反数据库校验误判编造→触发重生成（重生成保留了 STATE 标签未丢，s2 实际写入成功）。修复：a3_output.py 新增 `_player_name()`，白名单（2.5 玩家角色名行）+ 程序级豁免（missing 过滤双向包含）双防线；②`milestone_q1_watch_ready` 仍 false——AI 把「怀表走满三刻」当过去发生的事不肯补打标。修复：director 点名措辞改为「该事实已发生（包括此前轮次已发生、本轮只是再次确认）也必须原样输出标签」。py_compile 通过、玩家名豁免单测通过（「沃雅妮莎的篮子」豁免/「露米恩」不豁免）、服务重启探活 200。**续玩验证：跟灰羽再说一句怀表相关的话 → AI 补打 watch_ready 标 → q1 completable → 完成按钮出现。**

**9/30 批次三（8.24d：打标主通道移至 A4 分区输出 + 双时间修复）**：用户拍板「A3 不动，分区给 A4」。①A4 玩家状态调用升级为「剧情状态总分析」（复用同一次调用零新增 API）：prompts.py 新增 `A4_STORY_STATE_SYSTEM`（分区输出：主区=玩家状态整块 + `===STATE===` 分隔行 + 一行 JSON 尾块 `{"flags":[...],"attitude":{},"npc_location":{}}`，归并数组防 silent omission）；a4_manager.py `_update_player_state(snapshot, story_brief)` 拆两区（主区照旧写玩家状态记忆，尾块 json.loads 容错解析后放 stats["proposals"]）。②director.py 新增 `state_brief_for_ai()`（可用键清单：任务条件点名确切 key + 秘密内容/持有人 + 里程碑/汇报键 + 位置事实——清单即白名单的 AI 可见面）与 `apply_ai_proposals(payload, allow_secrets)`（flags 走 _apply_flag 同一白名单、attitude 走 _adjust_attitude、npc_location 走 _move_npc 本人权限；编造键静默丢弃）。③pipeline.py `_run_a4` 扩展（story_brief/director/allow_secret_proposals 三参）：单聊 allow=True、剧场每 responder allow=False（与行内标签同规则）；普通身份完全不传新参，行为不变。④双时间修复：A3 generate 新增 `time_override`（副本身份传 `d.clock_text()`，替代 build_time_context 现实时间）；A4 摘要 snapshot["time_context"]="第X天 HH:MM" 替代真实日期——NPC 只看一套游戏时间。⑤A3 的行内 [STATE:] 标签保留为双保险通道不删。冒烟 ALL PASS（关键链路：A4 提议 watch_ready=true → finalize 判定 q1=completable + 里程碑 m3 触发）；服务重启 200。

**9/30 批次二（反事实校验重构 + 叙事节奏，行业调研后拍板）**：反事实校验从「词存在性拦截」转向行业惯例 approved-lore 模式（生成期 critic 把关、运行期信任产物）。①改C（a3_output.py `_verify_and_revise`）：校验前先剥 `[STATE:]/[NOTE:]/[PRIVATE_REQ]` 标签（模块级 `_STATE_TAG_RE`），校验/重生成只针对正文，重生成输出再剥一次后把原标签程序拼回——切断「重生成弄丢任务链标记」链路；②改A（a3_output.py `_story_entities`）：剧本 NPC/地点（兼容字符串数组与 dict 数组两种形态）/任务名/玩家名全部注入校验白名单 + 程序级信任豁免（双向包含，扩展说法如「露米恩的篮子」也放行）——星露实测 12 实体上户口，陌生词（赛博坦大厦）仍拦截；③BUILDER_STORY_SYSTEM：任务序列三幕排布（激励事件→中点反转（假胜利/假失败/重大揭示）→收伏笔，禁流水账）+ 第 7 条题材张力优先（背叛/复仇/守护/误会内核）；④CRITIC_STORY_SYSTEM 第 3 维扩为「目标冲突与三幕节奏」（反接做交流水账）；⑤A3_FORMAT_AND_STYLE 新增第 7 条悬念密度铁律（信息分次释放、秘密先感受后真相、感官细节代抽象形容）。冒烟 ALL PASS，服务重启 200。此批次后词级校验误拦副本实体的问题应绝迹；「风控分级深查（FACTOR 式）」与「critic 原子事实审查（FActScore 式）」列为后续可选项未实施。

**10/01 批次四（8.24e：A4 尾块解析加固 + 星露存档解锁）**：10/01 17:52-17:55 三轮对话全局日志确诊：A4 状态总分析两次报「===STATE=== 尾块不是合法 JSON，状态提议丢弃」（轮1怀表认主+轮3月桂叶说破的事实 A4 主区都正确记入了玩家状态记忆，但 JSON 尾块解析失败提议全丢）；且星露存档 state.flags 被旧导演对象回写过（9/28 auto_meet 写入的 met_npc_n1/n3、9/30 灰羽说破的 knows_secret_s2 全部丢失，milestone_q1_watch_ready 键缺失）。用户授权「先帮我改一下故事文件」。①改档：story_20260927_003845.json flags 补写 met_npc_n1/n3=true、knows_secret_s2=true、milestone_q1_watch_ready=true（全部有日志证据；n2 未见面保持 false；quest_q1_state 保持 undiscovered——下轮 finalize 自动连跳 active→completable，m2/m3 里程碑旁白也会由程序补播，无叙事损失），备份 .bak_20261001；②a4_manager.py 新增 `_parse_tail_json()` 静态方法：逐级降级解析（原文→剥 ``` 围栏→截取 {...} 块→全角引号/尾逗号清洗），解析失败时日志打印尾块前 200 字便于排查；容错单测 9/9 通过（围栏/全角引号/尾逗号/前后说明文字/非 JSON/空串）。py_compile 通过，服务重启探活 200。**续玩验证：任意跟灰羽说一句 → 任务面板出现 q1 且显示「可完成」→ 点「完成」拿月桂叶奖励。**

**10/03 批次五（8.24f：A4 打标协议化——tool calling 替代散文 JSON 尾块）**：源于模组化架构讨论（§8.19 第 7 条）：用户洞察「模组调用跟 skill 调用差不多，任务/标签都该用这个逻辑」。工具调用（function calling）把「编 key、格式坏、忘打标」这类幻觉交给协议消灭（清单注入→JSON Schema 强校验→校验失败协议层重试），白名单兜底仍在（双保险）。实施四文件：①ai_client.py 新增 `chat_with_tools()`（OpenAI 兼容 tools 透传 + tool_calls 解析返回 `(content, calls)`，重试策略与 chat() 一致；现有 chat()/chat_stream() 一行未动，普通身份零影响）；②director.py 新增 `allowed_flag_keys()`（bool 键=met/秘密/里程碑/汇报键 + quest_state 键 + 地点白名单——与 _apply_flag 白名单同源，作为工具参数 enum 的数据源，编造 key 在枚举层提交不进去）；③a4_manager.py 新增模块级 `_build_state_tool(hints)`（report_state 工具 schema：player_state/flags[{key:enum,value}]/attitude/npc_location 四参数）与 `_normalize_tool_args()`（flags 统一转 "key=value" 字符串形态，下游 apply_ai_proposals 零改动）；`_update_player_state` story 模式改三级兜底：工具通道（player_state 也走工具参数——冒烟实测 DeepSeek 有 tool_calls 时 content 为空，正文不可依赖）→ 正文 ===STATE=== 文本兜底（8.24e 容错保留）→ chat_with_tools 异常时降级普通 chat（最坏=8.24d 现状）；pipeline.py `_run_a4` 把 `director.allowed_flag_keys()` 传入 manage()。④prompts.py `A4_STORY_STATE_SYSTEM` 改为工具调用纪律（全部产物走 report_state；判定纪律保留；第 5 条保留降级格式说明）。验证：真实 API 冒烟复刻 9/28 星露卡死场景——**watch_ready + s2 两个关键提议同轮全中、键全落枚举内、纯寒暄 flags 空（对比 8.24d 散文尾块同场景两次解析失败全丢）**；FakeClient 单测 4/4（工具通道/文本兜底/降级路径/普通身份不变）。py_compile 通过、服务重启探活 200。

**10/03 续修（8.24g：输出预算截断修复）**：用户续玩两轮日志确诊 8.24f 首轮实况——轮2 工具通道全通（提议=有、玩家状态成功覆盖更新），轮1 `出=512` 恰好打满 max_tokens：player_state 被模型塞入大量剧情细节（断绳/笛子/湖心灯原话）导致 JSON 拦腰截断，提议丢弃。修复：a4_manager.py 两处 max_tokens 512→1024；prompts.py 与工具 schema 的 player_state 描述同步加「只记状态性事实（持有物/身体状态/所在位置/已知信息/约定），不复述剧情经过」控长治本。服务重启 200。

**10/03 续修二（8.24h：双文件结构确诊——8.24e 改档改错了文件）**：用户报告任务面板无完成按钮。诊断发现剧本 JSON（story_*.json）里的 state.flags 只是**生成时的初始快照**，运行时状态存在独立文件 `data/story/{identity}_state.json`（store.py `_state_path`）——8.24e 改档改的是快照，真身没动，且当时"q1=undiscovered"的诊断依据也是错的（读的快照）。运行时文件实况：met_npc_n1/n2/n3=true（auto_meet 兜底全部生效）、knows_secret_s2=true、quest_q1_state=active，唯一缺口 milestone_q1_watch_ready=false（A3 时代漏打+8.24d 解析失败期间从未写上）。修复：①运行时 _state.json 补 watch_ready=true（9/30+10/03 日志双重证据：怀表已认主走满三刻）→ 下一轮 finalize q1→completable；②剧本快照用 .bak_20261001 回滚为初始值（防新存档一出生带已解锁状态）。无需重启（8.20c 导演每轮从磁盘重建）。**教训：改存档状态一律改 _state.json，剧本 JSON 的 state 只是快照。**

**10/03 批次六（8.24i：副本对话历史回放——补功能非修 bug）**：用户问"重启服务器后再进副本上下文不显示了，是没保存还是没显示"。查实：历史一直在保存（data/memory/{identity}/history.json，星露 72 条，NPC 侧 A1 每轮取最近 6 条喂 AI 完好），但 **story/ 前端从未实现"重新进入时回放历史"**——story.js 零 history 代码、16 个 /api/story/* 端点无历史端点（主系统"页面打开全量加载历史供审阅"的约定未跟到 story/）。实施：①app.py 新增 `_story_history_payload(identity, limit=200)`（直读 history.json，不装配 Director；NPC id→名字映射直接读剧本；role=user/npc 精简视图含 private 标记；旧格式 role=assistant 兼容），挂进 /api/story/enter 与 /api/story/current 两个返回体（F5 刷新恢复同效）；②story.js `enterPlay` 清空消息区后回放 history（复用 addMsg，新增第 6 参 noScroll——逐条渲染不滚动，回放完一次性 scrollTop 到底，防 72 条连滚卡顿）；NPC 开场白仍在历史之后渲染（语义=重新进场时 NPC 再打招呼）；③story.html 内嵌脚本全量重同步（84621 字符，同步日期标记）。验证：探活 200、/api/story/current 返回 72 条（首条 user 正常、末条 npc name=灰羽）。**老坑再次验证：改 story.js 必须同步 story.html 内嵌脚本。**

**10/05 批次七（8.25：删前端游览路线 + 生成前检索建议）**：①用户拍板"游览路线不能直接暴露给玩家，玩家应通过一个个任务自由探索剧情"——删除任务面板中的路线渲染块（story.js renderQuests，route-head/route-item 区块），导航按钮 title 改「任务面板」；routes 仍注入 AI（build_injection 软引导，台词级带路不算管报），debug 开发者端点保留。②用户要求"陌生世界观（战锤40K 类）除非特别有把握强烈建议搜索"——现状是搜索全靠前端 researchSwitch 手动开（默认关），无 AI 判定。新增：prompts.py `RESEARCH_ADVICE_SYSTEM`（现实 IP/真实历史/专业领域→strong；自创/日常→optional；输出 JSON 一行）+ app.py `POST /api/story/research/advice`（轻量判定：temperature 0.1 / max_tokens 120，失败静默降级 optional 不打断生成）+ story.js startGenerate 未开检索时先调判定，strong 则 confirm 弹窗「确定直接生成 / 取消返回开检索」。实测：战锤40K→strong、便利店日常→optional。③**信息扩散机制搁置**：用户提出"完成任务后其他 NPC 收不到消息、NPC 像信息没更新"（湖边完成任务别处 NPC 不知道），且要求"新信息推最前面"，但随即自己指出矛盾——"既要有不在场不知道的感觉，又要对话流畅"方案未想清，明确先不做（news 广播方向被否，勿再提）。8.24f 工具调用实测生效：用户确认"换成工具调用之后一下就可以结束任务了"。

**10/05 批次八（8.25b：检索契约加专名规范表——"给 AI 看的资料"定位改造）**：用户指出外源检索资料"是给造城 AI 吃的，不是给人看的报告"，要求调研后按业界方案优化。调研（Knows 论文 arXiv 2604.17309 + context engineering 共识）证实：结构化事实卡 vs 人读文档，弱模型准确率 +30pp、token 省 29-86%。**已有部分保留不动**（主题分组、原子事实、uncertain 通道、relations——业界同款），只补两件：①**entities 专名规范表**（治谐音变体——沃雅妮莎教训）：检索提示词要求先整理实体表（规范写法/类型/一句话定义），全文专名一律用规范写法；契约 JSON 加可选 `entities: [{name,type,note}]` 区块（validate_research 放行 + 非法报错，旧格式兼容）；format_research 把实体表渲染在注入文本最前且**不参与主题截断**（截掉它防变体就失去意义），acc 预算计入实体表。②RESEARCH_INJECT 加铁律："专名写法以规范表为准，表里没收录的相似名称（谐音、别写、自行变体）一律不用"。改动文件：agent/prompts.py（RESEARCH_PROMPT_TEMPLATE 硬性要求 4→5 条 + 契约 JSON + RESEARCH_INJECT）、story/schema.py（validate_research / format_research）、mcp_research_bridge.py（submit 工具 docstring 契约同步）。单测 8/8（旧格式兼容/新格式通过/三种非法 entities 拦截/实体表最前渲染/无实体不渲染/截断保留实体表）；服务已重启探活 200。**MCP 桥安装法**：`pip install mcp` + 外部工具 MCP 配置（command=python.exe，args=[mcp_research_bridge.py 绝对路径]），端口非 5004 时加 env STORY_BASE_URL。待办衔接：外部 AI 输出上限若实测撑不满 6000 字契约 → 给桥加分片提交（多次 append、AI 显式收口）；背包系统（8.26 计划，动态注册制）与生存规则开关待开工。

### 8.26 设计：背包系统 + 副本类型开关 + 信息扩散（背包同步）（2026-10-05 用户拍板，已实施）

**总原则**：程序是真身（AI 记忆忘没忘无所谓）；AI 提议、导演裁决；玩家只读不操作。

#### A. 背包系统（Inventory）

- **数据结构**（state 下，随存档持久化）：`inventory: {owner_id: {item_id: count}}`——每实体一个背包，len() 即格数；物品定义（name/desc/type）在注册表，背包只存 id+数量
- **双层注册**（业界动态注册制，8.25b 调研定调）：
  - **剧情物品**：造城时 BUILDER 产出 items 表（id/名字/类型/描述/key 标记），锁死注册——任务条件/结局引用的物品只能是这档
  - **自由物品**：AI 首次提议时动态注册（first-write-wins）——带名字/类型/一句话描述 → 程序校验上户口 → 之后所有轮次引用同一 id；AI 自由发挥但造过就留痕
- **上限**：每背包 **60 格**（不同 item id 数，防数据溢出）；同 id **堆叠无上限**
- **玩家只读**：前端手册展示背包（物品名×数量），无拖拽/丢弃/转移 UI
- **导演是唯一搬运工**：所有物品变动走导演白名单裁决——AI 只能提议，不能直接改
- **A4 工具调用扩展**（8.24f 通道，复用 report_state 同一次调用零新增 API）：
  - `transfer_item {from, to, item_id, count}`——谁把什么给谁（赠予/交出/搜出）；from 扣 to 加
  - `take_item {owner, item_id, count}`——消耗（喝药水/交任务物品/毁尸灭迹）
  - 动态注册物品在工具参数里带 note 字段（名字+类型+描述），未注册的剧情物品 id 直接拒绝
- **任务判定扩展**：条件表达式支持 `has_item_<id>>=N`；任务加 `reward_items` 字段，点「完成」程序直接入包（月桂叶教训的根治——不依赖 AI）
- **60 格满的行为**：拒绝入库，A4 下轮收到错误提示，可让 NPC 台词演"背包装不下了"

#### B. 副本类型开关（生成时选择）

- 生成界面新增选项：**剧情型（默认）/ 生存型**，存剧本 `rules.survival: true/false`
- **生存型才启用**：hp/饥饿数值（state.player）、饥饿扣血（finalize 扫）、死亡判定、濒死注入（NPC 言行配合紧张感）
- **AI 不可改 hp**（10/05 用户修正）：扣血是战斗系统的活，而战斗 = 模组挂载（T-006）的范畴——第一版 hp 变更只走程序（饥饿扣血），AI 提议通道预留给战斗模组一并实现
- **死亡 = 任意注册实体都可死亡**：NPC 死亡 = 剧情事实（实体状态标记，AI 提议+导演裁决，剧情继续演）；玩家死亡 = 全部终局（death ending → 终局遮罩+「再来一局」，第一版不做中途读档）
- **剧情型**：以上全部不注入不显示不判定（星露类副本零感知，代码路径完全隔离）
- BUILDER 提示词按类型微调（生存型出物资/危险/环境设定）

#### C. 信息扩散 = 背包同步（用户方案，替代被否的 news 广播）

- **每实体一个 inventory_sync 开关（默认开）**：
  - 开：该实体背包快照对其他实体实时可见（NPC 知道你身上有什么——拿到月桂叶，NPC 提得到）
  - 关：**冻结**——其他实体只能看到关闭瞬间的最后快照（sync_snapshot），之后不再更新；自己重新打开 → 恢复实时
- **谁控制**（10/05 用户修正）：玩家的同步开关由**玩家**在前端自己点；NPC 的同步开关由**导演**控制——AI 提议（藏东西/保密剧情）→ 导演白名单裁决 → 程序写入
- **覆盖范围声明**：这是**物品信息**的扩散通道，不是对话/事件信息的扩散——后者（"不在场不知道 vs 对话流畅"矛盾）继续搁置
- 导演注入按此组装：对每个 NPC 注入"在场各实体同步中的背包 + 你记得的已冻结快照"

**开工顺序建议**：A（背包+工具+任务扩展）→ B（类型开关）→ C（同步）。A 是通用基础，B/C 依赖 A 的数据结构。

**实施记录（2026-10-05，已上线）**：
- **director.py**：`_migrate_state` 补 4 字段（`item_registry` 从剧本 items 表装载 / `inventory` / `inventory_sync` / `inventory_snapshot`）+ 预置 `inventory_sync_<nid>=True`（player 不预置——只许玩家自己点）；新方法组 `register_item`（中文短名动态注册，已有 id 不覆盖）/`bag`/`sync_on`/`_set_sync`（关→拍快照、开→清快照）/`visible_bag`/`_inventory_block`（注入块，关同步的标注"记忆中的旧数据"）/`_apply_transfer/_apply_gain/_apply_take`/`grant_rewards`/`set_player_sync`；`_apply_flag` 开头拦截 `inventory_sync_` 键（player 拒绝、NPC 走 `_set_sync`）；`apply_ai_proposals` 加 items 通道（op: gain/transfer/take）；`state_brief_for_ai` 末尾注入注册表清单+各实体真实背包（上帝视角）+提议格式；`build_injection` 加生存濒死提示（HP≤30/饥饿≤20）；`finalize` 步骤 2.5 生存结算（tick 保护 `survival_last_tick` 记 `day|time`，饥饿-1/时刻、饿透 HP-5、HP≤0→ended+death）；`complete_quest` 发放 `reward_items`；`get_public_state` 加 `rules/inventory/inventory_slots/inventory_sync`
- **a4_manager.py**：`_build_state_tool` 加 items 数组参数（op enum gain/transfer/take + item/count/owner/from/to/note，required 含 items）；`_normalize_tool_args` 透传
- **prompts.py**：A4_STORY_STATE_SYSTEM 加 items 说明+判定纪律（inventory_sync 键：NPC 藏东西才提议 false）；BUILDER_STORY_SYSTEM 加 items 表字段（2~5 个关键物品、中文短名 id）+ quests 的 reward_items；FEW_SHOT 加半块虎符/密写账册示例
- **builder.py**：build_story 收尾写 `story["rules"] = {"survival": ...}`（生成设置单选）
- **app.py**：新端点 `POST /api/story/inventory/sync`（玩家开关，set_player_sync + save）
- **前端（story.js + story.html 内嵌脚本已全量同步，同步标记 2026-10-05）**：生成页高级选项加副本类型 radio（剧情型默认/生存型）；手册重写=身份卡→生存状态条（rules.survival 时 HP/饱食条）→MC 风格背包网格（8 列，暗格=已填+数量角标+key 金边，浅格=空，格子数 `max(24, ceil(occ/8)*8+8)` 封顶 60）→占用统计+📡背包同步按钮（`toggleBagSync` 走新端点）；任务奖励点「完成」自动入包

**8.26d NPC 死亡通道（2026-10-06 实施，用户拍板"就按你说的来做"）**：
- 设计：死亡 = 任意注册实体可死；NPC 死亡 = 剧情事实（AI 提议 + 导演裁决，剧情继续演）；玩家死亡走生存结算（HP≤0→death 终局），不在此通道；剧情型/生存型都可用
- **director.py**：`is_dead(nid)`（查 flags `dead_<nid>`）；`presence_ids()` 过滤死者（单点收口——群场响应者、同处名单、私聊校验全部自动生效）；`_kill_npc(nid)`（名册硬校验 + 只进不退 + 私聊对象死亡自动断开 + 固定旁白「☠️ X死了。（该角色已退出剧情）」）；A3 行内标签与 A4 JSON 提议的 `dead_` 前缀均走专用裁决（不进通用 `_apply_flag` 白名单，防绕过）；`allowed_flag_keys` 暴露 `dead_<nid>` 进 A4 工具枚举（协议投影）；`state_brief_for_ai` 加死亡提议纪律（确切 key + 慎用 + 玩家死亡禁提议）；`_player_state_block` 加"已死亡的角色"行（单聊/群场共用，可回忆不可出场）；`_inventory_block` 跳过死者（随身物品不再作为同步实况注入）；`set_private` 死者拒绝私聊；`get_public_state` NPC 加 `dead` 字段且死者 `available=false`
- **pipeline.py**：点名已死 NPC → 播报「（X 已经死了，不会再回应）」
- **prompts.py**：A4 判定纪律第 6 条：仅当本轮剧情明确该 NPC 已死亡（被杀/重伤不治/明确死亡描写）才提议，不可逆慎用
- **前端（story.js + story.html 已同步）**：死亡 chip = ☠ 前缀 + 暗红底 + 删除线（`.chip.dead`），点击/私聊均拦截并播报旁白；死者 available=false 自动不渲染开场白
- 单测通过：正常死亡生效/重复死亡拒绝/非法 id 拒绝/A4 通道 routing/枚举暴露/通用 `_apply_flag` 拒绝 dead_ 键

### 8.27 任务树 + 重新开始副本（2026-10-06 用户拍板，已实施）

**实施记录（2026-10-06）**：
- **schema.py**：QUEST_STATES 增 `locked`（只许程序写）；quest 校验新增 type(push/branch)/next(str)/branch(分支表≥2项)；branch 型 done_flags 改为可选；routes 校验改宽容（有则校验、无则通过，正式退役）；new_state 为 branch 型任务预置 `choice_{qid}=""`（空串=未选）
- **director.py**：①`_apply_choice` 专用裁决通道（branch 表校验 + 选择窗口=active/completable + first-write-wins 不可逆），A3 行内标签（apply_reply）与 A4 提议（apply_ai_proposals）双入口挂接，参照 dead_ 模式不进通用 _apply_flag；②finalize 中 branch 型任务完成判定=choice_qX 非空（程序判定，不依赖模型写 done_flags）；③complete_quest 挂 `_route_branches`：branch 任务 done 瞬间，未选分支置 locked（未记录选择则全部锁定，绝不双线齐开），选中分支照常走 accept 激活（BUILDER 会写 `accept_flags="choice_父任务id==本任务id"`，选择记录即激活，先于点击完成出现）；④get_public_state 过滤 locked（前端零改动）；⑤state_brief_for_ai 注入分支走向描述+已选定提示+选择纪律（只许提议一次）；⑥allowed_flag_keys 新增 choice_keys 投影
- **a4_manager.py**：report_state 工具 key 枚举并入 choice_* 键；value 描述注明 choice_* 用分支任务 id（值合法性由导演裁决）
- **prompts.py**：BUILDER_STORY_SYSTEM 重写任务规则（主线 push 链 3~5 环带 next、至少 1 个 branch 分叉、分支任务完整定义且 accept_flags 用 choice_键、未选分支玩家永远玩不到）；few-shot 范例改为含分支树的完整示例（q2=branch 分叉 → q3a/q3b）；routes 从结构与范例中移除；CRITIC 第 6 维改为任务树审稿
- **app.py**：~~`POST /api/story/restart` 备份清空式重置~~ → 已被 8.27b 并行副本模式取代（见下）
- **前端 story.js + story.html（已同步）**：暂停菜单加「🔄 重新开始此副本」（btn-danger 样式+风险说明文案）→ confirm 二次确认 → 调 restart → 重新 enter → 提示重新开始
- **验证**：py_compile 5 文件通过、node --check 通过、临时单测 24 项全过（choice 裁决/first-write-wins/分支激活/locked 路由/公开视图过滤/协议投影/state_brief 注入/旧扁平副本兼容/重开归零，脚本已删）、5004 探活 200、restart 路由 404 校验通过；星露等旧副本无 type 字段按现状扁平跑

**用户要点**：任务结构太扁平、游览路线只是一句话没有意义 → 升级为任务树；好几个分支，根据玩家选择进入不同分支，有的分支玩家玩不到；两种任务——推进型（push，只把剧情推到下一环）与分支型（branch，需要玩家做选择或根据表现，后面有多个分支）；另优化存档逻辑，设置界面加「重新开始此副本」（需要副本"刚造完成"的状态）。

### 8.27b 用户修正：任务编排自由化 + 并行副本（2026-10-06，已实施）

**用户原话要点**：①任务树不是强制要求——AI 的分支是一套自由的、属于 AI 的任务编辑系统，运行时按写好的任务来（已基本实现），AI 提前构建目录树，**扁平结构和树形结构都可以**，不特别要求哪个；②「重新开始此副本」要像**我的世界重新生成世界**——再开一个**并行的副本**，不是把数据直接抹了。

**实施记录**：
- **prompts.py**：BUILDER 任务规则从「至少 1 个 branch 分叉」的硬性要求放松为「任务结构由你自由编排：扁平推进链或分支目录树都合法，按剧情需要选择」（branch 标注为可选，仅剧情确实需要分叉时用）；CRITIC 第 6 维同步改为「不强制分叉，若设计了 branch 则审分叉质量」
- **app.py** `POST /api/story/restart` 重写为并行副本模式（类我的世界「重新生成世界」）：原副本**原封不动**；复制其剧本 JSON（story_id 改为新 id、title 加「（重开N-1）」便于存档列表区分，封面/背景图随剧本 JSON 一并复制）→ 新 identity=`{原id}_r{N}`（N 从 2 递增避让剧本与身份注册表冲突）→ 运行时状态用 `new_state(new_story)` 重建（与 build 落盘的初始 state 同源，即「刚造完成」状态，记忆目录自然为空）→ 注册新副本身份（条目格式同 builder._register_identity，persona 继承原副本）→ `set_current(新id)`；返回 `{identity: 新id}`，前端直接进入新副本
- **前端 story.js + story.html（已同步）**：按钮改「🔄 重新开始（开并行副本）」，confirm/说明文案改为并行模式表述；restart 成功后 `enterSave(resp.identity)` 进入新副本
- **验证**：py_compile（app/prompts）+ node --check 通过、同步标记确认、旧服务进程清理后重启 5004 探活 200
- **注意**：新副本的 game_db 不复制（director 不依赖 GameDB，人物卡只在原副本的 game_db 里）；旧版「备份清空式」restart 已彻底移除，不再有 data/archive 备份逻辑

### 9.1 投入时间事件化（15 分钟分段演出）（2026-10-07）

**用户要点**：调时不能只是干巴巴扣账——15 分钟片段之间要触发事件。样本：「两把锤子的沉默」的军队徽章应该是投入 15 分钟后**在草原真的被发现**（旁白演出+徽章进背包），而不是玩家靠任务提示脑补。以后投入时间只能以 15 分钟为单位。
**拍板决策**：①事件来源=混合（BUILDER 预埋 time_events 为主，A4 可提议补充事件）②新副本（任务带 time_events）完成不再扣时（时间已分段流逝），旧副本照旧扣差额。
**数据结构**：任务新增可选 `time_events: [{at_minute: 15, text: "旁白文案", give_item: "物品名", item_note: "一句话描述", set_flags: {键: true}}]`——at_minute 为任务内累计投入分钟触点（15 的倍数），事件只触发一次（state.quest_events_fired 记账）。
**实施清单**：①schema 校验 time_events ②director.invest_time 只收 15 分钟+事件触发引擎（官方直写背包/flags，无需白名单）③complete_quest 有 time_events 不扣时 ④get_public_state 任务条目加 has_time_events ⑤A4 事件提议通道（payload.events：text+set_flags 走白名单+give_item）⑥BUILDER 提示词+few-shot ⑦前端单按钮「+15 分钟」+完成文案 ⑧同步 story.html+重启

**实施记录（2026-10-07 全部完成）**：
- ①**schema.py**（235-263 行）：time_events 必须是数组；at_minute 为 15 的正整数倍且不重复；text 非空；give_item 未在 items 注册必须带 item_note；set_flags 必须是对象
- ②**director.py** 六处：invest_time（1582 行）改 `if minutes != 15:` 拒绝非 15 分钟投入；finalize 前调 `self._fire_time_events(q, after, notices)`；`_fire_time_events`（1553-1591 行）引擎——`state.quest_events_fired` 记账只触发一次，跨过 at_minute 旁白 `🔎 {text}`、give_item 走 `_apply_gain`（未注册物品带 item_note 动态注册，失败 warning 跳过）、set_flags 直写 flags；complete_quest（1677-1683 行）`q.get("time_events")` 存在则 minutes=0（防双扣）；get_public_state 任务条目加 `has_time_events`；allowed_flag_keys 把剧本 time_events 的 set_flags 键并入 bool_keys（A4 补充事件可引用官方键）；apply_ai_proposals（2188-2211 行）新增 events 通道——text 必填、give_item 被拒则整个事件跳过+warning、set_flags 仅白名单键、文案进 `_quest_notices`（🔎 前缀）
- ⑤**a4_manager.py**：report_state 工具 schema 新增 events 属性（text 必填/give_item/item_note/set_flags，146-171 行），required 加 "events"；`_sanitize` 透传 events 列表（198-199 行）
- ⑥**prompts.py**：BUILDER 规则新增 time_events bullet（505-512 行，时长≥30 分钟任务预埋 1~2 个）；few-shot q1「夜探地窖」60 分钟任务加 time_events 样例（577-584 行：15 分钟发现铜钥匙+give_item、45 分钟听见头顶脚步+set_flags）
- **director.py** A4 注入协议补 events 提议格式说明（2083-2086 行，紧跟背包提议格式行）
- ⑦**story.js**：三档投入按钮改单个「+15 分钟」（681-683 行）；提示语按 has_time_events 分档（「每次投入 15 分钟，途中可能遇到事件；时间随投入流逝，完成时不再扣时」）；完成按钮 remain 计算按 has_time_events 免扣时（703 行）
- ⑧**story.html**：临时脚本全量同步+同步标记更新（2026-10-07 9.1），脚本用完即删；抽查确认内嵌块含 has_time_events、旧 data-min=30/60 已消失
- **验证**：py_compile（schema/director/prompts/a4_manager/app）+ node --check 全过；旧服务进程清理后重启 5004 探活 200
- **测试路径**：新生成的副本才带 time_events（BUILDER 对时长≥30 分钟任务预埋）；旧副本投入只推时钟、完成照旧扣差额。实测：接带 time_events 任务 → 点「+15 分钟」→ 应看到 🔎 旁白事件 + 物品入包（🎒）；A4 亦可在聊天轮次中补充提议事件（走白名单裁决）

### 9.2 玩家交流能力：交流动作 + 双手槽 + 心理防线（2026-10-10）

**问题**：「逼老陶说出军队真相」类任务需要玩家有谈判能力，但玩家是来玩游戏的——原神式选项只是氛围（被否），纯自由文本又卡不会表达的玩家。参照 UT（交流=选动作+攒筹码的解谜）。
**拍板**（AskUserQuestion 两次）：①路线=UT 式行动菜单（按钮=保底通道，自由文本=高阶通道，两路汇到导演同一套 flags，判定权照旧在导演）；②按钮手感=**填充舞台指令到输入框**（可改可发，扮演感留在玩家手里）；③用户追加**双手槽**（MC 主手/副手简化版：就两个槽，玩家从背包把物品拿在手上，手上东西 NPC 当面可见=出示；不出示按钮，出示就是空间机制；通用字段 state.hands 以后 NPC 也能用）。
**数据结构**：`state.hands = {owner: {main: item_id|None, off: item_id|None}}`（按实体存，玩家先启用）；`state.stance = {npc_id: int(-3~3)}`（心理防线，负=抗拒正=松动，缺省 0）。
**核心规则**（效果纪律写在 A4 工具 schema 与 A4_STORY_STATE_SYSTEM）：有筹码在手（手上亮着的物品）+施压 → 防线+2 好感-1；空手硬施压 → 防线不动 好感-2（玩家会学到「这人不能硬来」）；套话需好感>0 → +1；示好 → 好感+2 防线+0~+1；关键证据拿手上被看到并当面质问 → +3。**防线跨 +2 → 程序直写该 NPC 持有的未揭示秘密 knows_secret=true**（保底必达，不依赖 AI 打标）+旁白「💬 XX的心理防线撑不住了……」。
**实施记录（2026-10-10 全部完成）**：
- **director.py**：state 初始化 hands/stance（259-268 行）；`_stance/_stance_tone`（1193-1211 行，五档语气文案）；`set_hand/hands_info/_hands/_clear_hand_refs`（2026-2072 行——同槽占用拒绝、物品必须在包里、transfer/take 数量归零自动清手上引用）；`_inventory_block` 首行注入「玩家双手（当面可见，实况）」（不受背包同步开关影响，1927-1941 行）；build_injection 态度行后加防线档位行（544-550 行，A3 台词语气匹配档位、禁报数值）；apply_ai_proposals 加 stance 通道（单轮 ±3 钳制、累计 -3~3、跨 2 解锁秘密+旁白，2263-2292 行）；state_brief_for_ai 加各 NPC 防线当前值（2120-2126 行）；get_public_state 加 hands（1834 行）；A4 注入协议加 stance 提议格式行（2186-2189 行）
- **a4_manager.py**：report_state 工具 schema 加 stance 参数（含完整效果纪律，103-110 行）；required 加 stance（181 行）；_sanitize 透传（202-203 行）
- **prompts.py**：A4_STORY_STATE_SYSTEM 加 stance 参数说明+效果纪律（354-358 行）、无变化空值清单补 stance/events（372 行）；A3_STORY_STATE_RULE 加第 8 条舞台指令铁律（753-757 行——必须按指令演出不可无视不可替换、手上物品可自然注意、深层后果导演结算）
- **app.py**：POST /api/story/hands（932-943 行，{slot, item|null}，拿起/放回）
- **story.js + story.html（已同步，标记 2026-10-10）**：手册背包区加双手槽行+「把X放到：主手/副手」待拿起条（点背包格→选槽，点手槽放回，/api/story/hands 调用）；聊天框上方交流动作条（💬套话 ⚡施压 🤝示好 🤐沉默 chips，点击按当前对话对象组装「（对老陶：施压）」填入输入框，可改可发；群场玩家可自行改名字）；CSS 加 .act-bar/.act-chip/.hand-* 系列
- **验证**：py_compile + node --check 全过；重启 5004 探活 200
- **测试路径**：进副本 → 手册背包点物品 → 拿到主手 → 回聊天发消息（NPC 台词应自然注意到你手上的东西）→ 点「⚡ 施压」发指令（A4 应提议 stance+2/好感-1，日志见「[A4状态提议] 防线」）→ 攒到防线 2 出现「💬 心理防线撑不住了」旁白、任务面板 knows_secret 相关条件达成。旧副本无需重生成（stance/hands 是运行时状态，_ensure 自动补默认键）

### 9.3 纸条系统：关键信息物品化（2026-10-10）

**问题**：承诺/约定目前只活在记忆文本里（记忆摘要 299 行、玩家状态块 a4 81 行、NPC 检索层），会衰减、会漏检、AI 会忘——且**不可出示**，没法「拍在老陶面前对质」。用户提案：把关键信息物品化，纸条系统必须做。
**物品化调研结论**：记忆是水物品是石头——物品是导演落盘的结构化权威数据，A3/A4 看同一份真身，永不衰减；可验证性=玩法（出示/对质/兑现成仪式，与 9.2 双手槽天然配套）；先例=逆转裁判证物系统、Outer Wilds 飞船日志、大表哥2 字条。**度**（拍板=克制版）：记忆系统管软回忆，物品化只管「可携带、可验证」的硬凭证（承诺字据/线索/密码/地图）。
**拍板**（AskUserQuestion）：①写入范围=**仅 NPC 写给玩家**（A4 工具通道）；②物品化力度=克制（只对承诺/约定/关键线索）。
**数据结构**：纸条=普通注册物品，`note_{seq}` 独立 id（不堆叠）、name=「纸条·{标题}」、**desc=内容全文**、type=纸条、key 默认 false；`state.note_seq` 计数器。塞包里只露标题（背包同步/名称），**拿在手上（9.2 双手注入）才读得到内容**——信息卫生天然成立。
**实施记录（2026-10-10 全部完成）**：
- **director.py**：`_add_note(content, title, key)`（2007-2025 行——注册→入包→失败回滚防孤儿户口→旁白「🗒️ 一张纸条被塞进了你的背包」）；items 通道加 op=="note" 分支（2342-2347 行，item=标题、note=内容）；`_inventory_block` 双手行升级带 desc（纸条拿在手上注入内容全文，截 60 字，1935-1944 行）；A4 协议文本加 note 格式行（2214-2218 行）
- **a4_manager.py**：items op 枚举加 "note"（124 行）、item/note 参数说明补纸条语义（129-133/150-154 行）
- **prompts.py**：A4_STORY_STATE_SYSTEM items 行改四选一+纸条纪律（克制版：仅承诺/约定/关键线索，内容写清谁答应谁/做什么/何时，361-369 行）；BUILDER items 规则加「线索类物品优先做成纸条形态（desc=内容全文）」（526-531 行，剧本预埋「搜出字条」走普通注册即可）
- **验证**：py_compile 过；重启 5004 探活 200；前端零改动（手册 tooltip 已显示 desc，纸条在背包里就是普通物品格子）
- **测试路径**：跟 NPC 聊出一个承诺（如「今晚把账册交给你」）→ 日志应出现「[纸条] note_1（纸条·xx）」+ 旁白🗒️ → 手册背包看到纸条格（tooltip 显示内容）→ 拿到主手 → NPC 下轮应能读到纸条内容并对承诺表态 → 拿去给另一个 NPC 看（对质仪式）。旧副本不用重生成

### 9.4 演出旁白层：描写与表演分区（2026-10-14，补记）

**动机**：输出只有对话缺描写；环境描写是信息通道（AI 以为玩家知道、其实不知道的动静必须旁白呈现）；描写归旁白、和角色表演分区——未来渲染器 Mod 只消费旁白通道。用户拍板：环境可以少一点（未配渲染器前保持克制）。
**实施**：
- **统一格式**：A3 回复 = 可选 `[旁白]描写行` + 台词正文，单聊/群场两路径通用
- **director.py**：`NARRATION_RE` + 限值常量（单条 80 字/单场 3 条）；apply_stage_reply 循环前置旁白行剥离（不占台词行数，走 notices 通道）；apply_reply 单聊旁白剥离；build_injection 加单聊输出格式块（最多 2 条）
- **prompts.py**：A3_STAGE_SCRIPT_RULE 第 8 条演出旁白（只写可观察的动作神态/环境/天气/物件，**绝不写内心**；有信息量才写；「在场者能看见、玩家未必注意到的动静优先旁白呈现，别默认玩家已知」）；第 5 条同步修正（[旁白] 行是唯一合法的无标签行）
- **前端零改动**：复用 notices 斜体灰字旁白气泡
- **渲染器钩子**：台词行内小动作（（擦了擦手））留台词层（未来驱动立绘表情，ai-galgame-demo ActionTagParser 先例）；场面调度级描写走 [旁白] 行独立通道（未来升级结构化 JSON 描述块）
- **已知边界**：旁白不持久化（刷新后不重放），留给渲染器阶段做结构化存储

### 9.5 NPC 档案填表法移植：画像+示范台词后置补全（2026-10-16）

**动机**：story/ 的 game_db 是「只写不读」孤岛（BUILDER 建档后游玩链路零消费）；NPC 说话风格全靠 personality 一句话——陌生角色千人一面。移植原系统 T-002 关联链填表法（灰眸标杆：reference.dialogue_samples 30 句覆盖情境 + summary 行为化画像）的 AI 代填版。
**用户拍板**：三个都做（对话样本/行为化画像/实体入库）；**生成完故事主干后分步调用补全各 NPC**；剧情完整性和人物完整性永远大于 token 消耗（优先给最优提示词）；反事实校验不用动（现状符合事实性表达好）。
**实施**：
- **prompts.py**：`BUILDER_PROFILE_SYSTEM`（档案师提示词：画像 300-500 字纯行为化禁抽象形容词；样本覆盖 6 情境；**秘密零泄漏铁律**——样本会进 A3 人设前缀区绕过权限门，绝不能含秘密内容，「藏着心事」可演内容不可写）+ `BUILDER_PROFILE_USER`（世界基调+角色设定+任务/秘密上下文，只输出 JSON）
- **builder.py**：`_enrich_npc_profiles(story, on_progress)`——主干+审稿通过后**逐 NPC 一次小调用**（绕开单次输出上限；主 NPC=秘密持有人或任务发布人给 15 句、次要 8 句；max_tokens=3000；单 NPC 失败降级跳过不阻塞出本；进度条 86+i）；build_story 4.5 节接入（`schema.new_state` 之后、建档之前）；`_fill_game_db` 扩展——items→「道具」、locations→「地点」入库（未来 A2 知识问答接缝）+ `reference` 区按 NPC 名归档画像/样本（灰眸结构：source_game=""/persona_summary/dialogue_samples）
- **director.py 注入消费**：persona_for()（单聊/私聊）personality 拼接「行为画像 + 全量说话示范」（进 system 前缀缓存区）；build_stage_injection()（群场）角色区加画像全文 + 前 4 句样本
- **兼容**：旧副本 npc 无 persona_summary/sample_lines 字段 → 判空跳过，行为不变
- **验证**：py_compile 过；重启 5004 探活 200
- **测试路径**：生成新副本 → 进度条走到 86~87%（补全 N 名 NPC 档案）→ game_db 文件应有 reference 区+道具/地点类目 → 进副本跟 NPC 聊，台词语气应贴档案（日志无异常）；旧副本行为不变

**三决策（AskUserQuestion 拍板）**：
1. 选择捕获 = **AI 提议式**：A4 提议 `choice_qX=分支任务id`，导演裁决（first-write-wins，选择不可逆）
2. 未选分支 = **完全隐藏**：locked 任务不出现在公开视图（程序侧保证，前端零改动）
3. 重新开始 = **全部清空**：state/记忆/历史先备份到 archive 再重置，NPC 不记得上一周目

**设计**：
1. **任务树数据结构**（剧本 JSON quests 扩展，向后兼容）：
   - 每个 quest 加 `type`（"push"|"branch"，缺省 push=现状行为）
   - push 型加 `next`: "q2"（完成后指向下一环，单值=线性链）
   - branch 型加 `branch`: {"q2": "分支走向描述", "q3": "分支走向描述"}（2 个 key=分叉点；描述供 A4 判定 choice）
   - `routes` 字段退役（8.25 前端展示已删；任务 desc 承担引导职责）
2. **choice 通道**：branch 型任务进白名单 `choice_qX`（新键类别）；A4 state_brief 注入各分支描述+判定纪律（明确选择/表现明显倾向才提议，只许一次）；导演裁决 first-write-wins（参照 8.26d dead_ 专用裁决模式，不进通用 `_apply_flag`）
3. **分支路由**：branch 型任务 done 瞬间读 choice_qX → 选中分支任务照常走 accept 条件激活；未选分支置 state=locked（accept 永不判真 + 公开视图过滤）；push 型 next 照常（现状条件链）
4. **公开视图**：get_public_state 过滤 locked 任务；任务面板只显示所在分支
5. **BUILDER 提示词**：quests 改任务树（主线 push 链 3~5 环 + 至少 1 个 branch 分叉）；branch 任务 desc 写明选择情境；分支任务也要完整定义；few-shot 同步
6. **重新开始此副本**：
   - `POST /api/story/restart`：备份 `data/story/{id}_state.json` + `data/memory/{id}/` → `data/archive/{id}_{时间戳}/`；随后删除 state 文件（下次装配从剧本 JSON 初始快照重建 + _migrate_state 补新字段）、清空 memory 目录（index/episodic/working/core/history.json）
   - 保留：剧本 JSON（初始快照即"刚造完成状态"，无需另存）、game_db、封面图标、identities 登记
   - 前端：游玩界面加「🔄 重新开始此副本」+ 二次确认 → 调端点 → 重新 enter
7. **旧存档兼容**：无 type/next/branch 字段的老副本一律按现状扁平逻辑跑

### 8.18 视觉定制：存档图标 + 游玩背景图（2026-09-25 第七批需求，已实施）

**用户需求**：①背景图导入（此前一直没实施的"背景图计划"）——玩家可给每个副本设置游玩界面背景图；②存档选择界面每个副本可设置图标，默认留白。

**实施**：
- **存储**：图片由前端 canvas 压缩（图标 320px / 背景 1280px 宽，JPEG 0.78）成 dataURL，存进副本剧本 JSON 的 `cover` / `background` 字段（每副本独立，随副本走；生成后 story JSON 不再重写，无写放大问题）
- **API**：`POST /api/story/cover`（body: identity + image，image 空=清除恢复留白）、`POST /api/story/background`（identity 缺省=当前副本，image 空=清除）；list 摘要返回 cover（不返回 background 防大 payload）；/current 与 /enter 的公开视图附 background
- **前端**：存档卡片左侧 52px 图标位（无图显示"留白"二字占位，hover 出"换图标"遮罩，点击弹文件框不触发进入副本）；游玩页暂停菜单新增"更换背景图"；背景用 CSS 多背景（url + linear-gradient 浅色遮罩 rgba(245,246,248,.86)）保证消息可读，进入副本（enterPlay）与上传后即时生效
- story.js 已同步；py_compile + node --check 通过

**状态：已实施（2026-09-25）。真实测试由用户负责。**

### 8.17 行业调研：AI 生成游戏参考项目与我们的定位（2026-09-25，仅调研）

**结论先行**：我们的"导演（确定性状态机）+ 演出（LLM）"分离架构与 2026 年行业前沿方向完全一致（Lunar Command GRAG 白皮书称其为"消除幻觉循环"的核心做法），不是土法；reference 里的 ai-galgame-demo 确实已无更多价值，真正值得借鉴的是下面几个新项目。

**可借鉴项目清单**（按价值排序）：
1. **worldsmith**（npm 游戏引擎库）：LLM 只输出"计划"（JSON 数组），引擎按 8 种类型化原语（mutate/roll/resolve/remember/emit/do/move/for_each）逐步执行，回调委托给游戏本体。→ 对我们的启发：[STATE:] 标签可升级为类型化原语集，flags 之外加 mutate（数值）/remember（NPC 记忆写入）原语，Phase 2 可参考
2. **story-engine**（Go）：每回合三 Agent 分层——referee（裁判判定玩家行为是否合法）→ narrator（流式叙述）→ reducer（后台从叙述中提取结构化状态变更）。→ 启发：我们的 A2（相关性/判定）+ A3（演出）+ 导演 apply_reply（归约）已经同构；它把 reducer 放后台异步，值得借鉴的是"叙述与状态提取解耦"的明确分层表述
3. **RPG Roleplay Platform**（GitHub，小说转游戏世界）：**Git 风格存档**（commit/branch/checkout，剧情分叉可回溯）+ 内置 BM25+向量检索喂原文。→ 启发：①"小说/长文本转世界"与我们 8.16 资料桥同属"外源资料进世界"问题，它的检索方案是资料量大时的 Phase 2 方向；②Git 式存档可在存档界面加"剧情分支"（存副本状态快照），成本低价值高
4. **mnehmos.rpg.mcp**：RPG 规则引擎做成 MCP server（"让 LLM 当不会作弊的 GM"），195→28 工具合并经验（action 枚举 + 模糊匹配 + 元工具发现）。→ 启发：8.16 资料桥接收端如果未来扩成"外部 AI 反向操作游戏状态"，此项目是完整先例
5. **Lunar Command GRAG 白皮书**：确定性状态引擎（知识图谱）+ 生成叙事层（SLM），LLM 定位为"文本渲染器"。→ 不是借鉴对象，是**我们架构的行业佐证**，可引用其表述（"LLM 不是 Game Master，是 Textual Renderer"）
6. **MistScale 实践指南**（7 条）：世界状态与 NPC 记忆分离存、关系变量慢更新、心情是慢变量、claims 先校验后生效、持久化记忆只存承诺/恩怨/偏好/任务。→ 我们已实现其中 1/2/5（导演权威、记忆分层、只进不退）；**未做的**：per-NPC 对玩家的关系变量层（好感/信任慢更新）、心情慢变量——Phase 2 候选
7. **text-adventure-tui**：YAML 事件驱动（flags/位置/背包/回合数触发 → 注入叙事/改提示词/设旗标）。与我们 quests 里程碑同构，无新意，仅佐证
8. **LingMo-Engine / ai_rpg（envy-ai）**：中文圈同类引擎（插件系统/世界配置/区域生成/needs 系统/多轴 disposition）。→ ai_rpg 的"多轴好感慢变量"（友谊/爱情/信任/尊重分轴）是关系层的细化参考

**我们的优点（相对行业）**：
- 跨 NPC 秘密隔离 + 揭示状态动态注入——比通用 lorebook（SillyTavern 关键词触发）更强，是剧情向的独有设计
- 机器可判任务状态机（flags 只进不退 + AI 只能提议）——与 worldsmith/story-engine 同级，但开源圈普遍还没做到"任务判定权在引擎"
- 生成侧审稿循环（builder→critic 打分退回）——调研项目几乎全是"生成即用"，无质量把关层
- 完整记忆系统（A1 检索/A4 建档/历史管理）+ 反事实校验——多数项目只有向量检索一层
- 双端（PC/安卓）+ 并行多副本存档——自托管项目几乎都是 PC 单端

**对 8.16 资料桥的补充佐证**：CyborgPrime 的"City Bible"工作流（结构化世界观文档库 + AI 合成 + 人工策展）证明"结构化资料先行，AI 只在资料约束内叙事"是桌面 TRPG 圈验证过的成熟做法；8.16 的 JSON 契约即轻量版 City Bible。

**状态：调研完成（2026-09-25）。结论：架构无需转向，8.16 按计划开工；Phase 2 候选新增：类型化状态原语（worldsmith）、剧情分支存档（RPG Roleplay）、NPC 关系/心情慢变量（MistScale/ai_rpg）。**

### 8.19 模组化架构计划（T-006，远期，2026-10-03 讨论定方向，未开工）

**定位**：游戏的灵魂是剧情，但没有玩法剧情就是壳。模组化是把玩法挂载上去的唯一路径（用户原话：除了这个办法想不出来怎么把玩法挂上去）；也是架构健壮性的必要项——生态级工程，短期不动工，但方向定死，且从现在起每步改动不堵这条路。

**目标形态（用户定的最终形态）**：后端 = 游戏剧情核心（导演 + AI + 状态），前端 = 纯显示层（一堆模组），连主界面本身都是一个模组。别人拿这套系统做游戏 = 写一个界面模组 + N 个玩法模组即可完成全流程开发，AI 只是后端插上的能力之一。

**要点清单**：
1. **前提：演出层舞台化**——从微信气泡式聊天界面改成原神式逐拍演出：beat 流（逐行台词+旁白+选项/按钮）、流式输出、玩家只能点前进。保证玩家不乱切页（模组挂载需要确定性的同步点）；气泡界面不删，降级为开发者调试视图。舞台本身 = 第一个界面模组（吃狗粮）。
2. **钩子 → 挂载机制**——剧情内按钮（NPC 说"开打"）→ AI 提议模组 id + 参数包（战斗强度/怪物数量等）→ 程序白名单裁决 → 壳加载模组 → 模组跑完经专用回调 API 把结果（胜负/耗时/掉落）传回核心 → 导演写 flags → 舞台恢复剧情继续。参考原神"过着过着剧情就进了场景"。
3. **三层结构**——壳（bootloader + SDK + 权限收敛）／界面模组（舞台）／玩法模组（战斗、解谜、小游戏……一个场景一个模组，形态 = 前端 HTML/JS 文件，通过后端 API 交流）。
4. **SDK 契约先行，先自产自销**——契约三样：舞台 API（say/advance/choice/loading）、模组生命周期（mount/pause/resume/unmount）、状态交换（只读快照 + 提议通道）。先用舞台模组 + 一个试金石玩法模组跑通，契约稳定后再谈第三方生态。
5. **安全边界**——AI 不能编模组（模组 id 必须在已安装白名单里，编造即丢弃，与 flag key 同一套哲学）；模组回传结果也要白名单裁决（模组可以"演"，不能直接改剧情状态）。
6. **现在就要守的规矩（防堵路）**——后端输出逐步结构化（整段文本 → 事件流）、状态权限继续走白名单裁决、前端不再加深和气泡 DOM 结构的耦合。
7. **统一调用协议方向（2026-10-03 补）**——模组调用、任务、状态标签最终都收敛到 tool calling 协议（function calling：清单注入 + JSON Schema 强校验 + enum 枚举防编造 + 协议层自动重试）。模型编 key / 格式坏 / 忘打标这类幻觉由协议消灭，而非靠白名单静默丢弃兜底。现状对照：A3 行内标签 = 提示词模拟的土法 tool calling（最弱）；8.24d A4 JSON 尾块 = 手工半步；升级路径 = A4 状态调用（已独立）换成 API 原生 function calling，键名全部 enum 上白名单——"编 key 被丢弃"升级为"编 key 提交不进去"。模组调用 launch_mod(mod_id: enum(已安装清单), params) 是同一逻辑的自然延伸。
8. **节奏**——等剧情系统本体（导演/任务/秘密/终局）跑稳后再拆壳；本计划只是定方向，未排期。

## 9. 运行与调试速查

```bash
# PC 启动
python app.py          # 端口 5003，浏览器开 static/index.html

# 安卓打包安装（USB 调试已授权）
cd android && ./gradlew assembleDebug
adb install -r app/build/outputs/apk/debug/app-debug.apk   # 同签名覆盖安装，静默免确认
```

- 冒烟测试惯例：Temp 目录写临时脚本（打桩 FakeClient/FakeStore/FakeIndex），跑完删除。
- 修改 PC 端代码后重启服务生效；安卓端改动需重新打包安装。
