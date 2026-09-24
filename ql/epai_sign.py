#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# /*
# ------------------------------------------
# @Author: onijiang0
# @Date: 2026.09.24
# @Description: 东风奕派(eπ) 每日签到 - 手机号+wx.login code 换 token + SHA512 双签名
# cron: 40 17 * * *
# #定时使用10-19点 随机时间 每天
# ------------------------------------------
# 变量名：epai
# 变量值：phone#openid，多账号换行或 & 分隔，可加 #备注
#   例：<手机号>#<openid>#<备注>
#   （phone=账号手机号，openid=取码服务里的 openid；上面仅占位示例）
#   phone  = 账号明文手机号（登录用）
#   openid = 取码服务 smallcat 里保存的 openid（/wx/code 用）
#
# 依赖变量：
# wx_server_url  必填，取码服务地址（使用者自备，勿写进仓库）
# wx_auth        必填，取码服务鉴权
# QL_NOTIFY      选填，0 关闭推送
# EPAI_DRY_RUN   选填，1 只查状态不签到
# ------------------------------------------
# 已实现：
# 1. 多账号；缺变量报错；单号失败不中断
# 2. /wx/code 拿 loginCode -> user.v2.wxLogin 换 token + userId
# 3. growth.signin.list 查状态；growth.taskCenter.signin 签到（空 body）
# 4. SHA512 双签名（sign + keysign），已抓包逆验通过
# 5. send_notify 统一简报；phone/openid/token 脱敏
#
# 契约（appid wx272cd36461ba9b05，网关 sapp.dfmc.com.cn/appv3/api）：
# 取码     POST {wx_server_url}/wx/code  json:{openid, appid} -> data.code (=loginCode)
# 登录     POST gateWayUrl  api=ly.mp.miniprogram.user.v2.wxLogin
#          body {phone, loginCode, appCode:"miniprogram", equipNo:"1234",
#                channelId:"1234", isAutoLogin:"1"}
#          -> {result:"1", token, accessToken, user:{userId,...}}
# 签到列表 GET/POST api=ly.mp.miniprogram.growth.signin.list  body {type:"1"}
# 签到     POST api=ly.mp.miniprogram.growth.taskCenter.signin  body {}（空）
#          -> {result:"1", msg:"今天已经签到啦!"}
# 签名     noncestr=32hex, timestamp=毫秒
#          有body: sign=SHA512(uid+api+noncestr+ts+token+jsonstr)
#                  keysign=SHA512(appid+appkey+api+noncestr+ts+jsonstr)
#          无body: sign=SHA512(uid+api+noncestr+ts+token)
#                  keysign=SHA512(appid+appkey+api+noncestr+ts)
#          token 是登录响应里的 token 字段（非 accessToken）
# 请求头   lang/appcode/appsystem/apitype/api/appid/noncestr/uid/timestamp/sign/keysign
# 判定     result 是字符串 "1"
#
# 踩坑：
# 1. 签名 token 是响应的 token 字段，不是 accessToken（逆验 accessToken 对不上）
# 2. 空 body {} 不参与签名（走无 jsonstr 分支）
# 3. result 是字符串 "1"，用 == "1" 判断
# 4. phone 是账号明文手机号，属账号信息只进 env
# 5. 取码服务有短时风控（约 8 次/90s），失败**不要重试**（重试只会更快触发风控），
#    直接报错等下一轮定时即可
# ------------------------------------------
# */

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from send_notify import notify_and_format  # noqa: E402


APP_ID = "wx272cd36461ba9b05"
GATEWAY_URL = "https://sapp.dfmc.com.cn/appv3/api"
GW_APPID = os.getenv("epai_appid") or "app1vbsGs1cRiUDA7RFBOv6ZyFk60wjSz9Z"
GW_APPKEY = os.getenv("epai_appkey") or "JvIfhWqA8lzewUI5bxChXqsAbpIOIqERlSy4N9xBFeJJWTbyLGkxPrRK8COs7fM2EMhOOthshEEv576cDsSfVlhRC4U2rZVZYh9wThdOiQjXetT2c8DE7nS4XvfJGUHl"
TIMEOUT = 25

CK_NAME = "epai"
DRY_RUN = os.getenv("EPAI_DRY_RUN") == "1"
QL_NOTIFY_ON = os.getenv("QL_NOTIFY") != "0"

WX_SERVER_URL = (os.getenv("wx_server_url") or "").strip().rstrip("/")
WX_AUTH = (os.getenv("wx_auth") or "").strip()

OP_LOGIN = "ly.mp.miniprogram.user.v2.wxLogin"
OP_SIGN_LIST = "ly.mp.miniprogram.growth.signin.list"
OP_SIGNIN = "ly.mp.miniprogram.growth.taskCenter.signin"

ALREADY_MSG_RE = re.compile(r"已经签|已签|签到过|重复|already", re.I)

UA = (
    "Mozilla/5.0 (Linux; Android 17; 2509FPN0BC Build/CP2A.260605.016; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/150.0.7871.189 "
    "Mobile Safari/537.36 XWEB/1500135 MMWEBSDK/20260502 MMWEBID/9885 "
    "MicroMessenger/8.0.76.3141(0x28004C31) WeChat/arm64 Weixin NetType/WIFI "
    "Language/zh_CN ABI/arm64 MiniProgramEnv/android"
)
REFERER = f"https://servicewechat.com/{APP_ID}/134/page-frame.html"


def mask(v: Any, keep: int = 4) -> str:
    s = str(v or "")
    if not s:
        return "(空)"
    if len(s) <= keep * 2:
        return s[:2] + "***"
    return s[:keep] + "***" + s[-keep:]


def mask_phone(phone: Any) -> str:
    s = str(phone or "")
    digits = re.sub(r"\D", "", s)
    if len(digits) >= 11:
        return digits[:3] + "****" + digits[-4:]
    return mask(phone)


def split_accounts(value: str = "") -> List[str]:
    return [s.strip() for s in re.split(r"\n|&", value or "") if s.strip()]


def parse_account(raw: str) -> Dict[str, str]:
    text = raw.strip()
    parts = [p.strip() for p in text.split("#")]
    phone = parts[0] if parts else ""
    openid = parts[1] if len(parts) > 1 else ""
    remark = parts[2] if len(parts) > 2 else ""
    return {"phone": phone, "openid": openid, "remark": remark}


def noncestr32() -> str:
    import random
    return "".join(random.choice("0123456789abcdef") for _ in range(32))


def sha512(s: str) -> str:
    return hashlib.sha512(s.encode()).hexdigest()


class EpaiTask:
    def __init__(self, account: str, index: int):
        self.index = index
        a = parse_account(account)
        self.phone = a["phone"]
        self.openid = a["openid"]
        self.remark = a["remark"] or f"账号{index}"

    # ---------- 取码 ----------

    def fetch_code(self) -> str:
        """取 wx.login code。⚠️ 取码服务有短时风控（约 8 次/90s），失败不要重试
        （重试会更快触发风控），直接报错等下一轮即可。"""
        body = json.dumps({"openid": self.openid, "appid": APP_ID}).encode()
        req = urllib.request.Request(
            WX_SERVER_URL + "/wx/code", data=body, method="POST",
            headers={"auth": WX_AUTH, "Content-Type": "application/json", "User-Agent": UA},
        )
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                d = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"/wx/code HTTP{e.code}: {e.read().decode()[:150]}")
        if not d.get("status"):
            raise RuntimeError(f"/wx/code 失败: {d.get('message')}")
        code = (d.get("data") or {}).get("code") or d.get("code")
        if not code:
            raise RuntimeError("取码响应缺少 code")
        return code

    # ---------- 网关请求 ----------

    def _call(self, api: str, body: Optional[dict], uid: str, token: str) -> Dict[str, Any]:
        ts = str(int(time.time() * 1000))
        nc = noncestr32()
        jsonstr = json.dumps(body, separators=(",", ":")) if body else ""
        if jsonstr:
            sign = sha512(uid + api + nc + ts + token + jsonstr)
            keysign = sha512(GW_APPID + GW_APPKEY + api + nc + ts + jsonstr)
        else:
            sign = sha512(uid + api + nc + ts + token)
            keysign = sha512(GW_APPID + GW_APPKEY + api + nc + ts)
        headers = {
            "lang": "cn",
            "appcode": "miniprogram",
            "appsystem": "miniprogram",
            "apitype": "8",
            "api": api,
            "content-type": "application/json",
            "appid": GW_APPID,
            "noncestr": nc,
            "uid": uid,
            "timestamp": ts,
            "sign": sign,
            "keysign": keysign,
            "charset": "utf-8",
            "Referer": REFERER,
            "User-Agent": UA,
        }
        data = json.dumps(body, separators=(",", ":")).encode() if body else b"{}"
        req = urllib.request.Request(
            GATEWAY_URL, data=data, method="POST", headers=headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"网关 HTTP{e.code}: {e.read().decode()[:200]}")

    # ---------- 登录 ----------

    def login(self, code: str) -> Dict[str, str]:
        body = {
            "phone": self.phone,
            "loginCode": code,
            "appCode": "miniprogram",
            "equipNo": "1234",
            "channelId": "1234",
            "isAutoLogin": "1",
        }
        # 登录时 uid/token 为空
        r = self._call(OP_LOGIN, body, "", "")
        if str(r.get("result")) != "1":
            raise RuntimeError(f"登录失败: result={r.get('result')} msg={r.get('msg')}")
        user = r.get("user") or {}
        return {
            "token": r.get("token") or "",
            "userId": str(user.get("userId") or ""),
            "userPhone": user.get("userPhone") or self.phone,
        }

    # ---------- 签到 ----------

    def query_state(self, uid: str, token: str) -> Dict[str, Any]:
        r = self._call(OP_SIGN_LIST, {"type": "1"}, uid, token)
        if str(r.get("result")) != "1":
            raise RuntimeError(f"查询签到状态失败: result={r.get('result')} msg={r.get('msg')}")
        rows = r.get("rows") or {}
        return {"rows": rows, "raw": r}

    def do_signin(self, uid: str, token: str) -> Dict[str, Any]:
        r = self._call(OP_SIGNIN, {}, uid, token)
        code = str(r.get("result") or "")
        msg = r.get("msg") or ""
        if code == "1":
            # result=1 成功；msg 含"已经签"则判定为今日已签到（幂等）
            if ALREADY_MSG_RE.search(msg):
                return {"ok": True, "already": True, "msg": msg, "raw": r}
            return {"ok": True, "msg": msg, "raw": r}
        if ALREADY_MSG_RE.search(msg):
            return {"ok": True, "already": True, "msg": msg, "raw": r}
        return {"ok": False, "code": code, "msg": msg, "raw": r}

    # ---------- 主流程 ----------

    def run(self) -> Dict[str, Any]:
        acc = {
            "account": self.remark,
            "phone": self.phone,
            "status": "❌ 失败",
            "reward": "",
            "month_days": "?",
            "extra": [f"phone={mask_phone(self.phone)} openid={mask(self.openid)}"],
        }
        try:
            if not self.phone or not self.openid:
                raise RuntimeError("账号配置不完整（需 phone#openid）")

            code = self.fetch_code()
            login = self.login(code)
            uid, token = login["userId"], login["token"]
            if not uid or not token:
                raise RuntimeError("登录响应缺少 userId/token")
            acc["extra"].append(f"userId={mask(uid)}")

            state = self.query_state(uid, token)
            # growth.signin.list 的 rows 里若有签到状态字段，已签可据此跳过
            # 但列表接口主要返回配置/补签日期，状态判断交给签到接口返回"今天已经签到啦!"

            if DRY_RUN:
                acc["status"] = "🧪 DRY-RUN（未提交）"
                return acc

            res = self.do_signin(uid, token)
            if res.get("ok"):
                acc["status"] = "✅ 签到成功" if not res.get("already") else "✅ 今日已签到"
                acc["reward"] = ""
                acc["extra"].append(f"msg={res.get('msg')}")
            else:
                acc["status"] = "⚠️ 未完成"
                acc["extra"].append(f"result={res.get('code')} msg={res.get('msg')}")
        except Exception as e:
            acc["status"] = "❌ 失败"
            acc["extra"].append(f"异常: {e}")
        return acc


def main() -> int:
    start = time.time()
    raw = os.getenv(CK_NAME, "")
    if not raw.strip():
        print(f"未找到变量 {CK_NAME}")
        if QL_NOTIFY_ON:
            notify_and_format("东风奕派 签到", [], title="epai 未配置账号")
        return 0

    if not WX_SERVER_URL or not WX_AUTH:
        print("缺少 wx_server_url 或 wx_auth")
        if QL_NOTIFY_ON:
            notify_and_format("东风奕派 签到", [], title="epai 未配置取码服务")
        return 0

    accounts = split_accounts(raw)
    print(f"共 {len(accounts)} 个账号")

    results: List[Dict[str, Any]] = []
    for i, a in enumerate(accounts, 1):
        results.append(EpaiTask(a, i).run())
        if i < len(accounts):
            time.sleep(1.5)

    notify_and_format("东风奕派 签到", results, start_ts=start)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"脚本异常: {e}")
        if QL_NOTIFY_ON:
            notify_and_format("东风奕派 签到", [], title="epai 脚本异常")
        sys.exit(1)
