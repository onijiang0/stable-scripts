// [title: 青龙任务错峰]
// [name: ql_cron_stagger_node]
// [desc: 智能打散青龙任务定时。支持只处理指定时段、指定搬往区间，可列时段菜单并内置指令帮助。走青龙 /open 开放接口。]
// [author: AI_Engineer]
// [version: 3.3.0]
// [rule: ^青龙错峰(.*)$]
// [status: true]
// [admin: true]
// [public: false]
// [priority: 999]
// [class: 任务]
// [icon: https://api.iconify.design/lucide:zap.svg]
// [depe: []]

const { plugin, sender: s, Bucket, container, utils } = require("sillygirl");

// 1. 可视化配置表单
const config = new plugin.Form({
    qinglong_id: plugin.Form.integer()
        .title("青龙编号")
        .description("后台「容器」页面里青龙的编号，从 1 开始（发送【青龙错峰 检查】可列出）")
        .widget("qinglong-panel")
        .min(1)
        .default(1),
    source_range: plugin.Form.string()
        .title("只处理时段（可选）")
        .description("留空=全部。只打散定时落在这个区间的任务。如 08:00-09:00；也可临时用命令指定")
        .default(""),
    hour_range: plugin.Form.string()
        .title("默认搬往范围")
        .description("被打散的任务优先落到这个区间，如 8-20 表示 08:00~20:59。可用【到 ...】临时覆盖")
        .default("8-20"),
    scatter_hour: plugin.Form.boolean()
        .title("打散单次任务的小时")
        .description("开启后，将每天只执行1次的任务大范围随机分配到搬往区间内")
        .default(true),
    max_per_minute: plugin.Form.integer()
        .title("分钟最大并发")
        .description("任何一分钟最多允许挤几个任务，超出的自动错峰")
        .min(1)
        .default(2),
    exclude_keywords: plugin.Form.string()
        .title("排除关键词")
        .description("名称含这些词的任务保持原样（发送【青龙错峰 强制执行】可无视此项）")
        .default("抢购,秒杀,准点,固定,提现,抽奖,开奖"),
    dry_run: plugin.Form.boolean()
        .title("默认预览模式")
        .description("开启后发送【青龙错峰】仅预览，发送【青龙错峰 执行】才真正写入")
        .default(true),
});

const BACKUP_BUCKET = "ql_stagger_backup";
const MENU_BUCKET = "ql_stagger_menu";
const MENU_MAX_AGE_MS = 2 * 60 * 60 * 1000;

// ---- 青龙连接：统一走 SDK，内部自动换 token 并缓存 ----
// container.QingLong 读取 sillyGirl 桶的 qinglong_panels（即后台「容器」里配的面板），
// request() 会自动拼 /open 前缀并附带 Bearer token。
async function openQinglong(id) {
    const ql = new container.QingLong({ id: id });
    await ql.ready; // 编号不存在时在这里抛错，消息形如「青龙编号 3 不存在」
    return ql;
}

async function listPanels() {
    try {
        const info = await container.getList("qinglong");
        return (info && Array.isArray(info.list)) ? info.list : [];
    } catch (e) {
        return [];
    }
}

function panelHint(panels) {
    if (!panels.length) {
        return "当前后台没有配置任何青龙容器，请先到「容器」页面添加。";
    }
    const lines = panels.map(p => `  #${p.index}  ${p.name || "(未命名)"}  ${panelStatusText(p)}\n        ${p.address || "(无地址)"}`);
    return "现有青龙容器：\n" + lines.join("\n") + "\n请到插件配置里把「青龙编号」改成上面的编号。";
}

async function listCrons(ql) {
    const res = await ql.request("GET", "/crons", undefined, { page: 1 });
    return unwrapList(res);
}

// 更新定时的 body 必须是白名单：回传 status/pid/timestamp 等只读字段会出问题
function buildUpdatePayload(cron, schedule) {
    return {
        id: cron.id,
        name: cron.name,
        command: cron.command,
        schedule: schedule,
        labels: Array.isArray(cron.labels) ? cron.labels : [],
    };
}

// ---- 小工具 ----
function pad2(n) {
    return String(n).padStart(2, "0");
}

function hm(minutesOfDay) {
    const v = Math.max(0, Math.min(24 * 60 - 1, Number(minutesOfDay) || 0));
    return `${pad2(Math.floor(v / 60))}:${pad2(v % 60)}`;
}

function panelStatusText(panel) {
    const status = String(panel.status || "").toLowerCase();
    if (status === "online" || status === "ok") return "✅ " + status;
    if (panel.status) return "⚠️ " + panel.status;
    return "· 未检测";
}

// 解析时段：`8-20` 按「小时闭区间」= 08:00~20:59；`08:00-09:00` 按半开区间 = 08:00~08:59
function parseRange(text) {
    const raw = String(text || "").trim();
    if (!raw) return null;
    const m = raw.match(/(\d{1,2})(?::(\d{2}))?\s*[-~～到至]\s*(\d{1,2})(?::(\d{2}))?/);
    if (!m) return null;
    const h1 = Number(m[1]);
    const h2 = Number(m[3]);
    if (h1 < 0 || h1 > 23 || h2 < 0 || h2 > 23) return null;
    const m1 = m[2] === undefined ? 0 : Number(m[2]);
    const m2 = m[4] === undefined ? 0 : Number(m[4]);
    const startMin = h1 * 60 + m1;
    // 两个端点都只写了小时 → 视为闭区间；只要有一个带分钟 → 半开区间
    const endMin = (m[2] === undefined && m[4] === undefined) ? (h2 + 1) * 60 : h2 * 60 + m2;
    if (endMin <= startMin) return null;
    return { startMin: startMin, endMin: Math.min(endMin, 24 * 60) };
}

function fmtRange(range) {
    if (!range) return "全部时段";
    return `${hm(range.startMin)}-${hm(range.endMin)}（含 ${hm(range.startMin)} ~ ${hm(range.endMin - 1)}）`;
}

// 解析命令：标志词 + 源区间 + 目标区间
function parseCommand(arg) {
    const text = String(arg || "");
    const bare = text.trim();
    const flags = {
        help: text.includes("帮助") || text.includes("说明") || /^(help|\?|？)$/i.test(bare),
        check: text.includes("检查") || text.includes("自检"),
        restore: text.includes("恢复") || text.includes("还原"),
        menu: text.includes("时段") || text.includes("选项"),
        force: text.includes("强制"),
    };
    flags.execute = text.includes("执行") || flags.force;

    let pick = null;
    const pk = text.match(/选\s*(\d+)/);
    if (pk) pick = Number(pk[1]);

    let rest = text
        .replace(/帮助|说明|检查|自检|恢复|还原|时段|选项|强制|执行/g, " ")
        .replace(/选\s*\d+/g, " ")
        .replace(/[?？]/g, " ")
        .replace(/\s+/g, " ")
        .trim();

    let source = null;
    let target = null;
    const parts = rest.split(/到|至|→|->|=>/);
    if (parts.length >= 2) {
        source = parseRange(parts[0]);
        target = parseRange(parts[1]);
    } else {
        source = parseRange(rest);
    }
    return { help: flags.help, check: flags.check, restore: flags.restore, menu: flags.menu, force: flags.force, execute: flags.execute, pick: pick, source: source, target: target };
}

// 统计每个小时的定时任务数（只统计能定位到具体定时的任务）
function hourLoads(crons) {
    const byHour = new Array(24).fill(0);
    let total = 0;
    for (const task of crons) {
        const fields = String(task.schedule || "").trim().split(/\s+/);
        if (fields.length < 5 || fields.length > 6) continue;
        const minPart = fields[fields.length === 6 ? 1 : 0];
        const hourPart = fields[fields.length === 6 ? 2 : 1];
        if (!/^\d+$/.test(minPart) || !/^\d+$/.test(hourPart)) continue;
        byHour[Number(hourPart)]++;
        total++;
    }
    return { byHour: byHour, total: total };
}

// 统计"同一分钟挤了几个任务"，只认纯数字的 分/时 字段
function hotSlots(crons, limit) {
    const load = new Map();
    for (const task of crons) {
        const fields = String(task.schedule || "").trim().split(/\s+/);
        if (fields.length < 5 || fields.length > 6) continue;
        const minPart = fields[fields.length === 6 ? 1 : 0];
        const hourPart = fields[fields.length === 6 ? 2 : 1];
        if (!/^\d+$/.test(minPart) || !/^\d+$/.test(hourPart)) continue;
        const key = Number(hourPart) * 60 + Number(minPart);
        load.set(key, (load.get(key) || 0) + 1);
    }
    return [...load.entries()]
        .map(([key, count]) => ({ h: Math.floor(key / 60), m: key % 60, n: count }))
        .filter(item => item.n > 1)
        .sort((a, b) => b.n - a.n || (a.h * 60 + a.m) - (b.h * 60 + b.m))
        .slice(0, limit);
}

async function loadMenu() {
    try {
        const raw = await new Bucket(MENU_BUCKET).get("last_menu");
        if (!raw) return null;
        const saved = JSON.parse(raw);
        if (!saved || !Array.isArray(saved.items) || !saved.items.length) return null;
        return saved;
    } catch (e) {
        return null;
    }
}

async function main() {
    if (!(await s.isAdmin())) return s.reply("仅管理员可用");

    const cmd = parseCommand(await readArg());

    if (cmd.help) return await handleHelp();
    if (cmd.restore) return await handleRestore();
    if (cmd.check) return await handleCheck();

    let cfg;
    try {
        cfg = normalizeConfig(await config.get());
    } catch (e) {
        return s.reply(`配置有问题：${e.message}`);
    }

    // 列出时段菜单
    if (cmd.menu) return await handleMenu(cfg);

    // 用菜单序号选择要处理的时段
    if (cmd.pick !== null) {
        const saved = await loadMenu();
        if (!saved) return s.reply("❌ 还没有时段选项，请先发送【青龙错峰 时段】。");
        if (Date.now() - (saved.at || 0) > MENU_MAX_AGE_MS) {
            return s.reply("⚠️ 上次的时段选项已过期（超过 2 小时），请重新发送【青龙错峰 时段】。");
        }
        const item = saved.items[cmd.pick - 1];
        if (!item) {
            return s.reply(`❌ 没有选项 ${cmd.pick}，当前只有 ${saved.items.length} 个。请重发【青龙错峰 时段】。`);
        }
        cmd.source = { startMin: item.h * 60, endMin: (item.h + 1) * 60 };
        cfg.sourceFromLabel = `来源：选项 ${cmd.pick}（${pad2(item.h)}:00 这一小时，${item.count} 个任务）`;
    }

    if (cmd.source) cfg.source = cmd.source;
    if (cmd.target) cfg.target = cmd.target;
    if (cmd.execute) cfg.dryRun = false;

    return await runStagger(cfg, cmd);
}

async function runStagger(cfg, cmd) {
    let ql;
    try {
        ql = await openQinglong(cfg.qinglongId);
    } catch (e) {
        const panels = await listPanels();
        return s.reply(`❌ 青龙 #${cfg.qinglongId} 不可用：${e.message}\n\n${panelHint(panels)}`);
    }

    const scopeText = cfg.source
        ? `只处理 ${fmtRange(cfg.source)}`
        : "处理全部时段";

    try {
        await s.reply([
            `🔍 正在连接青龙容器 #${cfg.qinglongId}（${ql.name || ""} ${ql.address || ""}）...`,
            `🧠 二维水位线算法 · ${scopeText} · 搬往 ${fmtRange(cfg.target)}`,
        ].join("\n"));

        const allTasks = await listCrons(ql);
        if (!allTasks.length) return s.reply("❌ 获取任务列表为空，请检查面板状态。");

        const minuteLoads = new Array(60).fill(0);
        const slotLoads = new Array(24 * 60).fill(0);
        const bucketMin = Array.from({ length: 60 }, () => []);
        const bucketSlot = Array.from({ length: 24 * 60 }, () => []);

        const tasksToKeep = [];
        const tasksToMoveMin = [];
        const tasksToMoveHourMin = [];

        const inSource = (h, m) => {
            if (!cfg.source) return true;
            const t = h * 60 + m;
            return t >= cfg.source.startMin && t < cfg.source.endMin;
        };

        // 1. 扫描与入桶
        for (const task of allTasks) {
            const before = String(task.schedule || "").trim();
            const fields = before.split(/\s+/);
            if (fields.length < 5 || fields.length > 6) {
                tasksToKeep.push(task);
                continue;
            }

            const minIndex = fields.length === 6 ? 1 : 0;
            const hourIndex = fields.length === 6 ? 2 : 1;
            const minPart = fields[minIndex];
            const hourPart = fields[hourIndex];

            const hasKeyword = !cmd.force && cfg.keywords.some(k =>
                (task.name || "").includes(k) || (task.command || "").includes(k));

            const minPlain = /^\d+$/.test(minPart);
            const hourPlain = /^\d+$/.test(hourPart);
            const m = minPlain ? Number(minPart) : null;
            const h = hourPlain ? Number(hourPart) : null;

            // 关键词命中 / 分钟不是纯数字 → 原样保留（能定位的话照常计入水位）
            if (hasKeyword || !minPlain) {
                tasksToKeep.push(task);
                if (minPlain) minuteLoads[m]++;
                if (minPlain && hourPlain) slotLoads[h * 60 + m]++;
                continue;
            }

            // 指定了「只处理时段」：不在区间内、或小时无法定位的，一律原样保留并计入水位
            if (cfg.source) {
                if (!hourPlain || !inSource(h, m)) {
                    tasksToKeep.push(task);
                    minuteLoads[m]++;
                    if (hourPlain) slotLoads[h * 60 + m]++;
                    continue;
                }
                bucketSlot[h * 60 + m].push({ task, minIndex, hourIndex, before, fields, h, m });
                continue;
            }

            // 未指定时段：沿用原行为
            if (cfg.scatterHour && hourPlain) {
                bucketSlot[h * 60 + m].push({ task, minIndex, hourIndex, before, fields, h, m });
            } else {
                bucketMin[m].push({ task, minIndex, before, fields, m });
            }
        }

        // 2. 检测超载
        for (let i = 0; i < 24 * 60; i++) {
            for (const item of bucketSlot[i]) {
                if (slotLoads[i] < 1) {
                    slotLoads[i]++;
                    minuteLoads[item.m]++;
                    tasksToKeep.push(item.task);
                } else {
                    tasksToMoveHourMin.push(item);
                }
            }
        }

        for (let m = 0; m < 60; m++) {
            for (const item of bucketMin[m]) {
                if (minuteLoads[m] < cfg.maxPerMinute) {
                    minuteLoads[m]++;
                    tasksToKeep.push(item.task);
                } else {
                    tasksToMoveMin.push(item);
                }
            }
        }

        if (tasksToMoveHourMin.length === 0 && tasksToMoveMin.length === 0) {
            return s.reply(`🎉 ${scopeText}：这一段已经均匀了，没有需要搬运的任务。`);
        }

        const changes = [];
        const minSlot = Math.max(0, cfg.target.startMin);
        const maxSlot = Math.min(24 * 60, cfg.target.endMin);

        // 3. 全天二维大洗牌：优先落到搬往区间里最空闲的分钟；不搬回原区间
        for (const item of tasksToMoveHourMin) {
            let bestLoad = Infinity;
            let candidates = [];
            for (let t = minSlot; t < maxSlot; t++) {
                if (cfg.source && t >= cfg.source.startMin && t < cfg.source.endMin) continue;
                const load = slotLoads[t];
                if (load < bestLoad) {
                    bestLoad = load;
                    candidates = [t];
                } else if (load === bestLoad) {
                    candidates.push(t);
                }
            }
            if (!candidates.length) {
                tasksToKeep.push(item.task);
                continue;
            }
            const slot = candidates[Math.floor(Math.random() * candidates.length)];
            slotLoads[slot]++;
            minuteLoads[slot % 60]++;

            item.fields[item.minIndex] = String(slot % 60);
            item.fields[item.hourIndex] = String(Math.floor(slot / 60));
            item.after = item.fields.join(" ");

            changes.push({
                id: item.task.id,
                name: String(item.task.name || item.task.id || "未命名"),
                before: item.before,
                after: item.after,
            });
        }

        // 4. 局部一维洗牌（只改分钟，给多频次任务腾分钟）
        for (const item of tasksToMoveMin) {
            const targetLoad = Math.min(...minuteLoads);
            const candidates = minuteLoads.map((val, idx) => val === targetLoad ? idx : -1).filter(v => v !== -1);
            const choice = candidates[Math.floor(Math.random() * candidates.length)];

            minuteLoads[choice]++;
            item.fields[item.minIndex] = String(choice);
            item.after = item.fields.join(" ");

            changes.push({
                id: item.task.id,
                name: String(item.task.name || item.task.id || "未命名"),
                before: item.before,
                after: item.after,
            });
        }

        if (!changes.length) {
            return s.reply(`🎉 ${scopeText}：没有可搬运的任务（可能都被关键词或表达式复杂度挡下了）。`);
        }

        // 5. 下发修改
        let successCount = 0;
        const failures = [];
        if (!cfg.dryRun) {
            await s.reply(`🚀 正在通过青龙开放接口下发 ${changes.length} 条定时...`);

            const bkt = new Bucket(BACKUP_BUCKET);
            await bkt.set("last_backup", JSON.stringify(changes));
            await bkt.set("last_ql_id", String(cfg.qinglongId));
            await bkt.set("last_backup_at", new Date().toISOString());

            const byId = new Map(allTasks.map(t => [String(t.id), t]));
            let done = 0;
            for (const change of changes) {
                const origin = byId.get(String(change.id));
                if (!origin) {
                    failures.push(`${change.name}: 任务已不存在`);
                    continue;
                }
                try {
                    await ql.request("PUT", "/crons", buildUpdatePayload(origin, change.after));
                    successCount++;
                } catch (e) {
                    failures.push(`${change.name}: ${e.message}`);
                }
                done++;
                if (done % 25 === 0) await utils.sleep(300);
            }
        }

        // 6. 展示报表
        const MAX_LINES = 40;
        const shown = changes.slice(0, MAX_LINES);
        const lines = shown.map(item => `${item.name}\n   ${item.before}  →  ${item.after}`);
        if (changes.length > shown.length) {
            lines.push(`…… 其余 ${changes.length - shown.length} 条已省略`);
        }

        const summaryMsg = [
            cfg.dryRun ? "📊 错峰分析完成（预览模式，未修改）" : "✅ 青龙定时错峰完毕",
            "",
            `【容器】#${cfg.qinglongId} ${ql.name || ""}`.trimEnd(),
            `【范围】${scopeText}`,
            cfg.sourceFromLabel ? `　　　 ${cfg.sourceFromLabel}` : null,
            `【搬往】${fmtRange(cfg.target)}`,
            `【保持原样】${tasksToKeep.length} 个（关键词/复杂表达式/不在区间）`,
            `【全天打散】${tasksToMoveHourMin.length} 个`,
            `【分钟打散】${tasksToMoveMin.length} 个`,
            "",
            `【改动明细】共 ${changes.length} 条`,
            "----------------",
            ...lines,
            "----------------",
        ].filter(line => line !== null && line !== undefined);

        if (cfg.dryRun) {
            summaryMsg.push("⚠️ 当前未修改任何任务！");
            summaryMsg.push("要写入请在原命令后加【执行】，例如：");
            summaryMsg.push(` 青龙错峰 ${rangeCommand(cfg)} 执行`.replace(/\s+/g, " "));
        } else {
            summaryMsg.push(`✅ 成功写入 ${successCount}/${changes.length} 个任务！`);
            if (failures.length) {
                summaryMsg.push(`⚠️ 失败 ${failures.length} 个：`);
                summaryMsg.push(...failures.slice(0, 10).map(f => "  · " + f));
                if (failures.length > 10) summaryMsg.push(`  …… 其余 ${failures.length - 10} 条略`);
            }
            summaryMsg.push("💡 原定时已备份，后悔可发送：【青龙错峰 恢复】");
        }

        await s.reply(summaryMsg.join("\n"));
    } catch (error) {
        await s.reply(`执行失败：${error.message}`);
    }
}

function rangeCommand(cfg) {
    const parts = [];
    if (cfg.source) parts.push(`${hm(cfg.source.startMin)}-${hm(cfg.source.endMin)}`);
    if (cfg.target) parts.push(`到 ${hm(cfg.target.startMin)}-${hm(cfg.target.endMin)}`);
    return parts.join(" ");
}

async function handleHelp() {
    const lines = [
        "📖 青龙错峰 · 指令说明",
        "",
        "作用：把挤在同一时间开跑的青龙任务定时打散，降低面板瞬时压力。",
        "命令发给本机器人即可（QQ 私聊 / Web Bot 聊天框）。",
        "",
        "── 查看类（只读，绝对安全）──",
        "  青龙错峰 帮助          显示这份说明",
        "  青龙错峰 检查          自检：容器连接、任务总数、最拥挤的分钟",
        "  青龙错峰 时段          按小时列出任务数（带 ①②③ 编号），用于挑选",
        "",
        "── 预览类（不改动任何东西）──",
        "  青龙错峰                              处理全部时段",
        "  青龙错峰 08:00-09:00                  只处理这一段",
        "  青龙错峰 08:00-09:00 到 10:00-20:00   只处理这段，并指定搬到 10~20 点",
        "  青龙错峰 选 1                         用「时段」列表里的第 1 项",
        "  青龙错峰 选 1 到 10:00-20:00          选第 1 项 + 指定搬往",
        "  青龙错峰 强制执行                     无视配置里的「排除关键词」",
        "",
        "── 写入类（真正改定时，务必先预览）──",
        "  在上面任何一条末尾加「执行」，例如：",
        "  青龙错峰 08:00-09:00 执行",
        "  青龙错峰 选 1 执行",
        "",
        "── 回滚 ──",
        "  青龙错峰 恢复          按备份把上一次的改动还原回去",
        "",
        "── 时段怎么写 ──",
        "  08:00-09:00   带分钟 → 08:00 ~ 08:59（只含 8 点这一小时）",
        "  8-9           两端只写小时 → 当成闭区间 08:00 ~ 09:59",
        "  8-20          同上 → 08:00 ~ 20:59",
        "  拿不准就看输出里的【范围】那行，它永远写明实际命中范围。",
        "",
        "── 安全规则 ──",
        "  · 结尾不加「执行」= 预览，一个字都不改。",
        "  · 每次写入前自动备份，可用「青龙错峰 恢复」一键还原。",
        "  · 写入时不会把任务搬回你指定的源区间。",
        "  · 「到 XXX」是选填的，不写就用下面配置里的「默认搬往」。",
    ];

    try {
        const cfg = normalizeConfig(await config.get());
        lines.push("");
        lines.push("── 当前配置 ──");
        lines.push(`  青龙编号      #${cfg.qinglongId}`);
        lines.push(`  只处理时段    ${fmtRange(cfg.source)}`);
        lines.push(`  默认搬往      ${fmtRange(cfg.target)}`);
        lines.push(`  单分钟上限    ${cfg.maxPerMinute} 个`);
        lines.push(`  排除关键词    ${cfg.keywords.join(" / ") || "（无）"}`);
        lines.push(`  默认预览      ${cfg.dryRun ? "开（不加「执行」不会写入）" : "⚠️ 关（不加「执行」也会写入！）"}`);
    } catch (e) {
        lines.push("");
        lines.push(`── 当前配置 ── 读取失败：${e.message}`);
    }

    await s.reply(lines.join("\n"));
}

async function handleMenu(cfg) {
    let ql;
    try {
        ql = await openQinglong(cfg.qinglongId);
    } catch (e) {
        const panels = await listPanels();
        return s.reply(`❌ 青龙 #${cfg.qinglongId} 不可用：${e.message}\n\n${panelHint(panels)}`);
    }

    try {
        const crons = await listCrons(ql);
        const { byHour, total } = hourLoads(crons);

        const items = byHour
            .map((count, h) => ({ h: h, count: count }))
            .filter(x => x.count > 0)
            .sort((a, b) => b.count - a.count || a.h - b.h)
            .slice(0, 10);

        if (!items.length) {
            return s.reply("❌ 没有可定位到具体定时的任务（可能全用了 * 或 / 这类表达式）。");
        }

        await new Bucket(MENU_BUCKET).set("last_menu", JSON.stringify({ at: Date.now(), items: items }));

        const lines = [
            "🕐 时段选项（按任务数从多到少）",
            "",
        ];
        const circle = ["①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩"];
        items.forEach((item, index) => {
            const tag = index === 0 ? "   ← 最挤" : "";
            lines.push(`  ${circle[index]} ${pad2(item.h)}:00-${pad2(item.h + 1)}:00   ${String(item.count).padStart(3)} 个${tag}`);
        });

        lines.push("");
        lines.push(`共 ${total} 个任务能定位到具体定时（其余是 * / , 这类表达式，无法定点处理）`);
        lines.push("");
        lines.push("📋 三种用法，任选：");
        lines.push("");
        lines.push("  1) 回复序号，例如：");
        lines.push("     青龙错峰 选 1");
        lines.push("     青龙错峰 选 1 到 10:00-20:00");
        lines.push("     青龙错峰 选 1 执行");
        lines.push("");
        lines.push("  2) 直接写区间，例如：");
        lines.push(`     青龙错峰 ${pad2(items[0].h)}:00-${pad2(items[0].h + 1)}:00`);
        lines.push("     青龙错峰 08:00-09:00 到 10:00-20:00");
        lines.push("     青龙错峰 08:00-09:00 执行");
        lines.push("");
        lines.push("  3) 多小时一起处理，用冒号写端点：");
        lines.push("     青龙错峰 08:00-12:00 到 13:00-22:00");
        lines.push("");
        lines.push("提示：不带【执行】就是预览，不会改任何东西。选 1/选 2 的有效期 2 小时。");
        lines.push("注意：只写 `8-9` 会被当成「小时闭区间」= 08:00~09:59；");
        lines.push("      想只处理 8 点这一小时，请写 `08:00-09:00`。");
        lines.push("全部指令含义发【青龙错峰 帮助】。");

        await s.reply(lines.join("\n"));
    } catch (e) {
        await s.reply(`❌ 读取任务失败：${e.message}`);
    }
}

async function handleCheck() {
    const lines = ["🔎 青龙容器自检"];

    let cfg = null;
    let cfgErr = "";
    try {
        cfg = normalizeConfig(await config.get());
    } catch (e) {
        cfgErr = e.message;
    }

    const panels = await listPanels();
    lines.push("");
    lines.push(`【容器】共 ${panels.length} 个`);
    if (!panels.length) {
        lines.push("  ⚠️ 后台「容器」页面还没有青龙，请先添加一个");
    } else {
        for (const p of panels) {
            const mark = (cfg && p.index === cfg.qinglongId) ? "▶" : " ";
            lines.push(`  ${mark} #${p.index}  ${p.name || "(未命名)"}  ${panelStatusText(p)}`);
            lines.push(`        ${p.address || "(无地址)"}`);
        }
        lines.push("  ▶ = 当前配置使用的编号");
    }

    if (cfgErr) {
        lines.push("");
        lines.push(`【配置】❌ ${cfgErr}`);
        return s.reply(lines.join("\n"));
    }

    lines.push("");
    lines.push(`【当前配置】#${cfg.qinglongId} · 只处理 ${fmtRange(cfg.source)} · 搬往 ${fmtRange(cfg.target)} · 单分钟上限 ${cfg.maxPerMinute} 个`);

    if (!panels.some(p => p.index === cfg.qinglongId)) {
        lines.push("");
        lines.push("⚠️ 上面的容器里没有你配置的编号，请到插件配置里改掉。");
        return s.reply(lines.join("\n"));
    }

    try {
        const ql = await openQinglong(cfg.qinglongId);
        const crons = await listCrons(ql);

        lines.push("");
        lines.push(`【连接】✅ ${ql.name || ""} ${ql.address || ""}`.trimEnd());
        lines.push(`【任务】✅ 共 ${crons.length} 条`);

        const hot = hotSlots(crons, 3);
        lines.push("");
        if (hot.length) {
            lines.push("【最拥挤的分钟】");
            for (const spot of hot) {
                lines.push(`  ${pad2(spot.h)}:${pad2(spot.m)}   ${String(spot.n).padStart(3)} 个任务`);
            }
        } else {
            lines.push("【最拥挤的分钟】没有重合，分布很干净");
        }

        const sample = crons.slice(0, 3);
        if (sample.length) {
            lines.push("");
            lines.push("【抽样】");
            for (const c of sample) {
                lines.push(`  ${String(c.schedule || "").padEnd(15)}  ${c.name || c.command || ""}`);
            }
        }

        lines.push("");
        lines.push("💡 只想处理某一段？发【青龙错峰 时段】列出可选时段。");
        lines.push("   全部指令说明发【青龙错峰 帮助】。");
    } catch (e) {
        lines.push("");
        lines.push(`【连接】❌ ${e.message}`);
    }

    await s.reply(lines.join("\n"));
}

async function handleRestore() {
    try {
        const bkt = new Bucket(BACKUP_BUCKET);
        const backupStr = await bkt.get("last_backup");
        if (!backupStr) return s.reply("❌ 未找到可用的备份记录。");

        const qlId = Number(await bkt.get("last_ql_id") || 1);
        const at = await bkt.get("last_backup_at") || "未知时间";
        const changes = JSON.parse(backupStr);

        let ql;
        try {
            ql = await openQinglong(qlId);
        } catch (e) {
            return s.reply(`❌ 青龙 #${qlId} 不可用：${e.message}`);
        }

        await s.reply(`🔄 备份时间 ${at}，共 ${changes.length} 条，正在还原到容器 #${qlId}...`);

        const crons = await listCrons(ql);
        const byId = new Map(crons.map(t => [String(t.id), t]));

        let ok = 0;
        const missing = [];
        let done = 0;
        for (const change of changes) {
            const cur = byId.get(String(change.id));
            if (!cur) {
                missing.push(change.name);
                continue;
            }
            try {
                await ql.request("PUT", "/crons", buildUpdatePayload(cur, change.before));
                ok++;
            } catch (e) {
                missing.push(`${change.name}(${e.message})`);
            }
            done++;
            if (done % 25 === 0) await utils.sleep(300);
        }

        const msg = [`✅ 恢复完成：成功 ${ok}/${changes.length} 个`];
        if (missing.length) {
            msg.push(`⚠️ 未还原 ${missing.length} 个：`);
            msg.push(...missing.slice(0, 10).map(m => "  · " + m));
            if (missing.length > 10) msg.push(`  …… 其余 ${missing.length - 10} 条略`);
        }
        await s.reply(msg.join("\n"));
    } catch (error) {
        await s.reply(`青龙定时恢复失败：${error.message}`);
    }
}

// 兼容读取命令参数：优先规则捕获组，取不到再退回整条消息
async function readArg() {
    let arg = "";
    try {
        arg = String((await s.param(1)) || "");
    } catch (e) { }
    if (!arg) {
        try {
            const msg = String((await s.getMsg()) || "").trim();
            arg = msg.replace(/^青龙错峰/, "").trim();
        } catch (e) { }
    }
    return arg;
}

function boolValue(value, fallback) {
    if (value === undefined || value === null || value === "") return fallback;
    if (value === false || value === "false" || value === 0 || value === "0") return false;
    return true;
}

function normalizeConfig(raw) {
    const value = raw || {};
    const qinglongId = Number(value.qinglong_id || 1);
    const maxPerMinute = Number(value.max_per_minute || 2);

    if (!Number.isInteger(qinglongId) || qinglongId < 1) throw new Error("青龙编号必须从 1 开始");
    if (!Number.isInteger(maxPerMinute) || maxPerMinute < 1) throw new Error("每分钟最大并发数至少为 1");

    const target = parseRange(value.hour_range) || { startMin: 8 * 60, endMin: 21 * 60 };

    return {
        qinglongId: qinglongId,
        maxPerMinute: maxPerMinute,
        scatterHour: boolValue(value.scatter_hour, true),
        dryRun: boolValue(value.dry_run, true),
        source: parseRange(value.source_range),
        target: target,
        keywords: String(value.exclude_keywords || "抢购,秒杀,准点,固定,提现,抽奖,开奖")
            .split(/[,，\n]/).map(item => item.trim()).filter(Boolean),
    };
}

function unwrapList(value) {
    let current = value;
    for (let index = 0; index < 4 && current && !Array.isArray(current); index += 1) {
        current = current.data ?? current.items ?? current.list;
    }
    return Array.isArray(current) ? current : [];
}

main();
