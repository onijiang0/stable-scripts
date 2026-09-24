// [title: 服务器监控预警]
// [name: server_monitor]
// [desc: 定时检测服务器状态并发送资源预警。支持进程/Docker 监控、面板在线探测、主机别名、推送结果可查、一键自检。]
// [author: Gemini]
// [version: v2.2.0]
// [cron: */60 * * * *]
// [rule: ^(服务器状态|监控查询|监控自检|监控发现|监控日志)$]
// [status: true]
// [admin: true]
// [public: false]
// [priority: 1]
// [class: 运维工具]
// [icon: https://api.iconify.design/lucide:activity.svg]
// [depe: []]

const os = require('os');
const fs = require('fs');
const { execSync } = require('child_process');
const { sender: s, plugin, Bucket, container } = require("sillygirl");

// 1. 可视化配置表单
const config = new plugin.Form({
    enable: plugin.Form.boolean().title("是否启用监控").default(true),
    host_alias: plugin.Form.string()
        .title("主机别名")
        .description("报告里显示的名字，比容器 ID 好认。填 - 则显示系统 hostname")
        .default("miniPC"),
    show_panel_address: plugin.Form.boolean()
        .title("面板状态显示地址")
        .description("关闭时只显示「面板 [名称] 在线」，不附带 http://... 地址")
        .default(false),
    show_channel_users: plugin.Form.boolean()
        .title("推送通道显示账号数")
        .description("关闭时只显示通道名（如 qq），开启后显示 qq(1) 这类数量")
        .default(false),
    mem_normal: plugin.Form.integer()
        .title("内存普通提醒阈值(%)")
        .description("容器与宿主机取较高者判定")
        .default(80),
    mem_high: plugin.Form.integer().title("内存高危告警阈值(%)").default(90),
    mem_critical: plugin.Form.integer().title("内存紧急告警阈值(%)").default(95),
    cpu_threshold: plugin.Form.integer().title("CPU高危告警阈值(%)").default(85),
    disk_mounts: plugin.Form.string()
        .title("监控磁盘挂载点")
        .description("逗号分隔，如 / 或 /,/data。默认只监控根分区")
        .default("/"),
    disk_threshold: plugin.Form.integer().title("磁盘占用告警阈值(%)").default(90),
    cooldown_mins: plugin.Form.integer().title("相同告警冷却期(分钟)").default(30),
    watch_panels: plugin.Form.boolean()
        .title("监控面板在线状态")
        .description("探测傻妞里配置的青龙/smallcat/呆呆容器是否在线")
        .default(true),
    processes: plugin.Form.string()
        .title("监控进程(逗号分隔)")
        .description("只有跑在宿主机上才看得到宿主进程。发【监控发现】可列出本机可见的进程名")
        .default(""),
    containers: plugin.Form.string()
        .title("监控容器(逗号分隔,如: qinglong,redis)")
        .description("需要 docker CLI/socket 可用。发【监控发现】可列出正在跑的容器名")
        .default(""),
});

const BUCKET_STATE = "server_monitor_state";
const BUCKET_LOG = "server_monitor_log";
const LOG_KEEP = 20;
const PANEL_PING_TIMEOUT = 8000;

// ---- 配置兜底 ----
function resolveAlias(value) {
    const alias = String(value === undefined || value === null ? "" : value).trim();
    if (alias === "-") return os.hostname();
    return alias || "miniPC";
}

// ⚠️ 实测：表单 schema 的 .default() 只在网页端生效，服务端 config.get() 拿不到默认值。
// 未手工保存过配置时所有字段都是 undefined —— 而 `x >= undefined` 恒为 false，
// 会让所有阈值判断失效（永不告警）。所以这里必须自己兜底。
function normalizeConf(raw) {
    const v = raw || {};
    const num = (x, d) => {
        const n = Number(x);
        return Number.isFinite(n) && n > 0 ? n : d;
    };
    const bool = (x, d) => (x === undefined || x === null || x === "")
        ? d
        : !(x === false || x === "false" || x === 0 || x === "0");
    return {
        enable: bool(v.enable, true),
        host_alias: resolveAlias(v.host_alias),
        show_panel_address: bool(v.show_panel_address, false),
        show_channel_users: bool(v.show_channel_users, false),
        mem_normal: num(v.mem_normal, 80),
        mem_high: num(v.mem_high, 90),
        mem_critical: num(v.mem_critical, 95),
        cpu_threshold: num(v.cpu_threshold, 85),
        disk_threshold: num(v.disk_threshold, 90),
        disk_mounts: String(v.disk_mounts || "/"),
        cooldown_mins: num(v.cooldown_mins, 30),
        watch_panels: bool(v.watch_panels, true),
        processes: String(v.processes || ""),
        containers: String(v.containers || ""),
    };
}

async function loadConf() {
    let raw = {};
    try {
        raw = await config.get();
    } catch (e) { }
    return normalizeConf(raw);
}

// ---- 基础工具 ----

function pad2(v) { return String(v).padStart(2, '0'); }

function formatTime(date) {
    return `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())} ${pad2(date.getHours())}:${pad2(date.getMinutes())}:${pad2(date.getSeconds())}`;
}

function shortTime(date) {
    return `${pad2(date.getMonth() + 1)}-${pad2(date.getDate())} ${pad2(date.getHours())}:${pad2(date.getMinutes())}`;
}

function fmtBytes(bytes) {
    const n = Number(bytes);
    if (!Number.isFinite(n) || n <= 0) return "0";
    const units = ["B", "K", "M", "G", "T"];
    let i = 0;
    let v = n;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return `${v.toFixed(v >= 100 ? 0 : 1)}${units[i]}`;
}

function safePath(p) {
    const v = String(p || "").trim();
    return /^[A-Za-z0-9_./-]+$/.test(v) ? v : "";
}

function runCmd(cmd, timeout) {
    return execSync(cmd, { timeout: timeout || 8000, stdio: ['ignore', 'pipe', 'ignore'] }).toString().trim();
}

function withTimeout(promise, ms, label) {
    let timer;
    const guard = new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error(`${label} 超时 ${Math.round(ms / 1000)}s`)), ms);
    });
    return Promise.race([promise, guard]).finally(() => clearTimeout(timer));
}

function commandExists(cmd) {
    try {
        execSync(`command -v ${cmd}`, { timeout: 4000, stdio: ['ignore', 'pipe', 'ignore'] });
        return true;
    } catch (e) {
        return false;
    }
}

let _envCache = null;
function detectEnv() {
    if (_envCache) return _envCache;

    let inContainer = false;
    try {
        if (fs.existsSync('/.dockerenv')) inContainer = true;
        else {
            const cgroup = fs.readFileSync('/proc/1/cgroup', 'utf8');
            if (/docker|kubepods|containerd|lxc/.test(cgroup)) inContainer = true;
        }
    } catch (e) { }

    // 先看命令在不在（快），再试着连一次 daemon（慢，失败要等超时）
    const dockerBin = commandExists("docker");
    let dockerCli = false;
    if (dockerBin) {
        try {
            execSync('docker version --format "{{.Server.Version}}"', { timeout: 5000, stdio: ['ignore', 'pipe', 'ignore'] });
            dockerCli = true;
        } catch (e) { }
    }

    let dockerSock = false;
    try { dockerSock = fs.existsSync('/var/run/docker.sock'); } catch (e) { }

    _envCache = {
        inContainer: inContainer,
        dockerBin: dockerBin,
        dockerCli: dockerCli,
        dockerSock: dockerSock,
        hasPgrep: commandExists("pgrep"),
        hasPs: commandExists("ps"),
        hostname: os.hostname(),
    };
    return _envCache;
}

// ---- A2：容器内读数口径 ----

// 读取容器内存限额；读不到（非容器 / 无限额）返回 null
function readCgroupMemory() {
    try {
        const max = String(fs.readFileSync('/sys/fs/cgroup/memory.max', 'utf8')).trim();
        const cur = String(fs.readFileSync('/sys/fs/cgroup/memory.current', 'utf8')).trim();
        if (/^\d+$/.test(max) && /^\d+$/.test(cur)) {
            const limit = Number(max);
            if (limit > 0 && limit < 1e15) {
                return { limit: limit, used: Number(cur), source: 'cgroup v2' };
            }
        }
    } catch (e) { }
    try {
        const limit = String(fs.readFileSync('/sys/fs/cgroup/memory/memory.limit_in_bytes', 'utf8')).trim();
        const usage = String(fs.readFileSync('/sys/fs/cgroup/memory/memory.usage_in_bytes', 'utf8')).trim();
        if (/^\d+$/.test(limit) && /^\d+$/.test(usage)) {
            const l = Number(limit);
            if (l > 0 && l < 1e15) {
                return { limit: l, used: Number(usage), source: 'cgroup v1' };
            }
        }
    } catch (e) { }
    return null;
}

function pct(used, total) {
    if (!total) return 0;
    return Number(((used / total) * 100).toFixed(1));
}

function levelOf(value, conf) {
    if (value >= conf.mem_critical) return "紧急告警";
    if (value >= conf.mem_high) return "高危告警";
    if (value >= conf.mem_normal) return "普通提醒";
    return "正常";
}

// 内存：容器限额与宿主机两个口径都报，风险取较高者
function checkMemory(conf) {
    const total = os.totalmem();
    const free = os.freemem();
    const hostPct = pct(total - free, total);

    const cg = readCgroupMemory();
    const cgPct = cg ? pct(cg.used, cg.limit) : null;

    const worst = cgPct === null ? hostPct : Math.max(hostPct, cgPct);

    const parts = [];
    if (cgPct !== null) parts.push(`容器 ${fmtBytes(cg.used)}/${fmtBytes(cg.limit)}（${cgPct}%）`);
    parts.push(`宿主机 ${fmtBytes(total - free)}/${fmtBytes(total)}（${hostPct}%）`);

    return {
        text: parts.join(" · "),
        worst: worst,
        riskLevel: levelOf(worst, conf),
        cgroup: cg ? cg.source : null,
    };
}

// 磁盘：支持多挂载点
function checkDisk(conf) {
    const mounts = String(conf.disk_mounts || "/").split(/[,，\s]+/).map(x => safePath(x)).filter(Boolean);
    const list = [];
    let worst = 0;
    for (const mount of (mounts.length ? mounts : ["/"])) {
        try {
            const out = runCmd(`df -P ${mount} | awk 'NR==2 {print $2, $4, $5, $6}'`, 6000);
            const f = out.split(/\s+/);
            if (f.length < 4) throw new Error("输出异常");
            const usage = Number(String(f[2]).replace('%', '')) || 0;
            list.push({ mount: f[3] || mount, usage: usage, text: `${f[3] || mount} ${usage}%（可用 ${fmtBytes(Number(f[1]) * 1024)}）` });
            worst = Math.max(worst, usage);
        } catch (e) {
            list.push({ mount: mount, usage: null, text: `${mount} 读取失败（${e.message}）` });
        }
    }
    return {
        list: list,
        text: list.map(x => x.text).join(" · "),
        worst: worst,
        diskHigh: worst >= conf.disk_threshold,
        failed: list.filter(x => x.usage === null).length,
    };
}

// 采用采样计算一段时间内的 CPU 使用率，过滤瞬时毛刺
function checkCPU(conf) {
    return new Promise((resolve) => {
        const start = getCPUInfo();
        setTimeout(() => {
            const end = getCPUInfo();
            const idleDiff = end.idle - start.idle;
            const totalDiff = end.total - start.total;
            // A6：totalDiff 为 0 时不能算出 NaN，退化为 0 并标注
            const usage = totalDiff > 0 ? Number((100 - (100 * idleDiff / totalDiff)).toFixed(1)) : null;
            resolve({
                cpuUsage: usage,
                text: usage === null ? "采样不足（1 秒内无变化）" : `${usage}%`,
                cpuHigh: usage === null ? false : usage >= conf.cpu_threshold,
            });
        }, 1000); // 1秒采样
    });
}

function getCPUInfo() {
    const cpus = os.cpus();
    let idle = 0, total = 0;
    for (let cpu of cpus) {
        for (let type in cpu.times) {
            total += cpu.times[type];
        }
        idle += cpu.times.idle;
    }
    return { idle, total };
}

// ---- A1：进程与容器监控 ----
// 关键：先确认本机有没有检测能力，没有就明确说"跳过"，
// 绝不能因为 pgrep/docker 不存在而误报「异常退出」——那会刷出假告警。
function checkServices(conf) {
    const statusList = [];
    let hasError = false;
    const env = detectEnv();

    if (conf.processes) {
        const procs = conf.processes.split(',').map(p => p.trim()).filter(Boolean);
        if (!env.hasPgrep) {
            statusList.push(`⚠️ 本机没有 pgrep（procps 未安装），已跳过 ${procs.length} 个进程检查`);
            statusList.push("     容器内看不到宿主进程；如需进程监控，请给容器加 --pid=host 并安装 procps");
        } else {
            procs.forEach(proc => {
                if (!safePath(proc)) {
                    statusList.push(`⚠️ 进程 [${proc}] 名称含非法字符，已跳过`);
                    return;
                }
                try {
                    runCmd(`pgrep -x "${proc}"`, 5000);
                    statusList.push(`✅ 进程 [${proc}] 运行中`);
                } catch (e) {
                    statusList.push(`❌ 进程 [${proc}] 异常退出!`);
                    hasError = true;
                }
            });
        }
    }

    if (conf.containers) {
        const names = conf.containers.split(',').map(c => c.trim()).filter(Boolean);
        if (!env.dockerCli) {
            statusList.push(`⚠️ 本机没有可用的 docker（CLI/daemon），已跳过 ${names.length} 个容器检查`);
            statusList.push("     如需容器监控，请给傻妞容器挂载 /var/run/docker.sock 并安装 docker CLI");
        } else {
            names.forEach(name => {
                if (!safePath(name)) {
                    statusList.push(`⚠️ 容器 [${name}] 名称含非法字符，已跳过`);
                    return;
                }
                try {
                    const isRunning = runCmd(`docker inspect -f '{{.State.Running}}' ${name} 2>/dev/null`, 8000);
                    if (isRunning === 'true') {
                        statusList.push(`✅ 容器 [${name}] 运行中`);
                    } else {
                        statusList.push(`❌ 容器 [${name}] 已停止!`);
                        hasError = true;
                    }
                } catch (e) {
                    statusList.push(`⚠️ 容器 [${name}] 读取失败（无此容器？）`);
                }
            });
        }
    }

    if (!conf.processes && !conf.containers) {
        statusList.push("未配置进程/容器监控（发【监控发现】看可选项）");
    }

    return { statusList: statusList, hasServiceError: hasError };
}

// ---- B1：面板在线探测 ----
async function checkPanels(conf) {
    const lines = [];
    let hasError = false;
    const showAddr = !!(conf && conf.show_panel_address);
    for (const kind of ["qinglong", "smallcat", "daidai"]) {
        let info = null;
        try {
            info = await container.getList(kind);
        } catch (e) {
            lines.push(`⚠️ ${kind} 列表读取失败：${e.message}`);
            continue;
        }
        const panels = (info && Array.isArray(info.list)) ? info.list : [];
        if (!panels.length) continue;

        for (const panel of panels) {
            const label = `${panels.length > 1 ? `#${panel.index} ` : ""}${panel.name || kind}`;
            const tail = (showAddr && panel.address) ? `（${panel.address}）` : "";
            // 青龙做一次真实探测，避免只看缓存状态
            if (kind === "qinglong") {
                try {
                    const ql = new container.QingLong({ id: panel.index });
                    await ql.ready;
                    await withTimeout(ql.request("GET", "/system"), PANEL_PING_TIMEOUT, `青龙 ${label}`);
                    lines.push(`✅ 面板 [${label}] 在线${tail}`);
                } catch (e) {
                    lines.push(`❌ 面板 [${label}] 异常：${e.message}`);
                    hasError = true;
                }
            } else {
                const ok = String(panel.status || '').toLowerCase() === 'online';
                const tag = ok ? "✅" : "⚠️";
                if (!ok) hasError = true;
                lines.push(`${tag} 面板 [${label}] 在线（缓存）${tail}`);
            }
        }
    }
    return { lines: lines, hasPanelError: hasError };
}

// ---- A3：推送目标与送达结果 ----
async function pushChannels() {
    const out = [];
    try {
        const buckets = await new Bucket("sillyGirl").buckets();
        for (const bucket of buckets) {
            try {
                const raw = await new Bucket(bucket).get("masters", "");
                const users = String(raw || "").split(/[&,\s]+/).map(x => x.trim()).filter(Boolean);
                if (users.length) out.push({ platform: bucket, users: users });
            } catch (e) { }
        }
    } catch (e) { }
    return out;
}

async function appendLog(entry) {
    try {
        const bucket = new Bucket(BUCKET_LOG);
        const raw = await bucket.get("list", "[]");
        let list = [];
        try { list = JSON.parse(raw); } catch (e) { list = []; }
        if (!Array.isArray(list)) list = [];
        list.push(entry);
        while (list.length > LOG_KEEP) list.shift();
        await bucket.set("list", JSON.stringify(list));
    } catch (e) { }
}

// 发送告警并记录真实送达结果
async function deliver(content) {
    const entry = { at: shortTime(new Date()), ok: 0, total: 0, targets: [], errors: [], empty: false };
    try {
        const res = await s.pushAdmin(content);
        const arr = Array.isArray(res) ? res : [];
        entry.total = arr.length;
        if (!arr.length) {
            entry.empty = true;
        } else {
            for (const item of arr) {
                const tag = `${item.platform || '?'}:${item.user_id || '?'}`;
                if (item.error) entry.errors.push(`${tag} ${item.error}`);
                else { entry.ok++; entry.targets.push(tag); }
            }
        }
    } catch (e) {
        entry.errors.push(`调用异常：${e.message}`);
    }
    await appendLog(entry);
    return entry;
}

function pushSummary(entry) {
    if (!entry) return "本次运行还没有发过告警";
    if (entry.empty) return `⚠️ ${entry.at} 告警未发出：没有绑定任何管理员（后台「管理员」页面绑定后才会推送）`;
    const parts = [`${entry.at} 送达 ${entry.ok}/${entry.total}`];
    if (entry.targets.length) parts.push(`（${entry.targets.join(", ")}）`);
    if (entry.errors.length) parts.push(`失败 ${entry.errors.length} 个：${entry.errors.slice(0, 3).join(" / ")}`);
    return parts.join(" ");
}

// ---- 防刷冷却管理 ----
async function checkCooldown(currentRisk, cooldownMins) {
    const bucket = new Bucket(BUCKET_STATE);
    const stateStr = await bucket.get("last_alert", "{}");
    let state = {};
    try { state = JSON.parse(stateStr); } catch (e) { }

    const now = Date.now();
    const lastTime = state.time || 0;
    const lastRisk = state.risk || "";

    // 如果风险等级变高（如从 普通 -> 紧急），无视冷却时间，立即报警
    const riskWeights = { "正常": 0, "普通提醒": 1, "高危告警": 2, "紧急告警": 3 };
    if ((riskWeights[currentRisk] || 0) > (riskWeights[lastRisk] || 0)) {
        await bucket.set("last_alert", JSON.stringify({ time: now, risk: currentRisk }));
        return true;
    }

    // 仍在同一等级内，判断是否过冷却期
    if (now - lastTime > cooldownMins * 60 * 1000) {
        await bucket.set("last_alert", JSON.stringify({ time: now, risk: currentRisk }));
        return true;
    }

    return false;
}

async function resetCooldown() {
    const bucket = new Bucket(BUCKET_STATE);
    await bucket.set("last_alert", JSON.stringify({ time: 0, risk: "正常" }));
}

// ---- 采集汇总 ----
async function collect(conf) {
    const mem = checkMemory(conf);
    const cpu = await checkCPU(conf);
    const disk = checkDisk(conf);
    const svc = checkServices(conf);

    let panels = { lines: [], hasPanelError: false };
    if (conf.watch_panels) panels = await checkPanels(conf);

    let risk = "正常";
    if (mem.riskLevel === "紧急告警" || cpu.cpuHigh || svc.hasServiceError || panels.hasPanelError) risk = "紧急告警";
    else if (mem.riskLevel === "高危告警" || disk.diskHigh) risk = "高危告警";
    else if (mem.riskLevel === "普通提醒" || disk.failed) risk = "普通提醒";

    return { mem, cpu, disk, svc, panels, risk };
}

function buildReport(conf, data, lastEntry) {
    const timeStr = formatTime(new Date());
    const lines = [
        "【服务器预警】",
        `服务器：${conf.host_alias}`,
        `时间：${timeStr}`,
        `CPU：${data.cpu.text}`,
        `内存：${data.mem.text}`,
        `磁盘：${data.disk.text}`,
        `状态：\n${data.svc.statusList.join("\n")}`,
    ];
    if (conf.watch_panels && data.panels.lines.length) {
        lines.push(`面板：\n${data.panels.lines.join("\n")}`);
    }
    lines.push(`风险等级：${data.risk}`);
    if (lastEntry !== undefined) lines.push(`上次告警：${pushSummary(lastEntry)}`);
    return lines.join("\n");
}

// ---- 命令实现 ----
async function main() {
    const command = String(await s.getMsg().catch(() => "")).trim();
    const isManual = !!command;
    const conf = await loadConf();
    if (!conf.enable) return;

    if (isManual && !(await s.isAdmin())) {
        await s.reply("仅管理员可用");
        return;
    }

    if (isManual) {
        const cmd = command.trim();
        if (cmd.includes("自检")) return await handleSelfCheck();
        if (cmd.includes("发现")) return await handleDiscover();
        if (cmd.includes("日志")) return await handleLog();
        return await handleStatus();
    }

    // ---- 定时任务：仅在异常时且通过冷却期限制才推送 ----
    const data = await collect(conf);
    if (data.risk !== "正常") {
        const shouldSend = await checkCooldown(data.risk, conf.cooldown_mins);
        if (shouldSend) {
            const report = buildReport(conf, data, undefined);
            await deliver(report);
        }
    } else {
        await resetCooldown();
    }
}

async function handleStatus() {
    const conf = await loadConf();
    const data = await collect(conf);

    // 用告警日志里最后一条作为"上次告警"，比冷却状态更准
    let entry = null;
    try {
        const raw = await new Bucket(BUCKET_LOG).get("list", "[]");
        const list = JSON.parse(raw);
        if (Array.isArray(list) && list.length) entry = list[list.length - 1];
    } catch (e) { }

    const text = buildReport(conf, data, entry);
    const channels = await pushChannels();
    const channelText = channels.length
        ? channels.map(c => conf.show_channel_users ? `${c.platform}(${c.users.length})` : c.platform).join(" · ")
        : "⚠️ 无（告警发不出去，请到后台「管理员」页面绑定）";

    await s.reply(text + `\n推送通道：${channelText}`);
}

async function handleSelfCheck() {
    const conf = await loadConf();
    const env = detectEnv();
    const lines = ["🔎 监控自检", ""];

    lines.push("【运行环境】");
    lines.push(`  主机 ${conf.host_alias} · ${env.inContainer ? "容器内运行" : "宿主机运行"} · hostname ${env.hostname}`);
    lines.push(`  /proc ${fs.existsSync('/proc/stat') ? "✅" : "❌"} · pgrep ${env.hasPgrep ? "✅" : "❌"} · ps ${env.hasPs ? "✅" : "❌"}`);
    lines.push(`  docker 命令 ${env.dockerBin ? "✅" : "❌"} · daemon ${env.dockerCli ? "✅" : "❌"} · docker.sock ${env.dockerSock ? "✅" : "❌"}`);
    if (env.inContainer && (!env.hasPgrep || !env.dockerCli)) {
        lines.push("  ⚠️ 进程/容器监控在本环境不可用（缺 pgrep 或 docker）");
        lines.push("     · 要监控宿主机容器 → 给傻妞挂 /var/run/docker.sock 并装 docker CLI");
        lines.push("     · 要监控宿主机进程 → 容器加 --pid=host 并装 procps");
        lines.push("     · 或者把傻妞改用宿主机二进制 + systemd 运行");
        lines.push("     · 现阶段可用的替代：下面「面板在线」探测");
    }

    const cpu = await checkCPU(conf);
    const mem = checkMemory(conf);
    const disk = checkDisk(conf);

    lines.push("");
    lines.push("【指标采集】");
    lines.push(`  CPU   ${cpu.text} ${cpu.cpuUsage === null ? "⚠️" : "✅"}`);
    lines.push(`  内存  ${mem.text} ✅`);
    lines.push(`  磁盘  ${disk.text} ${disk.failed ? "⚠️" : "✅"}`);

    lines.push("");
    lines.push("【进程 / 容器】");
    if (!conf.processes && !conf.containers) {
        lines.push("  ⚠️ 未配置 —— 发【监控发现】列出可选项后再填进插件配置");
    } else {
        const svc = checkServices(conf);
        for (const l of svc.statusList) lines.push("  " + l);
    }

    lines.push("");
    lines.push("【面板在线】");
    if (!conf.watch_panels) {
        lines.push("  已关闭");
    } else {
        const panels = await checkPanels(conf);
        if (!panels.lines.length) lines.push("  没有配置任何面板");
        else for (const l of panels.lines) lines.push("  " + l);
    }

    lines.push("");
    lines.push("【推送通道】");
    const channels = await pushChannels();
    if (!channels.length) {
        lines.push("  ⚠️ 未绑定任何管理员 —— 定时告警发不出去！");
        lines.push("     请到傻妞后台「管理员」页面绑定一个账号");
    } else {
        for (const c of channels) {
            lines.push(conf.show_channel_users ? `  ✅ ${c.platform} → ${c.users.join(", ")}` : `  ✅ ${c.platform}`);
        }
    }

    lines.push("");
    lines.push("【阈值】");
    lines.push(`  内存 ${conf.mem_normal}/${conf.mem_high}/${conf.mem_critical}% · CPU ${conf.cpu_threshold}% · 磁盘 ${conf.disk_threshold}% · 冷却 ${conf.cooldown_mins} 分钟`);

    await s.reply(lines.join("\n"));
}

async function handleDiscover() {
    const env = detectEnv();
    const lines = ["🔍 监控发现（本机可见的资源）", ""];

    lines.push(`【环境】${env.inContainer ? "容器内运行" : "宿主机运行"} · pgrep ${env.hasPgrep ? "可用" : "不可用"} · docker ${env.dockerCli ? "可用" : "不可用"}`);

    lines.push("");
    lines.push("【正在运行的容器】");
    if (env.dockerCli) {
        try {
            const out = runCmd(`docker ps --format '{{.Names}}|{{.Status}}'`, 10000);
            const rows = out.split('\n').map(x => x.trim()).filter(Boolean);
            if (!rows.length) lines.push("  （没有正在运行的容器）");
            rows.slice(0, 15).forEach(r => {
                const [name, status] = r.split('|');
                lines.push(`  · ${name}  ${status || ""}`);
            });
            if (rows.length > 15) lines.push(`  …… 共 ${rows.length} 个`);
            lines.push("");
            lines.push(`  可复制进「监控容器」：`);
            lines.push(`  ${rows.slice(0, 8).map(r => r.split('|')[0]).join(",")}`);
        } catch (e) {
            lines.push(`  读取失败：${e.message}`);
        }
    } else {
        lines.push("  ❌ 这里读不到 docker（容器内一般没有 docker CLI / socket）");
        lines.push("     → 容器监控不可用，建议改用「监控面板在线状态」");
    }

    lines.push("");
    lines.push("【可见的进程（按实例数排序）】");
    try {
        const out = runCmd(`ps -eo comm= 2>/dev/null | sort | uniq -c | sort -rn | head -15`, 8000);
        const rows = out.split('\n').map(x => x.trim()).filter(Boolean);
        if (!rows.length) lines.push("  （ps 不可用，容器精简镜像里可能没有 procps）");
        rows.forEach(r => {
            const m = r.match(/^(\d+)\s+(.+)$/);
            if (m) lines.push(`  · ${m[2]} × ${m[1]}`);
        });
        if (rows.length) {
            const names = rows.map(r => (r.match(/^(\d+)\s+(.+)$/) || [])[2]).filter(Boolean).slice(0, 8);
            lines.push("");
            lines.push("  可复制进「监控进程」：");
            lines.push(`  ${names.join(",")}`);
        }
    } catch (e) {
        lines.push(`  读取失败：${e.message}`);
    }

    lines.push("");
    lines.push("说明：把上面列出的名字填进插件配置的「监控进程 / 监控容器」，才会真正开始监控。");

    await s.reply(lines.join("\n"));
}

async function handleLog() {
    let list = [];
    try {
        const raw = await new Bucket(BUCKET_LOG).get("list", "[]");
        list = JSON.parse(raw);
        if (!Array.isArray(list)) list = [];
    } catch (e) { }

    if (!list.length) return s.reply("📭 还没有告警记录（说明一直没触发过，或从未发送过）。");

    const lines = [`📜 最近告警记录（共 ${list.length} 条，新→旧）`, ""];
    for (const item of list.slice(-10).reverse()) {
        lines.push(`🕐 ${item.at}`);
        if (item.empty) lines.push("   ⚠️ 未发出：没有绑定管理员");
        else {
            lines.push(`   送达 ${item.ok || 0}/${item.total || 0}${item.targets && item.targets.length ? "（" + item.targets.join(", ") + "）" : ""}`);
            if (item.errors && item.errors.length) {
                item.errors.slice(0, 3).forEach(e => lines.push(`   ❌ ${e}`));
            }
        }
    }
    await s.reply(lines.join("\n"));
}

main().catch(async (error) => {
    console.error("服务器监控预警异常:", error);
    // A3/A4：异常也落桶，便于事后用【监控日志】追查
    await appendLog({ at: shortTime(new Date()), ok: 0, total: 0, targets: [], errors: [`运行异常：${String((error && error.message) || error).slice(0, 200)}`], empty: false });
});
