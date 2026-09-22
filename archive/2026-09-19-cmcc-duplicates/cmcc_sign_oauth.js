#!/usr/bin/env node
/**
 * 中国移动10086签到 - 取码服务 /wx/oauth + 完整跳转链
 * cron: 30 8 * * *
 *
 * 变量：
 *   wx_server_url  取码服务地址
 *   wx_auth        取码服务 AUTH
 *   cmcc           openid（& 分隔，留空读全部）
 *   cmcc_cookie    可选，手动 Cookie 逃逸（含 QWHD_SESSION_TOKEN）
 *
 * 契约：
 *   POST {wx_server}/wx/oauth -> data.full_url（业务回调，带 code）
 *   GET full_url 跟随重定向 -> d.sid cookie
 *   GET /qwhdsso/redirect?sid={d.sid} -> QWHD_SESSION_TOKEN
 *   POST /qwhdhub/api/mark/do/mark
 */

const BASE = "https://wx.10086.cn";
const ACTIVITY_ID = "1021122301";
const MARK_URL = `${BASE}/qwhdhub/api/mark/do/mark`;
const PRIZE_URL = `${BASE}/qwhdhub/api/mark/info/prizeInfo`;
const UA = "Mozilla/5.0 (Linux; Android 17; 2509FPN0BC Build/CP2A.260605.016; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/150.0.7871.189 Mobile Safari/537.36 XWEB/1500117 MMWEBSDK/20260502 MMWEBID/9885 MicroMessenger/8.0.76.3141(0x28004C31) WeChat/arm64 Weixin NetType/WIFI Language/zh_CN ABI/arm64 miniProgram/wx43aab19a93a3a6f2";
const APPID = process.env.cmcc_appid || "wx43a850f87498127d";
const YX = process.env.cmcc_yx || "JHT042591F0005";
const TOUCH_ID = process.env.cmcc_touch_id || "26-05-10005-2007-A01";

// 跟随重定向，累加 cookie，返回 {cookies, status, finalUrl}
async function followRedirects(startUrl, cookieMap = {}, maxHops = 20) {
  let current = startUrl;
  let status = 0;
  for (let i = 0; i <= maxHops; i++) {
    const cookieStr = Object.entries(cookieMap).map(([k, v]) => `${k}=${v}`).join("; ");
    const resp = await fetch(current, {
      method: "GET",
      redirect: "manual",
      headers: {
        "User-Agent": UA,
        "Cookie": cookieStr,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
      },
    });
    status = resp.status;
    // 累加 Set-Cookie
    const setCookies = resp.headers.getSetCookie ? resp.headers.getSetCookie() : [];
    for (const sc of setCookies) {
      const pair = sc.split(";")[0].trim();
      const idx = pair.indexOf("=");
      if (idx > 0) cookieMap[pair.slice(0, idx).trim()] = pair.slice(idx + 1).trim();
    }
    const loc = resp.headers.get("location");
    if (status >= 300 && status < 400 && loc) {
      current = new URL(loc, current).toString();
      continue;
    }
    break;
  }
  return { cookies: cookieMap, status, finalUrl: current };
}

// 取码服务 /wx/oauth
async function codeServerOauth(server, auth, openid, redirectUri) {
  try {
    const resp = await fetch(`${server}/wx/oauth`, {
      method: "POST",
      headers: { "auth": auth, "Content-Type": "application/json", "User-Agent": UA },
      body: JSON.stringify({ openid, appid: APPID, scope: "snsapi_base", state: "123", redirect_uri: redirectUri }),
    });
    const data = await resp.json();
    const fullUrl = data?.data?.full_url || "";
    if (!fullUrl) throw new Error(`无 full_url: ${data?.message || JSON.stringify(data).slice(0, 200)}`);
    return fullUrl;
  } catch (e) {
    throw new Error(`取码服务 /wx/oauth 失败: ${e.message} ${e.cause?.code || e.cause?.message || ""}`);
  }
}

// 获取 取码服务账号列表
async function getAccounts(server, auth) {
  const resp = await fetch(`${server}/api/accounts`, { headers: { "auth": auth, "User-Agent": UA } });
  const data = await resp.json();
  return (data?.data?.items || []).filter(a => a.openid && !a.disabled).map(a => a.openid);
}

// 签到
async function doMark(cookies) {
  const cookieStr = Object.entries(cookies).map(([k, v]) => `${k}=${v}`).join("; ");
  const resp = await fetch(MARK_URL, {
    method: "POST",
    headers: {
      "Host": "wx.10086.cn",
      "Content-Type": "application/json;charset=UTF-8",
      "Accept": "*/*",
      "Origin": BASE,
      "Referer": `${BASE}/qwhdhub/qwhdmark/${ACTIVITY_ID}?ys=&yx=${YX}&touch_id=${TOUCH_ID}#/`,
      "x-requested-with": "XMLHttpRequest",
      "login-check": "1",
      "User-Agent": UA,
      "Cookie": cookieStr,
    },
    body: "{}",
  });
  return resp.json();
}

// 解析 cookie 字符串
function parseCookie(str) {
  const m = {};
  for (const p of (str || "").split(";")) {
    const t = p.trim();
    const i = t.indexOf("=");
    if (i > 0) m[t.slice(0, i).trim()] = t.slice(i + 1).trim();
  }
  return m;
}

function mask(s) { return s.length > 8 ? s.slice(0, 8) + "***" : s; }

function interpretSign(raw) {
  if (!raw || typeof raw !== "object") return { ok: false, label: "未知返回" };
  const code = raw.code;
  const msg = (raw.msg || "").trim();
  const success = raw.success === true;
  const isDone = /TODAY_MARKED|已签|重复|already/i.test(msg) || code === "TODAY_MARKED";
  if (success || code === "SUCCESS" || code === 0 || code === "0" || isDone) {
    const prize = raw.data?.prizeName;
    return { ok: true, label: (isDone ? "今日已签到" : msg || "签到成功") + (prize ? ` | 奖品: ${prize}` : "") };
  }
  return { ok: false, label: msg || `code=${code}` };
}

// 单账号流程
async function runOne(server, auth, openid) {
  const activity = `${BASE}/qwhdhub/qwhdmark/${ACTIVITY_ID}?ys=&yx=${YX}&touch_id=${TOUCH_ID}`;

  // 1. wx_server OAuth
  console.log(`  取码服务 /wx/oauth...`);
  const fullUrl = await codeServerOauth(server, auth, openid, activity);
  console.log(`  full_url: ${fullUrl.slice(0, 100)}...`);

  // 2. 跟随 full_url
  let { cookies, status } = await followRedirects(fullUrl);
  console.log(`  跟随后: HTTP ${status}, cookies=[${Object.keys(cookies).join(", ")}]`);

  if (cookies["QWHD_SESSION_TOKEN"]) return cookies;

  // 3. 用 d.sid 尝试 qwhdsso/redirect
  if (cookies["d.sid"]) {
    const sidUrl = `${BASE}/qwhdsso/redirect?sid=${cookies["d.sid"]}`;
    console.log(`  尝试 qwhdsso/redirect...`);
    const r2 = await followRedirects(sidUrl, { ...cookies });
    cookies = r2.cookies;
    console.log(`  qwhdsso 后: cookies=[${Object.keys(cookies).join(", ")}]`);
  }

  if (!cookies["QWHD_SESSION_TOKEN"]) {
    throw new Error(`未获取到 QWHD_SESSION_TOKEN, cookies=[${Object.keys(cookies).join(", ")}]`);
  }
  return cookies;
}

async function main() {
  const manualCookie = (process.env.cmcc_cookie || "").trim();
  const server = (process.env.wx_server_url || "").trim().replace(/\/+$/, "");
  const auth = (process.env.wx_auth || "").trim();
  const openidsRaw = (process.env.cmcc || "").trim();

  console.log("========================================");
  console.log("中国移动10086签到 (Node.js)");
  console.log(`时间: ${new Date().toLocaleString("zh-CN")}`);
  console.log("========================================");

  // 手动 Cookie 模式
  if (manualCookie) {
    console.log("手动 Cookie 模式");
    const cookies = parseCookie(manualCookie);
    if (!cookies["QWHD_SESSION_TOKEN"]) {
      console.log("❌ cmcc_cookie 缺少 QWHD_SESSION_TOKEN");
      process.exit(1);
    }
    const result = await doMark(cookies);
    const { ok, label } = interpretSign(result);
    console.log(ok ? `✅ ${label}` : `❌ ${label}`);
    process.exit(ok ? 0 : 1);
  }

  // OAuth 模式
  if (!server || !auth) {
    console.log("❌ 缺少 wx_server_url 或 wx_auth");
    process.exit(1);
  }

  let openids;
  if (openidsRaw) {
    openids = openidsRaw.split(/[\n&]+/).map(s => s.trim()).filter(Boolean);
  } else {
    console.log("读取 取码服务账号...");
    openids = await getAccounts(server, auth);
  }

  if (!openids.length) {
    console.log("❌ 无可用 openid");
    process.exit(1);
  }

  console.log(`共 ${openids.length} 个账号\n`);
  const results = [];

  for (const openid of openids) {
    console.log(`--- [${mask(openid)}] ---`);
    try {
      const cookies = await runOne(server, auth, openid);
      const result = await doMark(cookies);
      const { ok, label } = interpretSign(result);
      const line = `${ok ? "✅" : "❌"} [${mask(openid)}] ${label}`;
      console.log(line);
      results.push(line);
    } catch (e) {
      const line = `❌ [${mask(openid)}] ${e.message}`;
      console.log(line);
      results.push(line);
    }
    await new Promise(r => setTimeout(r, 3000));
  }

  const okCount = results.filter(r => r.includes("✅")).length;
  console.log("\n========================================");
  console.log(`  签到简报  ${okCount}/${results.length} 成功`);
  console.log("========================================");
  results.forEach(r => console.log(r));
  process.exit(okCount > 0 ? 0 : 1);
}

main().catch(e => { console.error(`❌ ${e.message}`); process.exit(1); });
