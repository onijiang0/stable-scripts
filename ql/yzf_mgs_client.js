// BestPay MGS client for 翼支付签到专区 (Node)
// Sign: md5(secretKey + "&Operation-Type=" + op + "&Request-Data=" + b64(JSON.stringify([data])) + "&Ts=" + ts)
// Body: encryptType=2 pack via mgssdk WASM encrypt
// 密钥不入库：运行时读 YZF_SECRET / YZF_MGS_SECRET
const https = require("https");
const zlib = require("zlib");
const crypto = require("crypto");
const path = require("path");
const fs = require("fs");

function setupEnv() {
  const g = globalThis;
  g.self = g;
  g.btoa = (s) => Buffer.from(s, "binary").toString("base64");
  g.atob = (s) => Buffer.from(s, "base64").toString("binary");
  g.TextEncoder = TextEncoder;
  g.TextDecoder = TextDecoder;
  g.performance = { now: () => Date.now() };
}

const SECRET = (process.env.YZF_SECRET || process.env.YZF_MGS_SECRET || process.env.yzf_secret || "").trim();
const APPID = "FC1902C211615";
const PUB = `-----BEGIN PUBLIC KEY-----
MFkwEwYHKoZIzj0CAQYIKoEcz1UBgi0DQgAEhYXsxs453JtwhnUbksd1oNu0ujvM
+gRo1+HiRg4ZSr0GMjDf5cMToOyNQPALyhs9Hc+OIt0SirlE/efpl3NhfQ==
-----END PUBLIC KEY-----`;
const SSU = "8901010699000117";

const OPS = {
  signNewSwitch: "com.bestpay.redbag.product.api.signin.SignInService.signNewSwitch",
  queryPageConfig: "com.bestpay.marketingadapter.api.y2025.score.market.ScoreMarketService.queryPageConfig",
  querySignInConfigInfo: "com.bestpay.redbag.product.api.y2022.signIn.service.SignInService.querySignInConfigInfo",
  querySignInDateList: "com.bestpay.redbag.product.api.y2022.signIn.service.SignInService.querySignInDateList",
  queryUserSignInAwardRecord: "com.bestpay.redbag.product.api.signin.SignInService.queryUserSignInAwardRecord",
  greenEnergyHomePage: "com.bestpay.marketingadapter.api.y2025.score.market.ScoreMarketService.greenEnergyHomePage",
  queryMarketScore: "com.bestpay.marketingadapter.api.y2025.score.market.ScoreMarketService.queryMarketScore",
  queryScoresList: "com.bestpay.marketingadapter.api.y2025.score.market.ScoreMarketService.queryScoresList",
  queryRedbagList: "com.bestpay.marketingadapter.api.y2026.signpage.SignPageService.queryRedbagList",
  queryBestpayUserShip: "com.bestpay.minsheng.mkt.api.vip.BestpayVipService.queryBestpayUserShip",
  queryMarketProductList: "com.bestpay.marketingadapter.api.y2025.score.market.ScoreMarketService.queryMarketProductList",
  getDynamicScore: "com.bestpay.marketingadapter.api.y2025.score.market.ScoreMarketService.getDynamicScore",
  starReceiveQuery: "com.bestpay.marketingadapter.api.y2025.score.market.ScoreMarketService.starReceiveQuery",
};

function loadMP() {
  setupEnv();
  const zlib = require("zlib");
  const path = require("path");
  const fs = require("fs");
  const https = require("https");
  const cands = [
    process.env.YZF_MGSSDK,
    path.join(__dirname, "mgssdk.dec.js"),
    path.join(__dirname, "..", "yzf_js", "mgssdk.dec.js"),
    path.join(__dirname, "yzf_js", "mgssdk.dec.js"),
  ];
  for (const p of cands) {
    if (p && fs.existsSync(p)) return require(p);
  }
  // runtime fetch gzipped CDN bundle + gunzip
  const cacheDir = process.env.YZF_MGSSDK_DIR || path.join(__dirname, "yzf_js_cache");
  const cacheJs = path.join(cacheDir, "mgssdk.dec.js");
  if (fs.existsSync(cacheJs)) return require(cacheJs);
  fs.mkdirSync(cacheDir, { recursive: true });
  const url = "https://cdn.bestpay.cn/html/h5-page/mgssdk.min.js";
  const gzPath = path.join(cacheDir, "mgssdk.min.js.gz");
  const buf = require("child_process").execFileSync(
    process.execPath,
    [
      "-e",
      `
const https=require("https"),fs=require("fs");
const url=${JSON.stringify(url)};
const out=${JSON.stringify(gzPath)};
const f=fs.createWriteStream(out);
https.get(url,{headers:{"User-Agent":"Mozilla/5.0","Referer":"https://render.bestpay.cn/"}},res=>{
  if(res.statusCode>=300 && res.statusCode<400 && res.headers.location){
    https.get(res.headers.location,{headers:{"User-Agent":"Mozilla/5.0"}},r2=>r2.pipe(f));
    return;
  }
  res.pipe(f);
}).on("error",e=>{console.error(e);process.exit(1)});
f.on("finish",()=>process.exit(0));
`,
    ],
    { timeout: 120000, encoding: "buffer" }
  );
  void buf;
  const gz = fs.readFileSync(gzPath);
  const plain = zlib.gunzipSync(gz);
  fs.writeFileSync(cacheJs, plain);
  return require(cacheJs);
}

function md5hex(s) {
  return crypto.createHash("md5").update(s, "utf8").digest("hex");
}

function rpcSign(operationType, data, ts) {
  if (!SECRET) {
    throw new Error("缺少 YZF_SECRET（勿写进仓库，青龙环境变量配置）");
  }
  const Q = JSON.stringify([data]);
  const b64 = Buffer.from(Q, "utf8").toString("base64");
  const msg = `Operation-Type=${operationType}&Request-Data=${b64}&Ts=${ts}`;
  return { sign: md5hex(`${SECRET}&${msg}`), Q, b64 };
}

function packBody(encryptType, keySend, secData) {
  const A = encryptType;
  const r = Buffer.from(keySend);
  const e = Buffer.from(secData);
  const out = [A + 1, (16711680 & r.length) >> 16, (65280 & r.length) >> 8, 255 & r.length];
  for (const b of r) out.push(b);
  out.push(A + 1 < 3 ? 1 : 2);
  out.push((16711680 & e.length) >> 16, (65280 & e.length) >> 8, 255 & e.length);
  for (const b of e) out.push(b);
  return Buffer.from(out);
}

function createClient({ sessionKey, ipTId, ipRId, sdkPath }) {
  if (sdkPath) process.env.YZF_MGSSDK = sdkPath;
  const MP = loadMP();

  function encryptData(plainObj) {
    const gz = zlib.gzipSync(Buffer.from(JSON.stringify([plainObj]), "utf8"));
    return new Promise((resolve, reject) => {
      MP.MGS.call("encrypt", { encryptType: 2, publicKey: PUB, data: gz }).then((r) => {
        if (!r || !r.success) return reject(new Error("encrypt fail"));
        resolve({ symmetricKey: r.symmetricKey, wire: packBody(2, r.symmetricKeySend, r.secData) });
      }, reject);
    });
  }

  function decryptData(symmetricKey, body) {
    return new Promise((resolve, reject) => {
      const buf = Buffer.from(body);
      let sec = buf;
      if (buf.length > 8 && (buf[0] === 3 || buf[0] === 4)) {
        const klen = (buf[1] << 16) | (buf[2] << 8) | buf[3];
        let off = 4 + klen + 1;
        const slen = (buf[off] << 16) | (buf[off + 1] << 8) | buf[off + 2];
        off += 3;
        sec = buf.slice(off, off + slen);
      }
      MP.MGS.call("decrypt", {
        decryptType: 2,
        data: new Uint8Array(sec),
        symmetricKey,
        publicKey: PUB,
      }).then(resolve, reject);
    });
  }

  function httpPost(op, bodyBuf, ts, sign, session, tnt, env, eventOverride, extraHeaders) {
    return new Promise((resolve, reject) => {
      const event = JSON.stringify(
        eventOverride || {
          env: env || "PRD",
          tntId: tnt || "0101",
          ipTId: ipTId || "",
          ipRId: ipRId || "",
        }
      );
      const headers = Object.assign(
        {
        appid: APPID,
        workspaceid: "PRD",
        version: "2",
        "operation-type": op,
        sessionkey: session || "",
        authssucode: SSU,
        ts,
        sign,
        signtype: "md5",
        "content-type": "application/json",
        "x-cors-fc1902c211615-prd": "1",
        "event-context": event,
        "guest-token": "",
        "content-length": bodyBuf.length,
        "User-Agent":
          "Mozilla/5.0 (Linux; Android 17) MicroMessenger/8.0.76 miniProgram/wx1c4a70bbdfaa2029",
        Referer: "https://servicewechat.com/wx1c4a70bbdfaa2029/390/page-frame.html",
        Origin: "https://render.bestpay.cn",
        Accept: "application/json, text/plain, */*",
        },
        extraHeaders || {}
      );
      const req = https.request(
        {
          hostname: "spanner.bestpay.com.cn",
          port: 10081,
          path: "/",
          method: "POST",
          headers,
          rejectUnauthorized: false,
        },
        (res) => {
          const chunks = [];
          res.on("data", (c) => chunks.push(c));
          res.on("end", () => {
            resolve({
              status: res.statusCode,
              headers: res.headers,
              body: Buffer.concat(chunks),
              rs: res.headers["result-status"] || res.headers["Result-Status"],
            });
          });
        }
      );
      req.on("error", reject);
      req.write(bodyBuf);
      req.end();
    });
  }

  async function call(operationType, data, opts = {}) {
    const session = opts.sessionKey || sessionKey;
    const ts = String(Date.now());
    const payload = Object.assign(
      {
        appType: 117,
        traceLogId: crypto.randomUUID(),
        traceNo: crypto.randomUUID(),
        reqId: crypto.randomUUID(),
      },
      opts.withUser && opts.productNo
        ? { productNo: opts.productNo, sessionKey: session, ipTId: ipTId || "", ipRId: ipRId || "" }
        : {},
      data || {}
    );
    const { sign } = rpcSign(operationType, payload, ts);
    const enc = await encryptData(payload);
    const resp = await httpPost(
      operationType,
      enc.wire,
      ts,
      sign,
      session,
      opts.tnt,
      opts.env,
      opts.eventOverride,
      opts.extraHeaders
    );
    let body = null;
    if (resp.rs && String(resp.rs).startsWith("1000") && resp.body.length) {
      try {
        const dec = await decryptData(enc.symmetricKey, resp.body);
        if (dec instanceof Uint8Array) {
          try {
            body = JSON.parse(zlib.gunzipSync(Buffer.from(dec)).toString("utf8"));
          } catch (e) {
            body = Buffer.from(dec).toString("utf8");
          }
        } else body = dec;
      } catch (e) {
        body = { decryptError: String(e) };
      }
    }
    return { operationType, resultStatus: resp.rs, body, data: payload };
  }

  return { call, OPS, MP };
}

function unwrap(body) {
  if (!body) return null;
  if (typeof body === "string") return body;
  if (body.data && body.data.t !== undefined) return body.data.t;
  if (body.data && body.data.result !== undefined) return body.data.result;
  return body.data !== undefined ? body.data : body;
}

async function dailyCheck({ sessionKey, productNo, ipTId, ipRId, sdkPath }) {
  const client = createClient({ sessionKey, ipTId, ipRId, sdkPath });
  const lines = [];
  const data = {};
  const opts = { withUser: !!productNo, productNo };

  const sw = await client.call(OPS.signNewSwitch, {}, opts);
  const swU = unwrap(sw.body);
  data.signNewSwitch = swU;
  lines.push("签到开关: " + JSON.stringify(swU));

  const geo = await client.call(OPS.greenEnergyHomePage, {}, opts);
  const geoU = unwrap(geo.body) || {};
  data.greenEnergy = geoU;
  const act = geoU.integralActivityNo || "";
  const mall = geoU.marketActivityNo || "";
  lines.push("活动号: " + (act || "-") + " / " + (mall || "-"));

  const page = await client.call(OPS.queryPageConfig, {}, opts);
  data.pageConfig = unwrap(page.body);
  lines.push("页配置: " + JSON.stringify(data.pageConfig));

  const bag = await client.call(OPS.queryRedbagList, {}, opts);
  const bagU = unwrap(bag.body) || {};
  data.redbag = bagU;
  lines.push(
    "签到红包: 已领" +
      (bagU.receivedRedbags != null ? bagU.receivedRedbags : "?") +
      " / 总" +
      (bagU.redbagTotal != null ? bagU.redbagTotal : "?")
  );

  if (act) {
    const score = await client.call(
      OPS.queryMarketScore,
      { integralActivityNo: act, activityNo: act },
      opts
    );
    lines.push("积分已查询");
    data.score = unwrap(score.body);
  }

  const ship = await client.call(OPS.queryBestpayUserShip, {}, opts);
  data.vip = unwrap(ship.body);
  lines.push("会员已查询");

  return { lines, data };
}

const LOGIN_OPS = {
  authorizeCodeAuth:
    "com.bestpay.mobile.service.account.prd.event.AppletAuthorizeEvent.authorizeCodeAuth",
  appletAuthorizeLogin:
    "com.bestpay.mobile.service.account.prd.event.AppletAuthorizeEvent.appletAuthorizeLogin",
  authTokenLogin: "com.bestpay.mbp.customer.facade.LoginFacade.authTokenLogin",
};

function loginEventCtx() {
  return {
    env: "",
    tntId: "0108",
    ipTId: "",
    ipRId: "",
    arNo: "8901011101110001",
    pdPath: "appletAuthorize",
    pdCd: "01110110",
  };
}

async function tryLogin(client, mat) {
  const uid = () => crypto.randomUUID();
  const appId = mat.appid || "wx1c4a70bbdfaa2029";
  const code = mat.wxCode || "";
  const phoneCode = mat.phoneCode || "";
  const mobile = mat.mobile || "";
  const baseData = (extra) =>
    Object.assign(
      {
        appType: 117,
        traceLogId: uid(),
        traceNo: uid(),
        reqId: uid(),
      },
      extra || {}
    );

  const loginOpts = {
    sessionKey: mat.sessionKey || "",
    tnt: "0108",
    env: "",
    eventOverride: loginEventCtx(),
  };

  // 1) authorizeCodeAuth — 实测可返回 openId/unionId
  const authCode = await client.call(
    LOGIN_OPS.authorizeCodeAuth,
    baseData({
      code,
      wxCode: code,
      loginCode: code,
      phoneCode,
      encryptedData: mat.encryptedData || "",
      iv: mat.iv || "",
      appId,
      sourceAppId: appId,
      openId: mat.openid || "",
      authorizeId: mat.openid || "",
      productNo: mobile,
    }),
    loginOpts
  );
  const authBody = unwrap(authCode.body);
  console.log("LOGIN authorizeCodeAuth", authCode.resultStatus, "ok");
  const authT = authBody && typeof authBody === "object" ? authBody.t || authBody.result || authBody : authBody;
  const wxOpenId = (authT && (authT.openId || authT.openid || authT.wxOpenId)) || "";
  const unionId = (authT && (authT.unionId || authT.unionid)) || "";
  if (authT && (authT.sessionKey || authT.sk)) {
    return {
      ok: true,
      sessionKey: authT.sessionKey || authT.sk,
      productNo: authT.productNo || mobile,
      openId: wxOpenId,
      unionId,
      used: "authorizeCodeAuth",
      raw: authBody,
    };
  }

  const aid = wxOpenId || mat.openid || "";
  const chans = [
    { businessChannel: "MINI_PROGRAM", bizChannel: "MINI_PROGRAM", channel: "MINI_PROGRAM", channelType: "MINI_PROGRAM" },
    { businessChannel: "appletAuthorize", channel: "appletAuthorize", authorizeType: "appletAuthorize" },
    { businessChannel: "0108", channel: "0108" },
    { businessChannel: appId, channel: appId, authorizeType: appId },
  ];
  const srcs = [
    { authSource: "WEIXIN", authorizeSource: "WEIXIN", authType: "WEIXIN", authorizeType: "WEIXIN" },
    { authSource: "appletAuthorize", authorizeSource: "appletAuthorize" },
    { authSource: appId, authorizeSource: appId },
    { authSource: "1", authorizeSource: "1" },
  ];
  const phones = [
    phoneCode ? { phoneCode, code: phoneCode } : {},
    phoneCode ? { getPhoneNumberCode: phoneCode } : {},
  ];

  const payloads = [];
  for (const ch of chans) {
    for (const src of srcs) {
      for (const ph of phones) {
        payloads.push(
          baseData(
            Object.assign(
              {
                appId,
                sourceAppId: appId,
                openId: aid,
                wxOpenId: aid,
                authorizeId: aid,
                unionId,
                wxCode: code,
                loginCode: code,
                encryptedData: mat.encryptedData || "",
                iv: mat.iv || "",
              },
              ch,
              src,
              ph
            )
          )
        );
      }
    }
  }

  // 2) appletAuthorizeLogin — 控制次数，避免拖死青龙
  const maxTry = Math.min(payloads.length, 16);
  for (let i = 0; i < maxTry; i++) {
    const data = payloads[i];
    try {
      const r = await client.call(LOGIN_OPS.appletAuthorizeLogin, data, loginOpts);
      const body = unwrap(r.body);
      const t = body && typeof body === "object" ? body.t || body.result || body : body;
      const memo = (body && (body.memo || body.errorMsg)) || "";
      if (i < 3 || (t && (t.sessionKey || t.sk)) || !String(memo).includes("不能为空")) {
        console.log("LOGIN applet", i, r.resultStatus, (memo || "ok").slice(0, 60));
      }
      if (t && (t.sessionKey || t.sk)) {
        return {
          ok: true,
          sessionKey: t.sessionKey || t.sk,
          productNo: t.productNo || t.phoneNo || mobile,
          loginToken: t.loginToken || t.token || "",
          openId: wxOpenId,
          unionId,
          used: "appletAuthorizeLogin#" + i,
          raw: body,
        };
      }
    } catch (e) {
      console.log("LOGIN applet err", i, e.message);
    }
  }

  // 3) defAuthorizeAndRegister / crossPlatformLogin / authTokenLogin fallbacks
  const extras = [
    {
      name: "defAuthorizeAndRegister",
      op: "com.bestpay.mbp.mbpprovinceplatform.service.api.fusionLogin.defAuthorizeAndRegister",
      data: baseData({
        appType: 117,
        partner_token: code || phoneCode,
        targetAppType: "117",
        openId: aid,
        appId,
      }),
    },
    {
      name: "crossPlatformLogin",
      op: LOGIN_OPS.authTokenLogin,
      data: baseData({
        loginToken: code || phoneCode,
        productNo: mobile || aid,
        openId: aid,
        appId,
      }),
    },
  ];
  for (const p of extras) {
    try {
      const r = await client.call(p.op, p.data, loginOpts);
      const body = unwrap(r.body);
      console.log("LOGIN " + p.name, r.resultStatus, "done");
      const t = body && typeof body === "object" ? body.t || body.result || body : body;
      if (t && (t.sessionKey || t.sk || t.loginToken)) {
        return {
          ok: !!(t.sessionKey || t.sk),
          sessionKey: t.sessionKey || t.sk || "",
          productNo: t.productNo || mobile,
          loginToken: t.loginToken || t.token || "",
          openId: wxOpenId,
          raw: body,
          used: p.name,
        };
      }
    } catch (e) {
      console.log("LOGIN " + p.name + " err", e.message);
    }
  }

  return {
    ok: false,
    openId: wxOpenId,
    unionId,
    error:
      "authorizeCodeAuth_ok_but_no_session" +
      (wxOpenId ? " openId=" + wxOpenId : "") +
      "; applet still missing phone/channel/source fields",
  };
}

async function runFromEnv() {
  const ipTId = process.env.YZF_IPTID || "";
  const ipRId = process.env.YZF_IPRID || "";
  let payload = {};
  if (process.env.YZF_PAYLOAD) {
    try {
      payload = JSON.parse(process.env.YZF_PAYLOAD);
    } catch (e) {
      console.log("bad YZF_PAYLOAD", e.message);
    }
  }
  const sessionKey = payload.sessionKey || process.env.yzf || process.env.YZF_SESSION || "";
  const productNo = payload.mobile || payload.productNo || process.env.yzf_phone || process.env.YZF_PHONE || "";

  if (process.env.YZF_LOGIN === "1" || payload.wxCode || payload.phoneCode) {
    const loginClient = createClient({ sessionKey: "", ipTId, ipRId });
    const login = await tryLogin(loginClient, payload);
    if (!login.ok) {
      console.error(login.error || "login failed");
      if (login.openId) console.log("AUTH openId set", !!login.openId);
      process.exit(1);
    }
    console.log(
      "LOGIN OK via " +
        login.used +
        " sessionKey=" +
        !!login.sessionKey +
        " productNo=" +
        (login.productNo || "") +
        " openId=" +
        (login.openId || "")
    );
    payload.sessionKey = login.sessionKey;
    if (login.productNo) payload.mobile = login.productNo;
  }

  if (!payload.sessionKey && !sessionKey) {
    console.error("missing sessionKey after login");
    process.exit(1);
  }

  const r = await dailyCheck({
    sessionKey: payload.sessionKey || sessionKey,
    productNo: payload.mobile || productNo,
    ipTId: payload.ipTId || ipTId,
    ipRId: payload.ipRId || ipRId,
  });
  if (payload.sessionKey) r.data.sessionKey = payload.sessionKey;
  if (payload.mobile) r.data.productNo = payload.mobile;
  console.log(r.lines.join("\n"));
  console.log("DATA " + JSON.stringify(r.data));
}

module.exports = { createClient, dailyCheck, OPS, unwrap, APPID, tryLogin, LOGIN_OPS };

if (require.main === module) {
  runFromEnv().catch((e) => {
    console.error("yzf fail", (e && e.message) || e);
    process.exit(1);
  });
}
