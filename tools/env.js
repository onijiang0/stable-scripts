/**
 * 青龙 scripts/tools/env.js
 * 供 wxapp/*.js 使用：require("../tools/env.js")
 * 结束时自动调用同目录 sendNotify.js 推送日志
 */
const path = require("path");

class Env {
    constructor(name = "ql-task") {
        this.name = name;
        this.logs = [];
        this.userList = [];
        this.userCount = 0;
    }

    log(msg) {
        const line =
            typeof msg === "string"
                ? msg
                : (() => {
                      try {
                          return JSON.stringify(msg);
                      } catch (e) {
                          return String(msg);
                      }
                  })();
        console.log(line);
        this.logs.push(line);
        return line;
    }

    /** min~max 毫秒随机等待；只传 min 则固定等待 */
    wait(min, max) {
        const a = Number(min) || 0;
        const b = max == null ? a : Number(max) || a;
        const lo = Math.min(a, b);
        const hi = Math.max(a, b);
        const ms = lo + Math.floor(Math.random() * (hi - lo + 1));
        return new Promise((resolve) => setTimeout(resolve, ms));
    }

    /** 从环境变量读多账号（换行或 & 分隔） */
    checkEnv(key) {
        const raw = process.env[key] || "";
        this.userList = String(raw)
            .split(/\n|&/)
            .map((s) => s.trim())
            .filter(Boolean);
        this.userCount = this.userList.length;
        return this.userList;
    }

    async done(title) {
        const notifyOff = /^(0|false|no)$/i.test(process.env.QL_NOTIFY || "");
        if (notifyOff) return;
        const body = this.logs.length ? this.logs.join("\n") : "（无日志）";
        const t = title || this.name;
        try {
            const { sendNotify } = require("./sendNotify.js");
            await sendNotify(t, body);
        } catch (e) {
            console.log(`[notify] 失败: ${e && e.message ? e.message : e}`);
        }
    }
}

module.exports = { Env };
module.exports.default = Env;
