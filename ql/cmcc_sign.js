/*
------------------------------------------
@Description: 中国移动10086 - 签到领流量 (qwhdmark)
cron: 30 8 * * *
------------------------------------------
变量名：cmcc
变量值：完整 Cookie（含 QWHD_SESSION_TOKEN / d.sid 等）
示例：qwhd_center_router=hua; QWHD_SESSION_TOKEN=xxx; d.sid=yyy; ...

依赖变量：
cmcc_activity   可选，默认 1021122301（签到活动 ID）
------------------------------------------
已实现：
1. 用户信息 POST /qwhdhub/api/mark/user/info
2. 签到配置 POST /qwhdhub/api/mark/info/commonInfo
3. 签到状态 POST /qwhdhub/api/mark/info/prizeInfo
4. 签到动作 POST /qwhdhub/api/mark/do   ← 需实测确认
5. 任务列表 POST /qwhdhub/api/mark/task/taskList

契约（wx.10086.cn qwhdhub）：
BASE     https://wx.10086.cn
活动页   GET  /qwhdhub/qwhdmark/{activityId}?wmhToken=...
API      POST /qwhdhub/api/mark/...
鉴权     Cookie: QWHD_SESSION_TOKEN + d.sid + ...
         头: login-check:1, x-requested-with:XMLHttpRequest
         Origin/Referer 指向活动页
响应     {code,status,msg,data,success}；success=true 成功
         code=GOTO_LOGIN 表示 Cookie 过期
------------------------------------------
踩坑：
1. QWHD_SESSION_TOKEN 约 30 分钟过期，需定期重新抓包
2. 签到动作接口名待实测确认（当前用 /mark/do）
3. 所有 API 都要 POST + JSON body（可为空 {}）
4. Referer 必须带活动页完整 URL
------------------------------------------
*/

const { Env } = require("../tools/env.js");
const axios = require("axios");

const $ = new Env("CMCC");

const CK_NAME = "cmcc";
const BASE = "https://wx.10086.cn";
const ACTIVITY_ID = process.env.cmcc_activity || "1021122301";
const UA =
    "Mozilla/5.0 (Linux; Android 17; 2509FPN0BC Build/CP2A.260605.016; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/150.0.7871.189 Mobile Safari/537.36 XWEB/1500117 MMWEBSDK/20260502 MMWEBID/9885 MicroMessenger/8.0.76.3141(0x28004C31) WeChat/arm64 Weixin NetType/WIFI Language/zh_CN ABI/arm64 miniProgram/wx43aab19a93a3a6f2";

function short(v, max = 300) {
    if (v === undefined || v === null) return "";
    const t = typeof v === "string" ? v : JSON.stringify(v);
    return t.length > max ? `${t.slice(0, max)}...` : t;
}

function parseCookie(raw) {
    return String(raw || "").trim();
}

async function request(options) {
    const res = await axios.request({
        timeout: 25000,
        validateStatus: () => true,
        ...options,
        headers: {
            "User-Agent": UA,
            Accept: "application/json, text/plain, */*",
            ...(options.headers || {}),
        },
    });
    return { status: res.status, data: res.data };
}

class CmccTask {
    constructor(rawCookie, index) {
        this.index = index;
        this.cookie = parseCookie(rawCookie);
        this.referer = `${BASE}/qwhdhub/qwhdmark/${ACTIVITY_ID}`;
        this.signed = false;
        this.signDays = 0;
    }

    log(msg) {
        $.log(`账号[${this.index}] ${msg}`);
    }

    headers() {
        return {
            "Content-Type": "application/json;charset=UTF-8",
            "login-check": "1",
            "x-requested-with": "XMLHttpRequest",
            Origin: BASE,
            Referer: this.referer,
            Cookie: this.cookie,
        };
    }

    async api(path, body = {}) {
        const { status, data } = await request({
            method: "POST",
            url: `${BASE}${path}`,
            headers: this.headers(),
            data: body,
        });
        if (status !== 200) throw new Error(`${path} HTTP ${status}`);
        if (data?.code === "GOTO_LOGIN" || data?.status === "GOTO_LOGIN") {
            throw new Error("Cookie 过期，请重新抓包更新 cmcc 变量");
        }
        if (data && data.success === false) {
            throw new Error(`${path} 失败: ${data.msg || short(data)}`);
        }
        return data;
    }

    async userInfo() {
        const d = await this.api("/qwhdhub/api/mark/user/info", { appVersion: "", miniVersion: "" });
        const info = d?.data || {};
        const phone = info.mobile || info.phone || info.phoneNum || "";
        const masked = phone ? phone.slice(0, 3) + "****" + phone.slice(-4) : "未知";
        this.log(`用户: ${info.nickname || info.userName || "未知"} 手机: ${masked}`);
        return info;
    }

    async commonInfo() {
        const d = await this.api("/qwhdhub/api/mark/info/commonInfo", {});
        this.log(`签到配置: ${short(d?.data, 200)}`);
        return d?.data || {};
    }

    async prizeInfo() {
        const d = await this.api("/qwhdhub/api/mark/info/prizeInfo", {});
        const info = d?.data || {};
        // 常见字段：isSign/signStatus/signDays/todaySign
        const signed =
            info.isSign === true ||
            info.signStatus === 1 ||
            info.todaySign === true ||
            info.todaySigned === true;
        const days = info.signDays ?? info.continuousDays ?? info.day ?? "-";
        this.signed = !!signed;
        this.signDays = days;
        this.log(`签到状态: ${signed ? "今日已签" : "今日未签"} 连签/天数: ${days}`);
        if (info.prizeList || info.prizes) {
            this.log(`奖品: ${short(info.prizeList || info.prizes, 150)}`);
        }
        return info;
    }

    /** 签到动作：/mark/do（需实测确认路径） */
    async doSign() {
        const candidates = [
            "/qwhdhub/api/mark/do",
            "/qwhdhub/api/mark/sign",
            "/qwhdhub/api/mark/doSign",
            "/qwhdhub/api/mark/info/doSign",
        ];
        for (const path of candidates) {
            try {
                const d = await this.api(path, {});
                if (d && (d.success === true || d.code === "SUCCESS" || d.code === 0)) {
                    this.log(`签到成功 via ${path}: ${short(d.data, 150)}`);
                    return true;
                }
                // 接口存在但业务失败（如已签）
                if (d && d.code !== "GOTO_LOGIN") {
                    this.log(`签到尝试 ${path}: ${d.code || ""} ${d.msg || short(d.data, 80)}`);
                    if (String(d.msg || "").includes("已签") || String(d.msg || "").includes("重复")) {
                        return true;
                    }
                }
            } catch (e) {
                const msg = String(e.message || e);
                if (msg.includes("过期")) throw e;
                this.log(`签到尝试 ${path}: ${msg}`);
            }
        }
        this.log("签到动作: 所有候选路径均未确认成功，请抓包确认真实接口");
        return false;
    }

    async taskList() {
        try {
            const d = await this.api("/qwhdhub/api/mark/task/taskList", {});
            const list = d?.data?.taskList || d?.data?.list || d?.data || [];
            const tasks = Array.isArray(list) ? list : [];
            this.log(`任务列表: ${tasks.length} 条`);
            for (const t of tasks.slice(0, 10)) {
                this.log(
                    `  [${t.taskId || t.id}] ${t.taskName || t.name || "-"} ` +
                        `status=${t.status ?? t.taskStatus ?? "?"} ${t.taskDesc || t.desc || ""}`
                );
            }
            return tasks;
        } catch (e) {
            this.log(`任务列表失败: ${e.message || e}`);
            return [];
        }
    }

    async run() {
        try {
            this.log(`开始执行 中国移动10086 签到 (activity=${ACTIVITY_ID})`);
            if (!this.cookie) {
                this.log("缺少 cmcc Cookie 变量");
                return;
            }
            await this.userInfo();
            await this.commonInfo();
            await this.prizeInfo();
            if (!this.signed) {
                await this.doSign();
                await this.prizeInfo();
            } else {
                this.log("签到结果: 今日已签到，跳过");
            }
            await this.taskList();
        } catch (e) {
            this.log(`执行失败: ${e.message || e}`);
        }
    }
}

async function main() {
    $.checkEnv(CK_NAME);
    if (!$.userCount) {
        $.log(`未找到变量 ${CK_NAME}`);
        return;
    }
    for (let i = 0; i < $.userList.length; i++) {
        const task = new CmccTask($.userList[i], i + 1);
        await task.run();
        if (i < $.userList.length - 1) await $.wait(1500, 3000);
    }
}

main()
    .catch((e) => $.log(`脚本异常: ${e.message || e}`))
    .finally(() => $.done());
