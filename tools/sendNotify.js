/**
 * 青龙 scripts/tools/sendNotify.js
 * 统一推送入口：优先标准 sendNotify，其次 PushPlus HTTP
 * 控制台只打印简短结果（如「企业微信推送成功」），不打印完整 Response
 */
const fs = require("fs");
const path = require("path");
const https = require("https");

function pickToken() {
    return (
        process.env.PUSHPLUS_TOKEN ||
        process.env.PUSH_PLUS_TOKEN ||
        process.env.PUSHPLUS_KEY ||
        ""
    ).trim();
}

function isNoisy(line) {
    const s = String(line || "");
    if (!s.trim()) return false;
    if (/^\s*[\{\[]/.test(s)) return true;
    if (/"result-status"|"month"\s*:|"encryptData"|"sessionKey"|"accessToken"/i.test(s)) return true;
    if (/Response\s*[:：]/i.test(s) && s.length > 80) return true;
    if (s.length > 200 && /ok\s*[:：]/i.test(s)) return true;
    return s.length > 300;
}

function summarizePush(text, fallbackOk) {
    const blob = String(text || "");
    const low = blob.toLowerCase();
    if (!fallbackOk && !/ok\s*[:：]\s*true|成功|sendNotify\.js ok/i.test(blob)) {
        return "推送失败";
    }
    const pairs = [
        [["企业微信", "qywx", "wework", "wechat work"], "企业微信推送成功"],
        [["钉钉", "dingtalk"], "钉钉推送成功"],
        [["telegram"], "Telegram推送成功"],
        [["pushplus", "push+"], "PushPlus推送成功"],
        [["bark"], "Bark推送成功"],
        [["server酱", "serverchan", "sctapi"], "Server酱推送成功"],
        [["gotify"], "Gotify推送成功"],
        [["wxpusher"], "WxPusher推送成功"],
    ];
    for (const [keys, label] of pairs) {
        for (const k of keys) {
            if (low.includes(String(k).toLowerCase())) return label;
        }
    }
    return fallbackOk || /ok\s*[:：]\s*true|成功/i.test(blob) ? "推送成功" : "推送失败";
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
                            return resolve({ ok: true, channel: "PushPlus", raw: body.slice(0, 120) });
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

function withQuietConsole(fn) {
    const origLog = console.log;
    const origInfo = console.info;
    const origWarn = console.warn;
    const captured = [];
    const wrap =
        (orig) =>
        (...args) => {
            const line = args
                .map((a) => {
                    if (typeof a === "string") return a;
                    try {
                        return JSON.stringify(a);
                    } catch (e) {
                        return String(a);
                    }
                })
                .join(" ");
            if (isNoisy(line) || /Response|sendNotify|通知方式|推送方式/i.test(line)) {
                captured.push(line);
                return;
            }
            // 仍放行短的成功/失败提示，但去掉 Response 长文
            if (/成功|失败|error|ok/i.test(line) && line.length < 120) {
                captured.push(line);
                return;
            }
            captured.push(line);
        };
    console.log = wrap(origLog);
    console.info = wrap(origInfo);
    console.warn = wrap(origWarn);
    const restore = () => {
        console.log = origLog;
        console.info = origInfo;
        console.warn = origWarn;
    };
    return Promise.resolve()
        .then(fn)
        .then((v) => {
            restore();
            return { value: v, captured };
        })
        .catch((e) => {
            restore();
            throw Object.assign(e || new Error("notify fail"), { __captured: captured });
        });
}

async function sendNotify(title, content) {
    const t = title || "青龙通知";
    const c = String(content || "");
    if ((process.env.QL_NOTIFY || "").trim().toLowerCase() === "0") {
        console.log("🔔 推送结果：已关闭推送（QL_NOTIFY=0）");
        return true;
    }
    // 净化推送正文里的截断 JSON
    const safeContent = c
        .split(/\n/)
        .filter((line) => !isNoisy(line))
        .join("\n");

    const legacy = loadLegacySendNotify();
    let logs = [];
    if (legacy) {
        try {
            const r = await withQuietConsole(() => legacy.fn(t, safeContent));
            logs = r.captured || [];
            const summary = summarizePush(logs.join("\n"), true);
            console.log(`🔔 推送结果：${summary}`);
            return true;
        } catch (e) {
            logs = (e && e.__captured) || [];
            console.log(`🔔 推送结果：标准 sendNotify 失败，尝试 PushPlus`);
        }
    }
    try {
        await pushplusSend(t, safeContent);
        console.log("🔔 推送结果：PushPlus推送成功");
        return true;
    } catch (e) {
        const msg = e && e.message ? String(e.message).slice(0, 80) : "未知错误";
        console.log(`🔔 推送结果：推送失败（${msg}）`);
        return false;
    }
}

module.exports = { sendNotify, pushplusSend };
module.exports.default = sendNotify;
