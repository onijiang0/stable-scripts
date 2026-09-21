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

const SECRET = (process.env.YZF_SECRET || process.env.YZF_MGS_SECRET || "63cb711f852f6ab6f1ecc9ade8f518c7").trim();
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
  // 抓包 2026-09-21：真正签到接口
  signIn: "com.bestpay.redbag.product.api.y2022.signIn.service.SignInService.signIn",
  greenEnergyHomePage: "com.bestpay.marketingadapter.api.y2025.score.market.ScoreMarketService.greenEnergyHomePage",
  queryMarketScore: "com.bestpay.marketingadapter.api.y2025.score.market.ScoreMarketService.queryMarketScore",
  queryScoresList: "com.bestpay.marketingadapter.api.y2025.score.market.ScoreMarketService.queryScoresList",
  queryRedbagList: "com.bestpay.marketingadapter.api.y2026.signpage.SignPageService.queryRedbagList",
  queryBestpayUserShip: "com.bestpay.minsheng.mkt.api.vip.BestpayVipService.queryBestpayUserShip",
  queryMarketProductList: "com.bestpay.marketingadapter.api.y2025.score.market.ScoreMarketService.queryMarketProductList",
  getDynamicScore: "com.bestpay.marketingadapter.api.y2025.score.market.ScoreMarketService.getDynamicScore",
  starReceiveQuery: "com.bestpay.marketingadapter.api.y2025.score.market.ScoreMarketService.starReceiveQuery",
  queryUserInfo: "com.bestpay.mbp.customer.facade.UserInfoFacade.queryUserInfo",
  getIpRid: "com.bestpay.bestpaymall.bffmallcli.api.mlogin.service.MloginService.getIpRidAndIpTidByPhone",
  newQueryTheMonthTaskList: "com.bestpay.marketingadapter.api.y2024.mission.MissionService.newQueryTheMonthTaskList",
  queryTheMonthTaskList: "com.bestpay.marketingadapter.api.y2024.mission.MissionOutputService.queryTheMonthTaskList",
  queryTaskInfo: "com.bestpay.marketingadapter.api.y2024.mission.MissionOutputService.queryTaskInfo",
  queryCumulativeTaskList: "com.bestpay.marketingadapter.api.y2024.mission.MissionOutputService.queryCumulativeTaskList",
  // 抓包：浏览/行为完成上报（无独立 completeTask）
  sendTaskMessAge: "com.bestpay.marketingadapter.api.y2022.mission.service.MissionTaskService.sendTaskMessAge",
  receiveTaskAward: "com.bestpay.marketingadapter.api.y2024.mission.MissionTaskService.receiveTaskAward",
  receiveCumulativeTaskAward: "com.bestpay.marketingadapter.api.y2024.mission.MissionTaskService.receiveCumulativeTaskAward",
};

// 抓包回填的平台常量（非账号）
const DEFAULT_IPTID = process.env.YZF_IPTID || "890120031556467340724507";
const DEFAULT_IPRID_SIGN = process.env.YZF_IPRID || "890110031555074150724502";
const DEFAULT_IPRID_MALL = process.env.YZF_IPRID_MALL || "890810000365879820724509";
const SSU_LOGIN = "8901010699000117";
const SSU_GUEST = "8901010699000045";

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

function pickTaskList(u) {
  if (!u) return [];
  const bags = [
    u.taskList,
    u.list,
    u.monthTaskList,
    u.tasks,
    u.taskDTOList,
    u.rows,
    u.records,
    u.cumulativeTaskList,
  ];
  for (const b of bags) {
    if (Array.isArray(b) && b.length) return b;
  }
  if (Array.isArray(u)) return u;
  for (const k of Object.keys(u)) {
    const v = u[k];
    if (Array.isArray(v) && v.length && v[0] && typeof v[0] === "object" && (v[0].taskId || v[0].taskType || v[0].taskName)) {
      return v;
    }
    if (v && typeof v === "object" && Array.isArray(v.taskList)) return v.taskList;
  }
  return [];
}

function taskLabel(t) {
  return String(t.taskName || t.name || t.title || t.taskDesc || t.taskType || t.taskId || "");
}

function isBrowseLike(t) {
  const s = (taskLabel(t) + " " + (t.taskType || "") + " " + (t.bizType || "")).toLowerCase();
  return /浏览|逛|看看|访问|browse|visit|look|video|视频|页面|会场/.test(s);
}

function isTaskPending(t) {
  const st = String(t.taskStatus ?? t.status ?? t.state ?? t.finishFlag ?? "").toLowerCase();
  if (["", "0", "1", "todo", "pending", "unfinished", "doing", "in_progress", "未完成", "进行中"].includes(st)) return true;
  if (["2", "3", "done", "finish", "finished", "completed", "complete", "received", "已领取", "已完成"].includes(st)) return false;
  if (t.isFinish === true || t.finished === true || t.completed === true) return false;
  if (t.isReceive === true || t.received === true || t.awardStatus === 2) return false;
  return true;
}

async function runBrowseTasks(client, optsUser, actFromEnergy) {
  const lines = [];
  const data = { browsed: [], awarded: [], monthTasks: null };
  const month = await client.call(OPS.newQueryTheMonthTaskList, {}, optsUser);
  const mu = unwrap(month.body) || {};
  data.monthTasks = mu;
  data.monthRs = month.resultStatus;
  lines.push("月任务列表 rs=" + month.resultStatus);

  const prog = await client.call(OPS.queryTheMonthTaskList, {}, optsUser);
  const pu = unwrap(prog.body) || {};
  data.monthProgress = pu;
  lines.push("月任务进度 rs=" + prog.resultStatus);

  const tasks = pickTaskList(mu).length ? pickTaskList(mu) : pickTaskList(pu);
  const activityId =
    actFromEnergy ||
    mu.activityId ||
    mu.activityNo ||
    pu.activityId ||
    pu.activityNo ||
    (tasks[0] && (tasks[0].activityId || tasks[0].activityNo)) ||
    "";
  data.taskActivityId = activityId;

  const pendingBrowse = tasks.filter((t) => t && isTaskPending(t) && (isBrowseLike(t) || !t.taskType));
  const target = pendingBrowse.slice(0, 5);
  lines.push(
    "任务: 共" + tasks.length + " 待浏览类" + pendingBrowse.length + " 本次尝试" + target.length
  );

  for (const t of target) {
    const taskId = t.taskId || t.id || "";
    const taskType = t.taskType || t.bizType || "";
    const payload = {
      activityId,
      activityNo: activityId,
      taskId,
      taskType,
      taskCode: t.taskCode || "",
      finishFlag: 1,
      finishFlagStr: "1",
      msgType: t.msgType || taskType || "browse",
      source: "applet",
    };
    const fin = await client.call(OPS.sendTaskMessAge, payload, optsUser);
    const fu = unwrap(fin.body);
    data.browsed.push({ taskId, taskType, label: taskLabel(t), rs: fin.resultStatus, body: fu });
    lines.push(
      "浏览上报 " +
        (taskLabel(t).slice(0, 16) || taskId) +
        " rs=" +
        fin.resultStatus
    );
    const award = await client.call(
      OPS.receiveTaskAward,
      { activityId, activityNo: activityId, taskId, taskType, awardId: t.awardId || t.prizeId || "" },
      optsUser
    );
    const aw = unwrap(award.body);
    data.awarded.push({ taskId, rs: award.resultStatus, body: aw });
    const awMsg = (aw && (aw.memo || aw.msg || aw.errorMsg)) || "";
    lines.push("领奖 rs=" + award.resultStatus + (awMsg ? " " + String(awMsg).slice(0, 24) : ""));
  }

  const cum = await client.call(OPS.queryCumulativeTaskList, {}, optsUser);
  const cu = unwrap(cum.body) || {};
  data.cumulative = cu;
  const cumTasks = pickTaskList(cu);
  for (const t of cumTasks.slice(0, 3)) {
    if (!isTaskPending(t)) continue;
    const taskId = t.taskId || t.id || "";
    const r = await client.call(
      OPS.receiveCumulativeTaskAward,
      {
        activityId: activityId || t.activityId || "",
        taskId,
        taskType: t.taskType || "",
        awardId: t.awardId || "",
      },
      optsUser
    );
    data.awarded.push({ taskId, rs: r.resultStatus, cumulative: true });
    lines.push("累计任务领奖 " + (taskLabel(t).slice(0, 12) || taskId) + " rs=" + r.resultStatus);
  }

  const again = await client.call(OPS.queryTheMonthTaskList, {}, optsUser);
  data.monthProgressAfter = unwrap(again.body);
  lines.push("任务复核 rs=" + again.resultStatus);
  return { lines, data };
}

async function dailyCheck({ sessionKey, productNo, ipTId, ipRId, sdkPath, doLoginFirst }) {
  const client = createClient({
    sessionKey,
    ipTId: ipTId || DEFAULT_IPTID,
    ipRId: ipRId || DEFAULT_IPRID_SIGN,
    sdkPath,
  });
  const lines = [];
  const data = {};
  const optsUser = {
    withUser: !!productNo,
    productNo,
    tnt: "0101",
    env: "PRD",
    extraHeaders: { origin: "https://render.bestpay.cn" },
    eventOverride: {
      env: "PRD",
      tntId: "0101",
      ipTId: DEFAULT_IPTID,
      ipRId: DEFAULT_IPRID_SIGN,
    },
  };

  // 抓包序列（render.bestpay.cn / tntId=0101）
  const sw = await client.call(OPS.signNewSwitch, {}, optsUser);
  const swU = unwrap(sw.body);
  data.signNewSwitch = swU;
  data.signRs = sw.resultStatus;
  lines.push("签到开关: " + JSON.stringify(swU) + " rs=" + sw.resultStatus);

  if (String(sw.resultStatus || "").startsWith("2000")) {
    lines.push("session 过期(result-status=2000)，需刷新 YZF_SESSION");
    data.sessionExpired = true;
    return { lines, data, ok: false };
  }

  const cfg = await client.call(OPS.querySignInConfigInfo, {}, optsUser);
  data.signInConfig = unwrap(cfg.body);
  lines.push("签到配置 rs=" + cfg.resultStatus);

  const list = await client.call(OPS.querySignInDateList, {}, optsUser);
  data.signInDates = unwrap(list.body);
  lines.push("签到日历 rs=" + list.resultStatus);

  // 真正签到
  const act =
    (data.signInConfig &&
      (data.signInConfig.activityId ||
        data.signInConfig.activityNo ||
        data.signInConfig.signInActivityId)) ||
    (data.signInDates &&
      (data.signInDates.activityId || data.signInDates.activityNo)) ||
    "";
  const signPayload = act
    ? { activityId: act, activityNo: act, signDate: new Date().toISOString().slice(0, 10) }
    : {};
  const sign = await client.call(OPS.signIn, signPayload, optsUser);
  data.signIn = unwrap(sign.body);
  data.signInRs = sign.resultStatus;
  const signMemo =
    (data.signIn && (data.signIn.memo || data.signIn.msg || data.signIn.errorMsg)) || "";
  const signOk = String(sign.resultStatus || "").startsWith("1000");
  if (signOk) {
    lines.push("签到接口: 成功" + (signMemo ? " " + String(signMemo).slice(0, 40) : ""));
  } else {
    lines.push("签到接口: rs=" + sign.resultStatus + (signMemo ? " " + signMemo.slice(0, 40) : ""));
  }

  const list2 = await client.call(OPS.querySignInDateList, {}, optsUser);
  data.signInDatesAfter = unwrap(list2.body);
  lines.push("签到复核 rs=" + list2.resultStatus);

  const bag = await client.call(OPS.queryRedbagList, {}, optsUser);
  const bagU = unwrap(bag.body) || {};
  data.redbag = bagU;
  lines.push(
    "签到红包: 已领" +
      (bagU.receivedRedbags != null ? bagU.receivedRedbags : "?") +
      " / 总" +
      (bagU.redbagTotal != null ? bagU.redbagTotal : "?")
  );

  const geo = await client.call(OPS.greenEnergyHomePage, {}, optsUser);
  const geoU = unwrap(geo.body) || {};
  data.greenEnergy = geoU;
  const actScore = geoU.integralActivityNo || geoU.marketActivityNo || "";
  lines.push("能量/活动号: " + (actScore || "-"));

  // 抓包：浏览任务 = sendTaskMessAge 上报 + receiveTaskAward 领奖
  try {
    const taskRes = await runBrowseTasks(client, optsUser, actScore || "");
    lines.push(...taskRes.lines);
    data.tasks = taskRes.data;
  } catch (e) {
    lines.push("浏览任务异常: " + (e && e.message ? e.message : e));
  }

  if (productNo && actScore) {
    const score = await client.call(
      OPS.queryMarketScore,
      { integralActivityNo: actScore, activityNo: actScore, productNo },
      optsUser
    );
    data.score = unwrap(score.body);
    lines.push("积分 rs=" + score.resultStatus);
  }

  const ship = await client.call(OPS.queryBestpayUserShip, {}, optsUser);
  data.vip = unwrap(ship.body);
  lines.push("会员 rs=" + ship.resultStatus);
  data.ok = signOk;
  data.signInConfirmed = signOk;
  return { lines, data, ok: data.ok };
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
    extraHeaders: { origin: "https://h5.bestpay.cn" },
    eventOverride: loginEventCtx(),
  };

  // 抓包：真实入口 authTokenLogin。业务失败后不再盲试 applet（会刷屏且字段仍缺）
  for (let i = 0; i < authVariants.length; i++) {
    try {
      const r = await client.call(LOGIN_OPS.authTokenLogin, authVariants[i], loginOpts);
      const body = unwrap(r.body);
      const t = body && typeof body === "object" ? body.t || body.result || body : body;
      const memo = (body && (body.memo || body.errorMsg || body.tips)) || "";
      console.log("LOGIN authTokenLogin", i, r.resultStatus, String(memo || "ok").slice(0, 60));
      const sk = t && (t.sessionKey || t.sk || t.sessionkey);
      if (sk) {
        return {
          ok: true,
          sessionKey: sk,
          productNo: (t && (t.productNo || t.phoneNo)) || mobile,
          openId: (t && (t.openId || t.openid)) || "",
          unionId: (t && (t.unionId || t.unionid)) || "",
          used: "authTokenLogin#" + i,
          raw: body,
        };
      }
      if (String(memo).includes("登录失败") || String(memo).includes("重新登录")) {
        console.log("LOGIN authTokenLogin 业务失败，跳过 applet 盲试");
        break;
      }
    } catch (e) {
      console.log("LOGIN authTokenLogin err", i, e.message);
    }
  }

  // authorizeCodeAuth 仅一次：历史上可拿 openId，通常无 sessionKey
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
  const sessionKey =
    payload.sessionKey ||
    process.env.YZF_SESSION ||
    (/^[a-f0-9]{16,}$/i.test((process.env.yzf || "").trim()) ? process.env.yzf.trim() : "");
  const productNo = payload.mobile || payload.productNo || process.env.yzf_phone || process.env.YZF_PHONE || "";

  if (process.env.YZF_LOGIN === "1" || payload.wxCode || payload.phoneCode) {
    const loginClient = createClient({ sessionKey: "", ipTId, ipRId });
    const login = await tryLogin(loginClient, payload);
    if (!login.ok) {
      console.error(login.error || "login failed");
      if (login.openId) console.log("AUTH openId set", !!login.openId);
      if (!sessionKey) process.exit(1);
      console.log("LOGIN fail, fallback session from env");
    } else {
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
