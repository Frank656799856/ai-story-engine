#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
剧情副本 JSON Schema 与初始运行时状态生成

- validate_story(d)：校验造城 AI 产出的副本 JSON，返回错误清单（空列表 = 合法）
- new_state(story)：按副本内容生成初始运行时 state（flags/scene/ended/player/clock/npc_locations）

Phase 1 条件表达式只支持「flag 布尔 + quest 枚举比较」（形如 quest_q1_state==done），
由 director.py 手写解析，不做通用表达式引擎。

8.11/8.12 新增结构：
  - endings[]（多终局，2~3 个，条件互斥可分岔；旧副本单 ending_condition/ending_text 兼容保留）
  - routes[]（游览路线，2~3 条，软引导，不拦截玩家偏离）
  - locations[]（地点白名单，[STATE: location=xx] 只认这里的确切名）
  - player（hp/max_hp/hunger/location 初始值）与 clock（day/time 初始值）
  - quests 增加 accept_flags（可空=开局即激活）/ done_flags（机器可判）/ time_cost_minutes
"""

import re

# 任务状态枚举（builder 生成与 director 白名单共用）
# 状态只进不退：undiscovered → active → completable → done / failed
QUEST_STATES = ["undiscovered", "active", "completable", "done", "failed"]

# 时钟格式（24 小时制 HH:MM）
_CLOCK_RE = re.compile(r"^\d{2}:\d{2}$")


def _validate_condition_expr(val, tag: str, errors: list, required: bool) -> None:
    """校验条件表达式字段：非空字符串即可（具体语法由 director 评估时兜底）。"""
    if not isinstance(val, str) or not val.strip():
        if required:
            errors.append(f"{tag} 必须是非空字符串")
        elif val:
            errors.append(f"{tag} 必须是字符串")
        elif not required:
            return
        return


def validate_story(d) -> list:
    """校验副本 JSON 结构，返回错误清单（空列表 = 合法）。

    校验项：
      必填字段与类型 / NPC id 唯一 / 秘密 holders 指向存在的 NPC /
      NPC.known_secrets 指向存在的秘密 / milestones 有 order /
      endings[]（或旧版单终局）/ routes[] / locations[] / player / clock /
      quests 的 accept_flags/done_flags/time_cost_minutes。
    """
    errors = []
    if not isinstance(d, dict):
        return ["剧本根节点必须是 JSON 对象"]

    # ── 标题 ──
    title = d.get("title")
    if not isinstance(title, str) or not title.strip():
        errors.append("title 必须是非空字符串")

    # ── 世界观 ──
    setting = d.get("setting")
    if not isinstance(setting, dict):
        errors.append("setting 必须是对象")
    else:
        for key in ("world_name", "world_desc", "tone", "era"):
            if not isinstance(setting.get(key), str):
                errors.append(f"setting.{key} 必须是字符串")
        if not (setting.get("world_desc") or "").strip():
            errors.append("setting.world_desc 不能为空")

    # ── 地点白名单（8.12）──
    locations = d.get("locations")
    loc_set = set()
    if not isinstance(locations, list) or not (2 <= len(locations) <= 8):
        errors.append("locations 必须是 2~8 个非空字符串的数组（地点白名单）")
        locations = []
    for i, loc in enumerate(locations if isinstance(locations, list) else []):
        if not isinstance(loc, str) or not loc.strip():
            errors.append(f"locations[{i}] 必须是非空字符串")
        else:
            loc_set.add(loc.strip())

    # ── NPC ──
    npcs = d.get("npcs")
    npc_ids = set()
    if not isinstance(npcs, list) or not (2 <= len(npcs) <= 4):
        errors.append("npcs 必须是 2~4 个对象的数组")
        npcs = []
    for i, npc in enumerate(npcs if isinstance(npcs, list) else []):
        tag = f"npcs[{i}]"
        if not isinstance(npc, dict):
            errors.append(f"{tag} 必须是对象")
            continue
        for key in ("id", "name", "personality", "goal", "opening", "location"):
            if not isinstance(npc.get(key), str) or (key in ("id", "name") and not npc.get(key, "").strip()):
                errors.append(f"{tag}.{key} 必须是非空字符串" if key in ("id", "name") else f"{tag}.{key} 必须是字符串")
        if not isinstance(npc.get("known_secrets"), list):
            errors.append(f"{tag}.known_secrets 必须是字符串数组")
        nid = npc.get("id")
        if isinstance(nid, str) and nid:
            if nid in npc_ids:
                errors.append(f"NPC id 重复: {nid}")
            npc_ids.add(nid)
        # NPC 初始地点必须在白名单内
        nloc = (npc.get("location") or "").strip()
        if nloc and loc_set and nloc not in loc_set:
            errors.append(f"{tag}.location ('{nloc}') 不在 locations 白名单中")

    # ── 玩家初始状态（8.12）──
    player = d.get("player")
    if not isinstance(player, dict):
        errors.append("player 必须是对象（hp/max_hp/hunger/location）")
        player = {}
    for key in ("hp", "max_hp", "hunger"):
        v = player.get(key)
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            errors.append(f"player.{key} 必须是非负整数")
    if isinstance(player.get("hp"), int) and isinstance(player.get("max_hp"), int) \
            and player.get("hp", 0) > player.get("max_hp", 0):
        errors.append("player.hp 不能大于 player.max_hp")
    ploc = (player.get("location") or "").strip()
    if ploc and ploc not in locations:
        errors.append(f"player.location ('{ploc}') 不在 locations 白名单中")
    # 玩家角色卡（AI 自动生成或表单指定后 builder 整理）：name 必填，其余宽容
    persona = d.get("player_persona")
    if not isinstance(persona, dict) or not str(persona.get("name") or "").strip():
        errors.append("player_persona 必须是对象且 name 非空（玩家的角色卡）")
    else:
        for key in ("identity", "background", "skills"):
            if persona.get(key) is not None and not isinstance(persona.get(key), str):
                errors.append(f"player_persona.{key} 必须是字符串")

    # ── 时钟初始值（8.12）──
    clock = d.get("clock")
    if not isinstance(clock, dict):
        errors.append("clock 必须是对象（day/time）")
    else:
        day = clock.get("day")
        if not isinstance(day, int) or isinstance(day, bool) or day < 1:
            errors.append("clock.day 必须是 >=1 的整数")
        t = clock.get("time")
        if not isinstance(t, str) or not _CLOCK_RE.match(t or ""):
            errors.append('clock.time 必须是 24 小时制 "HH:MM" 格式字符串（如 "08:00"）')

    # ── 秘密 ──
    secrets = d.get("secrets")
    secret_ids = set()
    if not isinstance(secrets, list) or not secrets:
        errors.append("secrets 必须是非空数组")
        secrets = []
    for i, sec in enumerate(secrets if isinstance(secrets, list) else []):
        tag = f"secrets[{i}]"
        if not isinstance(sec, dict):
            errors.append(f"{tag} 必须是对象")
            continue
        sid = sec.get("id")
        if not isinstance(sid, str) or not sid.strip():
            errors.append(f"{tag}.id 必须是非空字符串")
        elif sid in secret_ids:
            errors.append(f"秘密 id 重复: {sid}")
        else:
            secret_ids.add(sid)
        if not isinstance(sec.get("content"), str) or not sec.get("content", "").strip():
            errors.append(f"{tag}.content 必须是非空字符串")
        holders = sec.get("holders")
        if not isinstance(holders, list) or not holders:
            errors.append(f"{tag}.holders 必须是非空字符串数组（秘密必须有人持有）")
        else:
            for h in holders:
                if h not in npc_ids:
                    errors.append(f"{tag}.holders 中的 '{h}' 不指向任何已定义的 NPC")

    # NPC 声称知晓的秘密必须真实存在（双向一致性）
    for i, npc in enumerate(npcs if isinstance(npcs, list) else []):
        if not isinstance(npc, dict):
            continue
        for sid in npc.get("known_secrets", []) if isinstance(npc.get("known_secrets"), list) else []:
            if sid not in secret_ids:
                errors.append(f"npcs[{i}].known_secrets 引用了不存在的秘密: {sid}")

    # ── 任务（8.11/8.12：机器可判 flags + 固定耗时）──
    quests = d.get("quests")
    quest_ids = set()
    if not isinstance(quests, list) or not quests:
        errors.append("quests 必须是非空数组")
        quests = []
    for i, q in enumerate(quests if isinstance(quests, list) else []):
        tag = f"quests[{i}]"
        if not isinstance(q, dict):
            errors.append(f"{tag} 必须是对象")
            continue
        for key in ("id", "name", "complete_condition", "reward"):
            if not isinstance(q.get(key), str) or (key in ("id", "name") and not q.get(key, "").strip()):
                errors.append(f"{tag}.{key} 必须是非空字符串" if key in ("id", "name") else f"{tag}.{key} 必须是字符串")
        giver = q.get("giver")
        if giver not in npc_ids:
            errors.append(f"{tag}.giver ('{giver}') 不指向任何已定义的 NPC")
        if q.get("state") not in QUEST_STATES:
            errors.append(f"{tag}.state 必须是 {'/'.join(QUEST_STATES)} 之一")
        # 判定模式（8.20）：flags=事实型（done_flags 判定）/ time=动作型（时间投入 + 口头汇报）
        if q.get("judge_mode", "flags") not in ("flags", "time"):
            errors.append(f"{tag}.judge_mode 必须是 flags/time 之一")
        qid = q.get("id")
        if isinstance(qid, str) and qid:
            if qid in quest_ids:
                errors.append(f"任务 id 重复: {qid}")
            quest_ids.add(qid)
        # 机器可判 flags（8.11）：accept_flags 可空（空 = 开局即激活），done_flags 必填
        _validate_condition_expr(q.get("accept_flags", ""), f"{tag}.accept_flags", errors, required=False)
        _validate_condition_expr(q.get("done_flags"), f"{tag}.done_flags", errors, required=True)
        # 固定耗时（8.12）：AI 考量写入，1~600 分钟
        tcm = q.get("time_cost_minutes")
        if not isinstance(tcm, int) or isinstance(tcm, bool) or not (1 <= tcm <= 600):
            errors.append(f"{tag}.time_cost_minutes 必须是 1~600 的整数（按任务量级考量）")

    # ── 游览路线（8.11，软引导）──
    routes = d.get("routes")
    route_ids = set()
    if not isinstance(routes, list) or not (2 <= len(routes) <= 3):
        errors.append("routes 必须是 2~3 个对象的数组")
        routes = []
    for i, r in enumerate(routes if isinstance(routes, list) else []):
        tag = f"routes[{i}]"
        if not isinstance(r, dict):
            errors.append(f"{tag} 必须是对象")
            continue
        rid = r.get("id")
        if not isinstance(rid, str) or not rid.strip():
            errors.append(f"{tag}.id 必须是非空字符串")
        elif rid in route_ids:
            errors.append(f"路线 id 重复: {rid}")
        else:
            route_ids.add(rid)
        for key in ("name", "brief"):
            if not isinstance(r.get(key), str) or not r.get(key, "").strip():
                errors.append(f"{tag}.{key} 必须是非空字符串")
        steps = r.get("steps")
        if not isinstance(steps, list) or not steps:
            errors.append(f"{tag}.steps 必须是非空字符串数组（有序步骤链）")
        else:
            for j, s in enumerate(steps):
                if not isinstance(s, str) or not s.strip():
                    errors.append(f"{tag}.steps[{j}] 必须是非空字符串")

    # ── 里程碑 ──
    milestones = d.get("milestones")
    if not isinstance(milestones, list) or not milestones:
        errors.append("milestones 必须是非空数组")
        milestones = []
    for i, m in enumerate(milestones if isinstance(milestones, list) else []):
        tag = f"milestones[{i}]"
        if not isinstance(m, dict):
            errors.append(f"{tag} 必须是对象")
            continue
        if not isinstance(m.get("id"), str) or not m.get("id", "").strip():
            errors.append(f"{tag}.id 必须是非空字符串")
        # order 必须存在且为整数（导演按 order 顺序评估触发）
        if not isinstance(m.get("order"), int) or isinstance(m.get("order"), bool):
            errors.append(f"{tag}.order 必须是整数")
        if not isinstance(m.get("trigger"), str) or not m.get("trigger", "").strip():
            errors.append(f"{tag}.trigger 必须是非空字符串")
        if not isinstance(m.get("narration_brief"), str) or not m.get("narration_brief", "").strip():
            errors.append(f"{tag}.narration_brief 必须是非空字符串")

    # ── 终局（8.11：endings[] 多终局，旧版单终局兼容）──
    endings = d.get("endings")
    ending_ids = set()
    has_valid_endings = False
    if isinstance(endings, list) and endings:
        if not (1 <= len(endings) <= 4):
            errors.append("endings 必须是 2~3 个对象的数组（最多容忍 1~4）")
        for i, e in enumerate(endings if isinstance(endings, list) else []):
            tag = f"endings[{i}]"
            if not isinstance(e, dict):
                errors.append(f"{tag} 必须是对象")
                continue
            eid = e.get("id")
            if not isinstance(eid, str) or not eid.strip():
                errors.append(f"{tag}.id 必须是非空字符串")
            elif eid in ending_ids:
                errors.append(f"终局 id 重复: {eid}")
            else:
                ending_ids.add(eid)
            for key in ("title", "condition", "ending_text"):
                if not isinstance(e.get(key), str) or not e.get(key, "").strip():
                    errors.append(f"{tag}.{key} 必须是非空字符串")
        if ending_ids:
            has_valid_endings = True
    if not has_valid_endings:
        # 旧版兼容：无 endings[] 时退回单终局字段
        if not isinstance(d.get("ending_condition"), str) or not d.get("ending_condition", "").strip():
            errors.append("endings 缺失时，ending_condition 必须是非空字符串（旧版单终局）")
    if not isinstance(d.get("ending_text"), str):
        errors.append("ending_text 必须是字符串")

    return errors


def new_state(story: dict) -> dict:
    """按副本内容生成初始运行时 state。

    flags 约定：
      knows_secret_{sid} = False   玩家尚不知晓该秘密
      met_npc_{nid}      = False   玩家尚未见过该 NPC
      quest_{qid}_state  = "undiscovered"（accept_flags 为空的任务直接 "active"）
      milestone_{mid}    = False   里程碑触发标记（仅导演程序写入，AI 提议标签不认）

    8.11/8.12 新增：
      player         {hp, max_hp, hunger, location}
      clock          {day, time}
      npc_locations  {npc_id: location}（导演可程序改写；AI 不可提议）
      private_with   None（当前私聊 NPC id，私聊模式开关）
      ending_reached None（达成的终局 id）
    """
    flags = {}
    for npc in story.get("npcs", []):
        flags[f"met_npc_{npc.get('id')}"] = False
    for sec in story.get("secrets", []):
        flags[f"knows_secret_{sec.get('id')}"] = False
    for q in story.get("quests", []):
        # accept_flags 为空 = 开局即激活（无需触发条件）
        start_active = not (q.get("accept_flags") or "").strip()
        flags[f"quest_{q.get('id')}_state"] = "active" if start_active else "undiscovered"
        # 动作型任务（8.20）：预置时间投入/汇报标记 flag
        # time_ready 仅程序写（时间投入达标）；reported AI 可提议（玩家口头汇报、NPC 认可）
        if q.get("judge_mode", "flags") == "time":
            flags[f"milestone_{q.get('id')}_time_ready"] = False
            flags[f"milestone_{q.get('id')}_reported"] = False
    for m in story.get("milestones", []):
        flags[f"milestone_{m.get('id')}"] = False

    npcs = story.get("npcs", [])
    scene = story.get("opening_scene") or (npcs[0].get("location", "") if npcs else "")

    # 玩家初始状态：缺省时给安全默认值（兼容旧副本）
    player = story.get("player") if isinstance(story.get("player"), dict) else {}
    default_loc = npcs[0].get("location", "") if npcs else ""
    state_player = {
        "hp": player.get("hp", 100),
        "max_hp": player.get("max_hp", 100),
        "hunger": player.get("hunger", 100),
        "location": player.get("location", "") or default_loc,
    }
    # 时钟初始值
    clock_in = story.get("clock") if isinstance(story.get("clock"), dict) else {}
    t = clock_in.get("time", "08:00")
    state_clock = {
        "day": clock_in.get("day", 1) if isinstance(clock_in.get("day"), int) and clock_in.get("day", 0) >= 1 else 1,
        "time": t if _CLOCK_RE.match(t or "") else "08:00",
    }
    # NPC 初始地点表（从剧本拷贝，供导演程序改写）
    npc_locations = {npc.get("id"): npc.get("location", "") for npc in npcs}

    return {
        "flags": flags,
        "scene": scene,
        "ended": False,
        "player": state_player,
        "clock": state_clock,
        "npc_locations": npc_locations,
        "private_with": None,
        "ending_reached": None,
        "manual_notes": [],   # 冒险手册：NPC 交代的重要信息（含时态由原文自带）
        "quest_invested": {},  # 动作型任务时间投入记账（8.20）：{quest_id: 已投入分钟数}
        "npc_knowledge": {},   # 8.21a NPC 认知账本：{npc_id: [事实, ...]}（去重、上限 8 条 FIFO）
        "npc_attitude": {},    # 8.21b NPC 关系/心情慢变量：{npc_id: {affinity, trust, mood}}
    }


# ==================== 外源检索资料桥（8.16） ====================

def validate_research(data) -> list:
    """校验外部 AI 提交的检索资料 JSON（8.16 契约），返回错误清单（空列表 = 通过）。

    契约：{source_model, topics: [{topic, facts[], uncertain[], relations[]}], notes}
    校验不通过时由调用方生成「补查提示词」给用户第二趟。
    """
    if not isinstance(data, dict):
        return ["提交内容必须是 JSON 对象（顶层 {}）"]
    topics = data.get("topics")
    if not isinstance(topics, list) or not topics:
        return ["缺少非空的 topics 主题数组（每个主题一个对象）"]
    errors = []
    for i, t in enumerate(topics, 1):
        tag = f"topics[{i}]"
        if not isinstance(t, dict):
            errors.append(f"{tag} 必须是对象")
            continue
        topic = str(t.get("topic") or "").strip()
        if not topic:
            errors.append(f"{tag}.topic 缺少主题名")
        facts = t.get("facts") if isinstance(t.get("facts"), list) else []
        uncertain = t.get("uncertain") if isinstance(t.get("uncertain"), list) else []
        if not facts and not uncertain:
            errors.append(f"主题「{topic or tag}」facts 与 uncertain 都为空，等于没查到内容，请补查")
    return errors


def format_research(data, max_chars: int = 6000, focus=None):
    """把检索资料 JSON 格式化成可注入文本（8.16 防灌爆）。

    Args:
        data: {source_model, topics: [{topic, facts, uncertain, relations}], notes}
        max_chars: 总字数预算（约 6000）
        focus: 可选关键词列表（如 [NPC名, NPC地点, 玩家地点]）。总量超预算时先只保留
               主题名或条目中含关键词的主题；仍超预算再按顺序整主题截断。
               focus 为空时直接按顺序截断。
    Returns:
        (text, truncation_notes)：text 为注入文本（可为空串）；
        truncation_notes 为截断说明清单（造城时记入 review_notes）。
    """
    topics = data.get("topics") if isinstance(data, dict) else None
    if not isinstance(topics, list):
        return "", []
    blocks = []
    for t in topics:
        if not isinstance(t, dict):
            continue
        topic = str(t.get("topic") or "").strip() or "（未命名主题）"
        blines = []
        for f in (t.get("facts") or [])[:10]:  # 每主题事实硬上限 10 条（控制注入体积）
            f = str(f).strip()
            if f:
                blines.append(f"- {f}")
        for r in (t.get("relations") or []):
            if isinstance(r, dict):
                a = str(r.get("a") or r.get("from") or "").strip()
                b = str(r.get("b") or r.get("to") or "").strip()
                rel = str(r.get("relation") or r.get("rel") or "").strip()
                if a and b and rel:
                    blines.append(f"- {a} —{rel}— {b}")
            else:
                r = str(r or "").strip()
                if r:
                    blines.append(f"- {r}")
        for u in (t.get("uncertain") or []):
            u = str(u).strip()
            if u:
                blines.append(f"- （未查实）{u}")
        if blines:
            blocks.append((topic, blines))
    if not blocks:
        return "", []
    notes_text = str(data.get("notes") or "").strip()

    def _render(pairs, with_notes):
        parts = [f"【{topic}】\n" + "\n".join(blines) for topic, blines in pairs]
        if with_notes and notes_text:
            parts.append(f"【补充说明】{notes_text}")
        return "\n".join(parts)

    text = _render(blocks, True)
    trunc = []
    if len(text) <= max_chars:
        return text, trunc
    # 超预算：有 focus 时先做相关性过滤（只留与当前 NPC/地点相关的主题）
    kept = blocks
    kws = [str(k).strip() for k in (focus or []) if str(k or "").strip()]
    if kws:
        relevant = [
            (topic, blines) for topic, blines in blocks
            if any(k in topic or any(k in ln for ln in blines) for k in kws)
        ]
        if relevant and len(relevant) < len(blocks):
            trunc.append(f"资料超出 {max_chars} 字预算，仅注入与当前 NPC/地点相关的主题")
            kept = relevant
            text = _render(kept, True)
            if len(text) <= max_chars:
                return text, trunc
    # 仍超预算：按顺序整主题截断
    kept2 = []
    acc = len(notes_text) + 10
    for topic, blines in kept:
        cost = len(topic) + sum(len(ln) + 1 for ln in blines) + 2
        if acc + cost > max_chars:
            trunc.append(f"资料超出 {max_chars} 字上限，主题「{topic}」及之后已截断")
            break
        kept2.append((topic, blines))
        acc += cost
    if not kept2:
        trunc.append(f"资料超出 {max_chars} 字上限，可注入内容为空")
        return "", trunc
    return _render(kept2, True), trunc
