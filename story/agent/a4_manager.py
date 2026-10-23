#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A4 — 记忆管理 Agent（多身份支持）

绑定到特定身份，使用独立的 store/index。
"""

import json
import logging
import re
from datetime import datetime
from typing import Optional

from memory.retrieval import LAYER_HALFLIFE, calc_recency_factor
from agent.ai_client import client
from agent import prompts as PROMPTS
from tools.time_utils import get_time_period

logger = logging.getLogger("a4_manager")

# 玩家状态记忆的标记 tag：用于在 core 层识别"整块玩家状态核心记忆"，实现覆盖更新
PLAYER_STATE_TAG = "player_state"


# 玩家状态归属的玩家标识（当前为单玩家场景，固定为 one_player）
PLAYER_ID = "one_player"


EVENT_SIGNALS = {
    "场景切换": ["离开", "进入", "到达", "来到", "出去", "开门", "通过"],
    "新角色": ["遇到", "见到", "来了", "出现", "看见", "打招呼"],
    "物品获得": ["得到", "获得", "拿到", "捡到", "收到", "购买", "赠送"],
    "任务变更": ["任务", "完成", "失败", "接取", "提交", "目标"],
    "情绪剧变": ["震惊", "愤怒", "伤心", "感动", "害怕", "狂喜", "哭泣"],
    "关系变化": ["好感", "信任", "背叛", "和解", "约定", "承诺"],
}


def _coarse_period(now: datetime = None) -> str:
    """8 档时段归并为 4 档笼统词（早上/中午/下午/晚上），供记忆摘要文本使用。
    摘要里不写「凌晨/深夜」这类细分时段（用户要求：时间只是参考，粗粒度即可）。"""
    h = (now or datetime.now()).hour
    if 5 <= h < 11:
        return "早上"
    if 11 <= h < 13:
        return "中午"
    if 13 <= h < 19:
        return "下午"
    return "晚上"


def _build_state_tool(hints: dict = None) -> dict:
    """8.24f：构建 report_state 工具 schema（function calling，OpenAI 兼容格式）。

    hints 来自 director.allowed_flag_keys()——键枚举即白名单的协议投影：
    编造的 key 在工具参数枚举层就提交不进去，导演白名单兜底仍然生效（双保险）。
    hints 为空时（如管线未传 director）构建无枚举的宽松 schema，行为退化为普通 JSON 约束。
    """
    hints = hints or {}
    # 8.27 分支选择键并入枚举：choice_qX 的值是分支任务 id（合法值由导演裁决，
    # 枚举层只保证 key 不被编造）
    key_enum = sorted(
        set(hints.get("bool_keys") or [])
        | set(hints.get("quest_state_keys") or [])
        | {c.get("key") for c in (hints.get("choice_keys") or []) if c.get("key")}
    )
    key_prop = {"type": "string", "description": "flags 键名，只能从可用键清单中选择，禁止编造"}
    if key_enum:
        key_prop["enum"] = key_enum
    return {
        "type": "function",
        "function": {
            "name": "report_state",
            "description": "提交本轮状态总分析结果：玩家状态整块 + 世界状态变化提议。flags/attitude/npc_location 只提议本轮确实发生的事，无变化给空值。",
            "parameters": {
                "type": "object",
                "properties": {
                    "player_state": {
                        "type": "string",
                        "description": "玩家状态整块最新完整版（在旧状态基础上增量合并；无可收录就写「无」）。只记状态性事实（持有物/身体状态/所在位置/已知信息/约定），不复述剧情经过，保持精炼",
                    },
                    "flags": {
                        "type": "array",
                        "description": "状态开关变化列表，无变化给空数组",
                        "items": {
                            "type": "object",
                            "properties": {
                                "key": key_prop,
                                "value": {
                                    "type": "string",
                                    "description": "布尔键用 true/false；quest_*_state 键用 active/failed；"
                                                   "choice_* 键用分支任务 id（如 q2b，只能取该任务分支走向里列出的 id）",
                                },
                            },
                            "required": ["key", "value"],
                        },
                    },
                    "attitude": {
                        "type": "object",
                        "description": "npc_id → 本轮好感变化整数（如被真诚相待 +2、被戳穿 -3），无变化给空对象",
                    },
                    "stance": {
                        "type": "object",
                        "description": "npc_id → 本轮心理防线变化整数（9.2，-3~+3，负=更抗拒、正=更松动）。"
                                       "效果纪律：玩家有证据/筹码在手（手上亮着的物品对 NPC 可见）且施压→+2（好感常-1）；"
                                       "空手硬施压→防线不动（好感-2）；套话在好感为正时有效→+1，否则+0；"
                                       "示好→好感+2 防线+0~+1；关键证据拿在手上被对方看到并当面质问→+3；"
                                       "无明确交流攻防给空对象。跨过 +2 程序自动解锁其秘密，不必代打 knows_secret",
                    },
                    "npc_location": {
                        "type": "object",
                        "description": "npc_id → 本轮实际动身前往的地点（必须在地点白名单内），无变化给空对象",
                    },
                    "items": {
                        "type": "array",
                        "description": "物品变动提议列表（8.26：gain=获得/捡到、transfer=谁把什么给谁、take=消耗；"
                                       "9.3 note=写纸条把承诺/约定/关键线索物品化），无变化给空数组",
                        "items": {
                            "type": "object",
                            "properties": {
                                "op": {
                                    "type": "string",
                                    "enum": ["gain", "transfer", "take", "note"],
                                    "description": "gain=获得（捡到/剧情授予）、transfer=转移（from 把 item 给 to）、"
                                                   "take=消耗（owner 用掉/交出）、note=写纸条（9.3：仅当本轮出现"
                                                   "承诺/约定/关键线索这类将来需要对质或兑现的信息时用）",
                                },
                                "item": {
                                    "type": "string",
                                    "description": "物品短名（如 月桂叶）；op=note 时是纸条短标题（如 今晚交账册）。"
                                                   "可用键清单物品注册表里没有的新物品，必须在 note 写一句话描述，否则会被拒绝",
                                },
                                "count": {
                                    "type": "integer",
                                    "description": "数量，默认 1",
                                },
                                "owner": {
                                    "type": "string",
                                    "description": "gain/take 的实体 id：player 或 npc id",
                                },
                                "from": {
                                    "type": "string",
                                    "description": "transfer 的给方实体 id（只能动自己拥有或剧情明确支配的物品）",
                                },
                                "to": {
                                    "type": "string",
                                    "description": "transfer 的收方实体 id",
                                },
                                "note": {
                                    "type": "string",
                                    "description": "新物品的一句话描述（动态注册用）；注册表已有的物品留空；"
                                                   "op=note 时改为纸条内容全文（如「老陶答应今晚把账册交给你」），导演会把它做成独立纸条塞进玩家背包",
                                },
                            },
                            "required": ["op", "item"],
                        },
                    },
                    "events": {
                        "type": "array",
                        "description": "9.1 时间事件补充提议：本轮剧情中玩家确实施行了任务投入、且剧本事件表之外发生了值得呈现的发现/遭遇时提议（以旁白呈现给玩家）；无则给空数组",
                        "items": {
                            "type": "object",
                            "properties": {
                                "text": {
                                    "type": "string",
                                    "description": "旁白文案：具体呈现玩家看见/发现/遭遇了什么，有画面有细节，一句到两句",
                                },
                                "give_item": {
                                    "type": "string",
                                    "description": "可选：事件中玩家获得的物品短名；注册表没有的新物品必须同时写 item_note，否则整个事件被拒绝",
                                },
                                "item_note": {
                                    "type": "string",
                                    "description": "give_item 新物品的一句话描述（动态注册用）",
                                },
                                "set_flags": {
                                    "type": "object",
                                    "description": "可选：事件写入的布尔键（只能用键清单里列出的 key，编造会被丢弃）",
                                },
                            },
                            "required": ["text"],
                        },
                    },
                },
                "required": ["player_state", "flags", "attitude", "stance", "npc_location", "items", "events"],
            },
        },
    }


def _normalize_tool_args(args: dict) -> dict:
    """把工具调用参数归一为 director.apply_ai_proposals 消费的形态。

    flags 元素统一转 "key=value" 字符串（工具通道的 {key, value} 对象与
    文本通道的 "key=value" 字符串都兼容）；attitude/npc_location 原样透传。
    """
    flags = []
    for f in args.get("flags") or []:
        if isinstance(f, dict) and "key" in f:
            flags.append(f"{f.get('key')}={f.get('value')}")
        elif isinstance(f, str):
            flags.append(f)
    out = {"flags": flags}
    if isinstance(args.get("attitude"), dict):
        out["attitude"] = args["attitude"]
    if isinstance(args.get("stance"), dict):
        out["stance"] = args["stance"]
    if isinstance(args.get("npc_location"), dict):
        out["npc_location"] = args["npc_location"]
    if isinstance(args.get("items"), list):
        out["items"] = args["items"]
    if isinstance(args.get("events"), list):
        out["events"] = args["events"]
    return out


class A4ManagerAgent:
    def __init__(self, store, index):
        self.store = store
        self.index = index
        self.conversation_buffer = []

    def manage(self, a3_snapshot: dict, persona: dict = None,
               story_brief: str = None, state_hints: dict = None) -> dict:
        """story_brief 不为空时进入剧情状态总分析模式（8.24d 分区输出 → 8.24f 工具调用）：
        正文仍写玩家状态记忆，状态提议通过 report_state 工具调用提交（键名枚举白名单），
        放入 stats["proposals"]，由 pipeline 转交 director.apply_ai_proposals 白名单消费。
        普通身份（story_brief=None）行为完全不变。"""
        self.conversation_buffer.append(a3_snapshot)
        stats = {"summaries_generated": 0, "events_detected": 0, "memories_created": 0, "promoted": 0, "demoted": 0, "player_state_updated": 0}

        # 三档相关性：完全无关的消息跳过摘要（省 token + 不污染记忆库），重要性打折
        relevance = a3_snapshot.get("relevance", "world_related")

        time_period = get_time_period()
        if relevance == "irrelevant":
            summary = a3_snapshot.get("summary_hint", "")[:50]
            logger.info("A4: 无关消息，跳过 LLM 摘要")
        else:
            summary = self._generate_summary(a3_snapshot, time_period)
        if summary:
            stats["summaries_generated"] = 1

        events = self._detect_events(a3_snapshot)
        stats["events_detected"] = len(events)

        # T-001 玩家状态：识别本轮玩家汇报的状态，覆盖更新/创建该玩家状态核心记忆
        # 8.24d：story_brief 存在时升级为状态总分析（主区玩家状态 + 尾区 JSON 提议）
        # 8.24f：提议改走 tool calling 协议（state_hints 提供键枚举）
        player_state_updated, proposals = self._update_player_state(
            a3_snapshot, story_brief, state_hints=state_hints)
        stats["player_state_updated"] = player_state_updated
        if proposals is not None:
            stats["proposals"] = proposals

        layer = self._decide_layer(a3_snapshot, events, persona)
        importance = self._calc_importance(a3_snapshot, events)
        if relevance == "irrelevant":
            importance = round(importance * 0.3, 4)  # 无关消息不易升级为长期记忆
        from memory.retrieval import tokenize
        keywords = tokenize(a3_snapshot.get("user_message", "") + " " + a3_snapshot.get("npc_reply", ""))[:8]
        content = f"玩家: {a3_snapshot.get('user_message', '')}\nNPC: {a3_snapshot.get('npc_reply', '')}"
        event_id = events[0] if events else ""
        tags = [a3_snapshot.get("mode", "chat"), time_period]
        if events:
            tags.append("event")

        memory = self.store.create(
            content=content, layer=layer, summary=summary, keywords=keywords,
            importance=importance, event_id=event_id, tags=tags, mode=a3_snapshot.get("mode", "chat"),
            time_period=time_period,
        )
        self.index.add(memory)
        stats["memories_created"] = 1

        if events:
            promoted, demoted = self._check_promotion_demotion(events)
            stats["promoted"] = promoted
            stats["demoted"] = demoted

        if len(self.conversation_buffer) >= 10:
            self._schedule_decay()
            self.conversation_buffer = []

        logger.info(f"A4 完成 [{self.store.identity_name}]: 摘要={stats['summaries_generated']}, 事件={stats['events_detected']}, 创建={stats['memories_created']}, 升级={stats['promoted']}, 降级={stats['demoted']}, 玩家状态更新={stats['player_state_updated']}")
        return stats

    def _generate_summary(self, snapshot: dict, time_period: str = "") -> str:
        user_msg = snapshot.get("user_message", "")
        npc_reply = snapshot.get("npc_reply", "")
        # 注入日期 + 四档笼统时段（不给精确时刻，避免模型把「几点几分」写进摘要）
        # 8.24d 双时间修复：副本身份 snapshot["time_context"]="第X天 HH:MM"（游戏内时间），
        # 替代真实日期——NPC 记忆里的时间感与游戏时间保持同一套，不再泄漏现实日期
        game_time = (snapshot.get("time_context") or "").strip()
        if game_time:
            prompt = f"今天：{game_time}（游戏内时间）\n用户: {user_msg}\nNPC: {npc_reply[:500]}"
        else:
            today = datetime.now().strftime("%Y年%m月%d日")
            prompt = f"今天：{today}；时段：{_coarse_period()}（仅限早上/中午/下午/晚上四档之一）\n用户: {user_msg}\nNPC: {npc_reply[:500]}"
        try:
            return client.chat(
                system_prompt=PROMPTS.A4_SUMMARY_SYSTEM,
                user_message=prompt, temperature=0.3, max_tokens=260,
                # 频率惩罚：抑制摘要中的重复/口水词，让总结更凝练
                frequency_penalty=0.4,
            ).strip()
        except Exception as e:
            logger.warning(f"A4: 摘要失败: {e}")
            return snapshot.get("summary_hint", "")[:50]

    def _find_player_state_memory(self) -> Optional[dict]:
        """在 core 层查找"玩家状态"核心记忆（通过 PLAYER_STATE_TAG 标记）"""
        core_mems = self.store.list_by_layer("core")
        for mem in core_mems:
            if PLAYER_STATE_TAG in mem.get("tags", []):
                return mem
        return None

    def _update_player_state(self, snapshot: dict, story_brief: str = None,
                             state_hints: dict = None):
        """
        T-001 玩家状态：采用"增量迭代"思路，让 LLM 读取当前玩家状态 + 本轮新增信息，
        产出"整块最新完整版"，再由程序覆盖写回（存在则 update，否则 create 成一条 core+pinned）。

        8.24d 剧情状态总分析（story_brief 非空时）：主区 = 玩家状态整块最新版；
        8.24f 状态提议升级为 tool calling 协议：模型通过 report_state 工具提交
        flags/attitude/npc_location（键名被参数枚举约束，编造 key 提交不进去）。
        三级兜底保证最坏情况不倒退：
          ① 模型没调工具但在正文输出 ===STATE=== 尾块 → 旧文本解析（8.24e 容错）；
          ② API 不支持 tools 字段 / 调用异常 → 降级普通文本调用（提示词含降级格式说明）。
        返回 (updated 0|1, proposals dict|None)；普通模式 proposals 恒为 None。
        """
        user_msg = snapshot.get("user_message", "")
        npc_reply = snapshot.get("npc_reply", "")
        if not user_msg.strip():
            return 0, None

        existing_mem = self._find_player_state_memory()
        existing_text = existing_mem.get("content", "") if existing_mem else ""

        if story_brief:
            prompt = (
                f"{story_brief}\n\n"
                f"当前已记录的玩家状态（可能为空）：\n{existing_text}\n\n"
                f"本轮对话：\n玩家：{user_msg}\nNPC：{npc_reply}"
            )
            system_prompt = PROMPTS.A4_STORY_STATE_SYSTEM
        else:
            prompt = (
                f"当前已记录的玩家状态（可能为空）：\n{existing_text}\n\n"
                f"玩家本轮消息：{user_msg}\n\n"
                f"NPC本轮回复：{npc_reply}"
            )
            system_prompt = PROMPTS.A4_PLAYER_STATE_SYSTEM

        calls = []
        if story_brief:
            # 8.24f 工具调用通道（正文与 tool_calls 同响应并存）
            # 8.24g：max_tokens 512→1024——player_state 是整块完整版会越玩越长，
            # 512 曾被打满截断 JSON 导致提议全丢（10/03 07:30 日志确诊）
            try:
                new_state, calls = client.chat_with_tools(
                    system_prompt=system_prompt,
                    user_message=prompt,
                    tools=[_build_state_tool(state_hints)],
                    temperature=0.2,
                    max_tokens=1024,
                )
                new_state = (new_state or "").strip()
            except Exception as e:
                # ② API 不支持 tools / 网络异常 → 降级普通文本调用（最坏=8.24d 现状）
                logger.warning(f"A4: 工具调用不可用，降级文本模式: {e}")
                try:
                    new_state = client.chat(
                        system_prompt=system_prompt,
                        user_message=prompt,
                        temperature=0.2,
                        max_tokens=1024,
                    ).strip()
                except Exception as e2:
                    logger.warning(f"A4: 玩家状态增量迭代失败: {e2}")
                    return 0, None
        else:
            try:
                new_state = client.chat(
                    system_prompt=system_prompt,
                    user_message=prompt,
                    temperature=0.2,
                    max_tokens=400,
                ).strip()
            except Exception as e:
                logger.warning(f"A4: 玩家状态增量迭代失败: {e}")
                return 0, None

        # 提议提取：优先工具通道；模型没调工具时正文尾块兜底（①）
        proposals = None
        via = "无"
        if story_brief:
            tool_state = ""
            for c in calls:
                if c.get("name") == "report_state":
                    args = c.get("arguments")
                    if isinstance(args, dict):
                        proposals = _normalize_tool_args(args)
                        # 8.24f：玩家状态主区也走工具参数——DeepSeek 有 tool_calls 时
                        # content 常为空（冒烟实测），正文不可依赖
                        tool_state = str(args.get("player_state") or "").strip()
                        via = "工具"
                    else:
                        logger.warning(
                            "A4: report_state arguments 不是合法 JSON，提议丢弃。原文: %s",
                            str(args)[:200],
                        )
                    break
            if tool_state:
                new_state = tool_state
            else:
                # 正文路径：content 或 ===STATE=== 文本兜底（模型没走工具参数时）
                main_part, _, tail = new_state.partition("===STATE===")
                main_part = main_part.strip()
                tail = tail.strip()
                if tail:
                    payload = self._parse_tail_json(tail)
                    if isinstance(payload, dict):
                        proposals = payload
                        via = "文本"
                    else:
                        # 8.24e：失败时打印原始尾块前 200 字，便于排查模型输出毛病
                        logger.warning(
                            "A4: ===STATE=== 尾块不是合法 JSON，状态提议丢弃。原始尾块: %s",
                            tail[:200],
                        )
                new_state = main_part
            logger.info(
                f"A4: 状态总分析完成 [{self.store.identity_name}]: 通道={via} "
                f"提议={'有' if proposals else '无'}（flags={len((proposals or {}).get('flags') or [])}项）"
            )

        # 模型输出"无"或空 → 无可收录的玩家状态信息
        if not new_state or new_state == "无":
            return 0, proposals

        # 程序覆盖写入：命中已有则更新内容，否则新建一条 core+pinned 玩家状态记忆
        if existing_mem:
            self.store.update(existing_mem["memory_id"], content=new_state, importance=1.0)
            logger.info(
                f"A4: 玩家状态已覆盖更新 [{self.store.identity_name}/{PLAYER_ID}]: {new_state[:60]}..."
            )
        else:
            memory = self.store.create(
                content=new_state,
                layer="core",
                pinned=True,
                importance=1.0,
                tags=[PLAYER_STATE_TAG, "player", PLAYER_ID],
                mode="chat",
            )
            self.index.add(memory)
            logger.info(
                f"A4: 玩家状态已收录 [新增] [{self.store.identity_name}/{PLAYER_ID}]: {new_state[:60]}..."
            )
        return 1, proposals

    @staticmethod
    def _parse_tail_json(tail: str) -> Optional[dict]:
        """8.24e：尽量宽容地解析 ===STATE=== 尾块里的 JSON。
        模型常见毛病：markdown 代码围栏包裹、全角引号、尾逗号、JSON 前后带说明文字。
        逐级降级尝试（先原文，后清洗），返回 dict 或 None。"""
        if not tail or not tail.strip():
            return None
        candidates = [tail.strip()]

        # 变体 1：剥 markdown 代码围栏（```json ... ``` / ``` ... ```）
        fenced = re.sub(r"^```[a-zA-Z0-9]*\s*", "", tail.strip())
        fenced = re.sub(r"\s*```$", "", fenced).strip()
        if fenced and fenced != candidates[0]:
            candidates.append(fenced)

        # 变体 2：截取首个 { 到最后一个 } 之间的块（去 JSON 前后的说明文字）
        m = re.search(r"\{.*\}", fenced, re.S)
        if m:
            block = m.group(0)
            candidates.append(block)
            # 变体 3：在块基础上清洗——全角引号→半角、尾逗号去除
            cleaned = block.replace("“", '"').replace("”", '"').replace("‘", '"').replace("’", '"')
            cleaned = re.sub(r",\s*([}\]])", r"\1", cleaned)
            if cleaned != block:
                candidates.append(cleaned)

        for text in candidates:
            try:
                data = json.loads(text)
                if isinstance(data, dict):
                    return data
            except json.JSONDecodeError:
                continue
        return None

    def _detect_events(self, snapshot: dict) -> list[str]:
        combined = snapshot.get("user_message", "") + " " + snapshot.get("npc_reply", "")
        detected = []
        for event_type, signals in EVENT_SIGNALS.items():
            for signal in signals:
                if signal in combined:
                    detected.append(f"evt_{datetime.now().strftime('%Y%m%d')}_{event_type}")
                    break
        return detected

    def _decide_layer(self, snapshot: dict, events: list[str], persona: dict) -> str:
        if events:
            return "episodic"
        if snapshot.get("mode") == "knowledge":
            return "working"
        if persona:
            name = persona.get("name", "")
            combined = snapshot.get("user_message", "") + snapshot.get("npc_reply", "")
            if name and name in combined:
                return "core"
        return "working"

    def _calc_importance(self, snapshot: dict, events: list[str]) -> float:
        score = 0.3
        if events:
            score += 0.4
        if snapshot.get("mode") == "knowledge":
            score -= 0.1
        if len(snapshot.get("user_message", "")) + len(snapshot.get("npc_reply", "")) > 200:
            score += 0.1
        return min(1.0, max(0.0, score))

    def _check_promotion_demotion(self, events: list[str]) -> tuple:
        promoted = 0
        demoted = 0
        for event_id in events:
            related = self.store.search_by_event(event_id)
            working_mems = [m for m in related if m.get("layer") == "working"]
            if len(working_mems) >= 3:
                for mem in sorted(working_mems, key=lambda m: m.get("timestamp", ""))[:2]:
                    self.store.update(mem["memory_id"], layer="episodic")
                    self.index.update_field(mem["memory_id"], layer="episodic")
                    promoted += 1
        episodic_mems = self.store.list_by_layer("episodic")
        for mem in episodic_mems:
            if mem.get("pinned"):
                continue
            halflife = LAYER_HALFLIFE.get("episodic", 60)
            weight = calc_recency_factor(mem.get("last_accessed", ""), halflife, False)
            if weight < 0.15:
                self.store.update(mem["memory_id"], layer="working")
                self.index.update_field(mem["memory_id"], layer="working")
                demoted += 1
        return promoted, demoted

    def _schedule_decay(self):
        all_mems = self.store.list_all()
        updated = 0
        for mem in all_mems:
            if mem.get("pinned") or mem.get("archived"):
                continue
            layer = mem.get("layer", "working")
            halflife = LAYER_HALFLIFE.get(layer, 7)
            if halflife <= 0:
                continue
            weight = calc_recency_factor(mem.get("last_accessed", ""), halflife, False)
            if mem.get("mode") == "knowledge":
                weight = weight ** 1.5
            self.store.update(mem["memory_id"], decay_weight=weight)
            self.index.update_field(mem["memory_id"], decay_weight=weight)
            updated += 1
        logger.info(f"A4: 衰减调度完成 [{self.store.identity_name}]，更新了 {updated} 条")

    def force_maintenance(self):
        self._schedule_decay()
        self.conversation_buffer = []