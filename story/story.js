/* 剧情副本前端逻辑（story.js）— 单页三视图：设置 → 生成 → 游玩
 *
 * 重要说明（加载方式）：
 * 本文件是前端脚本的"规范源码"，与 story.html 内嵌的脚本块保持同一份实现。
 * 原因：后端 app.py 仅把 static/ 目录映射到根 URL（static_url_path=""），
 * 并未提供 /story.js 路由（app.py 属于本次任务不可改动的文件），
 * 因此页面暂时以内嵌方式运行本脚本。
 * 若后端后续注册 /story.js 路由，可把 story.html 的内嵌脚本块替换为
 * 引用本文件的外链标签（script src="story.js"），即可回归单文件加载。
 */
(function () {
    'use strict';

    // ==================== 模块级状态 ====================
    var story = null;          // 当前副本公开视图（GET /api/story/current 的结果）
    var currentNpcId = '';     // 点名对话 NPC（群场中该 NPC 的回答排最前）
    var greeted = {};          // 本会话已渲染过开场白的 NPC（id -> true），保证只渲染一次
    var sending = false;       // 是否有消息正在发送
    var pollTimer = null;      // 生成进度轮询定时器
    var researchJobId = '';    // 8.16 检索任务 id（资料提交/跳过/看门狗轮询用）
    var researchTimer = null;  // 8.16 检索视图看门狗定时器（感知外部 AI 直提状态变化）
    var debugOn = false;       // 是否开启开发者面板（URL ?debug=1）
    var coverIdentity = '';    // 正在更换图标的副本 id（存档卡片图标位触发时暂存）
    var privReqQueue = [];     // 待处理的 NPC 私聊请求队列 [{npc_id, name}]
    var privReqShowing = false;// 私聊请求弹窗是否正在展示

    var JSONH = { 'Content-Type': 'application/json' };

    // ==================== 工具函数 ====================

    // HTML 转义（所有用户/AI 文本入库前先转义，防注入）
    function escapeHtml(text) {
        return String(text == null ? '' : text)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    // 极简 Markdown 渲染（static/ 无本地 marked.js，原系统用 CDN，本页禁用外部 CDN）
    // 支持行内格式与常见块级元素，全部先转义再替换，安全可靠
    function mdInline(s) {
        // 行内代码 → 加粗 → 斜体
        s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
        s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
        s = s.replace(/\*([^*]+)\*/g, '<em>$1</em>');
        return s;
    }

    function miniMd(src) {
        var text = escapeHtml(src || '');
        if (!text.trim()) return '';
        var codeBlocks = [];
        // 先摘出代码块，用占位符保护，避免内部被行级规则改写
        text = text.replace(/```[ \t]*([\s\S]*?)```/g, function (m, code) {
            code = code.replace(/^\n/, '').replace(/\n$/, '');
            codeBlocks.push('<pre class="md-code"><code>' + code + '</code></pre>');
            return '\x00CB' + (codeBlocks.length - 1) + '\x00';
        });
        var lines = text.split('\n');
        var out = [];
        var para = [];       // 普通段落缓冲
        var listTag = null;  // 'ul' | 'ol'
        var quoteBuf = null; // 引用块缓冲

        function flushPara() {
            if (para.length) {
                out.push('<p>' + para.map(mdInline).join('<br>') + '</p>');
                para = [];
            }
        }
        function flushList() {
            if (listTag) { out.push('</' + listTag + '>'); listTag = null; }
        }
        function flushQuote() {
            if (quoteBuf) {
                out.push('<blockquote>' + quoteBuf.map(mdInline).join('<br>') + '</blockquote>');
                quoteBuf = null;
            }
        }
        function flushAll() { flushPara(); flushList(); flushQuote(); }

        for (var i = 0; i < lines.length; i++) {
            var t = lines[i].trim();
            var m;
            // 代码块占位符行
            var cb = t.match(/^\x00CB(\d+)\x00$/);
            if (cb) { flushAll(); out.push(codeBlocks[+cb[1]]); continue; }
            if (!t) { flushAll(); continue; }
            // 标题（#/## → h2，###/#### → h3，与原系统消息内样式对应）
            if ((m = t.match(/^(#{1,4})\s+(.+)$/))) {
                flushAll();
                var tag = m[1].length <= 2 ? 'h2' : 'h3';
                out.push('<' + tag + '>' + mdInline(m[2]) + '</' + tag + '>');
                continue;
            }
            // 分隔线
            if (/^(-{3,}|\*{3,}|_{3,})$/.test(t)) { flushAll(); out.push('<hr>'); continue; }
            // 引用
            if ((m = t.match(/^&gt;\s?(.*)$/))) {
                flushPara(); flushList();
                if (!quoteBuf) quoteBuf = [];
                quoteBuf.push(m[1]);
                continue;
            }
            // 无序列表
            if ((m = t.match(/^[-*+]\s+(.+)$/))) {
                flushPara(); flushQuote();
                if (listTag !== 'ul') { flushList(); out.push('<ul>'); listTag = 'ul'; }
                out.push('<li>' + mdInline(m[1]) + '</li>');
                continue;
            }
            // 有序列表（兼容中文顿号与无空格写法）
            if ((m = t.match(/^(?:\d+[.)]\s+|\d+、\s*)(.+)$/))) {
                flushPara(); flushQuote();
                if (listTag !== 'ol') { flushList(); out.push('<ol>'); listTag = 'ol'; }
                out.push('<li>' + mdInline(m[1]) + '</li>');
                continue;
            }
            flushList(); flushQuote();
            para.push(t);
        }
        flushAll();
        return out.join('');
    }

    // 统一请求封装：非 2xx 时抛出后端 error 字段或 HTTP 状态码
    async function fetchJson(url, options) {
        var resp = await fetch(url, options);
        var data = {};
        try { data = await resp.json(); } catch (e) { /* 空/非 JSON 响应体 */ }
        if (!resp.ok) {
            var msg = (data && data.error) ? data.error : ('HTTP ' + resp.status);
            throw new Error(msg);
        }
        return data;
    }

    function $(id) { return document.getElementById(id); }

    // 本地图片压缩：canvas 缩放到 maxW 宽、JPEG 0.78（cover/background 上传前处理）
    function compressImage(file, maxW) {
        return new Promise(function (resolve, reject) {
            var reader = new FileReader();
            reader.onload = function () {
                var img = new Image();
                img.onload = function () {
                    var scale = Math.min(1, maxW / img.width);
                    var canvas = document.createElement('canvas');
                    canvas.width = Math.round(img.width * scale);
                    canvas.height = Math.round(img.height * scale);
                    canvas.getContext('2d').drawImage(img, 0, 0, canvas.width, canvas.height);
                    resolve(canvas.toDataURL('image/jpeg', 0.78));
                };
                img.onerror = function () { reject(new Error('图片读取失败')); };
                img.src = reader.result;
            };
            reader.onerror = function () { reject(new Error('文件读取失败')); };
            reader.readAsDataURL(file);
        });
    }

    // 应用/清除游玩界面自定义背景（多背景叠加浅色遮罩，保证消息可读）
    function applyBackground(dataUrl) {
        var play = $('view-play');
        if (!play) return;
        if (dataUrl) {
            play.classList.add('bgimg');
            play.style.backgroundImage =
                'linear-gradient(rgba(245,246,248,.86), rgba(245,246,248,.86)), url("' + dataUrl + '")';
        } else {
            play.classList.remove('bgimg');
            play.style.backgroundImage = '';
        }
    }

    function getNpc(id) {
        var npcs = (story && story.npcs) || [];
        for (var i = 0; i < npcs.length; i++) {
            if (npcs[i].id === id) return npcs[i];
        }
        return null;
    }

    // ==================== 视图切换 ====================

    function showView(name) {
        $('view-select').classList.toggle('hidden', name !== 'select');
        $('view-setup').classList.toggle('hidden', name !== 'setup');
        $('view-loading').classList.toggle('hidden', name !== 'loading');
        $('view-research').classList.toggle('hidden', name !== 'research');
        $('view-play').classList.toggle('hidden', name !== 'play');
    }

    // ==================== 视图零：存档选择 ====================

    // 拉取全部副本存档摘要（并行模式，各副本独立保存）
    async function loadSaveList() {
        var box = $('saveList');
        box.innerHTML = '<div class="save-empty">正在读取存档…</div>';
        try {
            var data = await fetchJson('/api/story/list');
            box.innerHTML = '';
            var saves = data.saves || [];
            if (!saves.length) {
                box.innerHTML = '<div class="save-empty">还没有存档，先生成一个新副本吧。</div>';
                return;
            }
            saves.forEach(function (s) {
                var btn = document.createElement('button');
                btn.type = 'button';
                btn.className = 'save-item';
                var meta = [];
                if (s.world_name) meta.push(s.world_name);
                if (s.genre) meta.push(s.genre);
                meta.push((s.npc_count || 0) + ' 名 NPC');
                if (!s.ended && s.clock_text) meta.push(s.clock_text);
                if (s.quest_total) meta.push('任务 ' + (s.quest_done || 0) + '/' + s.quest_total);
                meta.push('上次游玩 ' + (s.last_played || '？'));
                var coverHtml = s.cover
                    ? '<span class="save-cover" data-identity="' + escapeHtml(s.identity) + '" style="background-image:url(\'' + s.cover + '\')" title="点击更换图标"></span>'
                    : '<span class="save-cover" data-identity="' + escapeHtml(s.identity) + '" title="点击设置图标">留白</span>';
                btn.innerHTML =
                    coverHtml +
                    '<span class="save-info">' +
                        '<span class="save-title">' + escapeHtml(s.title || s.identity) + '</span>' +
                        '<span class="save-meta">' + escapeHtml(meta.join(' · ')) + '</span>' +
                    '</span>' +
                    '<span class="save-tag ' + (s.ended ? 'finished' : 'ongoing') + '">' +
                        (s.ended ? '已终局' : '进行中') + '</span>';
                btn.addEventListener('click', function () { enterSave(s.identity); });
                var coverEl = btn.querySelector('.save-cover');
                coverEl.addEventListener('click', function (ev) {
                    ev.stopPropagation();
                    coverIdentity = s.identity;
                    $('coverFile').click();
                });
                box.appendChild(btn);
            });
        } catch (e) {
            box.innerHTML = '<div class="save-empty">读取存档失败：' + escapeHtml(e.message) + '</div>';
        }
    }

    // 进入指定副本存档（后端设当前指针并返回公开视图）
    async function enterSave(identity) {
        try {
            var data = await fetchJson('/api/story/enter', {
                method: 'POST',
                headers: JSONH,
                body: JSON.stringify({ identity: identity })
            });
            enterPlay(data);
        } catch (e) {
            alert('进入副本失败：' + e.message);
        }
    }

    // 回到存档选择首屏（刷新列表）
    function showSelect() {
        loadSaveList();
        showView('select');
    }

    // ==================== 视图一：设置 ====================

    function showSetupError(msg) {
        var el = $('setupError');
        el.textContent = msg;
        el.classList.remove('hidden');
    }
    function hideSetupError() { $('setupError').classList.add('hidden'); }

    function resetStartBtn() {
        $('startBtn').disabled = false;
        $('startBtn').textContent = '开始生成';
    }

    // 创建副本生成任务（必填校验 + 生成中禁用按钮；高级项全部选填）
    async function startCreate() {
        var settings = {
            world_desc: $('worldDesc').value.trim(),
            genre: $('genre').value,
            npc_count: parseInt($('npcCount').value, 10),
            duration_hint: $('durationHint').value,
            tone: $('tone').value,
            title_hint: $('titleHint').value.trim(),
            player_name: $('playerName').value.trim(),
            player_desc: $('playerDesc').value.trim(),
            lore: $('lore').value.trim(),
            npc_hints: $('npcHints').value.trim(),
            ending_hint: $('endingHint').value.trim(),
            extra_req: $('extraReq').value.trim(),
            survival: $('modeSurvival').checked,
            research_enabled: $('researchSwitch').value === 'on'
        };
        if (!settings.world_desc) {
            showSetupError('世界观一句话为必填项，请先填写');
            return;
        }
        hideSetupError();
        // 8.25 检索建议：未开检索开关时先问 AI——陌生世界观（现实 IP/真实历史/专业领域）
        // 强烈建议先检索资料再生成；判定失败或建议 optional 时静默直接提交
        if (!settings.research_enabled) {
            try {
                var adv = await fetchJson('/api/story/research/advice', {
                    method: 'POST',
                    headers: JSONH,
                    body: JSON.stringify({ world_desc: settings.world_desc, genre: settings.genre })
                });
                if (adv && adv.advice === 'strong') {
                    var go = window.confirm(
                        'AI 检测到陌生/专有世界观，强烈建议先检索资料再生成：\n' +
                        (adv.reason || '仅凭模型知识可能写错设定') + '\n\n' +
                        '「确定」= 不管了，直接生成；「取消」= 返回去开启资料检索'
                    );
                    if (!go) { resetStartBtn(); return; }
                }
            } catch (e) { /* 建议判定失败不打断 */ }
        }
        $('startBtn').disabled = true;
        $('startBtn').textContent = '提交中…';
        try {
            var data = await fetchJson('/api/story/create', {
                method: 'POST',
                headers: JSONH,
                body: JSON.stringify(settings)
            });
            resetLoadingUI();
            showView('loading');
            pollCreate(data.job_id);
        } catch (e) {
            showSetupError('创建生成任务失败：' + e.message);
            resetStartBtn();
        }
    }

    // ==================== 视图二：生成进度 ====================

    function setStage(text) { $('stageText').textContent = text; }

    function setProgress(pct) {
        // 进度条：只前进不后退（阶段百分比可能不变，平滑过渡由 CSS transition 处理）
        var bar = $('progressBar'), label = $('progressPct');
        var cur = parseInt(bar.dataset.pct || '5', 10);
        var next = Math.max(cur, pct || 0);
        bar.dataset.pct = String(next);
        bar.style.width = next + '%';
        label.textContent = next + '%';
    }

    function showLoadError(msg) {
        $('loadError').textContent = msg;
        $('loadError').classList.remove('hidden');
        $('backBtn').classList.remove('hidden');
    }

    function resetLoadingUI() {
        $('loadError').classList.add('hidden');
        $('backBtn').classList.add('hidden');
        setStage('正在排队…');
        var bar = $('progressBar');
        bar.dataset.pct = '5';
        bar.style.width = '5%';
        $('progressPct').textContent = '5%';
    }

    function stopPoll() {
        if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    }

    // 轮询生成进度（每 2 秒一次，展示 stage 文案与百分比；done/error 时收尾）
    function pollCreate(jobId) {
        stopPoll();
        researchJobId = jobId;
        var tick = async function () {
            try {
                var data = await fetchJson('/api/story/create/' + jobId);
                if (data.stage) setStage(data.stage);
                if (typeof data.percent === 'number') setProgress(data.percent);
                if (data.kind === 'research' && data.status === 'waiting') {
                    // 8.16：检索清单就绪 → 切资料检索视图（停轮询，等用户提交/跳过）
                    stopPoll();
                    enterResearchView(data.prompt || '');
                    return;
                }
                if (data.status === 'done') {
                    stopPoll();
                    try {
                        var cur = await fetchJson('/api/story/current');
                        if (cur && cur.active) {
                            enterPlay(cur);
                        } else {
                            showLoadError('副本已生成但加载失败，请返回重试');
                        }
                    } catch (e2) {
                        showLoadError('加载副本失败：' + e2.message);
                    }
                } else if (data.status === 'error') {
                    stopPoll();
                    showLoadError(data.error || '副本生成失败，请返回重试');
                }
                // generating / critic_round2：继续轮询
            } catch (e) {
                stopPoll();
                showLoadError('查询生成进度失败：' + e.message);
            }
        };
        tick(); // 先立即查一次，避免白等 2 秒
        pollTimer = setInterval(tick, 2000);
    }

    // ==================== 视图二点五：外源资料检索（8.16） ====================

    // 通用剪贴板复制（clipboard API 优先，受限环境回退 execCommand）
    async function copyText(text) {
        try {
            await navigator.clipboard.writeText(text);
            return true;
        } catch (e) {
            var ta = document.createElement('textarea');
            ta.value = text;
            ta.style.position = 'fixed';
            ta.style.opacity = '0';
            document.body.appendChild(ta);
            ta.select();
            try {
                document.execCommand('copy');
                return true;
            } finally {
                document.body.removeChild(ta);
            }
        }
    }

    function showResearchError(msg) {
        $('researchError').textContent = msg;
        $('researchError').classList.remove('hidden');
    }

    function hideResearchError() {
        $('researchError').classList.add('hidden');
    }

    // 进入检索视图：填提示词、清空上一轮痕迹、启动看门狗
    function enterResearchView(promptText) {
        $('researchPrompt').value = promptText;
        $('researchPaste').value = '';
        $('researchRepair').value = '';
        $('researchRepairBox').classList.add('hidden');
        hideResearchError();
        $('submitResearchBtn').disabled = false;
        $('submitResearchBtn').textContent = '提交资料，开始生成';
        showView('research');
        startResearchWatch();
    }

    // 检索视图看门狗：用户走外部 AI 直提（HTTP/MCP）时，前端轮询感知状态跃迁
    function startResearchWatch() {
        stopResearchWatch();
        researchTimer = setInterval(async function () {
            try {
                var data = await fetchJson('/api/story/create/' + researchJobId);
                if (data.status && data.status !== 'waiting' && data.status !== 'brief_generating') {
                    stopResearchWatch();
                    hideResearchError();
                    resetLoadingUI();
                    showView('loading');
                    pollCreate(researchJobId);
                }
            } catch (e) { /* 网络抖动忽略，下一轮再查 */ }
        }, 3000);
    }

    function stopResearchWatch() {
        if (researchTimer) { clearInterval(researchTimer); researchTimer = null; }
    }

    // 提交检索资料：通过 → 回生成进度视图；不通过 → 展示问题与补查提示词（第二趟）
    async function submitResearch() {
        var raw = $('researchPaste').value.trim();
        if (!raw) {
            showResearchError('请先把外部 AI 输出的 JSON 粘贴到下面的框里');
            return;
        }
        var parsed;
        try {
            parsed = JSON.parse(raw);
        } catch (e) {
            showResearchError('粘贴的内容不是合法 JSON：' + e.message);
            return;
        }
        $('submitResearchBtn').disabled = true;
        $('submitResearchBtn').textContent = '提交中…';
        try {
            var resp = await fetchJson('/api/story/research/' + researchJobId, {
                method: 'POST',
                headers: JSONH,
                body: JSON.stringify(parsed)
            });
            if (resp && resp.ok) {
                stopResearchWatch();
                $('researchRepairBox').classList.add('hidden');
                resetLoadingUI();
                showView('loading');
                pollCreate(researchJobId);
            } else {
                showResearchError('资料未通过校验：' + ((resp && resp.errors) || []).join('；'));
                if (resp && resp.repair_prompt) {
                    $('researchRepair').value = resp.repair_prompt;
                    $('researchRepairBox').classList.remove('hidden');
                }
            }
        } catch (e) {
            showResearchError('提交失败：' + e.message);
        } finally {
            $('submitResearchBtn').disabled = false;
            $('submitResearchBtn').textContent = '提交资料，开始生成';
        }
    }

    // 跳过检索：不带资料直接造城（AI 纯自创）
    async function skipResearch() {
        $('skipResearchBtn').disabled = true;
        try {
            await fetchJson('/api/story/research/' + researchJobId + '/skip', {
                method: 'POST',
                headers: JSONH,
                body: '{}'
            });
            stopResearchWatch();
            hideResearchError();
            resetLoadingUI();
            showView('loading');
            pollCreate(researchJobId);
        } catch (e) {
            showResearchError('跳过失败：' + e.message);
        } finally {
            $('skipResearchBtn').disabled = false;
        }
    }

    // ==================== 视图三：游玩 ====================

    function updateScene(scene) {
        $('playScene').textContent = scene ? ('场景：' + scene) : '';
    }

    // 任务状态标签样式类
    function questStateClass(state) {
        var map = {
            undiscovered: 'q-undiscovered',
            active: 'q-active',
            completable: 'q-completable',
            done: 'q-done',
            failed: 'q-failed'
        };
        return map[state] || 'q-undiscovered';
    }

    // 状态栏：体力/饱食/时钟/地点 + 地点下拉 + 私聊横幅
    function renderStatus() {
        var p = (story && story.player) || {};
        var parts = [];
        parts.push('体力 ' + (p.hp != null ? p.hp : 100) + '/' + (p.max_hp != null ? p.max_hp : 100));
        parts.push('饱食 ' + (p.hunger != null ? p.hunger : 100) + '/100');
        parts.push(story.clock_text || '第1天 08:00');
        parts.push('地点：' + (p.location || '（未知）'));
        $('statusText').textContent = parts.join(' · ');

        // 地点下拉（白名单；已终局隐藏）
        var wrap = $('locWrap');
        var locs = (story && story.locations) || [];
        var cur = p.location || '';
        if (!locs.length || (story && story.ended)) {
            wrap.classList.add('hidden');
        } else {
            wrap.classList.remove('hidden');
            var sel = $('locSelect');
            sel.innerHTML = '';
            var optCur = document.createElement('option');
            optCur.value = cur;
            optCur.textContent = '当前：' + cur;
            sel.appendChild(optCur);
            locs.forEach(function (loc) {
                if (loc === cur) return;
                var o = document.createElement('option');
                o.value = loc;
                o.textContent = loc;
                sel.appendChild(o);
            });
            sel.value = cur;
        }

        // 私聊横幅
        var banner = $('privateBanner');
        if (story && story.private_with) {
            $('privateBannerText').textContent = '私聊中：仅你与 ' + (story.private_name || story.private_with) + ' 可见';
            banner.classList.remove('hidden');
        } else {
            banner.classList.add('hidden');
        }
    }

    // NPC chip：灰=不同地点不可对话（8.12）；私=私聊开关（8.13）
    function renderChips() {
        var wrap = $('npcChips');
        wrap.innerHTML = '';
        var npcs = (story && story.npcs) || [];
        var privId = story && story.private_with;
        npcs.forEach(function (npc) {
            var item = document.createElement('span');
            item.className = 'chip-wrap';
            var dead = !!npc.dead;
            var b = document.createElement('button');
            b.type = 'button';
            b.className = 'chip' + (npc.id === currentNpcId ? ' active' : '') +
                (dead ? ' dead' : (npc.available ? '' : ' disabled'));
            b.textContent = (dead ? '☠ ' : '') + (npc.name || npc.id);
            b.title = dead
                ? '已死亡，退出剧情'
                : (npc.available
                    ? ('在' + (npc.location || '此地') + '，点击设为点名对象')
                    : ('在' + (npc.location || '别处') + '，不同地点无法对话'));
            b.addEventListener('click', function () { switchNpc(npc.id); });
            item.appendChild(b);
            var pv = document.createElement('button');
            pv.type = 'button';
            pv.className = 'priv-btn' + (privId === npc.id ? ' on' : '') +
                (dead || !npc.available ? ' disabled' : '');
            pv.textContent = '私';
            pv.title = dead ? '该角色已死亡'
                : (privId === npc.id ? '退出私聊' : ('与 ' + (npc.name || npc.id) + ' 私聊（需同地）'));
            pv.addEventListener('click', function () {
                if (dead) return;
                if (privId === npc.id) {
                    exitPrivate();
                } else {
                    togglePrivate(npc.id);
                }
            });
            item.appendChild(pv);
            wrap.appendChild(item);
        });
    }

    // 任务面板（8.25：游览路线不再展示——玩家靠任务自由探索，路线只作为 AI 软引导素材）
    function renderQuests() {
        var body = $('questBody');
        body.innerHTML = '';
        var quests = (story && story.quests) || [];
        var completable = 0;
        quests.forEach(function (q) {
            if (q.state === 'completable') completable++;
            var isTime = q.judge_mode === 'time';
            var item = document.createElement('div');
            item.className = 'quest-item';
            var html =
                '<div class="q-top">' +
                    '<span class="q-name">' + escapeHtml(q.name || q.id) + '</span>' +
                    '<span class="q-tag ' + questStateClass(q.state) + '">' + escapeHtml(q.state_cn || q.state || '') + '</span>' +
                '</div>' +
                '<div class="q-meta">发布：' + escapeHtml(q.giver || '？') +
                ' · 预计耗时 ' + (q.time_cost_minutes || 0) + ' 分钟</div>';
            if (q.complete_condition) {
                html += '<div class="q-desc">' + escapeHtml(q.complete_condition) + '</div>';
            }
            // 调时投入（8.20，兼容旧副本）：所有进行中任务都可分批投入时间
            if (q.state === 'active') {
                var invested = q.invested_minutes || 0;
                var need = q.time_cost_minutes || 0;
                var pct = need ? Math.min(100, Math.round(invested / need * 100)) : 0;
                var tip = q.has_time_events
                    ? '每次投入 15 分钟，途中可能遇到事件；时间随投入流逝，完成时不再扣时'
                    : (isTime
                        ? '投入足够时间后，点「汇报完成」向发布任务的 NPC 当面汇报'
                        : '投入时间推进这件事；任务完成仍以对话中达成为准');
                html += '<div class="q-invest">' +
                    '<div class="q-progress"><div class="q-progress-fill" style="width:' + pct + '%"></div></div>' +
                    '<div class="q-invest-num">已投入 ' + invested + ' / ' + need + ' 分钟</div>' +
                    '<div class="q-invest-btns">' +
                        '<button type="button" class="btn-invest" data-min="15">+15 分钟</button>' +
                    '</div>' +
                    '<div class="q-invest-tip">' + tip + '</div>' +
                '</div>';
            }
            item.innerHTML = html;
            // 进行中任务：绑定调时按钮（点击投入时间并同步推进时钟）
            if (q.state === 'active') {
                item.querySelectorAll('.btn-invest').forEach(function (b) {
                    b.addEventListener('click', function () {
                        investQuest(q.id, parseInt(b.getAttribute('data-min'), 10));
                    });
                });
            }
            // 可完成任务：显示「完成」按钮（旧副本扣时 = 预留 - 已投入；9.1 带 time_events 的副本
            // 时间已随每段 15 分钟流逝，完成不再扣时，只显示「完成」）
            if (q.state === 'completable') {
                var btn = document.createElement('button');
                btn.type = 'button';
                btn.className = 'btn-complete';
                var remain = q.has_time_events ? 0 : Math.max(0, (q.time_cost_minutes || 0) - (q.invested_minutes || 0));
                btn.textContent = remain > 0 ? '完成（推进 ' + remain + ' 分钟）' : '完成';
                btn.addEventListener('click', function () { completeQuest(q.id); });
                item.appendChild(btn);
            }
            // 汇报完成按钮（8.20d）：投入型任务时间投满后，系统级确认 + 自动代发汇报消息
            if (q.state === 'active' && isTime && q.time_ready && !q.reported) {
                var rbtn = document.createElement('button');
                rbtn.type = 'button';
                rbtn.className = 'btn-complete';
                rbtn.textContent = '📣 汇报完成（当面告诉 ' + (q.giver || '发布人') + '）';
                rbtn.addEventListener('click', function () { reportQuest(q.id); });
                item.appendChild(rbtn);
            }
            body.appendChild(item);
        });
        // 8.25：游览路线不再在玩家面板展示（路线是 AI 侧软引导素材，
        // 玩家应通过一个个任务自由探索剧情，而不是看管报式路标）
        if (!quests.length) {
            body.innerHTML = '<div class="q-empty">还没有发现任务。与 NPC 交谈、探索地点，可能会遇到任务。</div>';
        }
        var badge = $('questBadge');
        if (completable > 0) {
            badge.textContent = completable + ' 项可完成';
            badge.classList.remove('hidden');
        } else {
            badge.classList.add('hidden');
        }
    }

    function renderAll() {
        renderStatus();
        renderChips();
        renderQuests();
        updateScene(story && story.scene);
    }

    // 用后端回传的最新公开状态刷新界面
    function applyState(st) {
        if (st && st.active !== false) {
            story = st;
            renderAll();
        }
    }

    // ── 冒险手册 + 角色卡 ──

    // 渲染身份卡 HTML（角色卡弹窗与手册共用）
    function personaHtml(p) {
        p = p || {};
        var lines = [];
        if (p.name) lines.push('<span class="pname">' + escapeHtml(p.name) + '</span>');
        if (p.identity) lines.push('<span class="pline"><b>身份：</b>' + escapeHtml(p.identity) + '</span>');
        if (p.background) lines.push('<span class="pline"><b>来此缘由：</b>' + escapeHtml(p.background) + '</span>');
        if (p.skills) lines.push('<span class="pline"><b>能力与随身物：</b>' + escapeHtml(p.skills) + '</span>');
        return lines.join('') || '<span class="hb-empty">（暂无身份信息）</span>';
    }

    // 开局角色卡（每个副本本会话只自动弹一次；手册中随时可看）
    var personaShownFor = '';
    function showPersonaCard(force) {
        var p = (story && story.player_persona) || {};
        if (!p.name && !p.identity) return;
        if (!force && personaShownFor === story.identity) return;
        personaShownFor = story.identity;
        $('personaBody').innerHTML = personaHtml(p);
        $('personaOverlay').classList.remove('hidden');
    }

    // 渲染冒险手册（身份卡 / 生存状态 / 背包 / 已完成任务 / NPC 交代的重要信息）
    function renderHandbook() {
        var html = '';
        html += '<div class="hb-section"><h3>身份卡</h3><div class="hb-persona">' +
            personaHtml(story && story.player_persona) + '</div></div>';
        // 8.26b 生存状态条（仅生存型副本显示；hp/hunger 只由程序变更）
        var rules = (story && story.rules) || {};
        if (rules.survival) {
            var pv = (story && story.player) || {};
            var hp = pv.hp == null ? 100 : pv.hp, mx = pv.max_hp || 100;
            var hg = pv.hunger == null ? 100 : pv.hunger;
            html += '<div class="hb-section"><h3>生存状态</h3>' +
                '<div class="surv-row"><span>❤️ HP</span><div class="surv-bar">' +
                '<div class="surv-fill hp" style="width:' + Math.max(0, Math.min(100, hp / mx * 100)) + '%"></div></div>' +
                '<span>' + hp + '/' + mx + '</span></div>' +
                '<div class="surv-row"><span>🍗 饱食</span><div class="surv-bar">' +
                '<div class="surv-fill hg" style="width:' + Math.max(0, Math.min(100, hg)) + '%"></div></div>' +
                '<span>' + hg + '/100</span></div></div>';
        }
        // 8.26 背包（MC 风格：暗格=已占+数量，浅格=空；玩家只读）
        // 9.2 双手槽：拿在手上的东西 NPC 当面看得见（出示）；点击背包物品拿起、点击手槽放回
        var inv = (story && story.inventory) || [];
        var slots = (story && story.inventory_slots) || 60;
        var occ = inv.length;
        var show = Math.min(slots, Math.max(24, Math.ceil(occ / 8) * 8 + 8));
        var hands = (story && story.hands) || {};
        var bagHtml = '<div class="hand-row">' +
            '<span class="hand-label">✋</span>' + handSlotHtml(hands.main, 'main') +
            '<span class="hand-label">🤲</span>' + handSlotHtml(hands.off, 'off') +
            '<span class="hand-tip">手上的东西 NPC 当面看得见（出示）；点手上的物品放回</span></div>';
        if (pendingHand) {
            bagHtml += '<div class="hand-pick">把「' + escapeHtml(pendingHand.name || pendingHand.item) + '」放到：' +
                '<button type="button" class="bag-sync" data-handact="main">✋ 主手</button>' +
                '<button type="button" class="bag-sync" data-handact="off">🤲 副手</button>' +
                '<button type="button" class="bag-sync" data-handact="cancel">取消</button></div>';
        }
        bagHtml += '<div class="bag-grid">';
        for (var i = 0; i < show; i++) {
            if (i < occ) {
                var it = inv[i];
                bagHtml += '<div class="bag-cell filled' + (it.key ? ' key' : '') + '" data-idx="' + i + '" title="' +
                    escapeHtml(it.name + (it.desc ? '：' + it.desc : '')) + '">' +
                    '<span class="bag-name">' + escapeHtml(String(it.name).slice(0, 2)) + '</span>' +
                    (it.count > 1 ? '<span class="bag-count">' + it.count + '</span>' : '') +
                    '</div>';
            } else {
                bagHtml += '<div class="bag-cell"></div>';
            }
        }
        bagHtml += '</div>';
        bagHtml += '<div class="bag-meta"><span>' + occ + '/' + slots + ' 格 · 堆叠无上限</span>' +
            '<button type="button" class="bag-sync" id="bagSyncBtn">📡 背包同步：' +
            (story.inventory_sync ? '开' : '关（NPC 只能看旧数据）') + '</button></div>';
        html += '<div class="hb-section"><h3>背包</h3>' + bagHtml + '</div>';
        var done = ((story && story.quests) || []).filter(function (q) { return q.state === 'done'; });
        html += '<div class="hb-section"><h3>已完成任务（' + done.length + '）</h3>';
        if (done.length) {
            done.forEach(function (q) {
                html += '<div class="hb-quest"><span class="q-title">' + escapeHtml(q.name || q.id) + '</span>' +
                    (q.reward ? '<span class="q-reward">' + escapeHtml(q.reward) + '</span>' : '') + '</div>';
            });
        } else {
            html += '<div class="hb-empty">还没有完成的任务。接下任务并达成条件后，这里会记录你的成绩。</div>';
        }
        html += '</div>';
        var notes = (story && story.manual_notes) || [];
        html += '<div class="hb-section"><h3>NPC 交代的重要信息（' + notes.length + '）</h3>';
        if (notes.length) {
            notes.slice().reverse().forEach(function (n) {
                var when = '第' + (n.day || 1) + '天' + (n.time ? ' ' + n.time : '');
                html += '<div class="hb-note"><div class="n-meta">' + escapeHtml(when) +
                    ' · ' + escapeHtml(n.npc || '未知') + '</div>' +
                    '<div class="n-text">' + escapeHtml(n.content) + '</div></div>';
            });
        } else {
            html += '<div class="hb-empty">还没有记录。NPC 向你透露关键线索、约定或身份信息时，会自动记到这里。</div>';
        }
        html += '</div>';
        $('handbookBody').innerHTML = html;
    }

    // 8.26c 玩家背包同步开关（信息扩散）：关=冻结快照，NPC 只能看旧数据
    async function toggleBagSync() {
        if (!story) return;
        try {
            var data = await fetchJson('/api/story/inventory/sync', {
                method: 'POST',
                headers: JSONH,
                body: JSON.stringify({ identity: story.identity, on: !story.inventory_sync })
            });
            story.inventory_sync = data.on;
            renderHandbook();
        } catch (e) { /* 切换失败静默，下次打开手册重试 */ }
    }

    // ── 9.2 双手槽 + 交流动作 ──────────────────────────

    var pendingHand = null; // 手册里点了哪个背包物品（待拿起），{item, name}

    // 双手槽单元格 HTML（filled=拿着，点击放回；empty=空手）
    function handSlotHtml(it, slot) {
        if (it) {
            return '<div class="hand-slot filled' + (it.key ? ' key' : '') +
                '" data-handact="unhold" data-slot="' + slot + '" title="' +
                escapeHtml(it.name + (it.desc ? '：' + it.desc : '') + '（点击放回背包）') + '">' +
                '<span class="bag-name">' + escapeHtml(String(it.name).slice(0, 2)) + '</span></div>';
        }
        return '<div class="hand-slot empty" title="空手（点击背包里的物品拿起）">' +
            '<span class="hand-empty">空</span></div>';
    }

    // 拿起/放回（程序裁决：同槽占用拒绝、物品必须真在包里；数量归零自动清槽由导演负责）
    async function setHand(slot, item) {
        if (!story) return;
        try {
            var data = await fetchJson('/api/story/hands', {
                method: 'POST',
                headers: JSONH,
                body: JSON.stringify({ identity: story.identity, slot: slot, item: item || null })
            });
            if (data.hands) story.hands = data.hands;
            if (!data.ok && data.msg) addMsg('narration', '（' + data.msg + '）');
        } catch (e) { /* 失败静默，下次打开手册重试 */ }
        pendingHand = null;
        renderHandbook();
    }

    // 交流动作：点击把舞台指令填进输入框（可改可发——扮演感留在玩家手里）
    function fillAction(act) {
        var input = $('chatInput');
        if (!input || !story || story.ended || sending) return;
        var npc = null;
        ((story && story.npcs) || []).forEach(function (n) { if (n.id === currentNpcId) npc = n; });
        var name = npc ? (npc.name || '') : '';
        var dir = name ? '（对' + name + '：' + act + '）' : '（' + act + '）';
        var old = (input.value || '').trim();
        input.value = dir + old.replace(/^（[^）]*）\s*/, '');
        input.focus();
    }

    // 进入游玩视图（新建开局与刷新续玩共用）
    function enterPlay(data) {
        story = data || {};
        applyBackground(story.background);
        showPersonaCard();
        greeted = {};
        privReqQueue = [];
        var npcs = story.npcs || [];
        currentNpcId = npcs.length ? npcs[0].id : '';
        $('messages').innerHTML = '';
        $('playTitle').textContent = story.title || '未知副本';
        renderAll();
        showView('play');
        // 终局恢复：直接展示遮罩，不再进入对话
        if (story.ended) {
            renderEnding(story.ending_text || '（本局副本已落幕）');
            return;
        }
        // 8.24i 历史回放：重新进入副本/F5 刷新时，把此前的对话原样铺回来
        // （历史一直都在保存，只是此前前端没有回放功能；逐条渲染不滚动，最后一次性到底）
        var hist = story.history || [];
        if (hist.length) {
            hist.forEach(function (h) {
                if (h.role === 'npc') {
                    addMsg('npc', h.text, h.name, h.private, false, true);
                } else if (h.role === 'user') {
                    addMsg('user', h.text, '你', h.private, false, true);
                }
            });
            $('messages').scrollTop = $('messages').scrollHeight;
        }
        // 渲染在场 NPC 开场白（每个 NPC 本会话只渲染一次，刷新续玩同样生效）
        npcs.forEach(function (npc) {
            if (npc.available !== false && npc.opening && !greeted[npc.id]) {
                addMsg('npc', npc.opening, npc.name);
                greeted[npc.id] = true;
            }
        });
        $('chatInput').focus();
    }

    // 点名对话 NPC（群场中所有人仍会回答，仅排序变化；灰 chip = 不同地点，拦截提示）
    function switchNpc(npcId) {
        if (npcId === currentNpcId) return;
        var npc = getNpc(npcId);
        if (npc && npc.dead) {
            addMsg('narration', (npc.name || npcId) + ' 已经死了，不会再回应。');
            return;
        }
        if (npc && npc.available === false) {
            addMsg('narration', (npc.name || npcId) + ' 在' + (npc.location || '别处') + '，不同地点听不到你说话。');
            return;
        }
        currentNpcId = npcId;
        renderChips();
        if (npc && npc.opening && !greeted[npcId]) {
            addMsg('npc', npc.opening, npc.name);
            greeted[npcId] = true;
        }
        $('chatInput').focus();
    }

    // 消息渲染：narration 居中灰斜体 / npc Markdown / user 右侧蓝块；isPrivate 加私聊标记
    // plain=true（8.24 剧场行）：不渲染 markdown，直接纯文本短句展示
    // noScroll=true（8.24i 历史回放）：不逐条滚动，回放完由调用方一次性滚到底
    function addMsg(role, text, label, isPrivate, plain, noScroll) {
        var box = $('messages');
        var div = document.createElement('div');
        div.className = 'msg ' + role + (isPrivate ? ' private' : '');
        var privTag = isPrivate ? '（私聊）' : '';
        var inner;
        if (role === 'narration') {
            inner = '<div class="content">' + escapeHtml(text).replace(/\n/g, '<br>') + '</div>';
        } else if (role === 'npc') {
            var body = plain
                ? escapeHtml(text).replace(/\n/g, '<br>')
                : miniMd(text);
            inner = '<div class="label">' + escapeHtml(label || 'NPC') + privTag + '</div>' +
                '<div class="content">' + body + '</div>';
        } else {
            inner = '<div class="label">' + escapeHtml(label || '你') + privTag + '</div>' +
                '<div class="content">' + escapeHtml(text).replace(/\n/g, '<br>') + '</div>';
        }
        div.innerHTML = inner;
        box.appendChild(div);
        if (!noScroll) div.scrollIntoView({ behavior: 'smooth' });
    }

    // 发送玩家消息：notices/narration → 多 NPC 回复（点名排前）→ 状态刷新 → 私聊请求
    async function doSend() {
        var inputEl = $('chatInput');
        var text = inputEl.value.trim();
        if (!text || sending) return;
        if (!story || story.ended) return;
        sending = true;
        $('sendBtn').disabled = true;
        var inPrivate = !!(story && story.private_with);
        addMsg('user', text, '你', inPrivate);
        inputEl.value = '';
        // 群场 = 在场 N 个 NPC 各一次 A3，耗时随人数增长，超时放宽到 5 分钟
        var controller = new AbortController();
        var timer = setTimeout(function () { controller.abort(); }, 300000);
        try {
            var data = await fetchJson('/api/chat', {
                method: 'POST',
                headers: JSONH,
                body: JSON.stringify({ message: text, npc_id: currentNpcId }),
                signal: controller.signal
            });
            (data.notices || []).forEach(function (n) { if (n) addMsg('narration', n); });
            if (data.narration) addMsg('narration', data.narration);
            var replies = (data.replies && data.replies.length)
                ? data.replies
                : (data.reply ? [{ name: null, reply: data.reply }] : []);
            // 8.24 剧场行（r.stage）按行序间隔弹出（短回合节奏感）；单聊/私聊立即渲染
            replies.forEach(function (r, idx) {
                var delay = (r.stage && idx > 0) ? idx * 500 : 0;
                setTimeout(function () {
                    addMsg('npc', r.reply || '（对方沉默了…）', r.name, inPrivate, !!r.stage);
                }, delay);
            });
            if (data.story_state) applyState(data.story_state);
            if (data.ended) await finishStory(data.story_state);
            if (data.private_requests && data.private_requests.length) {
                data.private_requests.forEach(function (pr) { privReqQueue.push(pr); });
                showNextPrivReq();
            }
        } catch (e) {
            if (e.name === 'AbortError') {
                addMsg('narration', '请求超时（超过5分钟）。后端可能仍在处理，可稍后重试或刷新页面。');
            } else {
                addMsg('narration', '出错了：' + e.message);
            }
        } finally {
            clearTimeout(timer);
            sending = false;
            $('sendBtn').disabled = false;
            inputEl.focus();
        }
    }

    // ── 私聊（8.13）：建立/解除（后端同地点硬校验）──
    async function togglePrivate(npcId) {
        var npc = getNpc(npcId);
        if (npc && npc.dead) {
            addMsg('narration', (npc.name || npcId) + ' 已经死了，无法私聊。');
            return;
        }
        try {
            var data = await fetchJson('/api/story/private', {
                method: 'POST',
                headers: JSONH,
                body: JSON.stringify({ npc_id: npcId || '' })
            });
            if (data.notice) addMsg('narration', data.notice);
            applyState(data.story_state);
        } catch (e) {
            addMsg('narration', '私聊设置失败：' + e.message);
        }
    }

    async function exitPrivate() {
        await togglePrivate('');
    }

    // NPC 主动私聊请求弹窗（队列逐个处理；已在私聊中则丢弃剩余请求）
    function showNextPrivReq() {
        if (privReqShowing || !privReqQueue.length) return;
        if (story && story.private_with) { privReqQueue = []; return; }
        var pr = privReqQueue.shift();
        privReqShowing = true;
        $('privReqText').textContent =
            (pr.name || pr.npc_id) + ' 想和你单独谈谈。同意后将进入私聊，其他 NPC 听不到你们的对话。';
        $('privReqOverlay').classList.remove('hidden');
        $('privReqYes').onclick = async function () {
            $('privReqOverlay').classList.add('hidden');
            privReqShowing = false;
            await togglePrivate(pr.npc_id);
            showNextPrivReq();
        };
        $('privReqNo').onclick = function () {
            $('privReqOverlay').classList.add('hidden');
            privReqShowing = false;
            addMsg('narration', '你婉拒了 ' + (pr.name || pr.npc_id) + ' 的私聊请求。');
            showNextPrivReq();
        };
    }

    // ── 任务完成（8.12）：点「完成」→ done + 扣时推进时钟 ──
    async function completeQuest(questId) {
        try {
            var data = await fetchJson('/api/story/quest/complete', {
                method: 'POST',
                headers: JSONH,
                body: JSON.stringify({ quest_id: questId })
            });
            if (data.notice) addMsg('narration', data.notice);
            applyState(data.story_state);
        } catch (e) {
            addMsg('narration', '完成任务失败：' + e.message);
        }
    }

    // ── 调时投入（8.20）：动作型任务分批投入时间，投入同步推进时钟 ──
    async function investQuest(questId, minutes) {
        try {
            var data = await fetchJson('/api/story/invest', {
                method: 'POST',
                headers: JSONH,
                body: JSON.stringify({ quest_id: questId, minutes: minutes })
            });
            (data.notices || []).forEach(function (n) { if (n) addMsg('narration', n); });
            applyState(data.story_state);
        } catch (e) {
            addMsg('narration', '投入时间失败：' + e.message);
        }
    }

    // 汇报完成（8.20d）：系统级确认（程序写 reported）→ 自动代发汇报消息给发布人，
    // NPC 正常回复演验收反应（对话活动），状态不受回复影响
    async function reportQuest(questId) {
        try {
            var data = await fetchJson('/api/story/report', {
                method: 'POST',
                headers: JSONH,
                body: JSON.stringify({ quest_id: questId })
            });
            (data.notices || []).forEach(function (n) { if (n) addMsg('narration', n); });
            applyState(data.story_state);
            if (data.draft) {
                addMsg('narration', '（你向 ' + (data.npc_name || '发布人') + ' 汇报了任务的完成情况。）');
                $('chatInput').value = data.draft;
                doSend();
            }
        } catch (e) {
            addMsg('narration', '汇报失败：' + e.message);
        }
    }

    // ── 地点切换（8.12）：地点下拉，后端白名单硬校验 ──
    async function changeLocation(loc) {
        try {
            var data = await fetchJson('/api/story/location', {
                method: 'POST',
                headers: JSONH,
                body: JSON.stringify({ location: loc })
            });
            if (data.notice) addMsg('narration', data.notice);
            applyState(data.story_state);
        } catch (e) {
            addMsg('narration', '移动失败：' + e.message);
        }
    }

    // 终局归档：置 ended + 清除当前副本指针，返回 ending_text；失败不阻塞遮罩展示
    async function finishStory(stateHint) {
        var endingText = (stateHint && stateHint.ending_text) || (story && story.ending_text) || '';
        try {
            var data = await fetchJson('/api/story/finish', {
                method: 'POST',
                headers: JSONH,
                body: '{}'
            });
            if (data.ending_text) endingText = data.ending_text;
        } catch (e) { /* 归档失败仍展示已有终局文案 */ }
        renderEnding(endingText || '（本局副本到此落幕）');
    }

    // 终局全屏遮罩
    function renderEnding(text) {
        $('endingText').innerHTML = escapeHtml(text).replace(/\n/g, '<br>');
        $('endingOverlay').classList.remove('hidden');
        if (story) story.ended = true;
    }

    // 再来一局：清空对局状态，回到存档选择界面
    function restart() {
        $('endingOverlay').classList.add('hidden');
        story = null;
        currentNpcId = '';
        greeted = {};
        privReqQueue = [];
        $('messages').innerHTML = '';
        resetStartBtn();
        resetLoadingUI();
        showSelect();
        if (debugOn) loadDebug(); // 面板随状态刷新（此时会显示"无进行中的副本"）
    }

    // ==================== 开发者面板（?debug=1 时才创建 DOM） ====================

    function setupDebugPanel() {
        var panel = document.createElement('div');
        panel.id = 'debugPanel';
        panel.className = 'debug-panel open';
        panel.innerHTML =
            '<div class="debug-head" id="debugHead">' +
            '<span>开发者面板（点击折叠/展开）</span>' +
            '<button type="button" id="debugRefresh">刷新</button>' +
            '</div>' +
            '<div class="debug-body" id="debugBody">加载中…</div>';
        document.body.appendChild(panel);
        $('debugHead').addEventListener('click', function () {
            panel.classList.toggle('open');
        });
        $('debugRefresh').addEventListener('click', function (ev) {
            ev.stopPropagation();
            loadDebug();
        });
        loadDebug();
    }

    function formatDebug(d) {
        var lines = [];
        lines.push('【副本】' + (d.title || '（未命名）') + '  (' + (d.identity || '') + ')');
        lines.push('【场景】' + (d.scene || '（无）') + '   【已终局】' + (d.ended ? '是' : '否') +
            (d.ending_reached ? '（' + d.ending_reached + '）' : ''));
        var p = d.player || {};
        lines.push('【玩家】体力 ' + (p.hp != null ? p.hp : '?') + '/' + (p.max_hp != null ? p.max_hp : '?') +
            '   饱食 ' + (p.hunger != null ? p.hunger : '?') + '/100   地点 ' + (p.location || '？'));
        lines.push('【时钟】' + (d.clock ? ('第' + d.clock.day + '天 ' + d.clock.time) : '？') +
            '   【私聊中】' + (d.private_with || '无'));
        lines.push('【NPC位置】' + JSON.stringify(d.npc_locations || {}));
        lines.push('【地点白名单】' + ((d.locations || []).join('、') || '（无）'));
        lines.push('');
        lines.push('【Flags 状态】');
        var flags = d.flags || {};
        var keys = Object.keys(flags);
        if (!keys.length) lines.push('  （暂无）');
        keys.forEach(function (k) { lines.push('  ' + k + ' = ' + flags[k]); });
        lines.push('');
        lines.push('【秘密揭示状态】');
        var secrets = d.secrets || [];
        if (!secrets.length) lines.push('  （暂无）');
        secrets.forEach(function (s) {
            lines.push('  [' + (s.revealed ? '已揭示' : '未揭示') + '] ' + s.id + '：' + (s.content || ''));
            lines.push('      持有者：' + ((s.holders || []).join('、') || '（无）') +
                '   揭示提示：' + (s.reveal_hint || '（无）'));
        });
        lines.push('');
        lines.push('【终局】');
        var endings = d.endings || [];
        if (!endings.length) lines.push('  （暂无）');
        endings.forEach(function (e) {
            lines.push('  [' + e.id + '] ' + (e.title || '') + '：' + (e.condition || ''));
        });
        lines.push('');
        lines.push('【审稿记录】');
        var notes = d.review_notes || [];
        if (!notes.length) lines.push('  （暂无）');
        notes.forEach(function (n, i) {
            lines.push('  ' + (i + 1) + '. ' + (typeof n === 'string' ? n : JSON.stringify(n)));
        });
        return lines.join('\n');
    }

    async function loadDebug() {
        var body = $('debugBody');
        try {
            var d = await fetchJson('/api/story/debug');
            body.textContent = formatDebug(d);
        } catch (e) {
            body.textContent = '（当前无进行中的副本或查询失败）\n' + e.message;
        }
    }

    // ==================== 初始化与刷新恢复 ====================

    function init() {
        // 视图零：存档选择（进游戏第一眼；生成完成自动进入对应副本）
        $('newSaveBtn').addEventListener('click', function () {
            hideSetupError();
            showView('setup');
        });
        // 视图一：设置表单
        $('startBtn').addEventListener('click', startCreate);
        // 高级选项收起按钮：折叠条在内容顶端，填完长表单后往往已滚出视口，就地收起并滚回原位
        $('advCollapseBtn').addEventListener('click', function () {
            var box = document.querySelector('.adv-box');
            if (!box) return;
            box.open = false;
            box.scrollIntoView({ behavior: 'smooth', block: 'start' });
        });
        $('backToSelectBtn').addEventListener('click', showSelect);
        // 视图二点五：外源资料检索（8.16）
        $('copyResearchBtn').addEventListener('click', async function () {
            try {
                await copyText($('researchPrompt').value);
                this.textContent = '已复制 ✓';
            } catch (e) {
                this.textContent = '复制失败，请手动全选复制';
            }
            var btn = this;
            setTimeout(function () { btn.textContent = '复制提示词'; }, 1600);
        });
        $('copyRepairBtn').addEventListener('click', async function () {
            try {
                await copyText($('researchRepair').value);
                this.textContent = '已复制 ✓';
            } catch (e) {
                this.textContent = '复制失败，请手动全选复制';
            }
            var btn = this;
            setTimeout(function () { btn.textContent = '复制补查提示词'; }, 1600);
        });
        $('submitResearchBtn').addEventListener('click', submitResearch);
        $('skipResearchBtn').addEventListener('click', skipResearch);
        $('researchBackBtn').addEventListener('click', function () {
            stopPoll();
            stopResearchWatch();
            resetStartBtn();
            showView('setup');
        });
        // 视图二：错误后返回
        $('backBtn').addEventListener('click', function () {
            stopPoll();
            resetStartBtn();
            resetLoadingUI();
            showView('setup');
        });
        // 视图三：发送消息（按钮 + 回车）
        $('sendBtn').addEventListener('click', doSend);
        $('chatInput').addEventListener('keydown', function (ev) {
            if (ev.key === 'Enter' && !ev.shiftKey) {
                ev.preventDefault();
                doSend();
            }
        });
        // 暂停菜单：继续 / 回存档选择（副本已自动保存）
        $('pauseBtn').addEventListener('click', function () {
            $('pauseOverlay').classList.remove('hidden');
        });
        $('pauseResume').addEventListener('click', function () {
            $('pauseOverlay').classList.add('hidden');
        });
        $('pauseExit').addEventListener('click', function () {
            $('pauseOverlay').classList.add('hidden');
            showSelect();
        });
        // 更换背景图（暂停菜单入口 → 选图 → 压缩 → 上传 → 即时生效）
        $('pauseBg').addEventListener('click', function () { $('bgFile').click(); });
        // 8.27b 重新开始此副本（并行副本模式，类似我的世界重新生成世界）：
        // 原副本原封保留，复制剧本开一个全新并行存档并直接进入
        $('pauseRestart').addEventListener('click', async function () {
            if (!story || !story.identity) return;
            if (!confirm('要为这个副本开一个全新的并行存档吗？\n\n新副本会回到刚生成时的初始状态（NPC 不记得任何事），原副本及其全部进度原封不动保留在存档列表里。')) return;
            try {
                const resp = await fetchJson('/api/story/restart', {
                    method: 'POST', headers: JSONH, body: JSON.stringify({ identity: story.identity })
                });
                $('pauseOverlay').classList.add('hidden');
                await enterSave(resp.identity);
                alert('并行副本已创建，已进入新存档，一切从头来过。');
            } catch (e) { alert('重新开始失败：' + e.message); }
        });
        $('bgFile').addEventListener('change', async function () {
            var file = this.files && this.files[0];
            this.value = '';
            if (!file) return;
            try {
                var image = await compressImage(file, 1280);
                await fetchJson('/api/story/background', {
                    method: 'POST', headers: JSONH, body: JSON.stringify({ image: image })
                });
                applyBackground(image);
                if (story) story.background = image;
            } catch (e) { alert('背景图设置失败：' + e.message); }
        });
        // 存档图标更换（存档卡片图标位入口）
        $('coverFile').addEventListener('change', async function () {
            var file = this.files && this.files[0];
            this.value = '';
            if (!file || !coverIdentity) return;
            try {
                var image = await compressImage(file, 320);
                await fetchJson('/api/story/cover', {
                    method: 'POST', headers: JSONH, body: JSON.stringify({ identity: coverIdentity, image: image })
                });
                loadSaveList();
            } catch (e) { alert('图标设置失败：' + e.message); }
        });
        // 私聊横幅退出按钮
        $('privateExit').addEventListener('click', exitPrivate);
        // 冒险手册与角色卡（手册打开时按最新公开视图渲染）
        $('handbookBtn').addEventListener('click', function () {
            renderHandbook();
            $('handbookOverlay').classList.remove('hidden');
        });
        // 8.26c 背包同步开关按钮（手册内容每次重渲染，用事件委托绑定）
        $('handbookBody').addEventListener('click', function (ev) {
            if (ev.target && ev.target.id === 'bagSyncBtn') { toggleBagSync(); return; }
            // 9.2 双手槽：点手槽放回 / 待拿起条选主手副手 / 点背包已占格进入待拿起
            var t = ev.target;
            var act = t.closest ? t.closest('[data-handact]') : null;
            if (act) {
                var kind = act.getAttribute('data-handact');
                if (kind === 'unhold') { setHand(act.getAttribute('data-slot'), ''); return; }
                if (kind === 'main' || kind === 'off') {
                    if (pendingHand) setHand(kind, pendingHand.item);
                    return;
                }
                pendingHand = null;
                renderHandbook();
                return;
            }
            var cell = t.closest ? t.closest('.bag-cell.filled') : null;
            if (cell) {
                var it = ((story && story.inventory) || [])[parseInt(cell.getAttribute('data-idx'), 10)];
                if (it) { pendingHand = it; renderHandbook(); }
            }
        });
        // 9.2 交流动作 chips（静态元素，一次绑定）：点击把舞台指令填进输入框
        document.querySelectorAll('.act-chip').forEach(function (b) {
            b.addEventListener('click', function () { fillAction(b.getAttribute('data-act')); });
        });
        $('handbookClose').addEventListener('click', function () {
            $('handbookOverlay').classList.add('hidden');
        });
        $('personaOk').addEventListener('click', function () {
            $('personaOverlay').classList.add('hidden');
        });
        // 地点下拉切换（change 后立即复位为「当前」，等待 applyState 重渲染）
        $('locSelect').addEventListener('change', function (ev) {
            var loc = ev.target.value;
            if (loc) changeLocation(loc);
        });
        // 任务面板展开/收起（导航栏按钮 toggle；点击面板与按钮以外区域自动收起）
        $('questToggleBtn').addEventListener('click', function (ev) {
            ev.stopPropagation();
            $('questPanel').classList.toggle('hidden');
        });
        document.addEventListener('click', function (ev) {
            var panel = $('questPanel');
            if (panel.classList.contains('hidden')) return;
            if (ev.target === panel || panel.contains(ev.target) || ev.target === $('questToggleBtn')) return;
            panel.classList.add('hidden');
        });
        // 终局遮罩
        $('restartBtn').addEventListener('click', restart);

        // 开发者面板：仅 URL 带 ?debug=1 时创建（无参数时完全不渲染）
        var params = new URLSearchParams(location.search);
        debugOn = params.get('debug') === '1';
        if (debugOn) setupDebugPanel();

        // 刷新恢复：进游戏第一眼 = 存档选择界面
        showSelect();
    }

    document.addEventListener('DOMContentLoaded', init);
})();
