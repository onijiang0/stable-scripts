#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# /*
# ------------------------------------------
# @Author: onijiang0
# @Date: 2026.09.21
# @Description: 奈雪的茶 - 会员每日签到（奈雪币 / 成长值）
# cron: 48 14 * * *
# #定时使用10-19点 随机时间 每天
# ------------------------------------------
# 变量名：naixue
# 变量值：业务 openid（wxab7430e6e8b9a4ab 下），多账号换行或 & 分隔，可加 #备注
#
# 依赖变量：
# wx_server_url  必填，取码服务地址（使用者自备，勿写进仓库）
# wx_auth        必填，取码服务鉴权
# QL_NOTIFY      选填，0 关闭推送
# ------------------------------------------
# 已实现：
# 1. 多账号；缺变量报错；单号失败不中断
# 2. code 换 accessToken；每次 code 登录，不缓存 token
# 3. /user/sign/condition 查签到状态；/user/sign/save 执行签到
# 4. send_notify 统一简报；openid/accessToken 脱敏；业务结果写清奈雪币
#
# 契约（小程序 appid wxab7430e6e8b9a4ab，host tm-api.pin-dao.cn）：
# 来源    抓包（Reqable）+ 小程序包反编译还原，平台参数见下方默认值
# code    POST {wx_server_url}/wx/code auth:{wx_auth} json:{openid,appid}
# 登录    POST https://tm-api.pin-dao.cn/passport/authenticate/wxapp/verify/grc
#         body {type:3, wxappCode:<code>}
#         -> data.accessToken / data.openId / data.unionId / data.firstLogin
# 会话    header Authorization: Bearer {accessToken}
# 请求体  {common:{...签名}, params:{...业务参数}}  统一 envelope
# 签到查询 POST /user/sign/condition  params {signDate}
# 签到     POST /user/sign/save       params {signDate}
# 签到记录 POST /user/sign/records    params {signDate, startDate}
# 用户     POST /user/base-userinfo
# 奈雪币   POST /user/coin/info
# 判定     code==0 为成功（sign/save 成功仅返回 code=0）
# signDate 格式 Y-m-d，月/日【不补零】（例 2026-9-21），与 H5 源码一致
# 签名     common 必带 openId / timestamp / nonce / signature
#          msg  = encodeURI("nonce={nonce}&openId={openId}&timestamp={timestamp}")
#          sign = base64(HmacSHA1(msg, KEY))
#          KEY 默认 iYkcDMF8Eti255hti5M629RQTQ4Z2XihG
#          当 brand∈{26000252,26000254} 且 common.platform=="wxapp"
#          且 common.version>=4.2 时改用 sArMTldQ9tqU19XIRDMWz7BO5WaeBnrezA
#          （本脚本不带 version，走默认 KEY）
#
# 平台业务参数（包内/抓包回填，非账号密钥）：
#   appid=wxab7430e6e8b9a4ab   brand=26000252   tenantId=1
#   H5 固定签名 openId=QL6ZOftGzbziPlZwfiXM（仅参与签名串，非用户身份）
#   H5 域名 tm-web.pin-dao.cn   接口域名 tm-api.pin-dao.cn
#
# 踩坑：
# 1. 签到页是 H5（/naixue/sign-in），接口在 tm-api，不在小程序 app-service.js 里
# 2. signDate 月/日不补零：2026-9-21，写成 2026-09-21 会被判为无效日期
# 3. 登录 type 固定为 3；wxappCode 是 wx.login 的 code（取码服务给的就是它）
# 4. 平台业务参数写脚本默认值；账号 openid/wx_server_url/wx_auth 只进环境变量
# 5. 日志脱敏 openid/accessToken
# ------------------------------------------
# */

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import random
import re
import sys
import time
import urllib.parse
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import requests
import urllib3
urllib3.disable_warnings()

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    from send_notify import clean_line, format_report, mask_phone, notify_and_format
except Exception:
    def clean_line(line: Any) -> str:
        s = str(line or "").strip()
        return "" if s.startswith(("{", "[")) else s[:180]

    def mask_phone(phone: Any) -> str:
        d = re.sub(r"\D", "", str(phone or ""))
        return (d[:3] + "****" + d[-4:]) if len(d) >= 11 else (d or "-")

    def format_report(task, accounts, push_result="", cost_s=None):
        return task

    def notify_and_format(task, accounts, **kwargs):
        print("🔔 推送结果：跳过（send_notify 不可用）")


APP_NAME = "奈雪的茶"
APPID = "wxab7430e6e8b9a4ab"
BRAND = 26000252
TENANT_ID = 1
HOST = "https://tm-api.pin-dao.cn"
LOGIN_URL = f"{HOST}/passport/authenticate/wxapp/verify/grc"
SIGN_CONDITION_URL = f"{HOST}/user/sign/condition"
SIGN_SAVE_URL = f"{HOST}/user/sign/save"
SIGN_RECORDS_URL = f"{HOST}/user/sign/records"
USER_INFO_URL = f"{HOST}/user/base-userinfo"
COIN_INFO_URL = f"{HOST}/user/coin/info"
REQUEST_TIMEOUT = 30

LOGIN_TYPE = 3
H5_SIGN_OPENID = "QL6ZOftGzbziPlZwfiXM"
SIGN_KEY_DEFAULT = "iYkcDMF8Eti255hti5M629RQTQ4Z2XihG"
SIGN_KEY_BRAND = "sArMTldQ9tqU19XIRDMWz7BO5WaeBnrezA"

UA = (
    "Mozilla/5.0 (Linux; Android 17; 2509FPN0BC Build/CP2A.260605.016; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/150.0.7871.189 "
    "Mobile Safari/537.36 MicroMessenger/8.0.76.3141(0x28004C31) "
    "WeChat/arm64 Weixin NetType/WIFI Language/zh_CN ABI/arm64 MiniProgramEnv/android"
)


def say(msg: str) -> None:
    line = clean_line(msg)
    if line:
        print(line)


def mask_id(value: Any, keep: int = 6) -> str:
    s = str(value or "")
    return (s[:keep] + "***") if len(s) > keep else (s or "-")


def mask_token(token: str) -> str:
    s = str(token or "")
    return f"有(len={len(s)})" if s else "无"


def split_openids(raw: str) -> List[str]:
    out: List[str] = []
    for part in (raw or "").replace("&", "\n").splitlines():
        s = part.strip()
        if not s or s.startswith("#"):
            continue
        out.append(s.split("#")[0].strip())
    return out


def direct_session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    return s


def http(method: str, url: str, **kwargs) -> requests.Response:
    kwargs.setdefault("timeout", REQUEST_TIMEOUT)
    kwargs.setdefault("verify", False)
    return direct_session().request(method, url, **kwargs)


def dumps(payload: Dict[str, Any]) -> str:
    return json.dumps(payload if payload is not None else {}, separators=(",", ":"), ensure_ascii=False)


def today_sign_date() -> str:
    """H5 源码：`${Y}-${M+1}-${D}`，月/日不补零。"""
    t = datetime.now()
    return f"{t.year}-{t.month}-{t.day}"


def build_sign_common() -> Dict[str, Any]:
    """构造 common 签名块（openId / timestamp / nonce / signature）。"""
    ts = int(time.time())
    nonce = random.randint(0, 999999)
    raw = f"nonce={nonce}&openId={H5_SIGN_OPENID}&timestamp={ts}"
    msg = urllib.parse.quote(raw, safe="=&")
    key = SIGN_KEY_DEFAULT
    sig = base64.b64encode(
        hmac.new(key.encode("utf-8"), msg.encode("utf-8"), hashlib.sha1).digest()
    ).decode("utf-8")
    return {"openId": H5_SIGN_OPENID, "timestamp": ts, "nonce": nonce, "signature": sig}


def envelope(biz_params: Dict[str, Any]) -> Dict[str, Any]:
    """统一请求体：{common:签名, params:业务参数}。"""
    params: Dict[str, Any] = {"brand": BRAND, "tenantId": TENANT_ID, "appId": APPID}
    params.update(biz_params or {})
    return {"common": build_sign_common(), "params": params}


def biz_headers(token: str = "") -> Dict[str, str]:
    h = {
        "User-Agent": UA,
        "content-type": "application/json",
        "Accept": "*/*",
        "Referer": f"https://servicewechat.com/{APPID}/264/page-frame.html",
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def api_post(url: str, payload: Dict[str, Any], token: str = "") -> Dict[str, Any]:
    try:
        r = http("POST", url, headers=biz_headers(token), data=dumps(payload).encode("utf-8"))
        data = r.json() if r.content else {}
        if not isinstance(data, dict):
            data = {"code": -1, "message": clean_line(data)}
        data["_http"] = r.status_code
        return data
    except Exception as e:
        msg = clean_line(e) or str(e)
        kind = "网络不可达"
        if re.search(r"ssl|certificate", msg, re.I):
            kind = "HTTPS异常"
        elif re.search(r"timeout|timed out", msg, re.I):
            kind = "请求超时"
        return {"code": -1, "message": f"{kind}: {msg[:80]}"}


def err_msg(data: Any) -> str:
    if not isinstance(data, dict):
        return clean_line(data) or "-"
    for k in ("message", "msg", "errorMsg", "error"):
        if data.get(k):
            return str(data.get(k))
    return ""


def is_ok(data: Any) -> bool:
    return isinstance(data, dict) and str(data.get("code", "-1")) == "0"


def get_wx_code(openid: str) -> str:
    base = os.getenv("wx_server_url", "").strip().rstrip("/")
    auth = os.getenv("wx_auth", "").strip()
    if not base or not auth:
        raise RuntimeError("缺少 wx_server_url / wx_auth")
    r = direct_session().post(
        f"{base}/wx/code",
        json={"openid": openid, "appid": APPID},
        headers={"auth": auth, "User-Agent": UA},
        timeout=20,
    )
    data = r.json()
    if not data.get("status"):
        raise RuntimeError(f"取码失败: {data.get('message') or data.get('code')}")
    code = str(((data.get("data") or {}).get("code") or "")).strip()
    if not code:
        raise RuntimeError("取码未返回 code")
    return code


def login_by_code(openid: str) -> Tuple[Optional[str], Dict[str, Any]]:
    print("🔐 使用 code 登录")
    code = get_wx_code(openid)
    print(f"ℹ️ 取码结果 code={'有' if code else '无'}")
    payload = {"type": LOGIN_TYPE, "wxappCode": code}
    data = api_post(LOGIN_URL, payload, token="")
    bag = data.get("data") if isinstance(data.get("data"), dict) else {}
    token = str(bag.get("accessToken") or "").strip()
    print(
        f"ℹ️ 登录响应 code={data.get('code')} msg={clean_line(err_msg(data))[:40] or ('ok' if token else '无')} "
        f"openId={'有' if bag.get('openId') else '无'} token={mask_token(token)}"
    )
    return (token or None), data


def query_user(token: str) -> Tuple[str, str]:
    data = api_post(USER_INFO_URL, envelope({}), token)
    if not is_ok(data):
        say(f"⚠️ 用户信息: code={data.get('code')} msg={err_msg(data)[:40]}")
        return "未知用户", ""
    bag = data.get("data") if isinstance(data.get("data"), dict) else {}
    name = str(bag.get("nickName") or bag.get("nickname") or bag.get("phone") or "未知用户")
    mobile = str(bag.get("phone") or "")
    return name, mobile


def query_coin(token: str) -> str:
    data = api_post(COIN_INFO_URL, envelope({}), token)
    if not is_ok(data):
        return "-"
    bag = data.get("data")
    if isinstance(bag, dict):
        for k in ("coinNum", "balance", "coin", "totalCoin", "amount"):
            if bag.get(k) is not None:
                return str(bag.get(k))
    elif bag is not None:
        return str(bag)
    return "-"


def query_sign_condition(token: str, sign_date: str) -> Dict[str, Any]:
    data = api_post(SIGN_CONDITION_URL, envelope({"signDate": sign_date}), token)
    if not is_ok(data):
        say(f"⚠️ 签到查询: code={data.get('code')} msg={err_msg(data)[:50]}")
        return {}
    return data.get("data") if isinstance(data.get("data"), dict) else {}


def do_sign(token: str, sign_date: str) -> Tuple[bool, str, str]:
    """返回 (是否成功, 简报文案, 奖励描述)。"""
    cond = query_sign_condition(token, sign_date)
    if isinstance(cond, dict):
        for k in ("signFlag", "signed", "hasSign", "isSign", "todaySign"):
            if cond.get(k) in (True, 1, "1", "true"):
                return True, "今日已签到 ✅", "已签到"
    data = api_post(SIGN_SAVE_URL, envelope({"signDate": sign_date}), token)
    if not is_ok(data):
        msg = err_msg(data) or str(data.get("code"))
        return False, f"{msg[:40]} ❌", ""
    bag = data.get("data") if isinstance(data.get("data"), dict) else {}
    reward = ""
    for k in ("coinNum", "coin", "point", "score", "growValue", "rewardNum"):
        if bag.get(k) is not None:
            reward = f"奈雪币+{bag.get(k)}" if k in ("coinNum", "coin") else f"{bag.get(k)}"
            break
    if not reward:
        for k in ("rewardDesc", "rewardName", "desc", "message"):
            if bag.get(k):
                reward = str(bag.get(k))
                break
    return True, (f"签到成功 ✅ {reward}" if reward else "签到成功 ✅"), reward


def run_account(openid: str, index: int, total: int) -> Dict[str, Any]:
    extras: List[str] = [f"openid：{mask_id(openid, 6)}", "code登录"]
    acc: Dict[str, Any] = {
        "account": f"账号{index}",
        "phone": "",
        "status": "-",
        "reward": "-",
        "extra": extras,
        "error": "",
        "success": False,
    }
    print(f"━━━━━━━━━━━━━━━━━━━━\n👤 账号 {index}/{total} {mask_id(openid, 6)}\n━━━━━━━━━━━━━━━━━━━━")

    def _fail(msg: str, status: str) -> Dict[str, Any]:
        acc["status"] = status
        acc["error"] = msg
        acc["success"] = False
        return acc

    try:
        token, _raw = login_by_code(openid)
        if not token:
            return _fail("登录失败", "登录失败 ❌")
        say(f"✅ 登录成功 token={mask_token(token)}")
        name, mobile = query_user(token)
        if not (name or "").strip() or (name or "").strip() in {"ㅤ", "-", "未知用户"}:
            name = f"账号{index}"
        acc["account"] = name or acc["account"]
        acc["phone"] = mobile
        extras.append(f"用户 {name} {mask_phone(mobile) if mobile else '-'}")
        coin_before = query_coin(token)
        extras.append(f"奈雪币 {coin_before}")
        time.sleep(0.4)
        sign_date = today_sign_date()
        print(f"ℹ️ 签到日期 {sign_date}")
        ok, status, reward = do_sign(token, sign_date)
        acc["status"] = status
        acc["success"] = ok
        acc["reward"] = reward or ("已签到" if ok else "-")
        if ok:
            time.sleep(0.4)
            coin_after = query_coin(token)
            if coin_after != "-" and coin_before != "-" and coin_after != coin_before:
                extras.append(f"奈雪币 {coin_before}→{coin_after}")
        if not ok:
            acc["error"] = status
            if re.search(r"登录|token|未授权|请登录|失效", status, re.I):
                say("⚠️ 业务鉴权失败：请确认 naixue 为 wxab7430e6e8b9a4ab 下的 openid，且手机端已完成会员授权")
        return acc
    except Exception as e:
        msg = clean_line(e) or str(e)
        say(f"❌ {msg}")
        return _fail(msg, f"失败 ❌ ({msg[:40]})")


def main() -> int:
    started = time.time()
    openids = split_openids(os.getenv("naixue", "").strip())
    if not openids:
        print("❌ 未配置 naixue 环境变量（openid，多账号换行或 &）")
        return 1
    print(
        f"==============================\n🧋 任务名称：{APP_NAME} 每日签到\n"
        f"📌 平台：奈雪会员 tm-api.pin-dao.cn appid={APPID}\n------------------------------"
    )
    accounts: List[Dict[str, Any]] = []
    for i, openid in enumerate(openids, 1):
        accounts.append(run_account(openid, i, len(openids)))
        if i < len(openids):
            time.sleep(1)
    cost = int(time.time() - started)
    ok_n = sum(1 for a in accounts if a.get("success"))
    print("------------------------------")
    for a in accounts:
        mark = "✅" if a.get("success") else "❌"
        print(f"{mark} {a.get('account')} | {a.get('status')} | {a.get('reward')}")
    print(f"------------------------------\n📊 成功 {ok_n}/{len(accounts)} | 耗时 {cost}s\n==============================")
    try:
        notify_and_format(
            f"{APP_NAME}签到",
            accounts,
            title=f"{APP_NAME}签到 {ok_n}/{len(accounts)}",
            start_ts=started,
        )
    except Exception:
        print(
            format_report(
                f"{APP_NAME}签到",
                accounts,
                push_result="推送模块异常",
                cost_s=time.time() - started,
            )
        )
    return 0 if ok_n else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n⏹️ 用户中断")
        raise SystemExit(130)
