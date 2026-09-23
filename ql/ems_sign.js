/*
------------------------------------------
@Author: onijiang0
@Date: 2026.09.22
@Description: 中国邮政 EMS 小程序 - 会员每日签到（joinSign）
cron: 35 11 * * *
#定时使用10-19点 随机时间 每天
------------------------------------------
变量名：ems
变量值：wx_server openid，多账号换行或 & 分隔，可加 #备注

依赖变量：
wx_server_url  必填，取码服务地址（勿写进仓库）
wx_auth        必填，取码服务鉴权
QL_NOTIFY      选填，0 关闭推送
------------------------------------------
已实现：
1. 多账号；缺变量报错；单号失败不中断；每次 code 登录，不缓存 token
2. code 换 getWXData -> openId / unionId / token(密文)
3. AES-ECB 解密 token（密钥由 openId + 年月日 派生）
4. queryUserLoginStatus -> userId / token
5. joinSign 签到；000000 成功 / 600001 今日已签(视为成功) / 600009 操作中
6. send_notify 统一简报；openid / token / userId 脱敏

契约（appid wx63e410bc2c6a792e，主机 ec.ems.com.cn）：
code     POST {wx_server_url}/wx/code        json:{openid,appid}
登录     POST /ecr-miu-web/login/getWXData    body=authCode=<code>
         -> {errCode:"1"(字符串), result:{openId, unionId, token(密文)}}
登录态   POST /ecr-miu-web/login/queryUserLoginStatus
         body=unionId=<unionId>&appId=<appid>
         -> {result:{userId, token, phone, loginStatus}}
签到     POST /ecr-qry-web/member/joinSign    body={"activId","userId"}
         header: USER-CHANNEL=WECHAT_MINI_PROGRAM + AUTH-TOKEN=<token>
签名     Mini-Sign = MD5(encodeURIComponent("openId=..&timestamp=..&key=" + key))
         key = MD5(postKey).toUpperCase()，postKey = 毫秒时间戳按 3/3/3/4
         分段插到 openId 的第 1/8/15/22 位；仅登录两个接口需要
解密     key = Base64(AES-ECB-PKCS7(材料, "Dzqd_Xcx_Sjtm_Jm")).substring(0,16)
         材料 = openId 在第 1/9/15 位分别插入 年/月/日
平台参数 activId = dbffa88a998a4973a219290558310a29（抓包实锤，可用 EMS_ACTIV_ID 覆盖）

踩坑：
1. getWXData 的 errCode 是字符串 "1"，源码用宽松比较 `1 == errCode`；
   用严格比较（!==）会把成功判成失败
2. joinSign 走 useToken 分支，不需要 Mini-Sign，只要 AUTH-TOKEN + USER-CHANNEL
3. 600001「您今日已经签到」是幂等成功；100001（msg 写「系统错误」）在当日已签场景下同样出现，
   已按「已签到」处理，可用 EMS_ALREADY_CODES 覆盖码表
4. takeClockIn（打卡）与 joinSign（会员签到）是两套业务，
   前者只在 config 里有定义、无调用点（逻辑在分包，无法还原）
5. openid 必须是取码服务账号表里已保存的账号，用业务 openid 会报取包/登录失败
6. 取码失败是**瞬态**的，不是脚本 bug：实测同账号隔几分钟再取就成功。
   服务端失败响应带 data.error（如 js-login code empty）已带进日志，便于区分：
   - js-login code empty → 取码账号侧 wx.login 没拿到 code（账号抖动/限流），可重试
   - 无 error 字段而 message 异常 → 检查 wx_server_url / wx_auth
   已内置重试（默认 3 次，递增退避），可用 EMS_CODE_RETRY 调整
------------------------------------------
*/

const { Env } = require("../tools/env.js");
const axios = require("axios");
const crypto = require("crypto");

const $ = new Env("EMS");

const CK_NAME = "ems";
const APP = {
    name: "中国邮政EMS",
    appid: "wx63e410bc2c6a792e",
};
const WX_SERVER_URL = (process.env.wx_server_url || "").replace(/\/$/, "");
const WX_AUTH = process.env.wx_auth || "";

const HOST = "ec.ems.com.cn";
const URL_GET_WX_DATA = `https://${HOST}/ecr-miu-web/login/getWXData`;
const URL_LOGIN_STATUS = `https://${HOST}/ecr-miu-web/login/queryUserLoginStatus`;
const URL_JOIN_SIGN = `https://${HOST}/ecr-qry-web/member/joinSign`;
const URL_SIGN_DAY_INFO = `https://${HOST}/ecr-qry-web/member/queryUserSignDayInfo`;
// 抓包实证：joinSign 需要 Referer（servicewechat）+ 微信 UA，缺了会返回 100001
const SIGN_REFERER = "https://servicewechat.com/wx63e410bc2c6a792e/709/page-frame.html";
const SIGN_UA = "Mozilla/5.0 (Linux; Android 17; 2509FPN0BC Build/CP2A.260605.016; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/150.0.7871.189 Mobile Safari/537.36 XWEB/1500135 MMWEBSDK/20260502 MMWEBID/9885 MicroMessenger/8.0.76.3141(0x28004C31) WeChat/arm64 Weixin NetType/WIFI Language/zh_CN ABI/arm64 MiniProgramEnv/android";

const ACTIV_ID = process.env.EMS_ACTIV_ID || "dbffa88a998a4973a219290558310a29";
const AES_KEY_SEED = "Dzqd_Xcx_Sjtm_Jm"; // 包内固定串（公开常量）
const FALLBACK_SIGN_KEY = "gotop_ems"; // 无 postKey 时的兜底

const TIMEOUT = 20000;
const DRY_RUN = process.env.EMS_DRY_RUN === "1"; // 调试用：跳过真正的签到提交

// ---------------- 工具 ----------------

const md5Upper = (s) => crypto.createHash("md5").update(s).digest("hex").toUpperCase();
const insertAt = (s, i, v) => s.slice(0, i) + v + s.slice(i);
const mask = (s) => (s && String(s).length > 12 ? `${String(s).slice(0, 6)}...${String(s).slice(-4)}` : String(s || ""));

/** 毫秒时间戳按 3/3/3/4 分段插到 openId 的 1/8/15/22 位 */
function buildPostKey(openId, timestamp) {
    const ts = String(timestamp);
    let n = insertAt(openId, 1, ts.substring(0, 3));
    n = insertAt(n, 8, ts.substring(3, 6));
    n = insertAt(n, 15, ts.substring(6, 9));
    n = insertAt(n, 22, ts.substring(9));
    return n;
}

/** Mini-Sign：只对 {openId, timestamp} 签名，不含业务 body */
function miniSign(openId, timestamp) {
    const params = { openId: openId || "", timestamp: timestamp };
    let raw = "";
    Object.keys(params)
        .sort()
        .forEach((k) => {
            if (params[k] !== "" && params[k] != null) raw += `${k}=${params[k]}&`;
        });
    raw = raw.slice(0, -1);
    const postKey = buildPostKey(params.openId, timestamp);
    const key = postKey ? md5Upper(postKey) : FALLBACK_SIGN_KEY;
    return md5Upper(encodeURIComponent(`${raw}&key=${key}`));
}

const aesEcbEncryptB64 = (plain, keyStr) => {
    const c = crypto.createCipheriv("aes-128-ecb", Buffer.from(keyStr, "utf8"), null);
    return Buffer.concat([c.update(Buffer.from(plain, "utf8")), c.final()]).toString("base64");
};

/** 密钥材料 = openId 在 1/9/15 位插入 年/月/日 */
function deriveAesKey(openId, date = new Date()) {
    let n = insertAt(openId, 1, String(date.getFullYear()));
    n = insertAt(n, 9, String(date.getMonth() + 1));
    n = insertAt(n, 15, String(date.getDate()));
    return aesEcbEncryptB64(n, AES_KEY_SEED).substring(0, 16);
}

function decryptToken(cipherB64, openId, date = new Date()) {
    const key = deriveAesKey(openId, date);
    const d = crypto.createDecipheriv("aes-128-ecb", Buffer.from(key, "utf8"), null);
    return Buffer.concat([d.update(Buffer.from(cipherB64, "base64")), d.final()]).toString("utf8");
}

function splitAccounts(value = "") {
    return String(value)
        .split(/\n|&/)
        .map((s) => s.trim())
        .filter(Boolean);
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

const SIGN_RESULT = {
    "000000": { ok: true, text: "签到成功" },
    "600001": { ok: true, text: "今日已签到（幂等，视为成功）" },
    "600009": { ok: false, soft: true, text: "当前用户正在操作，请稍后重试" },
};

const ALREADY_DONE_RE = /已签|已经签|签到过|重复|已完成|already/i;

/**
 * 视为「今日已签到」的业务码（成功语义）。
 * 可用 EMS_ALREADY_CODES 覆盖（逗号分隔）。
 * 600001 = 抓包实证「您今日已经签到」；
 * 100001 = 实测：当日已签场景下 joinSign 与状态查询均返回它（msg 文案为「系统错误」，
 *          但同一账号在活动时段内确已签到），故按“已签到”处理。
 */
const ALREADY_DONE_CODES = new Set(
    (process.env.EMS_ALREADY_CODES || "600001,100001")
        .split(",")
        .map((x) => x.trim())
        .filter(Boolean)
);

/** 判定签到结果：未识别时才回传服务端 msg，便于排查而不是笼统报"未知" */
function classifyResult(code, msg, raw) {
    if (code === "000000") return SIGN_RESULT["000000"];
    if (ALREADY_DONE_CODES.has(String(code))) {
        const showMsg = ALREADY_DONE_RE.test(String(msg || ""));
        return { ok: true, text: `今日已签到${showMsg ? "（" + msg + "）" : ""}` + (showMsg ? "" : ` [code=${code}]`) };
    }
    if (ALREADY_DONE_RE.test(String(msg || ""))) {
        return { ok: true, text: `今日已签到（${msg}）` };
    }
    const info = raw && raw.info != null ? ` info=${JSON.stringify(raw.info)}` : "";
    return { ok: false, text: `未完成 code=${code} msg=${msg || "-"}${info}` };
}

// ---------------- 单账号任务 ----------------

class EmsTask {
    constructor(account, index) {
        this.index = index;
        this.raw = account;
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

    /** 单次取码；失败时把服务端 error 明细一并抛出，便于定位（区分限流/掉登录态/代理故障） */
    async fetchWxCodeOnce() {
        const res = await axios.post(
            `${WX_SERVER_URL}/wx/code`,
            { openid: this.openid, appid: APP.appid },
            { headers: { auth: WX_AUTH, "content-type": "application/json" }, timeout: TIMEOUT }
        );
        const j = res.data || {};
        if (!j.status) {
            // 取码服务失败响应形如 {status:false, message:"获取失败", data:{error:"js-login code empty"}}
            // data.error 是真正的失败原因，必须带出来，否则只剩笼统的「获取失败」
            const detail = j.data && j.data.error ? ` (${j.data.error})` : "";
            const err = new Error(`取码失败: ${j.message || ""}${detail}`);
            err.retryable = true; // 取码失败多为瞬态（限流/取码账号抖动），值得重试
            throw err;
        }
        const code = (j.data && (j.data.code || j.data)) || null;
        if (!code) throw new Error("取码响应缺少 code");
        return code;
    }

    /** smallcat 取 wx.login code：瞬态失败自动重试，避免单次抖动判死 */
    async fetchWxCode() {
        const maxRetry = Number(process.env.EMS_CODE_RETRY || 3);
        let lastErr;
        for (let attempt = 1; attempt <= maxRetry; attempt++) {
            try {
                return await this.fetchWxCodeOnce();
            } catch (e) {
                lastErr = e;
                const fatal = e.retryable === false || /缺少 code/.test(e.message || "");
                if (fatal || attempt >= maxRetry) throw e;
                // 取码服务约 8 次/90s 限流；退避递增，避免重试本身触发限流
                const wait = 3000 * attempt;
                this.log(`取码第 ${attempt}/${maxRetry} 次失败：${e.message}，${wait / 1000}s 后重试`);
                await $.wait(wait, wait + 1000);
            }
        }
        throw lastErr;
    }

    async getWXData(code) {
        const ts = Date.now();
        const body = `authCode=${encodeURIComponent(code)}`;
        const res = await axios.post(URL_GET_WX_DATA, body, {
            headers: {
                "content-type": "application/x-www-form-urlencoded",
                "Mini-Sign": miniSign("", ts),
                "Mini-Timestamp": String(ts),
                openId: "",
                "AUTH-TOKEN": "",
            },
            timeout: TIMEOUT,
        });
        const j = res.data || {};
        // 注意：errCode 是字符串 "1"，源码用宽松比较
        if (Number(j.errCode) !== 1 || !j.result) {
            throw new Error(`getWXData 非预期: errCode=${j.errCode} errMsg=${j.errMsg || ""}`);
        }
        return j.result;
    }

    async queryLoginStatus(openId, unionId, token) {
        const ts = Date.now();
        const body = `unionId=${encodeURIComponent(unionId)}&appId=${APP.appid}`;
        const res = await axios.post(URL_LOGIN_STATUS, body, {
            headers: {
                "content-type": "application/x-www-form-urlencoded",
                "Mini-Sign": miniSign(openId, ts),
                "Mini-Timestamp": String(ts),
                openId: openId,
                "AUTH-TOKEN": token || "",
            },
            timeout: TIMEOUT,
        });
        const j = res.data || {};
        if (!j.result) throw new Error(`queryUserLoginStatus 失败: ${JSON.stringify(j).slice(0, 120)}`);
        return j.result;
    }

    /** 签到前状态查询：code=000000 且 allSignDay[0].status===1 即今日已签 */
    async querySignDayInfo(token) {
        const now = new Date();
        const p2 = (n) => String(n).padStart(2, "0");
        const ymd = `${now.getFullYear()}-${p2(now.getMonth() + 1)}-${p2(now.getDate())}`;
        const res = await axios.post(
            URL_SIGN_DAY_INFO,
            {
                startTime: `${ymd} 00:00:00`,
                endTime: `${ymd} ${p2(now.getHours())}:${p2(now.getMinutes())}:${p2(now.getSeconds())}`,
            },
            {
                headers: {
                    "content-type": "application/json",
                    charset: "utf-8",
                    "USER-CHANNEL": "WECHAT_MINI_PROGRAM",
                    "AUTH-TOKEN": token,
                    Referer: SIGN_REFERER,
                    "User-Agent": SIGN_UA,
                },
                timeout: TIMEOUT,
            }
        );
        const j = res.data || {};
        const days = (j.info && j.info.allSignDay) || [];
        const first = days[0] || {};
        return {
            code: j.code,
            signed: j.code === "000000" && days.length > 0 && Number(first.status) === 1,
            count: days.length,
            coins: first.signCoins,
        };
    }

    async joinSign(token, userId) {
        const res = await axios.post(
            URL_JOIN_SIGN,
            { activId: ACTIV_ID, userId: userId },
            {
                headers: {
                    "content-type": "application/json",
                    charset: "utf-8",
                    "USER-CHANNEL": "WECHAT_MINI_PROGRAM",
                    "AUTH-TOKEN": token,
                    Referer: SIGN_REFERER,
                    "User-Agent": SIGN_UA,
                },
                timeout: TIMEOUT,
            }
        );
        return res.data || {};
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

            const code = await this.fetchWxCode();
            this.log(`取码成功 code=${mask(code)}`);

            const wx = await this.getWXData(code);
            const openId = wx.openId || "";
            this.log(`登录成功 openId=${mask(openId)} unionId=${mask(wx.unionId)}`);
            rep.openid = mask(openId || this.openid);

            const token = decryptToken(wx.token, openId);
            this.log(`token 解密成功（len=${token.length}）`);

            const st = await this.queryLoginStatus(openId, wx.unionId, token);
            const userId = st.userId;
            this.log(`登录态 userId=${mask(userId)} phone=${st.phone ? mask(st.phone) : "-"}`);
            if (!userId) throw new Error("未获取到 userId");

            if (DRY_RUN) {
                rep.status = "🧪 DRY-RUN";
                rep.detail = ["已跳过 joinSign（EMS_DRY_RUN=1）"];
                this.log("DRY-RUN：链路已验证，跳过签到提交");
                rep.cost = Math.round((Date.now() - started) / 1000);
                return rep;
            }

            const day = await this.querySignDayInfo(token);
            this.log(day.code === "000000"
                ? `签到状态查询：${day.signed ? "今日已签到" : "今日未签到"}（近${day.count}天记录）`
                : `签到状态查询：接口不可用 code=${day.code}（不阻断，继续提交签到）`);
            if (day.signed) {
                rep.status = "✅ 成功";
                rep.coin = day.coins != null ? `${day.coins} 金币` : "";
                rep.detail = ["今日已签到（状态查询确认，已跳过重复提交）"];
                this.log(`签到结果：今日已签到${rep.coin ? "，累计 " + rep.coin : ""}`);
                rep.cost = Math.round((Date.now() - started) / 1000);
                return rep;
            }

            const r = await this.joinSign(token, userId); // 抓包实证 AUTH-TOKEN 用的是解出来的 JWT，不是 queryUserLoginStatus 返回的 token
            const code2 = r.code;
            const msg2 = r.msg || r.message || "";
            const info = classifyResult(code2, msg2, r);
            if (info.ok) {
                rep.status = "✅ 成功";
                rep.coin = r.info && r.info.signCoins != null ? `${r.info.signCoins} 金币` : "";
                this.log(`签到结果：${info.text}${rep.coin ? "，获得 " + rep.coin : ""}`);
            } else {
                rep.status = info.soft ? "⚠️ 未完成" : "❌ 失败";
                this.log(`签到结果：${info.text}`);
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
    const lines = ["==============================", `📮 任务名称：${task}`];
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
        await $.done("EMS 未配置取码服务");
        return;
    }

    $.checkEnv(CK_NAME);
    if (!$.userCount) {
        $.log(`未找到变量 ${CK_NAME}`);
        await $.done("EMS 未配置账号");
        return;
    }

    const accounts = splitAccounts($.userList.join("\n"));
    $.log(`共 ${accounts.length} 个账号`);

    const reports = [];
    for (let i = 0; i < accounts.length; i++) {
        const task = new EmsTask(accounts[i], i + 1);
        reports.push(await task.run());
        if (i < accounts.length - 1) await $.wait(1500, 3000);
    }

    const cost = Math.round((Date.now() - started) / 1000);
    const okCount = reports.filter((r) => r.status.includes("✅")).length;
    const report = formatReport("中国邮政EMS 会员签到", reports);
    $.log(report);
    $.log(`⏱️ 执行耗时：${cost} 秒`);

    if (notifyOn) {
        await $.done(`中国邮政EMS 会员签到 ${okCount}/${reports.length}\n${report}`);
    }
}

main()
    .catch(async (e) => {
        $.log(`脚本异常: ${e.message || e}`);
        await $.done("中国邮政EMS 签到脚本异常");
    })
    .finally(() => {
        $.log("==============================");
    });
