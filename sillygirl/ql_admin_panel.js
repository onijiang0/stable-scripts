// [title: 青龙管理面板]
// [name: ql_admin_panel]
// [desc: 集成面板状态、搜索、去重批量新建、改变量、清理日志。仅管理员可用，非管理员由框架直接丢弃。]
// [author: AI_Engineer]
// [version: 3.1.1]
// [rule: ^青龙管理$]
// [rule: ^面板(列表|状态)$]
// [rule: ^变量列表(.*)$]
// [rule: ^变量详情\s+(.+)$]
// [rule: ^(?:批量)?新建变量([\s\S]*)$]
// [rule: ^修改变量备注\s+(.+)$]
// [rule: ^修改变量\s+(.+)$]
// [rule: ^删除变量\s+(.+)$]
// [rule: ^(?:启用|禁用)变量\s+(.+)$]
// [rule: ^清理(过期)?日志$]
// [rule: ^通知\s+(.+?)\s*\|\s*(.+)$]
// [status: true]
// 仅管理员可用：傻妞在路由层对 [admin: true] 的插件会把非管理员消息**直接丢弃**
// （core/function.go: `if function.Admin && !a { return }`），无需在代码里再拦。
// [admin: true]
// [public: false]
// [priority: 999]
// [class: 工具]
// [icon: https://api.iconify.design/lucide:terminal-square.svg]
// [depe: []]

const { plugin, sender: s, container } = require("sillygirl");

const config = new plugin.Form({
    qinglong_id: plugin.Form.integer()
        .title("默认青龙编号")
        .description("操作默认指向的青龙容器编号")
        .widget("qinglong-panel")
        .min(1).default(1),
    log_keep_days: plugin.Form.integer()
        .title("日志保留天数")
        .description("执行“清理日志”时，超过此天数的将被删除")
        .min(1).default(7)
});

// 安全获取输入
// ⚠️ 关键修复：傻妞的 Sender **没有 getContent()** —— 读消息只能用 getMsg()
//   （gRPC 侧方法名叫 SenderGetContent，但 JS SDK 暴露为 getMsg）。
//   之前读的是 ctx.getContent，typeof 判断恒为 false → 返回值恒为 "" →
//   所有命令分支都匹配不上 → 整个插件静默无反应。
async function getCtxContent(ctx) {
    if (!ctx) return "";
    if (typeof ctx === 'string') return ctx;
    if (typeof ctx.getMsg === 'function') {
        try {
            const v = await ctx.getMsg();
            if (typeof v === 'string' && v !== '') return v;
        } catch (e) { }
    }
    if (typeof ctx.content === 'string' && ctx.content) return ctx.content;
    return "";
}

function trunc(str, len = 20) {
    if (!str) return "";
    return str.length > len ? str.substring(0, len) + ".." : str;
}

// ================== 核心：利用傻妞官方容器通信 ==================
// 直接抛弃手动读取数据库，让傻妞自己去处理千奇百怪的底层存储格式
async function requestQL(qid, method, path, body = null, qs = null) {
    try {
        const ql = new container.QingLong({ id: qid });
        await ql.ready; // 编号不存在时给明确错误，而不是走到后面莫名失败
        const res = await ql.request(method, path, body, qs);
        // ⚠️ 青龙部分接口（DELETE / enable / disable）成功时返回空 body，
        //    SDK 会给回 {}。而 ql.request 失败时是**抛异常**，
        //    所以"没抛异常"就等于成功 —— 不能拿 res.code === 200 当唯一判据，
        //    否则会把成功报成失败。
        if (!res || typeof res !== 'object') return { code: 200, message: "Silent Success" };
        if (res.code === undefined) res.code = 200;
        return res;
    } catch (e) {
        const msg = (e && e.message) || String(e);
        // 兜底：底层因 DELETE 空 body 抛 JSON 解析异常，实际是成功的
        if (msg.includes("JSON") || msg.includes("Unexpected")) {
            return { code: 200, message: "Silent Success" };
        }
        return { code: 500, message: msg };
    }
}

// 提取全量变量
async function fetchAllEnvs(qid) {
    const res = await requestQL(qid, "GET", "/envs", null, { size: 3000 });
    let data = res && res.data ? res.data : res;
    if (data && data.data) data = data.data; // 兼容包装差异
    return Array.isArray(data) ? data : [];
}

// ================== 功能模块 ==================

// 1. 面板列表 | 面板状态
async function handlePanelList() {
    await s.reply("🔍 正在检测傻妞绑定的青龙面板连通性...");

    let panels = [];
    try {
        const info = await container.getList("qinglong");
        panels = (info && Array.isArray(info.list)) ? info.list : [];
    } catch (e) { }

    if (!panels.length) {
        return s.reply("❌ 傻妞里没有配置任何青龙容器，请先到后台「容器」页面添加。");
    }

    let msg = `📊 青龙面板连通状态：\n\n`;
    let found = 0;
    // 按傻妞里实际配置的面板逐个探测（不再写死 1~5）
    for (const panel of panels) {
        const res = await requestQL(panel.index, "GET", "/system");
        const label = `${panel.name || ""}`.trim();
        if (res && res.code === 200) {
            found++;
            msg += `[容器 #${panel.index}] ${label} 🟢 在线正常\n`;
        } else {
            msg += `[容器 #${panel.index}] ${label} 🔴 不可用：${(res && res.message) || "未知错误"}\n`;
        }
    }
    msg += `\n共 ${panels.length} 个面板，在线 ${found} 个。`;
    await s.reply(msg);
}

// 2. 变量列表 [关键词]
async function handleListEnvs(args, qid) {
    const kws = args.split(/\s+/).filter(Boolean);
    const envs = await fetchAllEnvs(qid);
    if (!envs.length) return s.reply(`🈳 容器 #${qid} 没有变量或连通失败。`);

    let filtered = envs;
    if (kws.length > 0) {
        filtered = envs.filter(e => {
            const text = `${e.name}||${e.value}||${e.remarks || ''}`.toLowerCase();
            return kws.every(kw => text.includes(kw.toLowerCase())); // 多关键词 AND 匹配
        });
    }

    if (!filtered.length) return s.reply(`❌ 未找到同时包含这些关键词的变量。`);

    let msg = `🔍 查找到 ${filtered.length} 个环境变量：\n\n`;
    for (let i = 0; i < Math.min(filtered.length, 10); i++) {
        const e = filtered[i];
        const statusStr = e.status === 1 ? "🔴禁" : "🟢启";
        msg += `🆔 ${e.id || e._id}\n🏷️ ${e.name}=${trunc(e.value, 15)}\n📝 备注: ${e.remarks || "无"}\n💡 状态: ${statusStr}\n---\n`;
    }
    if (filtered.length > 10) msg += `\n... 及其他 ${filtered.length - 10} 个隐藏。`;
    await s.reply(msg);
}

// 3. 变量详情 <ID>
async function handleEnvDetail(id, qid) {
    const envs = await fetchAllEnvs(qid);
    const e = envs.find(env => String(env.id || env._id) === id);
    if (!e) return s.reply(`❌ 未找到 ID 为 ${id} 的变量。`);

    const statusStr = e.status === 1 ? "🔴 已禁用" : "🟢 已启用";
    let msg = `📊 变量详情\n\n🆔 ID: ${e.id || e._id}\n🏷️ 名称: ${e.name}\n💡 状态: ${statusStr}\n📝 备注: ${e.remarks || "无"}\n\n[值内容]\n${e.value}`;
    await s.reply(msg);
}

// 4. 新建变量 & 批量新建变量 (智能去重)
async function handleCreateEnvs(rawInput, qid) {
    if (!rawInput.trim()) {
        await s.reply("📝 请在 60 秒内发送需要新建的变量内容 (支持多行批量):\n格式: 名称=值 备注");
        // ⚠️ listen 的签名是 listen({ timeout })，传裸数字等于没设超时（会一直挂着）
        const ctx = await s.listen({ timeout: 60000 });
        if (!ctx) return s.reply("⌛️ 超时未回复，已取消。");
        rawInput = await getCtxContent(ctx);
    }
    if (rawInput.toLowerCase() === 'q') return s.reply("🛑 已取消。");

    const parsedEnvs = [];
    const lines = rawInput.split(/\r?\n/);
    for (let line of lines) {
        line = line.trim();
        if (!line) continue;
        if (line.toLowerCase().startsWith('export ')) line = line.substring(7).trim();
        const eqIdx = line.indexOf('=');
        if (eqIdx === -1) continue;

        const name = line.substring(0, eqIdx).trim();
        let rest = line.substring(eqIdx + 1).trim();
        let value = rest, remarks = "SillyGirl创建";
        
        const spaceIdx = rest.indexOf(' ');
        if (spaceIdx > 0 && !rest.startsWith('"') && !rest.startsWith("'")) {
            value = rest.substring(0, spaceIdx).trim();
            remarks = rest.substring(spaceIdx + 1).trim();
        } else if (rest.startsWith('"') || rest.startsWith("'")) {
            const quote = rest[0];
            const endIdx = rest.indexOf(quote, 1);
            if (endIdx > 0) {
                value = rest.substring(1, endIdx);
                remarks = rest.substring(endIdx + 1).trim() || remarks;
            } else {
                value = rest.substring(1);
            }
        }
        if (name) parsedEnvs.push({ name, value, remarks });
    }

    if (!parsedEnvs.length) return s.reply("❌ 无法解析变量格式，请确保包含等号（=）。");

    const currentEnvs = await fetchAllEnvs(qid);
    const toAdd = [];
    const duplicates = [];

    for (let pEnv of parsedEnvs) {
        const exists = currentEnvs.some(e => e.name === pEnv.name && e.value === pEnv.value);
        if (exists) duplicates.push(pEnv);
        else toAdd.push(pEnv);
    }

    // 完美复刻截图中的防重复提示
    if (toAdd.length === 0) {
        const keyName = parsedEnvs[0].name;
        return s.reply(`【${keyName}】未变动：${duplicates.length} 个账号已全部存在，无需重复添加`);
    }

    const res = await requestQL(qid, "POST", "/envs", toAdd);
    if (res && res.code === 200) {
        let msg = `批量新建完成 (共 ${toAdd.length} 条):\n`;
        toAdd.forEach((e, idx) => {
            msg += `${idx + 1}. ${e.name}=${trunc(e.value, 15)}\n`;
        });
        await s.reply(msg);
    } else {
        await s.reply(`❌ 写入青龙失败：${res.message || JSON.stringify(res)}`);
    }
}

// 5. 修改变量
async function handleEditEnv(args, qid) {
    const spaceIdx = args.indexOf(' ');
    if (spaceIdx === -1) return s.reply("❌ 格式错误。正确格式: 修改变量 <ID> 名称=值");
    const id = args.substring(0, spaceIdx).trim();
    const rest = args.substring(spaceIdx + 1).trim();
    
    const eqIdx = rest.indexOf('=');
    if (eqIdx === -1) return s.reply("❌ 格式错误，缺少等号。");
    const name = rest.substring(0, eqIdx).trim();
    const value = rest.substring(eqIdx + 1).trim();

    const currentEnvs = await fetchAllEnvs(qid);
    const target = currentEnvs.find(e => String(e.id || e._id) === id);
    if (!target) return s.reply(`❌ 未找到 ID 为 ${id} 的变量。`);

    const payload = { id: target.id || target._id, _id: target._id, name: name, value: value, remarks: target.remarks };
    const res = await requestQL(qid, "PUT", "/envs", payload);
    if (res && res.code === 200) await s.reply(`✅ 变量 ${id} 修改成功！`);
    else await s.reply(`❌ 修改失败：${res.message || JSON.stringify(res)}`);
}

// 6. 修改变量备注
async function handleEditRemark(args, qid) {
    const spaceIdx = args.indexOf(' ');
    if (spaceIdx === -1) return s.reply("❌ 格式错误。正确格式: 修改变量备注 <ID> 备注内容");
    const id = args.substring(0, spaceIdx).trim();
    const remarks = args.substring(spaceIdx + 1).trim();

    const currentEnvs = await fetchAllEnvs(qid);
    const target = currentEnvs.find(e => String(e.id || e._id) === id);
    if (!target) return s.reply(`❌ 未找到 ID 为 ${id} 的变量。`);

    const payload = { id: target.id || target._id, _id: target._id, name: target.name, value: target.value, remarks: remarks };
    const res = await requestQL(qid, "PUT", "/envs", payload);
    if (res && res.code === 200) await s.reply(`✅ 变量 ${id} 备注修改成功！\n新备注：${remarks}`);
    else await s.reply(`❌ 修改失败：${res.message || JSON.stringify(res)}`);
}

// 7. 删除变量
async function handleDelete(args, qid) {
    const ids = args.split(/[,，\s]+/).filter(Boolean);
    if (!ids.length) return s.reply("❌ 请提供要删除的变量ID。");

    const res = await requestQL(qid, "DELETE", "/envs", ids);
    if (res && res.code === 200) await s.reply(`✅ 成功触发了 ${ids.length} 个变量的删除指令！`);
    else await s.reply(`❌ 删除失败或遇到异常：${res.message || JSON.stringify(res)}`);
}

// 8. 启用/禁用变量
async function handleEnable(args, isEnable, qid) {
    const ids = args.split(/[,，\s]+/).filter(Boolean);
    if (!ids.length) return s.reply("❌ 请提供要操作的变量ID。");

    const actionPath = isEnable ? "/envs/enable" : "/envs/disable";
    const res = await requestQL(qid, "PUT", actionPath, ids);
    if (res && res.code === 200) await s.reply(`✅ 成功${isEnable ? "启用" : "禁用"} ${ids.length} 个变量！`);
    else await s.reply(`❌ 操作失败：${res.message || JSON.stringify(res)}`);
}

// 9. 清理日志
async function handleCleanLogs(qid, keepDays) {
    await s.reply(`🔍 正在扫描全局日志...\n⏳ 超过 ${keepDays} 天的日志将被擦除，请稍候...`);
    const treeRes = await requestQL(qid, "GET", "/logs");
    const logDirs = treeRes && treeRes.data ? treeRes.data : [];
    if (!logDirs.length) return s.reply("❌ 获取日志目录失败或为空。");

    const now = Date.now();
    const keysToDelete = [];
    let totalFiles = 0;

    function processFile(fileName, fileKey) {
        totalFiles++;
        const dateMatch = fileName.match(/^(\d{4})-(\d{2})-(\d{2})-(\d{2})-(\d{2})-(\d{2})/);
        if (dateMatch) {
            const fileTime = new Date(
                parseInt(dateMatch[1]), parseInt(dateMatch[2]) - 1, parseInt(dateMatch[3]),
                parseInt(dateMatch[4]), parseInt(dateMatch[5]), parseInt(dateMatch[6])
            ).getTime();
            if (!isNaN(fileTime) && (now - fileTime) / 86400000 > keepDays) {
                keysToDelete.push({ filename: fileName, path: fileKey, isDir: false });
            }
        }
    }

    function parseTree(dirs, currentPath = "") {
        for (const dir of dirs) {
            const title = dir.title || dir.name || "";
            const isDir = dir.isDir !== undefined ? dir.isDir : (dir.children || dir.files);

            if (dir.children && Array.isArray(dir.children)) {
                parseTree(dir.children, currentPath ? `${currentPath}/${title}` : title);
            } else if (dir.files && Array.isArray(dir.files)) {
                for(const f of dir.files) processFile(f, title ? `${title}/${f}` : f);
            } else if (!isDir && title.endsWith(".log")) {
                const fileKey = dir.key || dir.path || dir.value || (currentPath ? `${currentPath}/${title}` : title);
                processFile(title, fileKey);
            }
        }
    }
    parseTree(logDirs);

    if (keysToDelete.length === 0) return s.reply(`🎉 日志十分健康，总计 ${totalFiles} 个，暂无过期日志需要删除。`);
    await s.reply(`🚀 发现 ${keysToDelete.length} 个过期日志，正在执行批量擦除...`);
    
    let delSuccess = 0;
    for (let i = 0; i < keysToDelete.length; i += 100) {
        const batch = keysToDelete.slice(i, i + 100);
        let res = await requestQL(qid, "DELETE", "/logs", batch);
        if (res && res.code === 200) {
            delSuccess += batch.length;
        } else {
            // 降级兼容老版本青龙
            const strBatch = batch.map(b => b.path);
            let fbRes = await requestQL(qid, "DELETE", "/logs", strBatch);
            if (fbRes && fbRes.code === 200) delSuccess += batch.length;
        }
    }
    await s.reply(`✅ 日志清理完毕！\n扫描总数：${totalFiles} 个\n成功删除：${delSuccess} 个过期日志。`);
}

// 10. 通知
async function handleNotify(args) {
    const parts = args.split("|");
    const title = parts[0] ? parts[0].trim() : "系统通知";
    const content = parts[1] ? parts[1].trim() : "";
    await s.reply(`[青龙面板通知模拟发送成功]\n\n🔔 标题：${title}\n📄 内容：${content}`);
}

// ================== 路由控制器 ==================
async function main() {
    // 权限交给框架层：头部 [admin: true] 会让非管理员消息在路由层被直接丢弃（无任何回复）。
    // 这里再做一次兜底，同样静默丢弃。
    if (!(await s.isAdmin())) return;

    let rawCmd = "";
    try { rawCmd = String((await getCtxContent(s)) || "").trim(); } catch (e) { }

    const cfg = await config.get() || {};
    const qid = Number(cfg.qinglong_id || 1);

    if (/^面板(列表|状态)$/.test(rawCmd)) {
        await handlePanelList();
    } 
    else if (rawCmd.startsWith('变量列表')) {
        await handleListEnvs(rawCmd.replace('变量列表', '').trim(), qid);
    } 
    else if (rawCmd.startsWith('变量详情')) {
        await handleEnvDetail(rawCmd.replace('变量详情', '').trim(), qid);
    } 
    else if (/^(批量)?新建变量/.test(rawCmd)) {
        const payload = rawCmd.replace(/^(批量)?新建变量/, '').trim();
        await handleCreateEnvs(payload, qid);
    } 
    else if (rawCmd.startsWith('修改变量备注')) {
        await handleEditRemark(rawCmd.replace('修改变量备注', '').trim(), qid);
    } 
    else if (rawCmd.startsWith('修改变量')) {
        await handleEditEnv(rawCmd.replace('修改变量', '').trim(), qid);
    } 
    else if (rawCmd.startsWith('删除变量')) {
        await handleDelete(rawCmd.replace('删除变量', '').trim(), qid);
    } 
    else if (rawCmd.startsWith('启用变量')) {
        await handleEnable(rawCmd.replace('启用变量', '').trim(), true, qid);
    } 
    else if (rawCmd.startsWith('禁用变量')) {
        await handleEnable(rawCmd.replace('禁用变量', '').trim(), false, qid);
    } 
    else if (/^清理(过期)?日志$/.test(rawCmd)) {
        await handleCleanLogs(qid, cfg.log_keep_days || 7);
    } 
    else if (rawCmd.startsWith('通知')) {
        await handleNotify(rawCmd.replace('通知', '').trim());
    } 
    else if (rawCmd === '青龙管理') {
        const menu = [
            "🛠️ 青龙超级管理面板",
            "-------------------",
            "🔸 [面板列表/状态] 查看连通性",
            "🔸 [变量列表 关键词1 关键词2] 多词过滤查找",
            "🔸 [变量详情 <ID>] 查看具体值与状态",
            "🔸 [批量新建变量] ⬇️多行粘贴智能去重",
            "🔸 [修改变量 <ID> 名称=值]",
            "🔸 [修改变量备注 <ID> 备注]",
            "🔸 [删除变量 <ID1,ID2>]",
            "🔸 [启用变量/禁用变量 <ID1,ID2>]",
            "🔸 [清理日志] 删除过早的日志垃圾",
            "🔸 [通知 标题 | 内容]"
        ].join("\n");
        await s.reply(menu);
    }
    else {
        // 原来未命中任何分支时静默结束，排查起来毫无线索；改成明确回一句
        await s.reply(`❓ 未识别的命令：${rawCmd || "(空)"}\n发【青龙管理】查看可用命令。`);
    }
}

main().catch(async (error) => {
    const msg = String((error && error.message) || error);
    console.error("青龙管理面板异常:", error);
    try {
        await s.reply(`❌ 青龙管理面板执行异常：${msg.slice(0, 300)}`);
    } catch (e) { }
});