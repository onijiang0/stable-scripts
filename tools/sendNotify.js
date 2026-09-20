/**
 * 青龙 scripts/tools/sendNotify.js
 * 统一推送入口：优先标准 sendNotify，其次 PushPlus HTTP
 * 环境变量：PUSHPLUS_TOKEN / PUSH_PLUS_TOKEN / PUSHPLUS_KEY 任一
 *           可选 PUSHPLUS_TOPIC
 */
const fs = require("fs");
const path = require("path");
const https = require("https");
const http = require("http");

function pickToken() {
    return (
        process.env.PUSHPLUS_TOKEN ||
        process.env.PUSH_PLUS_TOKEN ||
        process.env.PUSHPLUS_KEY ||
        ""
    ).trim();
}

function pushplusSend(title, content) {
    return new Promise((resolve, reject) => {
        const token = pickToken();
        if (!token) {
            return reject(new Error("未配置 PUSHPLUS_TOKEN / PUSH_PLUS_TOKEN / PUSHPLUS_KEY"));
        }
        const payload = JSON.stringify({
            token,
            title: String(title || "青龙通知"),
            content: String(content || ""),
            topic: process.env.PUSHPLUS_TOPIC || "",
            template: "txt",
        });
        const u = new URL("https://www.pushplus.plus/send");
        const req = https.request(
            {
                hostname: u.hostname,
                path: u.pathname,
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "Content-Length": Buffer.byteLength(payload),
                },
                timeout: 15000,
            },
            (res) => {
                let body = "";
                res.on("data", (c) => (body += c));
                res.on("end", () => {
                    try {
                        const j = JSON.parse(body);
                        if (j.code === 200 || j.status === 200) {
                            console.log("[notify] PushPlus 发送成功");
                            return resolve(j);
                        }
                        reject(new Error(body.slice(0, 200)));
                    } catch (e) {
                        reject(new Error(body.slice(0, 200)));
                    }
                });
            }
        );
        req.on("error", reject);
        req.on("timeout", () => {
            req.destroy(new Error("PushPlus timeout"));
        });
        req.write(payload);
        req.end();
    });
}

/** 尝试加载青龙自带/其它目录的 sendNotify */
function loadLegacySendNotify() {
    const candidates = [
        path.join(__dirname, "..", "sendNotify.js"),
        "/ql/data/scripts/sendNotify.js",
        "/ql/data/scripts/smallfawn_QLScriptPublic_main/sendNotify.js",
        "/ql/data/scripts/本地/sendNotify.js",
    ];
    for (const p of candidates) {
        try {
            if (!fs.existsSync(p)) continue;
            const mod = require(p);
            const fn =
                (mod && (mod.sendNotify || mod.send || mod.default)) ||
                (typeof mod === "function" ? mod : null);
            if (typeof fn === "function") return { file: p, fn };
        } catch (e) {
            /* ignore */
        }
    }
    return null;
}

async function sendNotify(title, content) {
    const t = title || "青龙通知";
    const c = content || "";
    const legacy = loadLegacySendNotify();
    if (legacy) {
        try {
            await legacy.fn(t, c);
            console.log(`[notify] 已调用 ${legacy.file}`);
            return true;
        } catch (e) {
            console.log(`[notify] 标准 sendNotify 失败: ${e && e.message ? e.message : e}`);
        }
    }
    try {
        await pushplusSend(t, c);
        return true;
    } catch (e) {
        console.log(`[notify] PushPlus 失败: ${e && e.message ? e.message : e}`);
        return false;
    }
}

module.exports = { sendNotify, pushplusSend };
module.exports.default = sendNotify;
