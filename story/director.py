#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
导演（Director）— 剧情副本的真正大脑

职责：
  1. 权限门注入（build_injection）：把「当前 NPC 应该知道的内容」打包成 story_section
     交给 A3 注入——跨 NPC 秘密严格隔离，绝不含其他 NPC 的 known_secrets。
     8.11/8.12/8.13 扩展：游览路线软引导、玩家数值/时钟/地点/在场 NPC、地点白名单、
     可对话性提示。
  2. 后置解析（apply_reply）：从演出 AI 回复中剥除 [STATE:] / [PRIVATE_REQ] 标签
     （防泄漏给玩家），白名单校验后更新 flags 与玩家地点。
  3. 终局与任务判定（finalize）：accept_flags/done_flags 自动推进任务状态（只进不退）、
     milestones 按 order 触发、endings[] 多终局先满足先生效。
  4. 游戏状态操作：complete_quest（点完成→扣时→推进时钟）、玩家地点移动（含私聊
     自动断开）、私聊建立/解除（同地点硬校验）。
  5. 公开视图（get_public_state）：给前端的剧本公开信息，绝不含 secrets 原文。

状态机模型（路线 B 真状态机）：AI 只能"提议"状态变化（[STATE: key=值] 标签），
真正写状态的一律是这里的程序逻辑（白名单 + 枚举校验）。
任务完成的判定权在导演，不在 AI——AI 提议只是参考，flags 集合满足才算数。
"""

import logging
import re

from . import store
from .schema import QUEST_STATES, new_state, format_research
from agent import prompts as PROMPTS

logger = logging.getLogger("story_director")

# 匹配 [STATE: xxx] 提议标签：忽略大小写；xxx = 逗号分隔的 `key=值`
STATE_TAG_RE = re.compile(r"\[\s*state\s*:\s*([^\]]*?)\s*\]", re.IGNORECASE)
# 匹配 [PRIVATE_REQ] 私聊请求标签（NPC 主动发起私聊）
PRIVATE_REQ_RE = re.compile(r"\[\s*private_req\s*\]", re.IGNORECASE)
# 匹配 [NOTE: 内容] 冒险手册标签（NPC 向玩家交代的重要信息）
NOTE_RE = re.compile(r"\[\s*note\s*:\s*([^\]]*?)\s*\]", re.IGNORECASE)

# 布尔字面量解析表（标签值统一是小写字符串）
_TRUE_WORDS = {"true", "1", "yes", "是", "真"}
_FALSE_WORDS = {"false", "0", "no", "否", "假"}

# 任务状态中文映射（注入/公开视图用）
_QUEST_STATE_CN = {
    "undiscovered": "未发现",
    "active": "进行中",
    "completable": "可完成",
    "done": "已完成",
    "failed": "已失败",
}

# 任务状态只进不退的顺序表
_QUEST_ORDER = {s: i for i, s in enumerate(QUEST_STATES)}
# AI 可提议的任务状态（completable/done 由程序判定；undiscovered 是倒退，一律拒绝）
_AI_QUEST_STATES = {"active", "failed"}

# 一天的总分钟数（时钟推进用）
_MINUTES_PER_DAY = 24 * 60

# ── 8.21a 类型化状态原语 ──────────────────────────
# 相对增减型数值提议：hp+10 / hunger-20（绝对赋值 hp=50 走 k=v 通道）
MUTATE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)([+-])(\d+)$")
# 玩家数值白名单（Phase 1 只开 hp/hunger 两个键；NPC 侧数值不开）
_MUTABLE_KEYS = {"hp", "hunger"}
# 防作弊：单轮单键变化幅度 ±30 钳制（防 AI 一下清空/拉满），越界截断
_MUTATE_TURN_CLAMP = 30
# 8.21a 认知原语：每 NPC 记忆条数上限（超出 FIFO 淘汰最旧）
_NPC_KNOWLEDGE_MAX = 8

# ── 8.21b NPC 关系/心情慢变量 ─────────────────────
# 心情 7 档枚举（由 emotion-keywords 中文情绪词表归并）
_MOODS = ("平静", "开心", "警惕", "不满", "疲惫", "悲伤", "愤怒")
# 好感分档文案（注入用；数值本身不向玩家暴露）
_AFFINITY_TONES = ((81, "亲近"), (61, "友好"), (41, "中立"), (21, "冷淡"), (0, "排斥"))

# ── 8.24 群场剧场模式 ─────────────────────────────
# 剧本行解析：行首 [角色名] + 台词（[角色名] 内不许再嵌方括号）
STAGE_LINE_RE = re.compile(r"^\s*\[([^\[\]]{1,20})\]\s*(.*)$", re.DOTALL)
# 节奏硬限：单行字数上限（超限整行裁掉，宁缺毋滥）
_STAGE_LINE_MAX = 80
# 一场总行数上限（超出裁尾）
_STAGE_LINES_MAX = 12
# 一场总字数上限（超出裁尾）
_STAGE_CHARS_MAX = 500


def _affinity_tone(v: int) -> str:
    for lo, label in _AFFINITY_TONES:
        if v >= lo:
            return label
    return "中立"


def _parse_bool(val: str):
    """把标签值解析成布尔；无法识别返回 None（调用方直接丢弃）。"""
    v = (val or "").strip().lower()
    if v in _TRUE_WORDS:
        return True
    if v in _FALSE_WORDS:
        return False
    return None


def _strip_tags(text: str) -> str:
    """剥除全部 [STATE:] / [PRIVATE_REQ] / [NOTE:] 标签（防泄漏给玩家），并清理残留空行。"""
    cleaned = STATE_TAG_RE.sub("", text or "")
    cleaned = PRIVATE_REQ_RE.sub("", cleaned)
    cleaned = NOTE_RE.sub("", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _eval_condition(cond: str, flags: dict) -> bool:
    """评估条件表达式（Phase 1：仅支持 flag==值，可用 && 连接多个条件）。

    值比较语义：
      - 布尔型 flag（knows_secret_* / met_npc_* / milestone_*）：按布尔解析比较
      - 枚举型 flag（quest_*_state）：字符串忽略大小写比较
      - flag 不存在 → 条件不成立
    """
    cond = (cond or "").strip()
    if not cond:
        return False
    for part in re.split(r"&&", cond):
        part = part.strip().strip("()")
        if not part:
            continue
        if "==" not in part:
            logger.warning(f"条件表达式暂不支持（Phase 1 仅支持 ==）: {part}")
            return False
        key, _, expected = part.partition("==")
        key = key.strip()
        expected = expected.strip()
        if key not in flags:
            return False
        current = flags[key]
        if isinstance(current, bool):
            parsed = _parse_bool(expected)
            if parsed is None or parsed != current:
                return False
        else:
            if str(current).strip().lower() != expected.strip().lower():
                return False
    return True


# 8.24c flag 注册制（业界铁律：先注册后使用）：从任务条件与里程碑触发器提取
# flag 键的正则。只收三类布尔键 + 任务状态键；player_location 等非 flags 键不收。
# （星露实测：milestone_q1_watch_ready 这类剧情里程碑键 builder 未预置进 flags，
# AI 提议会被 _apply_flag 白名单首行「key not in self._flags」无声丢弃 →
# done_flags 永不满足、任务永不可完成。）
_FLAG_KEY_RE = re.compile(
    r"(knows_secret_\w+|met_npc_\w+|milestone_\w+|quest_\w+_state)\s*=="
)


class Director:
    """一个副本实例对应一个 Director（绑定 identity）。"""

    def __init__(self, identity: str):
        self.identity = identity
        self.story = store.load_story(identity)
        if not self.story:
            raise FileNotFoundError(f"副本剧本不存在: {identity}")
        self.state = store.load_state(identity)
        if not self.state:
            # 首次装配：生成初始状态并落盘
            self.state = new_state(self.story)
            store.save_state(identity, self.state)
        self._migrate_state()
        self._flags = self.state.setdefault("flags", {})
        # 本轮任务跃迁产生的玩家可见系统提示（接取/可完成/失败），pipeline 取走拼进 notices
        self._quest_notices: list = []
        # 动作型任务（8.20）汇报标记白名单：仅这些 milestone_* 允许 AI 提议
        # （剧本里程碑 milestone_{mid} 与 time_ready 仍只许程序写，AI 提议一律拒绝）
        self._ai_reported_flags = {
            f"milestone_{q.get('id')}_reported"
            for q in self.story.get("quests", [])
            if q.get("judge_mode", "flags") == "time"
        }
        # 8.24c 剧情里程碑 AI 通道：任务 accept/done 条件引用的 milestone_* 键
        # （排除 time_ready/reported 两个专门通道键）允许 AI 提议——注入已对
        # 进行中任务点名确切 key。trigger-only 的 milestone_m* 仍只许程序写。
        self._ai_milestone_flags = set()
        for q in self.story.get("quests", []):
            for cond in (q.get("accept_flags") or "", q.get("done_flags") or ""):
                for mk in re.findall(r"(milestone_\w+)\s*==", cond):
                    if mk.endswith("_time_ready") or mk.endswith("_reported"):
                        continue
                    if mk in self._flags:
                        self._ai_milestone_flags.add(mk)

    def _migrate_state(self) -> None:
        """旧副本状态迁移：补齐 8.11/8.12/8.13 新增字段（幂等，缺什么补什么）。"""
        changed = False
        if "player" not in self.state:
            npcs = self.story.get("npcs", [])
            default_loc = npcs[0].get("location", "") if npcs else ""
            self.state["player"] = {
                "hp": 100, "max_hp": 100, "hunger": 100, "location": default_loc,
            }
            changed = True
        if "clock" not in self.state:
            self.state["clock"] = {"day": 1, "time": "08:00"}
            changed = True
        if "npc_locations" not in self.state:
            self.state["npc_locations"] = {
                npc.get("id"): npc.get("location", "") for npc in self.story.get("npcs", [])
            }
            changed = True
        if "private_with" not in self.state:
            self.state["private_with"] = None
            changed = True
        if "ending_reached" not in self.state:
            self.state["ending_reached"] = None
            changed = True
        if "manual_notes" not in self.state:
            self.state["manual_notes"] = []
            changed = True
        if "quest_invested" not in self.state:
            self.state["quest_invested"] = {}
            changed = True
        if "npc_knowledge" not in self.state:
            # 8.21a NPC 认知账本：{nid: [事实, ...]}
            self.state["npc_knowledge"] = {}
            changed = True
        if "npc_attitude" not in self.state:
            # 8.21b 关系/心情慢变量：{nid: {affinity, trust, mood}}
            self.state["npc_attitude"] = {}
            changed = True

        # ── 8.24c flag 注册制：任务条件与里程碑触发器引用的 flag 键，缺失即预置 ──
        # （RPG Maker 式「先注册后使用」：done_flags 里的键必须存在于 flags 字典，
        # 否则 AI 打标被白名单丢弃、条件永远为假。quest_*_state 缺失补 "undiscovered"，
        # 布尔键补 False；player_location 等非 flags 键由正则前缀天然排除。）
        flags = self.state.setdefault("flags", {})
        _cond_parts = []
        for _q in self.story.get("quests", []):
            _cond_parts.append(_q.get("accept_flags") or "")
            _cond_parts.append(_q.get("done_flags") or "")
        for _m in self.story.get("milestones", []):
            _cond_parts.append(_m.get("trigger") or "")
        _missing = []
        for _key in sorted(set(_FLAG_KEY_RE.findall(" && ".join(_cond_parts)))):
            if _key in flags:
                continue
            flags[_key] = "undiscovered" if _key.startswith("quest_") else False
            _missing.append(_key)
        if _missing:
            changed = True
            logger.info(f"[flag注册制] 补预置 {len(_missing)} 键: {_missing}")

        if changed:
            logger.info(f"副本状态已迁移补齐新字段: {self.identity}")
            store.save_state(self.identity, self.state)

    # ── 内部查询 ──────────────────────────────────────

    def get_npc(self, npc_id: str) -> dict:
        for npc in self.story.get("npcs", []):
            if npc.get("id") == npc_id:
                return npc
        raise ValueError(f"副本中不存在 NPC: {npc_id}")

    # 兼容别名（旧代码调用点）
    _get_npc = get_npc

    def _get_secret(self, secret_id: str) -> dict:
        for sec in self.story.get("secrets", []):
            if sec.get("id") == secret_id:
                return sec
        return {}

    def locations_whitelist(self) -> list:
        """地点白名单：剧本 locations 优先；旧副本缺失时从 NPC/玩家位置推导。"""
        locs = [loc for loc in self.story.get("locations", []) if isinstance(loc, str) and loc.strip()]
        if locs:
            return locs
        derived = []
        for npc in self.story.get("npcs", []):
            loc = npc.get("location", "")
            if loc and loc not in derived:
                derived.append(loc)
        ploc = self.state.get("player", {}).get("location", "")
        if ploc and ploc not in derived:
            derived.append(ploc)
        return derived

    def npc_location(self, npc_id: str) -> str:
        """NPC 当前地点（运行时表优先，回退剧本初始值）。"""
        return self.state.get("npc_locations", {}).get(npc_id) or self.get_npc(npc_id).get("location", "")

    def player_location(self) -> str:
        return self.state.get("player", {}).get("location", "")

    def presence_ids(self) -> list:
        """与玩家同处的 NPC id 列表（剧本顺序；8.12 可对话性 = 同地点）。"""
        ploc = self.player_location()
        return [npc.get("id") for npc in self.story.get("npcs", [])
                if self.npc_location(npc.get("id")) == ploc]

    def presence_names(self, exclude_npc: str = None) -> list:
        """与玩家同处的 NPC 名单（注入用；exclude_npc 从名单里剔除该 NPC 自己）。"""
        names = []
        for nid in self.presence_ids():
            if exclude_npc and nid == exclude_npc:
                continue
            names.append(self.get_npc(nid).get("name", nid))
        return names

    def clock_text(self) -> str:
        """「第X天 HH:MM」格式文本（注入与前端共用）。"""
        clock = self.state.get("clock", {})
        return f"第{clock.get('day', 1)}天 {clock.get('time', '08:00')}"

    # ── 人设切换 ──────────────────────────────────────

    def persona_for(self, npc_id: str) -> dict:
        """把该 NPC 转成 pipeline 需要的 persona 字典（键与原系统一致）：
        {name, personality, world_setting}。goal/秘密等不在人设里，走权限门注入。
        """
        npc = self.get_npc(npc_id)
        setting = self.story.get("setting", {})
        return {
            "name": npc.get("name", npc_id),
            "personality": npc.get("personality", ""),
            "world_setting": setting.get("world_desc", ""),
        }

    # ── 权限门注入 ────────────────────────────────────

    def _quest_lines_for(self, giver_id: str) -> list:
        """该 NPC 是 giver 的任务注入行（含动作型任务汇报纪律），单聊/群场共用。"""
        quest_lines = []
        for q in self.story.get("quests", []):
            if q.get("giver") != giver_id:
                continue
            qid = q.get("id", "")
            state = self._flags.get(f"quest_{qid}_state", q.get("state", "undiscovered"))
            invested = self.state.get("quest_invested", {}).get(qid, 0)
            need = q.get("time_cost_minutes", 0)
            line = (
                f"- 任务「{q.get('name', '')}」（{qid}）当前状态：{_QUEST_STATE_CN.get(state, state)}；"
                f"完成条件：{q.get('complete_condition', '')}；奖励：{q.get('reward', '')}；"
                f"预计耗时：约 {need} 分钟；玩家已投入：{invested}/{need} 分钟"
            )
            quest_lines.append(line)
            # 8.24c 完成标记点名（仅进行中任务，防 undiscovered 剧透）：
            # 星露实测 silent omission——灰羽把 s2 说破三轮零打标、watch_ready 无通道。
            # 按 8.20b 汇报纪律同款硬指令写法，把 done_flags 里的确切 key 点名给模型。
            if state == "active":
                for part in re.split(r"&&", (q.get("done_flags") or "").strip()):
                    part = part.strip().strip("()")
                    if "==" not in part:
                        continue
                    dkey = part.partition("==")[0].strip()
                    if dkey.startswith("knows_secret_") and dkey in self._flags:
                        quest_lines.append(
                            f"- **完成标记点名（硬性输出要求）**：任务「{q.get('name', '')}」依赖玩家知晓"
                            f"你守着的那件秘密。若该秘密玩家已经知晓（包括此前轮次已说破、本轮只是再次确认），"
                            f"回复末尾必须原样输出一行 [STATE: {dkey}=true]；玩家尚不知晓就不要输出。"
                            f"漏写会导致任务永远无法完成"
                        )
                    elif dkey.startswith("milestone_") and dkey in self._flags:
                        quest_lines.append(
                            f"- **完成标记点名（硬性输出要求）**：若「{q.get('complete_condition', '')}」"
                            f"所述事实已经发生（包括此前轮次已发生、本轮只是再次确认，"
                            f"如关键物件已就绪、关键环节已完成），回复末尾必须原样输出一行"
                            f" [STATE: {dkey}=true]；尚未发生就不要输出。漏写会导致任务永远无法完成"
                        )
            # 动作型任务的汇报纪律单独成行（8.20b 实测：附在任务信息行尾会被模型忽略）
            if q.get("judge_mode", "flags") == "time":
                if need and invested >= need:
                    quest_lines.append(
                        f"- **汇报确认（硬性输出要求）**：任务「{q.get('name', '')}」的时间已投入达标。"
                        f"若玩家本轮已口头汇报干完（如「录好了」「整理完了」），你的回复末尾"
                        f"必须原样输出一行 [STATE: milestone_{qid}_reported=true]；"
                        f"玩家本轮没有汇报就不要输出。漏写会导致任务永远无法完成"
                    )
                else:
                    quest_lines.append(
                        f"- **汇报纪律**：任务「{q.get('name', '')}」的时间还没投入够"
                        f"（已投入 {invested}/{need} 分钟）。玩家问起或提前汇报时，"
                        f"委婉表示这件事还需要些工夫（如「别急，慢慢来」「这事急不得」），"
                        f"此时严禁输出 [STATE: milestone_{qid}_reported=true]"
                    )
        return quest_lines

    def _player_state_block(self, exclude_npc: str = None) -> str:
        """玩家状态摘要块（单聊 build_injection 与群场 build_stage_injection 共用）。

        只给事实摘要，秘密只给 id 与是否已知，绝不给未揭示内容。
        exclude_npc：从「同处 NPC」名单剔除该 NPC（单聊=NPC 自己；群场传 None 列全部在场者）。
        """
        met_names = []
        for n in self.story.get("npcs", []):
            if self._flags.get(f"met_npc_{n.get('id')}", False):
                met_names.append(n.get("name", n.get("id")))
        known_sids = [
            sec.get("id")
            for sec in self.story.get("secrets", [])
            if self._flags.get(f"knows_secret_{sec.get('id')}", False)
        ]
        quest_states = []
        for q in self.story.get("quests", []):
            qkey = "quest_{}_state".format(q.get("id"))
            qstate = self._flags.get(qkey, "undiscovered")
            quest_states.append(
                f"「{q.get('name', '')}」{_QUEST_STATE_CN.get(qstate, qstate)}"
            )
        player = self.state.get("player", {})
        others = self.presence_ids()
        other_names = [self.get_npc(nid).get("name", nid) for nid in others if nid != exclude_npc]

        player_lines = ["### 玩家当前状态（摘要）"]
        player_lines.append(f"- 已认识的 NPC：{'、'.join(met_names) if met_names else '尚无（本 NPC 若首次见面，请用开场白）'}")
        player_lines.append(f"- 玩家已知悉的秘密编号：{'、'.join(known_sids) if known_sids else '尚无'}")
        # 玩家人设（选填高级项，生成时指定；让 NPC 按身份称呼玩家）
        persona = self.story.get("player_persona") or {}
        if persona.get("name") or persona.get("desc"):
            pdesc = persona.get("desc") or ""
            player_lines.append(f"- 玩家身份：{persona.get('name') or '（无名）'}" + (f"，{pdesc}" if pdesc else ""))
        player_lines.append(f"- 任务进度：{'；'.join(quest_states) if quest_states else '暂无任务'}")
        # 8.12：玩家数值 + 时钟 + 地点 + 在场 NPC
        player_lines.append(
            f"- 玩家数值：体力 {player.get('hp', 100)}/{player.get('max_hp', 100)}，"
            f"饱食度 {player.get('hunger', 100)}/100"
        )
        player_lines.append(f"- 游戏内时间：{self.clock_text()}（时间只在玩家投入任务时间或完成任务时推进，对话不耗时，不要催促时间）")
        player_lines.append(f"- 玩家所在地点：{self.player_location() or '（未知）'}")
        player_lines.append(
            f"- 现在与玩家同处的 NPC：{'、'.join(other_names) if other_names else '只有你一个'}"
        )
        # 8.12：地点白名单（[STATE: location=xx] 只认这些确切名）
        other_locs = [loc for loc in self.locations_whitelist() if loc != self.player_location()]
        player_lines.append(f"- 可前往的地点：{'、'.join(other_locs) if other_locs else '（暂无其他地点）'}")
        player_lines.append(
            "- 玩家说要去某地、或答应跟你走时，你可在回复末尾用 [STATE: location=地点确切名] 提议移动"
            "（地点名必须一字不差取自上面的可前往地点）。"
        )
        return "\n".join(player_lines)

    def build_injection(self, npc_id: str) -> str:
        """构建当前 NPC 的权限门注入段（A3 story_section）。

        只包含：该 NPC 的 goal/性格提示、其 known_secrets 中秘密内容
        （按揭示状态区分措辞）、自己是 giver 的任务、游览路线（软引导）、
        玩家数值/时钟/地点/在场 NPC/地点白名单、当前场景。
        绝不包含其他 NPC 的 known_secrets（跨 NPC 隔离）。
        """
        npc = self.get_npc(npc_id)

        # 1) NPC 块：性格/目标/位置/开场白 + 自己持有的秘密（含揭示状态措辞）
        lines = [f"### 你的角色补充（{npc.get('name', npc_id)}）"]
        lines.append(f"- 性格基调：{npc.get('personality', '')}")
        lines.append(f"- 你的核心目标（驱动你的言行动机，但不必直说）：{npc.get('goal', '')}")
        lines.append(f"- 你当前所在位置：{self.npc_location(npc_id) or npc.get('location', '')}")
        lines.append(f"- 你的开场白（首次与玩家见面时可用）：{npc.get('opening', '')}")
        lines.append(f"- 你的 NPC 编号：{npc_id}（状态标签 npc_learned_/mood_ 里用它指代你自己）")
        # 8.21b 关系/心情慢变量：影响扮演，但不向玩家报出数值
        att = self._attitude(npc_id)
        lines.append(
            f"- 你对玩家的当前态度：好感 {att['affinity']}（{_affinity_tone(att['affinity'])}）、"
            f"信任 {att['trust']}、心情 {att['mood']}——让这些自然影响你的语气与举动，"
            f"严禁向玩家报出任何数值"
        )

        secret_lines = []
        for sid in npc.get("known_secrets", []):
            sec = self._get_secret(sid)
            if not sec:
                continue
            revealed = bool(self._flags.get(f"knows_secret_{sid}", False))
            if revealed:
                secret_lines.append(
                    f"- 【玩家已知】{sec.get('content', '')}（玩家已知晓，可平常谈起）"
                )
            else:
                # 8.22 修复：必须把确切的状态 key 给到 AI，否则它只能发明 key
                # （如 knows_secret_suwan_ledger），被白名单丢弃后秘密永远无法
                # 揭示、相关结局不可达。只有给了确切 key，[STATE:] 提议才合法。
                secret_lines.append(
                    f"- 【尚未对玩家揭示】{sec.get('content', '')}"
                    f"（你知情但绝不主动说出；平时最多按暗示旁敲侧击：{sec.get('reveal_hint', '')}"
                    f"。当玩家凭证据问破、你决定向玩家揭示时，**必须**在回复末尾输出"
                    f" [STATE: knows_secret_{sid}=true]）"
                )
        if secret_lines:
            lines.append("### 你掌握的秘密")
            lines.extend(secret_lines)

        # 8.21a 认知原语注入：该 NPC 通过相处得知的事实（表页动态扩展——
        # NPC 得知新真相后行为自然转变）
        facts = self.state.get("npc_knowledge", {}).get(npc_id, [])
        if facts:
            lines.append("### 你已知悉的事实（从与玩家的相处中得知，可在言行中自然体现）")
            for fact in facts:
                lines.append(f"- {fact}")

        # 2) 自己是 giver 的任务（其他 NPC 的任务不注入）
        quest_lines = self._quest_lines_for(npc_id)
        if quest_lines:
            lines.append("### 你可以发布的任务")
            lines.extend(quest_lines)

        # 3) 游览路线（8.11 软引导：主动把话题往下一步拉，玩家偏离不拦截）
        routes = self.story.get("routes", [])
        if routes:
            lines.append("### 游览路线（软引导，供你自然带话题用）")
            for r in routes:
                steps = " → ".join(r.get("steps", []))
                lines.append(f"- 路线「{r.get('name', '')}」：{r.get('brief', '')}。步骤：{steps}")
            lines.append(
                "- 引导分寸：你可以借话头把玩家往路线的下一步引，但玩家明显想去别处时，"
                "顺着玩家走，不要强行拦截、不要反复推销。"
            )

        # 8.16 世界观资料（外源检索核实）：独立块，NPC 言行不得与之矛盾
        research_block = self._research_block(npc_id)
        if research_block:
            lines.append(research_block)

        npc_block = "\n".join(lines)

        # 4) 玩家 flags 摘要（共用块；秘密只给 id 与是否已知，绝不给未揭示内容）
        player_state = self._player_state_block(exclude_npc=npc_id)

        # 5) 当前场景
        scene_text = self.state.get("scene", "") or "（尚未设定）"
        scene = f"### 当前场景\n{scene_text}"

        return PROMPTS.A3_STORY_SECTION.format(
            npc_block=npc_block,
            player_state=player_state,
            scene=scene,
        )

    def _research_block(self, npc_id: str = None) -> str:
        """8.16 外源世界观资料块：6000 字预算内平铺 facts；超出预算时只保留与
        该 NPC / 当前地点相关的主题。与 A3 反事实校验（_verify_and_revise）构成双重防矛盾。
        npc_id 为空（8.24 群场剧场无单一焦点 NPC）时 focus 只取玩家所在地点。
        """
        data = self.story.get("research_summary")
        if not isinstance(data, dict) or not data.get("topics"):
            return ""
        npc = self.get_npc(npc_id) if npc_id else {}
        npc_loc = (self.npc_location(npc_id) if npc_id else "") or npc.get("location", "")
        focus = [
            npc.get("name", ""),
            npc_loc,
            self.player_location(),
        ]
        text, _trunc = format_research(
            data, max_chars=6000,
            focus=[str(f).strip() for f in focus if f and str(f).strip()],
        )
        if not text:
            return ""
        return "### 世界观资料（经检索核实，禁止与之矛盾）\n" + text

    # ── 8.24 群场剧场模式 ─────────────────────────────

    def build_stage_injection(self, responders: list) -> str:
        """群场剧场模式：单次调用的整场注入（A3 story_section）。

        与单聊 build_injection 的关键差异：
          - 一次注入全部在场角色的补充信息（性格/目标/编号/态度/认知/自己发布的任务）；
          - 秘密零泄漏：只注入【玩家已知】的秘密（可平常谈起）；未揭示秘密一概不注入、
            不给揭示指令——群场谁也不当众说破，泄漏问题不存在；
          - 追加群场剧本输出格式规则（A3_STAGE_SCRIPT_RULE：行格式/顺序自排/节奏硬限）。
        """
        names = [self.get_npc(rid).get("name", rid) for rid in responders]
        lines = [f"### 群像剧场：你本轮同时扮演以下全部角色（{'、'.join(names)}）"]
        for rid in responders:
            npc = self.get_npc(rid)
            nm = npc.get("name", rid)
            att = self._attitude(rid)
            lines.append(f"\n#### 角色：{nm}（编号 {rid}）")
            lines.append(f"- 性格基调：{npc.get('personality', '')}")
            lines.append(f"- 核心目标（驱动言行动机，但不必直说）：{npc.get('goal', '')}")
            lines.append(f"- 当前位置：{self.npc_location(rid) or npc.get('location', '')}")
            lines.append(f"- 你的开场白（首次与玩家见面时可用）：{npc.get('opening', '')}")
            lines.append(
                f"- 对玩家的当前态度：好感 {att['affinity']}（{_affinity_tone(att['affinity'])}）、"
                f"信任 {att['trust']}、心情 {att['mood']}——自然影响该角色的语气与举动，"
                f"严禁向玩家报出任何数值"
            )
            # 已知事实（8.21a 认知账本，从与玩家的相处中得知）
            for fact in self.state.get("npc_knowledge", {}).get(rid, []):
                lines.append(f"- {nm}已知悉的事实（可在言行中自然体现）：{fact}")
            # 该角色是 giver 的任务（群场任务信息公开）
            lines.extend(self._quest_lines_for(rid))
            # 秘密：只注入玩家已知的（可平常谈起）；未揭示的一概不给
            for sid in npc.get("known_secrets", []):
                sec = self._get_secret(sid)
                if sec and self._flags.get(f"knows_secret_{sid}", False):
                    lines.append(
                        f"- 【玩家已知】{nm}可以平常谈起的秘密：{sec.get('content', '')}"
                    )
        # 游览路线（软引导，全场共享）
        routes = self.story.get("routes", [])
        if routes:
            lines.append("### 游览路线（软引导，供你自然带话题用）")
            for r in routes:
                steps = " → ".join(r.get("steps", []))
                lines.append(f"- 路线「{r.get('name', '')}」：{r.get('brief', '')}。步骤：{steps}")
            lines.append(
                "- 引导分寸：你可以借话头把玩家往路线的下一步引，但玩家明显想去别处时，"
                "顺着玩家走，不要强行拦截、不要反复推销。"
            )
        # 8.16 世界观资料（群场无单一焦点 NPC，按玩家地点筛选）
        research_block = self._research_block(None)
        if research_block:
            lines.append(research_block)
        # 剧本输出格式规则（行格式/顺序自排/节奏硬限/禁 markdown）
        lines.append(PROMPTS.A3_STAGE_SCRIPT_RULE)

        player_state = self._player_state_block(exclude_npc=None)
        scene_text = self.state.get("scene", "") or "（尚未设定）"
        scene = f"### 当前场景\n{scene_text}"
        return PROMPTS.A3_STORY_SECTION.format(
            npc_block="\n".join(lines),
            player_state=player_state,
            scene=scene,
        )

    def apply_stage_reply(self, raw: str, responders: list) -> dict:
        """群场剧场模式：解析整场剧本（逐行校验分发，行序即说话顺序）。

        行格式 [角色名]台词：
          - 说话人不在场白名单（含旁白/场记/玩家名）→ 整行裁掉并 log；
          - 行内 [STATE:]/[NOTE:]/[PRIVATE_REQ] 标签按该行角色的权限校验分发
            （复用 apply_reply：npc_learned_/mood_/npc_location_ 只许本人=该行角色，
            全局 key 沿用现有校验）；knows_secret 秘密提议一律拒绝（群场零泄漏）；
          - [NOTE:] 归属该行角色入冒险手册；[PRIVATE_REQ] 归属该行 NPC。
        节奏硬限：单行 >80 字裁行、总 >12 行裁尾、总 >500 字裁尾（结构上不可能长墙）。

        Returns:
            dict: replies=[{npc_id, name, reply}]（行序）、changes、notices、private_requests
        """
        name_to_id = {}
        for rid in responders:
            name_to_id[self.get_npc(rid).get("name", rid).strip()] = rid
        replies: list = []
        changes: list = []
        notices: list = []
        private_requests: list = []
        total_chars = 0
        for raw_line in (raw or "").splitlines():
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            m = STAGE_LINE_RE.match(raw_line)
            if not m:
                logger.info(f"剧场行裁掉（无说话人标签）: {raw_line[:50]}")
                continue
            speaker, body = m.group(1).strip(), m.group(2).strip()
            rid = name_to_id.get(speaker)
            if not rid:
                # 容错：说话人带前后缀时做包含匹配兜底（如「[老周（工程师）]」）
                rid = next(
                    (i for n, i in name_to_id.items()
                     if n and (n in speaker or speaker in n)),
                    None,
                )
            if not rid:
                logger.info(f"剧场行裁掉（说话人不在场）: [{speaker}]")
                continue
            # 防模型不听话输出 markdown：清掉加粗/标题/列表记号
            body = body.replace("**", "").replace("__", "")
            body = re.sub(r"^(?:[#>\-]+\s*)+", "", body).strip()
            if not body:
                continue
            # 行长只按台词长度算：[STATE:]/[PRIVATE_REQ]/[NOTE:] 是给导演的元数据，
            # 不计入单行/总字上限（否则带长标签的合法台词行会被误裁）
            body_for_len = STATE_TAG_RE.sub("", PRIVATE_REQ_RE.sub("", NOTE_RE.sub("", body)))
            if len(body_for_len.strip()) > _STAGE_LINE_MAX:
                logger.info(f"剧场行裁掉（{len(body_for_len.strip())} 字超单行上限）: {body[:50]}")
                continue
            if len(replies) >= _STAGE_LINES_MAX:
                logger.info("剧场裁尾：台词行数达上限")
                break
            if total_chars + len(body_for_len.strip()) > _STAGE_CHARS_MAX:
                logger.info("剧场裁尾：总字数达上限")
                break
            npc_name = self.get_npc(rid).get("name", rid)
            # 逐行标签解析：权限按该行角色（allow_secrets=False 群场禁揭示）
            parsed = self.apply_reply(body, npc_name, npc_id=rid, allow_secrets=False)
            changes.extend(parsed["changes"])
            notices.extend(parsed["notices"])
            clean = parsed["clean"].strip()
            if clean:
                replies.append({"npc_id": rid, "name": npc_name, "reply": clean})
                total_chars += len(clean)
                # [NOTE:] 重要信息入冒险手册（归属该行角色）
                self.add_notes(npc_name, parsed.get("notes", []))
                # 8.20c 兜底结算：时间投满的动作任务与发布人（该行角色）对话即确认
                notices.extend(self.auto_settle_report(rid))
            if parsed["private_requests"]:
                private_requests.append({"npc_id": rid, "name": npc_name})
        if not replies:
            logger.warning("剧场解析后无有效台词行（模型可能未按剧本格式输出）")
        return {
            "replies": replies,
            "changes": changes,
            "notices": notices,
            "private_requests": private_requests,
        }

    # ── 后置解析与状态推进 ────────────────────────────

    def apply_reply(self, reply: str, npc_name: str = "", npc_id: str = "",
                    allow_secrets: bool = True) -> dict:
        """解析单个演出 AI 回复：剥标签 → 白名单应用（flags + 玩家地点 + 数值/认知/心情）。

        Args:
            reply: 演出 AI 原始回复（可含 [STATE:] / [PRIVATE_REQ] / [NOTE:] 标签）
            npc_name: 该回复所属 NPC 名（[NOTE:] 入手册时标注来源）
            npc_id: 该回复所属 NPC 编号（8.21：npc_learned_/mood_ 只许写本人的，校验用）
            allow_secrets: False = 拒绝 knows_secret_* 揭示提议（8.24 群场剧场专用——
                群场注入本就不含未揭示秘密，谁也不当众说破；单聊保持 True 不受影响）

        Returns:
            dict:
              clean   剥除全部标签后的干净回复（给玩家看的）
              changes 本轮实际生效的变更列表（字符串，调试/日志用）
              private_requests 该 NPC 是否发出了 [PRIVATE_REQ] 私聊请求（bool）
              notices 程序产生的固定旁白（地点移动播报/私聊断开提示等）
              notes   [NOTE:] 提取的重要信息列表（未入库；调用方决定是否 add_notes）
        """
        changes: list = []
        notices: list = []
        notes = [n.strip() for n in NOTE_RE.findall(reply or "") if n.strip()]

        tags = STATE_TAG_RE.findall(reply or "")
        private_req = bool(PRIVATE_REQ_RE.search(reply or ""))
        clean = _strip_tags(reply or "")

        # 白名单应用：key 必须已存在于 state.flags，且属于受支持形态；location 是特殊 key
        for content in tags:
            for token in content.split(","):
                token = token.strip()
                if not token:
                    continue
                # 8.21a 相对增减型数值提议（hp+10 / hunger-20），先于 k=v 通道解析
                m = MUTATE_RE.match(token)
                if m:
                    change = self._mutate_player(m.group(1), m.group(2) + m.group(3),
                                                 absolute=False)
                    if change:
                        changes.append(change)
                    continue
                if "=" not in token:
                    continue
                key, _, val = token.partition("=")
                key = key.strip()
                val = val.strip()
                if key.lower() == "location":
                    notice = self._move_player(val)
                    if notice:
                        changes.append(f"玩家地点 → {self.player_location()}")
                        notices.append(notice)
                    continue
                # 8.21a 认知原语：npc_learned_{nid}=事实（权限：只许写本人的认知）
                if key.lower().startswith("npc_learned_"):
                    change = self._npc_learn(key[len("npc_learned_"):].strip(), val, npc_id)
                    if change:
                        changes.append(change)
                    continue
                # 8.21b 心情慢变量：mood_{nid}=枚举（权限：只许本人提议）
                if key.lower().startswith("mood_"):
                    change = self._set_mood(key[len("mood_"):].strip(), val, npc_id)
                    if change:
                        changes.append(change)
                    continue
                # 8.23 位置跟随：npc_location_{nid}=地点（权限：只许本人提议）
                if key.lower().startswith("npc_location_"):
                    notice = self._move_npc(key[len("npc_location_"):].strip(), val, npc_id)
                    if notice:
                        changes.append(notice)
                        notices.append(notice)
                    continue
                # 8.21a 绝对赋值型数值提议（hp=50），先于 flags 白名单
                if key in _MUTABLE_KEYS:
                    change = self._mutate_player(key, val, absolute=True)
                    if change:
                        changes.append(change)
                    continue
                change = self._apply_flag(key, val, allow_secrets=allow_secrets)
                if change:
                    changes.append(change)

        return {
            "clean": clean,
            "changes": changes,
            "private_requests": private_req,
            "notices": notices,
            "notes": notes,
        }

    def add_notes(self, npc_name: str, notes: list) -> int:
        """把 [NOTE:] 提取的重要信息写入冒险手册（state.manual_notes），返回新增条数。

        同一 NPC 说过的同一句话去重；NPC 是该 NPC 当前对玩家的重要交代（玩家视角）。
        """
        if not notes:
            return 0
        manual = self.state.setdefault("manual_notes", [])
        added = 0
        for content in notes:
            content = (content or "").strip()
            if not content:
                continue
            if any(m.get("npc") == npc_name and m.get("content") == content for m in manual):
                continue
            manual.append({
                "day": self.state.get("clock", {}).get("day", 1),
                "time": self.state.get("clock", {}).get("time", ""),
                "npc": npc_name,
                "content": content,
            })
            added += 1
        if added:
            store.save_state(self.identity, self.state)
        return added

    def take_quest_notices(self) -> list:
        """取走本轮任务跃迁系统提示（取后清空；pipeline 拼进返回给前端的 notices）。"""
        out = self._quest_notices
        self._quest_notices = []
        return out

    def end_prompt_reminder(self) -> str:
        """动作型任务结案的导演末位提醒（8.20c）。

        存在「时间已投入达标、尚未确认汇报、仍处于 active」的动作任务时返回提醒文本，
        由 pipeline 拼到 user prompt 最末尾（末位指令遵从率最高）——
        解决注入段里汇报要求被叙事惯性压过、A3 漏发 reported 标签的问题。
        无满足条件的任务返回空串（不增加 token）。
        """
        lines = []
        for q in self.story.get("quests", []):
            qid = q.get("id", "")
            if q.get("judge_mode", "flags") != "time":
                continue
            if self._flags.get(f"quest_{qid}_state") != "active":
                continue
            if not self._flags.get(f"milestone_{qid}_time_ready"):
                continue
            if self._flags.get(f"milestone_{qid}_reported"):
                continue
            lines.append(
                f"若玩家本轮在汇报任务「{q.get('name', '')}」已完成"
                f"（如「录好了」「整理完了」「交给你了」），"
                f"你的回复末尾必须原样输出一行 [STATE: milestone_{qid}_reported=true]，"
                f"否则任务无法结案；玩家本轮没有汇报就不要输出。"
            )
        if not lines:
            return ""
        return "（导演硬性提醒）" + "".join(lines)

    def auto_settle_report(self, npc_id: str) -> list:
        """汇报环节的程序级兜底（8.20c 用户裁定）。

        时间已投满（time_ready）的动作任务：只要玩家与发布人完成任意一轮对话
        （本轮该 NPC 有回复即算），若 AI 仍未提议 reported 标签，程序补写并
        记入冒险手册——玩家不必反复尴尬地口头汇报。
        AI 主动提议过（reported 已为 true）则跳过，维持原方案优先。
        返回给前端的提示列表。
        """
        notices = []
        changed = False
        for q in self.story.get("quests", []):
            if q.get("giver") != npc_id or q.get("judge_mode", "flags") != "time":
                continue
            qid = q.get("id", "")
            if self._flags.get(f"quest_{qid}_state") != "active":
                continue
            if not self._flags.get(f"milestone_{qid}_time_ready"):
                continue
            if self._flags.get(f"milestone_{qid}_reported"):
                continue
            self._flags[f"milestone_{qid}_reported"] = True
            changed = True
            name = q.get("name", qid)
            giver_name = self.get_npc(npc_id).get("name", npc_id)
            notices.append(f"📣 {giver_name}确认了任务「{name}」的完成情况。")
            self.add_notes(giver_name, [f"确认了任务「{name}」已完成。"])
        if changed:
            # reported 补写后 done_flags 可能满足 → 统一跃迁判定 + 落盘
            self.finalize()
            store.save_state(self.identity, self.state)
        return notices

    def report_quest(self, quest_id: str) -> dict:
        """玩家点任务面板「汇报完成」按钮（8.20d）：汇报 = 系统级指令 + 对话活动。

        系统级：校验通过后程序直接写 reported=true（确定性，不依赖 AI 提议）→
        finalize 跃迁判定（时间已投满 → done_flags 满足 → completable）。
        对话活动：返回发布人信息与代拟汇报消息 draft，前端自动代发给 NPC，
        NPC 的回复是验收演出（注入里已有"已确认汇报"分支），不影响状态。
        返回 {ok, error, npc_id, npc_name, draft}。
        """
        q = self.get_npc_quest(quest_id)
        if not q:
            return {"ok": False, "error": f"任务不存在: {quest_id}"}
        qid = q.get("id", "")
        if q.get("judge_mode", "flags") != "time":
            return {"ok": False, "error": "该任务不是投入型任务，无需面板汇报"}
        if self._flags.get(f"quest_{qid}_state") != "active":
            return {"ok": False, "error": "该任务当前不可汇报"}
        if not self._flags.get(f"milestone_{qid}_time_ready"):
            return {"ok": False, "error": "时间还没投满，先通过任务面板调时投入"}
        if self._flags.get(f"milestone_{qid}_reported"):
            return {"ok": False, "error": "这个任务已经汇报过了"}
        giver_id = q.get("giver", "")
        giver = self.get_npc(giver_id)
        giver_name = giver.get("name", giver_id)
        if self.npc_location(giver_id) != self.player_location():
            return {"ok": False,
                    "error": f"需要到 {giver_name}（{self.npc_location(giver_id)}）身边当面汇报"}
        self._flags[f"milestone_{qid}_reported"] = True
        self.finalize()
        return {
            "ok": True, "error": "", "npc_id": giver_id, "npc_name": giver_name,
            "draft": f"我把「{q.get('name', '')}」的活儿干完了，跟你汇报一声。",
        }

    def _push_quest_notice(self, q: dict, prev: str, now: str) -> None:
        """任务状态跃迁时生成玩家可见提示（8.19 测试反馈：接取/可完成/失败都要有系统提示）。"""
        qid = q.get("id", "")
        name = q.get("name") or qid
        minutes = q.get("time_cost_minutes", 0)
        is_time = q.get("judge_mode", "flags") == "time"
        if prev == "undiscovered" and now == "active":
            cond = q.get("complete_condition") or ""
            extra = f"，完成条件：{cond}" if cond else ""
            if is_time:
                hint = f"（需投入约 {minutes} 分钟——可在右上角任务面板分批调时，干完后向发布人汇报）"
            else:
                hint = f"（预计耗时 {minutes} 分钟，进度见右上角任务面板）"
            self._quest_notices.append(f"📌 接取任务「{name}」{extra}{hint}")
        elif prev in ("undiscovered", "active") and now == "completable":
            invested = self.state.get("quest_invested", {}).get(qid, 0)
            remain = max(0, minutes - invested)
            if remain > 0:
                extra = f"（推进 {remain} 分钟）"
            else:
                extra = "（时间已在投入时消耗，直接点击完成即可）"
            self._quest_notices.append(
                f"✅ 任务「{name}」目标达成！可在右上角任务面板点击「完成」{extra}"
            )
        elif now == "failed" and prev != "failed":
            self._quest_notices.append(f"❌ 任务「{name}」已失败。")

    def finalize(self) -> tuple:
        """一轮对话全部回复应用完毕后的程序判定（幂等，可安全重复调用）。

        Returns:
            (changes, events)
            changes: 变更描述列表（日志用）
            events:  触发的事件（里程碑 narration_brief 等，交给 narrator 转写）
        """
        changes: list = []
        events: list = []

        # 1) 任务自动判定（8.11）：accept_flags → active；done_flags → completable（只进不退）
        for q in self.story.get("quests", []):
            qid = q.get("id")
            key = f"quest_{qid}_state"
            cur = self._flags.get(key)
            if cur not in ("undiscovered", "active"):
                continue
            accept = (q.get("accept_flags") or "").strip()
            if cur == "undiscovered" and accept and _eval_condition(accept, self._flags):
                self._flags[key] = "active"
                changes.append(f"任务 {qid} 激活（accept_flags 满足）")
                self._push_quest_notice(q, "undiscovered", "active")
            done_flags = (q.get("done_flags") or "").strip()
            if self._flags.get(key) == "active" and done_flags and _eval_condition(done_flags, self._flags):
                self._flags[key] = "completable"
                changes.append(f"任务 {qid} 转为可完成（done_flags 满足，等待玩家点击完成）")
                self._push_quest_notice(q, "active", "completable")

        # 2) 里程碑评估（按 order 升序；已触发的跳过）
        for m in sorted(self.story.get("milestones", []), key=lambda x: x.get("order", 0)):
            mid = m.get("id")
            mkey = f"milestone_{mid}"
            if mkey not in self._flags or self._flags.get(mkey):
                continue
            if _eval_condition(m.get("trigger", ""), self._flags):
                self._flags[mkey] = True
                events.append(m.get("narration_brief") or f"里程碑 {mid} 达成")
                changes.append(f"里程碑 {mid} 触发")

        # 3) 终局评估（8.11 多终局：先满足先生效；达成后保持 ended，不重复触发）
        if not self.state.get("ended"):
            for e in self._endings_list():
                if _eval_condition(e.get("condition", ""), self._flags):
                    self.state["ended"] = True
                    self.state["ending_reached"] = e.get("id")
                    changes.append(f"终局达成：{e.get('title', e.get('id', ''))}")
                    break

        return changes, events

    def _endings_list(self) -> list:
        """统一的终局列表：新 endings[] 优先；旧副本回退单 ending_condition。"""
        endings = self.story.get("endings")
        if isinstance(endings, list) and endings:
            return [e for e in endings if isinstance(e, dict)]
        return [{
            "id": "ending",
            "title": "终局",
            "condition": self.story.get("ending_condition", ""),
            "ending_text": self.story.get("ending_text", ""),
        }]

    def ending_text(self) -> str:
        """已达成的终局文本（ending_reached 优先，其次旧版 ending_text，再退第一条终局）。"""
        reached = self.state.get("ending_reached")
        endings = self._endings_list()
        if reached:
            for e in endings:
                if e.get("id") == reached:
                    return e.get("ending_text", "")
        legacy = self.story.get("ending_text", "")
        if legacy:
            return legacy
        return endings[0].get("ending_text", "") if endings else ""

    # ── 8.21 类型化状态原语与 NPC 慢变量 ──────────────

    def _attitude(self, npc_id: str) -> dict:
        """取某 NPC 的态度记录（缺省自动补默认：好感/信任 50，心情平静）。"""
        table = self.state.setdefault("npc_attitude", {})
        att = table.setdefault(npc_id, {"affinity": 50, "trust": 50, "mood": "平静"})
        att.setdefault("affinity", 50)
        att.setdefault("trust", 50)
        att.setdefault("mood", "平静")
        return att

    def _adjust_attitude(self, npc_id: str, affinity: int = 0, trust: int = 0) -> None:
        """程序事件驱动的好感/信任调整（8.21b：AI 无权直接写这两项，防讨好循环）。

        事件清单：任务完成 giver+8 / 任务失败 giver-8 / 秘密揭示持有人+10 /
        私聊被玩家接受 发起者+5。钳 0~100。
        """
        if not npc_id or (affinity == 0 and trust == 0):
            return
        att = self._attitude(npc_id)
        att["affinity"] = max(0, min(100, att["affinity"] + affinity))
        att["trust"] = max(0, min(100, att["trust"] + trust))
        logger.info(
            f"态度事件调整: {npc_id} 好感→{att['affinity']} 信任→{att['trust']}"
            f"（{affinity:+d}/{trust:+d}）"
        )

    def _mutate_player(self, key: str, raw: str, absolute: bool = False):
        """8.21a 数值原语应用：白名单键 + 单轮 ±30 钳制 + 值域截断。

        Args:
            key: 玩家数值键（仅 hp/hunger，NPC 侧数值不开）
            raw: 标签里的数值文本（如 "10" / "-20" / "50"）
            absolute: True=绝对赋值（hp=50，按与当前值之差钳制），False=相对增减（hp+10）
        """
        if key not in _MUTABLE_KEYS:
            return None
        try:
            num = int((raw or "").strip())
        except (TypeError, ValueError):
            return None
        player = self.state.setdefault("player", {})
        if key == "hp":
            cur = int(player.get("hp", int(player.get("max_hp", 100))))
            hi = int(player.get("max_hp", 100))
        else:
            cur = int(player.get("hunger", 100))
            hi = 100
        delta = (num - cur) if absolute else num
        # 防作弊：单轮单键 ±30 钳制
        delta = max(-_MUTATE_TURN_CLAMP, min(_MUTATE_TURN_CLAMP, delta))
        if delta == 0:
            return None
        new = max(0, min(hi, cur + delta))
        if new == cur:
            return None
        player[key] = new
        label = "体力" if key == "hp" else "饱食度"
        return f"玩家{label} {cur} → {new}（{delta:+d}）"

    def _npc_learn(self, nid: str, fact: str, reply_nid: str = ""):
        """8.21a 认知原语：NPC 得知新事实 → state.npc_knowledge（去重、上限 8 条 FIFO）。

        权限：nid 必须是本轮回复 NPC 本人（防 A 替 B 记忆）；fact 非空才记。
        """
        fact = (fact or "").strip()
        if not nid or not fact:
            return None
        if not reply_nid or nid != reply_nid:
            logger.info(f"丢弃认知提议（只许写本人认知）: {nid} ≠ 回复者 {reply_nid or '未知'}")
            return None
        try:
            self.get_npc(nid)
        except ValueError:
            return None
        facts = self.state.setdefault("npc_knowledge", {}).setdefault(nid, [])
        if fact in facts:
            return None
        facts.append(fact)
        while len(facts) > _NPC_KNOWLEDGE_MAX:
            facts.pop(0)
        name = self.get_npc(nid).get("name", nid)
        return f"{name} 认知 +1：{fact}"

    def _set_mood(self, nid: str, val: str, reply_nid: str = ""):
        """8.21b 心情提议应用：白名单枚举校验 + 只许本人提议。"""
        val = (val or "").strip()
        if val not in _MOODS:
            return None
        if not reply_nid or nid != reply_nid:
            logger.info(f"丢弃心情提议（只许写本人心情）: {nid} ≠ 回复者 {reply_nid or '未知'}")
            return None
        try:
            self.get_npc(nid)
        except ValueError:
            return None
        att = self._attitude(nid)
        if att["mood"] == val:
            return None
        att["mood"] = val
        name = self.get_npc(nid).get("name", nid)
        return f"{name} 心情 → {val}"

    def _move_npc(self, nid: str, loc: str, reply_nid: str = ""):
        """8.23 NPC 位置跟随提议：本人限定 + 地点白名单校验。

        修复 8.22 遗留①：AI 带玩家移动时可通过 [STATE: npc_location_自己=地点] 带上
        本人，否则玩家被单独挪走、NPC 留原地会被同地点硬校验拦对话（"跟丢"）。

        Returns:
            固定旁白字符串（移动/私聊断开播报），无效提议返回 None。
        """
        loc = (loc or "").strip()
        if not loc or loc not in self.locations_whitelist():
            logger.info(f"丢弃 NPC 位置提议（不在地点白名单）: {nid} → {loc}")
            return None
        if not reply_nid or nid != reply_nid:
            logger.info(f"丢弃 NPC 位置提议（只许写本人位置）: {nid} ≠ 回复者 {reply_nid or '未知'}")
            return None
        try:
            npc = self.get_npc(nid)
        except ValueError:
            return None
        if self.npc_location(nid) == loc:
            return None
        self.state.setdefault("npc_locations", {})[nid] = loc
        name = npc.get("name", nid)
        # 移到玩家所在地点 = 跟随；移到别处 = 离开，若该 NPC 是私聊对象则私聊自动断开
        if self.player_location() == loc:
            return f"{name}也来到了{loc}。"
        notice = f"{name}动身去了{loc}，离开了这里。"
        if self.state.get("private_with") == nid:
            self.state["private_with"] = None
            notice += f"{name}走开了，私聊结束了。"
        return notice

    def _apply_flag(self, key: str, val: str, allow_secrets: bool = True):
        """把单个 key=值 提议应用到 flags（白名单 + 值域校验 + 只进不退）。

        仅认五类 key：
          knows_secret_*（布尔）/ met_npc_*（布尔）/ quest_*_state（枚举，AI 只能提议
          active / failed，且只能前进不能倒退）/ 动作型任务汇报标记 milestone_{qid}_reported
          （布尔，仅 _ai_reported_flags 白名单内的 key）/ 任务条件引用的剧情里程碑
          milestone_q*_xxx（布尔，仅 _ai_milestone_flags 白名单，8.24c）。
        knows_secret_* 提议在 allow_secrets=False 时一律拒绝（8.24 群场剧场禁揭示）。
        time_ready 与 trigger-only 剧本里程碑 milestone_{mid} 只许程序写，AI 提议一律拒绝；
        AI 编造的 key、非法值一律丢弃；无实际变化的也不记。生效返回变更描述，否则 None。
        """
        if not key or key not in self._flags:
            return None  # 白名单：AI 发明的 key 直接丢弃
        if key.startswith("knows_secret_"):
            if not allow_secrets:
                logger.info(f"丢弃秘密揭示提议（群场剧场禁揭示）: {key}")
                return None
        if key.startswith("knows_secret_") or key.startswith("met_npc_"):
            b = _parse_bool(val)
            if b is None or self._flags[key] == b:
                return None
            self._flags[key] = b
            # 8.21b 事件钩子：秘密揭示成功 → 持有人好感 +10
            if b and key.startswith("knows_secret_"):
                for holder in self._get_secret(key[len("knows_secret_"):]).get("holders", []):
                    self._adjust_attitude(holder, affinity=10)
            return f"{key} → {b}"
        if key in self._ai_reported_flags:
            b = _parse_bool(val)
            if b is None or self._flags[key] == b:
                return None
            self._flags[key] = b
            return f"{key} → {b}"
        if key in self._ai_milestone_flags:
            b = _parse_bool(val)
            if b is None or self._flags[key] == b:
                return None
            self._flags[key] = b
            return f"{key} → {b}"
        if key.startswith("quest_") and key.endswith("_state"):
            v = (val or "").strip().lower()
            # AI 只能提议 active / failed；completable/done 由程序判定
            if v not in _AI_QUEST_STATES:
                logger.debug(f"丢弃任务状态提议（仅程序可判或非法）: {key}={v}")
                return None
            cur = self._flags[key]
            # 只进不退：当前已经是更靠后的状态则拒绝
            if _QUEST_ORDER.get(cur, 0) >= _QUEST_ORDER.get(v, 0):
                return None
            self._flags[key] = v
            qid = key[len("quest_"):-len("_state")]
            # 8.21b 事件钩子：任务失败 → 发布人好感 -8
            if v == "failed":
                self._adjust_attitude(
                    (self.get_npc_quest(qid) or {}).get("giver", ""), affinity=-8)
            self._push_quest_notice(self.get_npc_quest(qid) or {"id": qid}, cur, v)
            return f"{key} → {v}"
        return None

    def get_npc_quest(self, qid: str):
        """按 id 取任务定义（不存在返回 None）。"""
        for q in self.story.get("quests", []):
            if q.get("id") == qid:
                return q
        return None

    # ── 游戏状态操作（8.12）──────────────────────────

    def _move_player(self, loc: str):
        """把玩家移动到 loc（地点白名单硬校验）。

        Returns:
            固定旁白字符串（移动成功/私聊断开），地点不变或非法返回 None。
        """
        loc = (loc or "").strip()
        if not loc:
            return None
        if loc not in self.locations_whitelist():
            logger.info(f"丢弃地点提议（不在白名单）: {loc}")
            return None
        player = self.state.setdefault("player", {})
        if player.get("location") == loc:
            return None
        player["location"] = loc
        notice = f"你们来到了{loc}。"
        # 私聊自动断开：玩家移动后私聊对象不在新地点 → 解除并提示（8.13）
        pid = self.state.get("private_with")
        if pid:
            if self.npc_location(pid) != loc:
                name = self.get_npc(pid).get("name", pid)
                self.state["private_with"] = None
                notice += f"{name}留在了原地，私聊结束了。"
                logger.info(f"私聊因地点变更自动断开: {name}")
        return notice

    def set_location(self, loc: str) -> dict:
        """玩家主动换地点（前端地点下拉，UI 自由移动；与 AI 提议同一套白名单校验）。

        Returns:
            {"ok": bool, "notice": str|None, "error": str|None}
        """
        loc = (loc or "").strip()
        if loc not in self.locations_whitelist():
            return {"ok": False, "notice": None, "error": f"不存在地点：{loc}"}
        before = self.player_location()
        if loc == before:
            return {"ok": True, "notice": None, "error": None}
        notice = self._move_player(loc)
        return {"ok": True, "notice": notice, "error": None}

    def set_private(self, npc_id: str) -> dict:
        """建立私聊（8.13）：同地点硬校验，双方不同地点不能建立。

        Returns:
            {"ok": bool, "error": str|None}
        """
        try:
            npc = self.get_npc(npc_id)
        except ValueError:
            return {"ok": False, "error": f"副本中不存在 NPC: {npc_id}"}
        if self.npc_location(npc_id) != self.player_location():
            return {"ok": False, "error": f"{npc.get('name', npc_id)} 不在这里，无法私聊"}
        self.state["private_with"] = npc_id
        # 8.21b 事件钩子：私聊建立（请求被玩家接受）→ 该 NPC 好感 +5
        self._adjust_attitude(npc_id, affinity=5)
        return {"ok": True, "error": None}

    def clear_private(self, reason: str = None) -> str:
        """解除私聊，返回固定旁白（无私聊时返回 None）。"""
        pid = self.state.get("private_with")
        if not pid:
            return None
        self.state["private_with"] = None
        if reason:
            return reason
        name = self.get_npc(pid).get("name", pid)
        return f"你与{name}的私聊结束了。"

    def advance_clock(self, minutes: int) -> dict:
        """推进时钟（只在任务完成时调用）：跨天自动进位。

        Returns:
            推进后的 clock dict（{day, time}）。
        """
        clock = self.state.setdefault("clock", {"day": 1, "time": "08:00"})
        try:
            minutes = int(minutes)
        except (TypeError, ValueError):
            minutes = 0
        h, m = (clock.get("time", "08:00") or "08:00").split(":")[:2]
        try:
            h, m = int(h), int(m)
        except ValueError:
            h, m = 8, 0
        total = (clock.get("day", 1) - 1) * _MINUTES_PER_DAY + h * 60 + m + max(0, minutes)
        clock["day"] = total // _MINUTES_PER_DAY + 1
        cur = total % _MINUTES_PER_DAY
        clock["time"] = f"{cur // 60:02d}:{cur % 60:02d}"
        return dict(clock)

    def invest_time(self, quest_id: str, minutes: int) -> dict:
        """玩家调时投入（8.20，兼容旧副本）：进行中任务分批投入时间。

        所有 active 任务都可投入（记账 state.quest_invested + 时钟同步推进，
        时间真的在过）；投入累计达标时：time 型任务程序写
        milestone_{qid}_time_ready=true（汇报标记仍需玩家口头汇报、NPC 认可后由
        AI 提议），flags 型任务不参与完成判定（完成仍以对话达成为准）。
        写入后走统一 finalize 判定跃迁。

        Returns:
            {"ok": bool, "error": str|None, "quest_name": str, "invested": int,
             "need": int, "clock": dict|None, "notices": [str]}
        """
        empty = {"ok": False, "error": "", "quest_name": quest_id, "invested": 0,
                 "need": 0, "clock": None, "notices": []}
        q = self.get_npc_quest(quest_id)
        if not q:
            empty["error"] = f"副本中不存在任务: {quest_id}"
            return empty
        name = q.get("name", quest_id)
        empty["quest_name"] = name
        empty["need"] = q.get("time_cost_minutes", 0)
        if self._flags.get(f"quest_{quest_id}_state") != "active":
            empty["error"] = "任务不在进行中，无法投入时间"
            return empty
        try:
            minutes = int(minutes)
        except (TypeError, ValueError):
            minutes = 0
        if minutes not in (15, 30, 60):
            empty["error"] = "单次投入只能是 15/30/60 分钟"
            return empty
        invested = self.state.setdefault("quest_invested", {})
        after = invested.get(quest_id, 0) + minutes
        invested[quest_id] = after
        need = q.get("time_cost_minutes", 0)
        clock = self.advance_clock(minutes)
        notices = [f"⏳ 你为「{name}」投入了 {minutes} 分钟（累计 {after}/{need} 分钟）。"]
        is_time = q.get("judge_mode", "flags") == "time"
        if not is_time:
            # 8.23 遗留③增强：flags 型任务投入不参与完成判定，明示防困惑
            notices.append("（该任务以当面沟通汇报确认为准，投入时间只推进时间。）")
        ready_key = f"milestone_{quest_id}_time_ready"
        if is_time and need and after >= need and not self._flags.get(ready_key):
            self._flags[ready_key] = True
            try:
                giver_name = self.get_npc(q.get("giver", "")).get("name", q.get("giver", ""))
            except ValueError:
                giver_name = q.get("giver", "")
            notices.append(f"✅ 「{name}」的活儿干得差不多了，记得向{giver_name}口头汇报。")
        # time_ready 写入后 done_flags 可能整体满足（reported 已提议过）→ 走统一跃迁判定
        self.finalize()
        return {"ok": True, "error": None, "quest_name": name, "invested": after,
                "need": need, "clock": clock, "notices": notices}

    def complete_quest(self, quest_id: str) -> dict:
        """玩家点击「完成」：completable → done + 扣时推进时钟。

        扣时 = 预留时长 - 已调时投入（8.20，下限 0）：time 型任务投入必然已达标
        （time_ready 才会跃迁），完成不再扣时；flags 型任务未调时则全额扣（与
        8.12 一致），分批调时过则只扣剩余，避免双扣。

        Returns:
            {"ok": bool, "error": str|None, "minutes": int, "clock": dict|None,
             "quest_name": str}
        """
        for q in self.story.get("quests", []):
            if q.get("id") != quest_id:
                continue
            key = f"quest_{quest_id}_state"
            if self._flags.get(key) != "completable":
                return {"ok": False, "error": "任务尚未达成完成条件", "minutes": 0, "clock": None,
                        "quest_name": q.get("name", quest_id)}
            self._flags[key] = "done"
            # 8.21b 事件钩子：任务完成 → 发布人好感 +8
            self._adjust_attitude(q.get("giver", ""), affinity=8)
            need = q.get("time_cost_minutes", 30)
            invested = self.state.get("quest_invested", {}).get(quest_id, 0)
            minutes = max(0, need - invested)
            if minutes > 0:
                clock = self.advance_clock(minutes)
            else:
                clock = dict(self.state.get("clock", {}))
            logger.info(f"任务完成 {quest_id}，时间推进 {minutes} 分钟（已投入 {invested}）→ {clock}")
            return {"ok": True, "error": None, "minutes": minutes, "clock": clock,
                    "quest_name": q.get("name", quest_id)}
        return {"ok": False, "error": f"副本中不存在任务: {quest_id}", "minutes": 0,
                "clock": None, "quest_name": quest_id}

    # ── 导演侧主动揭示 ────────────────────────────────

    def reveal_secret(self, secret_id: str) -> bool:
        """导演主动揭示某秘密（如里程碑触发时调用）：置 knows_secret_{sid}=True。"""
        key = f"knows_secret_{secret_id}"
        if key not in self._flags:
            logger.warning(f"试图揭示不存在的秘密: {secret_id}")
            return False
        self._flags[key] = True
        # 8.21b 事件钩子：导演主动揭示 → 持有人好感 +10
        for holder in self._get_secret(secret_id).get("holders", []):
            self._adjust_attitude(holder, affinity=10)
        return True

    def set_scene(self, scene: str) -> None:
        """更新当前场景（导演/里程碑推进时可选调用）。"""
        if scene:
            self.state["scene"] = scene

    # ── 公开视图与持久化 ──────────────────────────────

    def get_public_state(self) -> dict:
        """给前端的剧本公开视图——绝不含 secrets 原文与 NPC 内心目标。"""
        ploc = self.player_location()
        npcs = [
            {
                "id": n.get("id", ""),
                "name": n.get("name", ""),
                "opening": n.get("opening", ""),
                "location": self.npc_location(n.get("id", "")),
                "available": self.npc_location(n.get("id", "")) == ploc,
            }
            for n in self.story.get("npcs", [])
        ]
        npc_name_by_id = {n.get("id", ""): n.get("name", "") for n in self.story.get("npcs", [])}
        # 玩家可见的任务：只返回已发现（非 undiscovered）的——玩家自己发现任务，
        # 系统提示接取，未发现的任务对玩家完全隐藏（8.19 测试反馈）
        visible_quests = [
            q for q in self.story.get("quests", [])
            if self._flags.get(f"quest_{q.get('id')}_state", q.get("state", "undiscovered")) != "undiscovered"
        ]
        quests = [
            {
                "id": q.get("id", ""),
                "name": q.get("name", ""),
                "giver": npc_name_by_id.get(q.get("giver", ""), q.get("giver", "")),
                "state": self._flags.get(f"quest_{q.get('id')}_state", q.get("state", "undiscovered")),
                "state_cn": _QUEST_STATE_CN.get(
                    self._flags.get(f"quest_{q.get('id')}_state", q.get("state", "undiscovered")), ""),
                "judge_mode": q.get("judge_mode", "flags"),
                "invested_minutes": self.state.get("quest_invested", {}).get(q.get("id", ""), 0),
                "time_ready": bool(self._flags.get(f"milestone_{q.get('id', '')}_time_ready")),
                "reported": bool(self._flags.get(f"milestone_{q.get('id', '')}_reported")),
                "reward": q.get("reward", ""),
                "complete_condition": q.get("complete_condition", ""),
                "time_cost_minutes": q.get("time_cost_minutes", 0),
            }
            for q in visible_quests
        ]
        player = self.state.get("player", {})
        pid = self.state.get("private_with")
        persona = self.story.get("player_persona") if isinstance(self.story.get("player_persona"), dict) else {}
        return {
            "identity": self.identity,
            "title": self.story.get("title", ""),
            "npcs": npcs,
            "quests": quests,
            "routes": self.story.get("routes", []),
            "locations": self.locations_whitelist(),
            "endings": [{"id": e.get("id", ""), "title": e.get("title", "")} for e in self._endings_list()],
            "ending_reached": self.state.get("ending_reached"),
            "player": dict(player),
            "player_persona": {
                "name": persona.get("name", ""),
                "identity": persona.get("identity", ""),
                "background": persona.get("background", ""),
                "skills": persona.get("skills", ""),
            },
            "manual_notes": list(self.state.get("manual_notes", [])),
            "clock": dict(self.state.get("clock", {})),
            "clock_text": self.clock_text(),
            "private_with": pid,
            "private_name": self.get_npc(pid).get("name", pid) if pid else None,
            "scene": self.state.get("scene", ""),
            "ended": bool(self.state.get("ended")),
            "ending_text": self.ending_text() if self.state.get("ended") else "",
        }

    def auto_meet(self, npc_ids) -> None:
        """8.24b 程序兜底「出过台词即相识」：本轮真的对玩家说过话的 NPC，由程序
        直接写 met_npc（AI 打标保留为双保险）。

        相识是「照过面」级别的确定事实，不该依赖 AI 概率性打标——漏打会锁死
        以 met 为 accept_flags 条件的任务链（8.27 星露实测：met_npc_n1 漏打
        → q1 永远 undiscovered → 任务链全锁）。只写不退，幂等。
        """
        for rid in npc_ids:
            if rid and not self._flags.get(f"met_npc_{rid}", False):
                self._flags[f"met_npc_{rid}"] = True
                logger.info(f"[兜底] met_npc_{rid}=true（该角色本轮出过台词，程序自动记相识）")

    # ── 8.24d A4 分区输出的状态提议通道 ─────────────────

    def state_brief_for_ai(self) -> str:
        """生成给 A4 状态总分析调用的「可用键清单 + 当前值」上下文。

        A4 分区输出的 flags 提议只有落在这份清单里的键才会被 apply_ai_proposals
        放行——清单即白名单的 AI 可见面（先注册后使用，RPG Maker 式铁律）。
        """
        lines = ["可用键清单（flags 只允许使用这些键）："]
        # 任务状态键 + 条件原文（条件里点名确切 key，A4 照单打标）
        for q in self.story.get("quests", []):
            qid = q.get("id", "")
            cur = self._flags.get(f"quest_{qid}_state", "undiscovered")
            lines.append(
                f"- quest_{qid}_state（当前={cur}）｜任务「{q.get('name', '')}」"
                f"接取条件: {q.get('accept_flags', '')}｜完成条件: "
                f"{q.get('done_flags') or q.get('complete_condition', '')}"
            )
        # 相识键
        met_keys = sorted(k for k in self._flags if k.startswith("met_npc_"))
        if met_keys:
            lines.append("- " + "、".join(
                f"{k}（当前={str(bool(self._flags[k])).lower()}）" for k in met_keys))
        # 秘密键（含内容摘要与持有人，A4 需要判断「内容是否已说破」）
        for s in self.story.get("secrets", []):
            sid = s.get("id", "")
            if not sid:
                continue
            revealed = "已揭示" if self._flags.get(f"knows_secret_{sid}") else "未揭示"
            content = s.get("summary") or s.get("content") or s.get("description") or ""
            lines.append(
                f"- knows_secret_{sid}（{revealed}｜持有人={'、'.join(s.get('holders', []))}）"
                f"内容: {content}"
            )
        # AI 可写的剧情里程碑键 / 汇报键
        if self._ai_milestone_flags:
            lines.append("- " + "、".join(
                f"{k}（当前={str(bool(self._flags.get(k, False))).lower()}）"
                for k in sorted(self._ai_milestone_flags)))
        if self._ai_reported_flags:
            lines.append("- " + "、".join(sorted(self._ai_reported_flags)))
        # 位置事实（attitude/npc_location 提议的事实基础）
        lines.append(f"- 玩家位置: {self.player_location()}")
        for n in self.story.get("npcs", []):
            lines.append(
                f"- NPC {n.get('id')}（{n.get('name', '')}）位置: {self.npc_location(n.get('id'))}")
        lines.append("- 地点白名单: " + "、".join(self.locations_whitelist()))
        return "\n".join(lines)

    def allowed_flag_keys(self) -> dict:
        """8.24f：AI 可提议键的结构化清单（供 A4 tool calling 的参数枚举约束）。

        与 _apply_flag 白名单同一数据源——协议枚举即白名单的前置投影：
        编造的 key 在工具参数层就提交不进去，白名单兜底仍然生效（双保险）。
        """
        quest_state_keys = [
            f"quest_{q.get('id')}_state"
            for q in self.story.get("quests", []) if q.get("id")
        ]
        bool_keys = set()
        bool_keys |= {f"met_npc_{n.get('id')}" for n in self.story.get("npcs", []) if n.get("id")}
        bool_keys |= {f"knows_secret_{s.get('id')}" for s in self.story.get("secrets", []) if s.get("id")}
        bool_keys |= set(self._ai_milestone_flags)
        bool_keys |= set(self._ai_reported_flags)
        return {
            "quest_state_keys": quest_state_keys,
            "bool_keys": sorted(bool_keys),
            "locations": self.locations_whitelist(),
        }

    def apply_ai_proposals(self, payload, allow_secrets: bool = True) -> list:
        """消费 A4 分区输出的 JSON 状态提议（8.24d）。

        flags 走 _apply_flag 白名单（与 A3 行内标签同一条通道）；
        attitude 走 _adjust_attitude；npc_location 走 _move_npc（权限=本人）。
        群场剧场调用时 allow_secrets=False（与 A3 行内标签同规则）。
        返回生效变更描述列表；非法 payload / 编造 key 一律静默丢弃。
        """
        changes: list = []
        if not isinstance(payload, dict):
            return changes
        for raw in payload.get("flags") or []:
            if isinstance(raw, str) and "=" in raw:
                k, _, v = raw.partition("=")
                ch = self._apply_flag(k.strip(), v.strip(), allow_secrets=allow_secrets)
                if ch:
                    changes.append(f"A4提议 {ch}")
        att = payload.get("attitude")
        if isinstance(att, dict):
            for nid, v in att.items():
                try:
                    aff = int(str(v).strip().replace("＋", "+").replace("－", "-") or 0)
                except ValueError:
                    continue
                if aff and self.get_npc(nid):
                    self._adjust_attitude(nid, affinity=aff)
                    changes.append(f"A4提议 好感 {self.get_npc(nid).get('name', nid)} {aff:+d}")
        loc = payload.get("npc_location")
        if isinstance(loc, dict):
            for nid, dest in loc.items():
                notice = self._move_npc(nid, str(dest or "").strip(), nid)
                if notice:
                    changes.append(f"A4提议 {notice}")
        if changes:
            logger.info(f"[A4状态提议] 生效 {len(changes)} 项: {'; '.join(changes)}")
        return changes

    def save(self) -> None:
        """把运行时状态写回磁盘（pipeline 在 director.finalize 后调用）。"""
        store.save_state(self.identity, self.state)
