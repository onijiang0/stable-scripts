/*
------------------------------------------
@Author: onijiang0
@Date: 2026.09.23
@Description: 申通快递小程序（appid wxcc866a20987fe1ca）- 会员每日积分签到
cron: 38 13 * * *
#定时使用10-19点 随机时间 每天
------------------------------------------
变量名：sto
变量值：wx_server openid，多账号换行或 & 分隔，可加 #备注

依赖变量：
wx_server_url  必填，取码服务地址（勿写进仓库）
wx_auth        必填，取码服务鉴权
QL_NOTIFY      选填，0 关闭推送
------------------------------------------
已实现：
1. 多账号；缺变量报错；单号失败不中断；每次 code 登录，不缓存 token
2. code 换 token（api/wx/user/login）
3. 签到前查 hasSignIn，已签则跳过提交（幂等）
4. continuous/signIn 签到；解析 signInCalendar 当日奖励积分
5. send_notify 统一简报；token/openId 脱敏

契约（appid wxcc866a20987fe1ca，base https://customer-app.sto.cn/）：
code     POST {wx_server_url}/wx/code        json:{openid,appid}
登录     POST api/wx/user/login             body {"code":"<wx.login code>"}
         -> {"success":true,"data":{"token":"<32hex>","openId":"...",...}}
已签     GET  api/app/gateway/membership/api/mine/interests/hasSignIn
         -> {"success":true,"data":{"signIn":true|false}}
签到     POST api/app/gateway/membership/api/mine/interests/continuous/signIn
         -> {"success":true,"data":{"signInCalendar":[{today:true,signIn:true,rewardPoints:36},...]}}
公共头   source=wechat appId=mini_wechat SDKVersion/appVersion/version/platform/scene
         token=<登录后 token> charset=utf-8 brand/model/system + Referer(servicewechat) + UA
签名     signature = MD5(secret + timestamp + uuid)
         timestamp = 毫秒时间戳，uuid = 32 位（去横线），三者均随请求变化
         secret 生产实例实测为 edaea52591239f1fa9e809145225a400（见踩坑 2）
平台参数 appid/base/secret 写默认值，secret 可用 STO_SECRET 覆盖

踩坑：
1. 必须用 axios（或原生 https）发送；用 Node fetch 会因自动附加的请求头被服务端拒绝，
   返回 SYSTEM_001「系统异常」而非 4xx，极易误判为签名错误
2. secret 有两个：模块初始值是 2a095ae8896305a8f165339b397b01b4，
   但线上实际生效的是 setEnv(0) 分支的 edaea52591239f1fa9e809145225a400。
   验证方法：用抓包的 timestamp/requestId/signature 逆算 MD5(secret+ts+rid) 比对
3. 登录失败会返回 200 + {"success":false,"errorCode":"SYSTEM_001"}，
   不抛 HTTP 错误，必须判 success 字段
4. hasSignIn 的 data.signIn 才是"今日是否已签"，不要看 HTTP 状态码
5. ruleDesc 接口在部分账号下返回 UN_KNOW_EX（未知异常），与签到无关，忽略即可
------------------------------------------
*/

const { Env } = require("../tools/env.js");
const axios = require("axios");
const crypto = require("crypto");

const $ = new Env("STO");

const CK_NAME = "sto";
const APP = {
    name: "申通快递",
    appid: "wxcc866a20987fe1ca",
};
const WX_SERVER_URL = (process.env.wx_server_url || "").replace(/\/$/, "");
const WX_AUTH = process.env.wx_auth || "";

const BASE = "https://customer-app.sto.cn/";
const SECRET = process.env.STO_SECRET || "edaea52591239f1fa9e809145225a400";

const URL_LOGIN = BASE + "api/wx/user/login";
const URL_HAS_SIGN = BASE + "api/app/gateway/membership/api/mine/interests/hasSignIn";
const URL_SIGN_IN = BASE + "api/app/gateway/membership/api/mine/interests/continuous/signIn";

const TIMEOUT = 20000;
const DRY_RUN = process.env.STO_DRY_RUN === "1";

const SIGN_REFERER = `https://servicewechat.com/${APP.appid}/574/page-frame.html`;
const SIGN_UA =
    "Mozilla/5.0 (Linux; Android 17; 2509FPN0BC Build/CP2A.260605.016; wv) AppleWebKit/537.36 " +
    "(KHTML, like Gecko) Version/4.0 Chrome/120.0.0.0 Mobile Safari/537.36 MicroMessenger/8.0.76.2660(0x28004C5B) " +
    "WeChat/arm64 Weixin NetType/WIFI Language/zh_CN ABI/arm64 MiniProgramEnv/android";

// ---------------- 工具 ----------------

const md5 = (s) => crypto.createHash("md5").update(s).digest("hex");
const uuid32 = () => crypto.randomUUID().replace(/-/g, "");
const mask = (s) => (s && String(s).length > 12 ? `${String(s).slice(0, 6)}...${String(s).slice(-4)}` : String(s || ""));

function splitAccounts(value = "") {
    return String(value).split(/\n|&/).map((s) => s.trim()).filter(Boolean);
}

function parseAccount(raw = "") {
    const text = String(raw || "").trim();
    if (text.startsWith("{")) {
        try {
            const d = JSON.parse(text);
            return { openid: d.openid || d.openId || "", remark: d.remark || d.name || "" };
        } catch (e) {
            return { openid: text, remark: "" };
        }
    }
    const [openid, remark] = text.split("#").map((s) => s.trim());
    return { openid, remark };
}

/** 公共头 + 签名（对应反编译 mor.c.js 的 signwithoutParams） */
function buildHeaders(token) {
    const ts = String(Date.now());
    const rid = uuid32();
    return {
        "content-type": "application/json",
        charset: "utf-8",
        source: "wechat",
        appId: "mini_wechat",
        SDKVersion: "3.17.3",
        appVersion: "14.0.0",
        version: "8.0.76",
        platform: "android",
        scene: "1005",
        brand: "Xiaomi",
        model: "2509FPN0BC",
        system: "Android 17",
        token: token || "",
        timestamp: ts,
        requestId: rid,
        signature: md5(SECRET + ts + rid),
        Referer: SIGN_REFERER,
        "User-Agent": SIGN_UA,
    };
}

// ---------------- 单账号任务 ----------------

class StoTask {
    constructor(account, index) {
        this.index = index;
        const { openid, remark } = parseAccount(account);
        this.openid = openid;
        this.remark = remark;
        this.logs = [];
    }

    log(msg) {
        const line = `[账号${this.index}] ${msg}`;
        this.logs.push(line);
        $.log(line);
    }

    async call(method, url, body, token) {
        const cfg = {
            method,
            url,
            headers: buildHeaders(token),
            timeout: TIMEOUT,
            validateStatus: () => true,
        };
        if (method !== "GET") cfg.data = body == null ? {} : body;
        const res = await axios(cfg);
        const isStr = typeof res.data === "string";
        let json = res.data;
        if (isStr) { try { json = JSON.parse(res.data); } catch (e) { json = null; } }
        return { status: res.status, json, text: isStr ? res.data : JSON.stringify(res.data) };
    }

    async fetchCode() {
        const res = await axios.post(
            `${WX_SERVER_URL}/wx/code`,
            { openid: this.openid, appid: APP.appid },
            { headers: { auth: WX_AUTH, "content-type": "application/json" }, timeout: TIMEOUT }
        );
        const j = res.data || {};
        if (!j.status) throw new Error(`取码失败: ${j.message || ""}`);
        const code = (j.data && (j.data.code || j.data)) || null;
        if (!code) throw new Error("取码响应缺少 code");
        return code;
    }

    async login(code) {
        const r = await this.call("POST", URL_LOGIN, { code }, "");
        const j = r.json || {};
        if (!j.success || !j.data || !j.data.token) {
            throw new Error(`登录失败: ${j.errorCode || ""} ${j.errorMessage || ""} HTTP ${r.status}`);
        }
        return j.data;
    }

    async hasSignIn(token) {
        const r = await this.call("GET", URL_HAS_SIGN, null, token);
        const d = (r.json && r.json.data) || {};
        return { signed: d.signIn === true, raw: r.json };
    }

    async doSignIn(token) {
        const r = await this.call("POST", URL_SIGN_IN, {}, token);
        const j = r.json || {};
        if (!j.success) {
            return { ok: false, text: `签到失败: ${j.errorCode || ""} ${j.errorMessage || ""}`, raw: j };
        }
        const cal = (j.data && j.data.signInCalendar) || [];
        const today = cal.find((x) => x.today) || cal[0] || {};
        return {
            ok: today.signIn === true,
            points: today.rewardPoints,
            days: cal.length,
            text: today.signIn ? `签到成功，+${today.rewardPoints != null ? today.rewardPoints : "?"} 积分` : "签到已提交",
            raw: j,
        };
    }

    async run() {
        const started = Date.now();
        const rep = {
            index: this.index,
            name: this.remark || `账号${this.index}`,
            openid: mask(this.openid),
            status: "❌ 失败",
            coin: "",
            detail: [],
        };
        try {
            if (!this.openid) throw new Error("openid 为空");

            const code = await this.fetchCode();
            this.log(`取码成功 code=${mask(code)}`);

            const login = await this.login(code);
            const token = login.token;
            this.log(`登录成功 openId=${mask(login.openId)} nickname=${login.nickname ? mask(login.nickname) : "-"} mobile=${login.mobile ? mask(login.mobile) : "未绑定"}`);
            rep.openid = mask(login.openId || this.openid);

            const has = await this.hasSignIn(token);
            if (has.signed) {
                rep.status = "✅ 成功";
                rep.detail = ["今日已签到（hasSignIn 确认，跳过提交）"];
                this.log("签到状态：今日已签到，跳过提交");
                rep.cost = Math.round((Date.now() - started) / 1000);
                return rep;
            }
            this.log("签到状态：今日未签到");

            if (DRY_RUN) {
                rep.status = "🧪 DRY-RUN";
                rep.detail = ["已跳过 continuous/signIn（STO_DRY_RUN=1）"];
                this.log("DRY-RUN：链路已验证，跳过签到提交");
                rep.cost = Math.round((Date.now() - started) / 1000);
                return rep;
            }

            const sign = await this.doSignIn(token);
            if (sign.ok) {
                rep.status = "✅ 成功";
                rep.coin = sign.points != null ? `${sign.points} 积分` : "";
                this.log(`签到结果：${sign.text}`);
            } else {
                rep.status = "⚠️ 未完成";
                this.log(`签到结果：${sign.text}`);
            }
            rep.detail = this.logs.slice(-3);
        } catch (e) {
            rep.status = "❌ 失败";
            rep.detail = [e.message || String(e)];
            this.log(`异常：${e.message || e}`);
        }
        rep.cost = Math.round((Date.now() - started) / 1000);
        return rep;
    }
}

// ---------------- 报表 ----------------

function formatReport(task, reports) {
    const lines = ["==============================", `📦 任务名称：${task}`];
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
        await $.done("STO 未配置取码服务");
        return;
    }

    $.checkEnv(CK_NAME);
    if (!$.userCount) {
        $.log(`未找到变量 ${CK_NAME}`);
        await $.done("STO 未配置账号");
        return;
    }

    const accounts = splitAccounts($.userList.join("\n"));
    $.log(`共 ${accounts.length} 个账号`);

    const reports = [];
    for (let i = 0; i < accounts.length; i++) {
        const task = new StoTask(accounts[i], i + 1);
        reports.push(await task.run());
        if (i < accounts.length - 1) await $.wait(1500, 3000);
    }

    const cost = Math.round((Date.now() - started) / 1000);
    const okCount = reports.filter((r) => r.status.includes("✅")).length;
    const report = formatReport("申通快递 会员签到", reports);
    $.log(report);
    $.log(`⏱️ 执行耗时：${cost} 秒`);

    if (notifyOn) {
        await $.done(`申通快递 会员签到 ${okCount}/${reports.length}\n${report}`);
    }
}

main()
    .catch(async (e) => {
        $.log(`脚本异常: ${e.message || e}`);
        await $.done("申通快递 签到脚本异常");
    })
    .finally(() => {
        $.log("==============================");
    });
