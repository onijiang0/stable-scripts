/*
------------------------------------------
@Author: sm
@Date: 2026.06.09
@Description: OPPO 小程序会员查询/积分签到/做任务赚积分
cron: 21 8 * * *
------------------------------------------
变量名：oppo
变量值：wx_server 里的 openid，多账号用 & 或换行

依赖变量：
wx_server_url  必填，wx_server 地址（勿写进仓库）
wx_auth        必填，wx_server 鉴权值
------------------------------------------
已实现：
1. 登录 /user/pre/auth（code -> sessionId）
2. 会员信息 /member/info、签到入口 /activity/signIn/entrance
3. 签到：运行时从 H5 页发现 activityId（活动按月轮换）
4. 任务：GET /marketing/task/queryTaskList
   - 浏览类(SCAN_PAGE/SCAN_GOODS)先访问目标页再领奖
   - 对 taskStatus=GO_AWARD(2) 调 GET /marketing/task/receiveAward
5. 积分前后对比

契约（heytap/opposhop）：
MINI_API  https://omoapplet-api-cn.heytap.com
H5_API    https://hd.opposhop.cn
任务 API  GET {MINI_API}/marketing/task/queryTaskList?activityId=
          GET {MINI_API}/marketing/task/receiveAward?taskId=&activityId=
TASK_TYPE 0签到 1浏览页 2分享 3浏览商品 4预约商品 5预约直播
TASK_STATUS 1待完成 2可领奖 3已完成 6无次数
签到 H5  POST /api/cn/oapi/marketing/cumulativeSignIn/signIn
踩坑：签到 activityId 按月换，硬编码会过期；content-type 必须 json；
      任务领奖是 GET 不是 POST。
------------------------------------------
*/

const { Env } = require("../tools/env.js");
const axios = require("axios");

const $ = new Env("OPPO");

const CK_NAME = "oppo";
const APP = {
    name: "OPPO",
    appid: "wxe705c556754a1de2",
    version: 361,
};
const WX_SERVER_URL = (process.env.wx_server_url || "").replace(/\/$/, "");
const WX_AUTH = process.env.wx_auth || "";
const MINI_API = "https://omoapplet-api-cn.heytap.com";
const H5_API = "https://hd.opposhop.cn";
const SIGN_ACTIVITY_ID = "2061050217641549824"; // 仅作回落
const CREDITS_ADD_ACTION_ID = "1788913e6d9e4683b8b9ab0088733560";
const BUSINESS = 1;
const SIGN_PAGE =
    "https://hd.opposhop.cn/bp/b371ce270f7509f0?nightModelEnable=true&utm_source=huiyuanwx&utm_medium=me_qiandao";
const USER_AGENT =
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) MicroMessenger/3.9.12 MiniProgramEnv/Windows WindowsWechat/WMPF";
const H5_USER_AGENT =
    "Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/107.0.0.0 Mobile Safari/537.36 MicroMessenger/8.0.30";

const TASK_TYPE = {
    SIGN_REPORT: 0,
    SCAN_PAGE: 1,
    SHARE: 2,
    SCAN_GOODS: 3,
    APPOINTMENT_GOODS: 4,
    APPOINTMENT_PLAY: 5,
};
const TASK_STATUS = {
    PREPARE_FINISH: 1,
    GO_AWARD: 2,
    FINISHED: 3,
    NOT_REMAINING_NUMBER: 6,
};

function splitAccounts(value = "") {
    return String(value)
        .split(/\n|&/)
        .map((item) => item.trim())
        .filter(Boolean);
}

function short(value, max = 400) {
    if (value === undefined || value === null) return "";
    const text = typeof value === "string" ? value : JSON.stringify(value);
    return text.length > max ? `${text.slice(0, max)}...` : text;
}

function parseAccount(raw = "") {
    const text = String(raw || "").trim();
    if (!text) return {};
    if (text.startsWith("{")) {
        const data = JSON.parse(text);
        return { openid: data.openid || data.openId || "", remark: data.remark || data.name || "" };
    }
    const [openid, remark] = text.split("#").map((item) => item.trim());
    return { openid, remark };
}

function awardTypeName(type) {
    const map = { 0: "无奖励", 1: "积分", 2: "优惠券", 3: "抽奖机会" };
    return map[Number(type)] || `类型${type}`;
}

function taskTypeName(type) {
    const map = {
        0: "签到",
        1: "浏览页",
        2: "分享",
        3: "浏览商品",
        4: "预约商品",
        5: "预约直播",
        6: "购买商品",
        7: "购买金卡",
        8: "拼团",
        11: "预约门店",
    };
    return map[Number(type)] || `类型${type}`;
}

function todayText() {
    const now = new Date();
    const y = now.getFullYear();
    const m = String(now.getMonth() + 1).padStart(2, "0");
    const d = String(now.getDate()).padStart(2, "0");
    return `${y}-${m}-${d}`;
}

async function request(options) {
    const res = await axios.request({
        timeout: 25000,
        validateStatus: () => true,
        ...options,
        headers: {
            "User-Agent": USER_AGENT,
            Accept: "application/json, text/plain, */*",
            ...(options.headers || {}),
        },
    });
    return { status: res.status, headers: res.headers || {}, data: res.data };
}

async function getWxCode(openid) {
    if (!WX_SERVER_URL) throw new Error("未配置 wx_server_url");
    if (!WX_AUTH) throw new Error("未配置 wx_auth，无法从 wx_server 获取 code");
    const { status, data } = await request({
        method: "POST",
        url: `${WX_SERVER_URL}/wx/code`,
        headers: {
            auth: WX_AUTH,
            "content-type": "application/json",
            Referer: `https://servicewechat.com/${APP.appid}/${APP.version}/page-frame.html`,
        },
        data: { appid: APP.appid, openid },
    });
    const code = data?.data?.code || data?.code;
    if (status !== 200 || !code) throw new Error(`获取 code 失败 HTTP ${status}: ${short(data)}`);
    return code;
}

class OppoTask {
    constructor(rawAccount, index) {
        this.index = index;
        this.account = parseAccount(rawAccount);
        this.sessionId = "";
        this.encryptedSession = "";
        this.openId = "";
        this.memberInfo = {};
        this.baseInfo = {};
        this.signActivityId = SIGN_ACTIVITY_ID;
        this.creditsAddActionId = CREDITS_ADD_ACTION_ID;
        this.taskActivityId = "";
        this.pointBefore = 0;
    }

    log(message) {
        $.log(`账号[${this.index}]${this.account.remark ? `[${this.account.remark}]` : ""} ${message}`);
    }

    sessionHeaders() {
        return {
            sessionId: this.sessionId || "",
            NEWOPPOSID: this.encryptedSession || "",
            openid: this.openId || "",
            sa_distinct_id: this.openId || "",
            constToken: this.sessionId || "",
        };
    }

    miniHeaders(extra = {}) {
        return {
            "content-type": "application/json",
            s_channel: "oppo",
            source_type: "2",
            s_version: "010000",
            spCallSource: "oppohy",
            Referer: `https://servicewechat.com/${APP.appid}/${APP.version}/page-frame.html`,
            ...this.sessionHeaders(),
            ...extra,
        };
    }

    h5Headers(extra = {}) {
        const sid = this.sessionHeaders();
        return {
            "content-type": "application/json",
            Origin: H5_API,
            Referer: SIGN_PAGE,
            ...sid,
            Cookie: [
                `NEWOPPOSID=${encodeURIComponent(sid.NEWOPPOSID)}`,
                `sessionId=${encodeURIComponent(sid.sessionId)}`,
                `openid=${encodeURIComponent(sid.openid)}`,
            ].join("; "),
            ...extra,
        };
    }

    async miniRequest(method, path, data = {}) {
        const upperMethod = method.toUpperCase();
        const { status, data: result } = await request({
            method: upperMethod,
            url: `${MINI_API}${path}`,
            headers: this.miniHeaders(),
            params: upperMethod === "GET" ? data : undefined,
            data: upperMethod === "GET" ? undefined : data,
        });
        if (status !== 200) throw new Error(`${path} HTTP ${status}: ${short(result)}`);
        if (result?.ret && String(result.ret) !== "1") {
            throw new Error(`${path} 失败: ${result.errMsg || result.message || short(result)}`);
        }
        return result;
    }

    async h5Request(method, path, data = {}) {
        const upperMethod = method.toUpperCase();
        const { status, data: result } = await request({
            method: upperMethod,
            url: `${H5_API}${path}`,
            headers: this.h5Headers(),
            params: upperMethod === "GET" ? data : undefined,
            data: upperMethod === "GET" ? undefined : data,
        });
        if (status !== 200) throw new Error(`${path} HTTP ${status}: ${short(result)}`);
        if (Number(result?.code) !== 200 && result?.succeed !== true) {
            throw new Error(`${path} 失败: ${result?.message || result?.errorMessage || short(result)}`);
        }
        return result;
    }

    async login() {
        if (!this.account.openid) throw new Error("账号格式错误，请配置 wx_server 里的 openid");
        const code = await getWxCode(this.account.openid);
        const { status, data } = await request({
            method: "POST",
            url: `${MINI_API}/user/pre/auth`,
            headers: {
                "content-type": "application/json",
                Referer: `https://servicewechat.com/${APP.appid}/${APP.version}/page-frame.html`,
            },
            data: { code },
        });
        if (status !== 200 || String(data?.ret) !== "1") throw new Error(`登录失败 HTTP ${status}: ${short(data)}`);
        const info = data.data || {};
        this.sessionId = info.sessionId || "";
        this.encryptedSession = info.encryptedSession || "";
        this.openId = info.openId || "";
        if (!this.sessionId) throw new Error(`登录响应缺少 sessionId: ${short(data)}`);
        this.log(`登录成功 openId=${this.openId || "未知"}`);
    }

    async queryMember() {
        const member = await this.miniRequest("GET", "/member/info", { sessionId: this.sessionId });
        const base = await this.miniRequest("GET", "/member/baseInfo", { sessionId: this.sessionId }).catch(() => ({}));
        this.memberInfo = member.data || {};
        this.baseInfo = base.data || {};
        const userName = this.memberInfo.userName || this.baseInfo.userName || "未知";
        const phone = this.baseInfo.pnumber ? `，手机号: ${this.baseInfo.pnumber}` : "";
        this.log(
            `用户信息: ${userName}${phone}，积分: ${this.memberInfo.pointAmount ?? 0}，成长值: ${
                this.memberInfo.growthValue ?? 0
            }，等级: ${this.memberInfo.gradeCode || "未知"}`
        );
    }

    async queryEntrance() {
        const result = await this.miniRequest("GET", "/activity/signIn/entrance", { sessionId: this.sessionId });
        const data = result.data || {};
        this.log(`签到入口: ${data.signInIsStarted ? "已开启" : "未开启"}，连续/累计天数: ${data.signInDays ?? "-"}`);
    }

    async discoverSignActivity() {
        try {
            const { status, data } = await request({
                method: "GET",
                url: SIGN_PAGE,
                headers: { "User-Agent": H5_USER_AGENT, Accept: "text/html,*/*", Referer: SIGN_PAGE },
                responseType: "text",
                transformResponse: [(value) => value],
            });
            const html = typeof data === "string" ? data : "";
            if (status !== 200 || !html) {
                this.log(`签到活动发现: 页面不可读 HTTP ${status}，回落到内置 id`);
                return;
            }
            const anchor = html.search(/"type"\s*:\s*"SignIn"/);
            if (anchor < 0) {
                this.log("签到活动发现: 页面里没有 SignIn 楼层，回落到内置 id");
                return;
            }
            const segment = html.slice(anchor, anchor + 2000);
            const idMatch = segment.match(/"activityInfo"\s*:\s*\{[^{}]*"activityId"\s*:\s*"(\d+)"/);
            const nameMatch = segment.match(/"activityName"\s*:\s*"((?:[^"\\]|\\.)*)"/);
            const actionMatch = segment.match(/"creditsAddActionId"\s*:\s*"([0-9a-f]{32})"/);
            if (!idMatch) {
                this.log("签到活动发现: SignIn 楼层里没有 activityId，回落到内置 id");
                return;
            }
            this.signActivityId = idMatch[1];
            if (actionMatch) this.creditsAddActionId = actionMatch[1];
            const name = nameMatch ? nameMatch[1] : "";
            this.log(`签到活动: ${name || "未命名"} (activityId=${this.signActivityId})`);
        } catch (e) {
            this.log(`签到活动发现失败: ${e.message || e}，回落到内置 id`);
        }
    }

    async getSignDetail() {
        const result = await this.h5Request("GET", "/api/cn/oapi/marketing/cumulativeSignIn/getSignInDetail", {
            activityId: this.signActivityId,
            creditsAddActionId: this.creditsAddActionId,
            business: BUSINESS,
        });
        return result.data || {};
    }

    todayAward(detail = {}) {
        const today = todayText();
        const awards = Array.isArray(detail.baseAwards) ? detail.baseAwards : [];
        return awards.find((item) => String(item.signTime || "").slice(0, 10) === today) || awards[0] || {};
    }

    async querySignDetail() {
        const detail = await this.getSignDetail();
        const award = this.todayAward(detail);
        const signed = Number(award.status) === 1;
        this.log(
            `签到详情: ${signed ? "今日已签" : "今日未签"}，已签天数: ${detail.signInDayNum ?? 0}，今日奖励: ${
                award.awardValue ?? "-"
            }${award.awardType !== undefined ? awardTypeName(award.awardType) : ""}`
        );
        return { detail, signed };
    }

    async signIn() {
        const { signed } = await this.querySignDetail();
        if (signed) return this.log("签到结果: 今日已签到，跳过");
        const result = await this.h5Request("POST", "/api/cn/oapi/marketing/cumulativeSignIn/signIn", {
            activityId: this.signActivityId,
            captchaCode: "",
            creditsAddActionId: this.creditsAddActionId,
            business: BUSINESS,
        });
        const data = result.data || {};
        if (data.receiveStatus === false) {
            this.log(`签到结果: 失败，${data.receiveFailMsg || result.message || "未知原因"}`);
            return;
        }
        this.log(`签到结果: 成功，获得 ${data.awardValue ?? "-"}${awardTypeName(data.awardType)}`);
        await this.querySignDetail();
    }

    /** 任务列表：GET /marketing/task/queryTaskList?activityId= */
    async queryTaskList() {
        const activityId = this.taskActivityId || this.signActivityId;
        const result = await this.miniRequest("GET", "/marketing/task/queryTaskList", { activityId });
        const list = result.data?.taskList || result.data?.list || result.data || [];
        const tasks = Array.isArray(list) ? list : [];
        this.log(`任务列表: ${tasks.length} 条 (activityId=${activityId})`);
        return tasks;
    }

    extractJumpUrl(task) {
        const cfgs = [task.attachConfigOne, task.attachConfigTwo, task.jumpLink, task.link, task.url];
        for (const cfg of cfgs) {
            if (!cfg) continue;
            if (typeof cfg === "string" && /^https?:\/\//.test(cfg)) return cfg;
            if (typeof cfg === "string" && cfg.startsWith("{")) {
                try {
                    const obj = JSON.parse(cfg);
                    const url = obj.url || obj.jumpUrl || obj.pageUrl || obj.link || obj.h5Url;
                    if (url) return url;
                } catch (_) {}
            }
            if (typeof cfg === "object") {
                const url = cfg.url || cfg.jumpUrl || cfg.pageUrl || cfg.link || cfg.h5Url;
                if (url) return url;
            }
        }
        return "";
    }

    /** 浏览类任务：带会话 GET 目标页，尝试触发完成 */
    async browseTask(task) {
        const url = this.extractJumpUrl(task);
        if (!url) {
            this.log(`浏览任务[${task.taskId}] ${taskTypeName(task.taskType)}: 无目标链接，跳过`);
            return;
        }
        try {
            const { status } = await request({
                method: "GET",
                url,
                headers: {
                    "User-Agent": H5_USER_AGENT,
                    Accept: "text/html,*/*",
                    Referer: SIGN_PAGE,
                    ...this.sessionHeaders(),
                    Cookie: [
                        `NEWOPPOSID=${encodeURIComponent(this.encryptedSession || "")}`,
                        `sessionId=${encodeURIComponent(this.sessionId || "")}`,
                        `openid=${encodeURIComponent(this.openId || "")}`,
                    ].join("; "),
                },
                responseType: "text",
                transformResponse: [(v) => v],
                timeout: 15000,
            });
            this.log(`浏览任务[${task.taskId}] ${taskTypeName(task.taskType)} HTTP ${status} ${url.slice(0, 60)}`);
        } catch (e) {
            this.log(`浏览任务[${task.taskId}] 访问失败: ${e.message || e}`);
        }
    }

    /** 领奖：GET /marketing/task/receiveAward */
    async receiveAward(task) {
        try {
            const result = await this.miniRequest("GET", "/marketing/task/receiveAward", {
                taskId: task.taskId,
                activityId: this.taskActivityId || this.signActivityId,
            });
            const data = result.data || {};
            this.log(
                `领奖[${task.taskId}] ${task.taskName || taskTypeName(task.taskType)}: 成功 ` +
                    `${data.awardValue ?? data.num ?? "-"}${awardTypeName(data.awardType ?? task.awardType)}`
            );
            return true;
        } catch (e) {
            this.log(`领奖[${task.taskId}] 失败: ${e.message || e}`);
            return false;
        }
    }

    async doTasks() {
        let tasks = [];
        try {
            tasks = await this.queryTaskList();
        } catch (e) {
            this.log(`任务列表失败: ${e.message || e}`);
            // 猜测任务活动 id 可能与签到不同，再试一次不带 activityId
            try {
                const result = await this.miniRequest("GET", "/marketing/task/queryTaskList", {});
                const list = result.data?.taskList || result.data?.list || result.data || [];
                tasks = Array.isArray(list) ? list : [];
                this.log(`任务列表(无id重试): ${tasks.length} 条`);
            } catch (e2) {
                this.log(`任务列表重试失败: ${e2.message || e2}`);
                return;
            }
        }
        if (!tasks.length) return;

        for (const task of tasks) {
            const status = Number(task.taskStatus);
            const type = Number(task.taskType);
            this.log(
                `任务[${task.taskId}] ${task.taskName || taskTypeName(type)} ` +
                    `status=${status}(${status === 3 ? "已完成" : status === 2 ? "可领奖" : status === 1 ? "待完成" : status}) ` +
                    `类型=${taskTypeName(type)}`
            );
        }

        // 1) 先尝试浏览未完成的浏览类任务
        for (const task of tasks) {
            const type = Number(task.taskType);
            const status = Number(task.taskStatus);
            if (status === TASK_STATUS.FINISHED || status === TASK_STATUS.NOT_REMAINING_NUMBER) continue;
            if ([TASK_TYPE.SCAN_PAGE, TASK_TYPE.SCAN_GOODS].includes(type)) {
                await this.browseTask(task);
                await new Promise((r) => setTimeout(r, 1500));
            }
        }

        // 2) 重新拉列表，对可领奖的领取
        try {
            tasks = await this.queryTaskList();
        } catch (_) {}

        for (const task of tasks) {
            if (Number(task.taskStatus) === TASK_STATUS.GO_AWARD) {
                await this.receiveAward(task);
                await new Promise((r) => setTimeout(r, 1200));
            }
        }

        // 3) 汇总
        const done = tasks.filter((t) => Number(t.taskStatus) === TASK_STATUS.FINISHED).length;
        this.log(`任务汇总: 已完成 ${done}/${tasks.length}`);
    }

    async run() {
        try {
            this.log(`开始执行 ${APP.name}`);
            await this.login();
            await this.queryMember();
            this.pointBefore = Number(this.memberInfo.pointAmount || 0);
            await this.queryEntrance();
            await this.discoverSignActivity();
            await this.signIn();
            await this.doTasks();
            await this.queryMember();
            const after = Number(this.memberInfo.pointAmount || 0);
            this.log(`积分变化: ${this.pointBefore} -> ${after} (${after - this.pointBefore >= 0 ? "+" : ""}${after - this.pointBefore})`);
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
        const task = new OppoTask($.userList[i], i + 1);
        await task.run();
        if (i < $.userList.length - 1) await $.wait(1500, 3000);
    }
}

main()
    .catch((e) => $.log(`脚本异常: ${e.message || e}`))
    .finally(() => $.done());
