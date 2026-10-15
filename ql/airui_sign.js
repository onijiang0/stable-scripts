/*
------------------------------------------
@Author: onijiang0
@Date: 2026.10.15
@Description: 艾瑞调研问卷（ikbang）- 每日签到
cron: 57 16 * * *
#定时使用10-19点 随机时间 每天
------------------------------------------
变量名：airui
变量值：wx_server openid，多账号换行或 & 分隔，可加 #备注

依赖变量：
wx_server_url  必填，取码服务地址（勿写进仓库）
wx_auth        必填，取码服务鉴权
QL_NOTIFY      选填，0 关闭推送
AIRUI_DRY_RUN  选填，1 只查状态不签到
------------------------------------------
已实现：
1. 多账号；缺变量报错；单号失败不中断；每次 code 登录，不缓存 token
2. code -> /app/auth/authorization 换 token
3. getUserDetails 取用户/积分；getSignInInfo 查今日是否已签（幂等）
4. /iclick-new/signIn/sign 签到；签到后再查一次拿连续天数与总积分
5. send_notify 统一简报；openid/token 脱敏

契约（appid wx342d760f674b013b，base https://api.ikbang.cn/v2）：
取码   POST {wx_server_url}/wx/code        json:{openid,appid} -> data.code
登录   POST /app/auth/authorization        body {code,type:"register",acceptCode:""}
       -> result.{token,userId,openid,unionid,mobileAuthStatus}
          ⚠️ mobileAuthStatus 必须为 1（已授权手机号），否则需先在小程序登录一次
用户   GET  /iclick-new/usercenter/getUserDetails
       -> result.{userId,userName,totalPoints}
签到信息 GET /iclick-new/signIn/getSignInInfo
       -> result.{currentSignIn,continuityDay,totalSignInScore,signInRuleId}
签到   POST /iclick-new/signIn/sign        -> result = 本次获得积分
请求头 token / sign / timestamp / Content-Type:application/json / Referer / UA
签名   sign = md5(API_BASE + urlPath + timestamp + payload + APP_KEY + token)
       payload：POST = JSON.stringify(params ?? {})   ← ⚠️ 无参也要 "{}"
                GET  = params ? URLSearchParams(params) : ""
业务码 code=1 成功；code=-3 token 失效需重登；code=402 签名不合法
------------------------------------------
踩坑：
1. ★ POST 无参时 payload 必须序列化成 "{}"，不是空串！
   公共脚本 smallfawn 版就是踩这里：submitSign 不传参，JS 里 `if (params)` 判空
   → payload="" → 服务端 402「签名不合法」。GET 无参用 "" 才是对的。
2. 所有请求体必须显式发 {}（axios data: params || {}），跟签名口径一致
3. code=-3 表示 token 失效，需重新取码登录一次
4. 请求头 referer 里的版本号（/127/）只作标识，实测不影响
------------------------------------------
*/

const { Env } = require("../tools/env.js");
const axios = require("axios");
const crypto = require("crypto");

const $ = new Env("艾瑞调研问卷");

const CK_NAME = "airui";
const APP_ID = "wx342d760f674b013b";
const API_BASE = "https://api.ikbang.cn/v2";
const APP_KEY = "A749380BBD5A4D93B55B4BE245A42988";

const WX_SERVER_URL = (process.env.wx_server_url || "").replace(/\/$/, "");
const WX_AUTH = process.env.wx_auth || "";

const PATH_LOGIN = "/app/auth/authorization";
const PATH_USER = "/iclick-new/usercenter/getUserDetails";
const PATH_SIGN_INFO = "/iclick-new/signIn/getSignInInfo";
const PATH_SIGN = "/iclick-new/signIn/sign";

const TIMEOUT = 15000;
const DRY_RUN = process.env.AIRUI_DRY_RUN === "1";

const UA = "Mozilla/5.0 MicroMessenger MiniProgramEnv/Windows";
const REFERER = `https://servicewechat.com/${APP_ID}/127/page-frame.html`;

// ---------------- 工具 ----------------

const md5 = (s) => crypto.createHash("md5").update(String(s)).digest("hex");

const mask = (v) => {
    const s = String(v == null ? "" : v);
    if (!s) return "(空)";
    if (s.length <= 12) return `${s.slice(0, 3)}***`;
    return `${s.slice(0, 6)}***${s.slice(-6)}`;
};

function splitAccounts(value = "") {
    return String(value).split(/\n|&/).map((s) => s.trim()).filter(Boolean);
}

function parseAccount(raw = "") {
    const text = String(raw || "").trim();
    if (!text) return { openid: "", remark: "" };
    if (text.startsWith("{")) {
        try {
            const d = JSON.parse(text);
            return { openid: d.openid || d.openId || "", remark: d.remark || d.name || "" };
        } catch (e) {
            return { openid: text, remark: "" };
        }
    }
    const [openid, remark] = text.split("#").map((s) => s.trim());
    return { openid: openid || "", remark: remark || "" };
}

function stringifyQuery(params = {}) {
    return new URLSearchParams(params).toString();
}

/** ★ 签名：POST 无参也要序列化成 "{}"，不是空串（血的教训，见头注释踩坑 1） */
function makeSign(urlPath, method, params, timestamp, token = "") {
    let payload = "";
    if (method === "POST") {
        payload = JSON.stringify(params == null ? {} : params);
    } else if (params) {
        payload = stringifyQuery(params);
    }
    return md5(`${API_BASE}${urlPath}${timestamp}${payload}${APP_KEY}${token || ""}`);
}

async function apiRequest(method, urlPath, { token = "", params = null } = {}) {
    const timestamp = String(Date.now());
    const sign = makeSign(urlPath, method, params, timestamp, token);
    const body = method === "POST" ? (params == null ? {} : params) : undefined;
    const res = await axios({
        method,
        url: `${API_BASE}${urlPath}`,
        data: body,
        params: method === "GET" ? (params || undefined) : undefined,
        timeout: TIMEOUT,
        validateStatus: () => true,
        headers: {
            token,
            sign,
            timestamp,
            "Content-Type": "application/json",
            "User-Agent": UA,
            Referer: REFERER,
        },
    });
    return res.data;
}

function assertOk(res, action) {
    if (!res || Number(res.code) !== 1) {
        const msg = res && (res.description || res.msg);
        throw new Error(`${action}失败: ${msg || JSON.stringify(res)}`);
    }
    return res.result;
}

// ---------------- 单账号任务 ----------------

class AiruiTask {
    constructor(account, index) {
        this.index = index;
        const { openid, remark } = parseAccount(account);
        this.openid = openid;
        this.remark = remark;
        this.userId = "";
        this.userName = "";
        this.token = "";
    }

    log(msg) {
        $.log(`账号[${this.index}] ${msg}`);
    }

    async fetchCode() {
        if (!this.openid) throw new Error("缺少 openid，无法自动登录");
        const res = await axios.post(
            `${WX_SERVER_URL}/wx/code`,
            { openid: this.openid, appid: APP_ID },
            { headers: { auth: WX_AUTH, "content-type": "application/json" }, timeout: TIMEOUT }
        );
        const j = res.data || {};
        if (!j.status) throw new Error(`取码失败: ${j.message || ""}`);
        const code = (j.data && (j.data.code || j.data)) || null;
        if (!code) throw new Error("取码响应缺少 code");
        return code;
    }

    async login() {
        const code = await this.fetchCode();
        const result = assertOk(
            await apiRequest("POST", PATH_LOGIN, {
                params: { code, type: "register", acceptCode: "" },
            }),
            "登录授权"
        );
        if (Number(result.mobileAuthStatus) !== 1 || !result.token) {
            throw new Error("账号未完成手机号授权，需先在小程序登录一次");
        }
        this.token = result.token;
        this.userId = result.userId || "";
        this.log(`登录成功: ${mask(this.userId || this.token)}`);
    }

    async requestWithRelogin(method, urlPath, options = {}) {
        const res = await apiRequest(method, urlPath, { ...options, token: this.token });
        if (Number(res && res.code) === -3) {
            this.log("token 失效，重新登录");
            await this.login();
            return apiRequest(method, urlPath, { ...options, token: this.token });
        }
        return res;
    }

    async getUserInfo() {
        const info = assertOk(
            await this.requestWithRelogin("GET", PATH_USER),
            "查询用户信息"
        );
        this.userId = info.userId || this.userId;
        this.userName = info.userName || "";
        return info;
    }

    async getSignInfo() {
        return assertOk(
            await this.requestWithRelogin("GET", PATH_SIGN_INFO),
            "查询签到信息"
        );
    }

    async submitSign() {
        return assertOk(
            await this.requestWithRelogin("POST", PATH_SIGN),
            "签到"
        );
    }

    async run() {
        const rep = {
            index: this.index,
            name: this.remark || "微信用户",
            status: "❌ 失败",
            coin: "",
            openid: mask(this.openid),
            detail: [],
        };
        try {
            await this.login();
            const info = await this.getUserInfo();
            rep.detail.push(`用户 ${this.userName || mask(this.userId)}，当前积分 ${info.totalPoints ?? "未知"}`);

            const before = await this.getSignInfo();
            if (before.currentSignIn) {
                rep.status = "✅ 今日已签到";
                rep.detail.push(
                    `连续 ${before.continuityDay ?? "?"} 天，总签到积分 ${before.totalSignInScore ?? "?"}`
                );
            } else if (DRY_RUN) {
                rep.status = "🧪 DRY-RUN（未提交）";
            } else {
                const score = await this.submitSign();
                const after = await this.getSignInfo();
                rep.status = "✅ 签到成功";
                rep.coin = score != null ? `+${score} 积分` : "";
                rep.detail.push(
                    `连续 ${after.continuityDay ?? "?"} 天，总签到积分 ${after.totalSignInScore ?? "?"}`
                );
            }
        } catch (e) {
            rep.status = "❌ 失败";
            rep.detail.push(`异常: ${e.message || e}`);
        }
        return rep;
    }
}

// ---------------- 报表 ----------------

function formatReport(task, reports) {
    const lines = ["==============================", `📋 任务名称：${task}`];
    (reports || []).forEach((r) => {
        lines.push(`【账号${r.index}】${r.name}`);
        lines.push(`   状态：${r.status}${r.coin ? "  奖励：" + r.coin : ""}`);
        lines.push(`   账号：${r.openid}`);
        (r.detail || []).forEach((d) => lines.push(`   · ${d}`));
        lines.push("");
    });
    lines.push("==============================");
    return lines.join("\n");
}

// ---------------- 入口 ----------------

async function main() {
    const started = Date.now();
    const notifyOn = process.env.QL_NOTIFY !== "0";

    if (!WX_SERVER_URL || !WX_AUTH) {
        $.log("缺少 wx_server_url 或 wx_auth");
        if (notifyOn) await $.done("艾瑞调研问卷 未配置取码服务");
        return;
    }

    $.checkEnv(CK_NAME);
    if (!$.userCount) {
        $.log(`未找到变量 ${CK_NAME}`);
        if (notifyOn) await $.done("艾瑞调研问卷 未配置账号");
        return;
    }

    const accounts = splitAccounts($.userList.join("\n"));
    $.log(`共 ${accounts.length} 个账号`);

    const reports = [];
    for (let i = 0; i < accounts.length; i++) {
        reports.push(await new AiruiTask(accounts[i], i + 1).run());
        if (i < accounts.length - 1) await $.wait(1500, 3000);
    }

    const cost = Math.round((Date.now() - started) / 1000);
    const okCount = reports.filter((r) => r.status.includes("✅")).length;
    const report = formatReport("艾瑞调研问卷", reports);
    $.log(report);
    $.log(`⏱️ 执行耗时：${cost} 秒`);

    if (notifyOn) {
        await $.done(`艾瑞调研问卷 ${okCount}/${reports.length}\n${report}`);
    }
}

main()
    .catch(async (e) => {
        $.log(`脚本异常: ${(e && e.message) || e}`);
        if (process.env.QL_NOTIFY !== "0") await $.done("艾瑞调研问卷 脚本异常");
    })
    .finally(() => {});
