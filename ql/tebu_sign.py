#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# /*
# ------------------------------------------
# @Author: onijiang0
# @Date: 2026.09.21
# @Description: 特步会员中心 - 微盟OneCRM 签到/积分
# cron: 16 14 * * *
# #定时使用10-19点 随机时间 每天
# ------------------------------------------
# 变量名：tebu
# 变量值：业务 openid（wx40915bdd108116da 下），多账号换行或 & 分隔，可加 #备注
#
# 依赖变量：
# wx_server_url  必填，取码服务地址（使用者自备，勿写进仓库）
# wx_auth        必填，取码服务鉴权
# QL_NOTIFY      选填，0 关闭推送
# ------------------------------------------
# 已实现：
# 1. 多账号；缺变量报错；单号失败不中断
# 2. code 换 x-wx-token；每次 code 登录不缓存 token
# 3. signMainInfo 查签到状态/规则；core/c/sign 执行签到
# 4. send_notify 统一简报；openid/token 脱敏；业务结果写清积分
#
# 契约（appid wx40915bdd108116da，微盟 xapi.weimob.com）：
# code     POST {wx_server_url}/wx/code auth:{wx_auth} json:{openid,appid}
# 登录     POST https://xapi.weimob.com/fe/mapi/user/loginX
#          body = 包内 form + {code} -> data.token / data.wid
# 会话     header x-wx-token / X-WX-Token
# 业务域   https://xapi.weimob.com/api3
# 签到查询 POST /api3/onecrm/mactivity/sign/misc/sign/activity/c/signMainInfo
# 签到     POST /api3/onecrm/mactivity/sign/misc/sign/activity/core/c/sign
#          body 统一 envelope（appid+basicInfo+extendInfo+customInfo.wid）
#          -> errcode=0 且 data.fixedReward.points
# 用户     POST /api3/onecrm/user/center/usercenter/queryUserInfo
# 判定     errcode==0；token 字段为 data.token
# 平台参数（包内/抓包回填，非账号密钥）：
#   bosId=4020381477530 cid=113678530
#   CRM productInstanceId=996176530 productId=146
#   vid=6017536855530 merchantId=2000025069530
#   wxTemplateId=8306 bosTemplateId=1000002354
#
# 踩坑：
# 1. 真实业务在微盟 OneCRM，不是 vecrp 商城；勿再打 mall-mobile-v6
# 2. openid 必须是 wx40915 下的，与旧 vecrp openid 不是同一身份
# 3. 登录不缓存 token；无缓存时不打印「token缓存登录」
# 4. 平台业务参数写脚本默认值；账号 openid/wx_server_url/wx_auth 只进环境变量
# 5. 日志脱敏 openid/token；签到成功写清积分
# ------------------------------------------
# */

from __future__ import annotations

import json
import os
import re
import sys
import time
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


APP_NAME = "特步"
APPID = "wx40915bdd108116da"
HOST = "https://xapi.weimob.com"
API3 = f"{HOST}/api3"
LOGIN_URL = f"{HOST}/fe/mapi/user/loginX"
SIGN_MAIN_URL = f"{API3}/onecrm/mactivity/sign/misc/sign/activity/c/signMainInfo"
SIGN_DO_URL = f"{API3}/onecrm/mactivity/sign/misc/sign/activity/core/c/sign"
USER_INFO_URL = f"{API3}/onecrm/user/center/usercenter/queryUserInfo"
REQUEST_TIMEOUT = 30

# 包内/抓包回填的平台业务参数（非账号信息）
BOS_ID = "4020381477530"
CID = "113678530"
FORM_VID = "6015041824530"
CRM_VID = "6017536855530"
EC_PRODUCT_INSTANCE_ID = "996607530"
CRM_PRODUCT_ID = "146"
CRM_PRODUCT_INSTANCE_ID = "996176530"
MERCHANT_ID = "2000025069530"
WX_TEMPLATE_ID = "8306"
BOS_TEMPLATE_ID = "1000002354"
CHILD_TEMPLATE_IDS = [
    {"customId": 90004, "version": "crm@0.1.106"},
    {"customId": 90002, "version": "ec@90.4"},
    {"customId": 90006, "version": "hudong@0.0.255"},
    {"customId": 90008, "version": "cms@0.0.539"},
    {"customId": 90070, "version": "1.0.44"},
]

UA = (
    "Mozilla/5.0 (Linux; Android 17; 2509FPN0BC Build/CP2A.260605.016; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/150.0.7871.189 "
    "Mobile Safari/537.36 MicroMessenger/8.0.76.3141(0x28004C31) "
    "WeChat/arm64 Weixin NetType/WIFI Language/zh_CN ABI/arm64 MiniProgramEnv/android"
)

_LOGIN_SESSION: Dict[str, Any] = {}


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


def err_msg(data: Any) -> str:
    if not isinstance(data, dict):
        return clean_line(data) or "-"
    for k in ("errmsg", "msg", "message", "errorMsg"):
        if data.get(k):
            return str(data.get(k))
    return ""


def weimob_ok(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    return str(data.get("errcode", "0")) == "0"


def form_login_payload(code: str) -> Dict[str, Any]:
    return {
        "appid": APPID,
        "basicInfo": {
            "bosId": BOS_ID,
            "cid": CID,
            "productInstanceId": EC_PRODUCT_INSTANCE_ID,
            "tcode": "weimob",
            "vid": FORM_VID,
        },
        "env": "production",
        "extendInfo": {
            "analysis": [],
            "bosTemplateId": int(BOS_TEMPLATE_ID) if BOS_TEMPLATE_ID.isdigit() else BOS_TEMPLATE_ID,
            "childTemplateIds": CHILD_TEMPLATE_IDS,
            "quickdeliver": {"enable": False},
            "wxTemplateId": int(WX_TEMPLATE_ID) if WX_TEMPLATE_ID.isdigit() else WX_TEMPLATE_ID,
            "youshu": {"enable": False},
        },
        "is_pre_fetch_open": True,
        "parentVid": 0,
        "pid": "",
        "storeId": "",
        "code": code,
    }


def form_biz_payload(wid: str, refer: str = "onecrm-signgift") -> Dict[str, Any]:
    return {
        "appid": APPID,
        "basicInfo": {
            "vid": CRM_VID,
            "vidType": 10,
            "bosId": BOS_ID,
            "productId": int(CRM_PRODUCT_ID) if str(CRM_PRODUCT_ID).isdigit() else CRM_PRODUCT_ID,
            "productInstanceId": int(CRM_PRODUCT_INSTANCE_ID) if str(CRM_PRODUCT_INSTANCE_ID).isdigit() else CRM_PRODUCT_INSTANCE_ID,
            "productVersionId": "14026",
            "merchantId": int(MERCHANT_ID) if str(MERCHANT_ID).isdigit() else MERCHANT_ID,
            "tcode": "weimob",
            "cid": CID,
        },
        "extendInfo": {
            "wxTemplateId": int(WX_TEMPLATE_ID) if WX_TEMPLATE_ID.isdigit() else WX_TEMPLATE_ID,
            "analysis": [],
            "bosTemplateId": int(BOS_TEMPLATE_ID) if BOS_TEMPLATE_ID.isdigit() else BOS_TEMPLATE_ID,
            "childTemplateIds": CHILD_TEMPLATE_IDS,
            "quickdeliver": {"enable": False},
            "youshu": {"enable": False},
            "source": 1,
            "channelsource": 5,
            "refer": refer,
            "mpScene": 1089,
        },
        "queryParameter": {},
        "i18n": {"language": "zh", "timezone": "8"},
        "pid": "",
        "storeId": "",
        "customInfo": {"source": 0, "wid": wid},
    }


def biz_headers(token: str, wid: str = "") -> Dict[str, str]:
    h = {
        "User-Agent": UA,
        "Content-Type": "application/json;charset=utf-8",
        "Accept": "*/*",
        "Referer": f"https://servicewechat.com/{APPID}/264/page-frame.html",
        "host": "xapi.weimob.com",
        "cloud-bosid": BOS_ID,
        "weimob-bosid": BOS_ID,
        "weimob-pid": "N/A",
        "x-biz-id": "146",
        "x-req-from": "onecrm",
        "x-page-route": "onecrm/signgift",
        "x-component-is": "onecrm/signgift",
        "x-wmsdk-vid": CRM_VID,
        "x-wmsdk-close-store": "v2",
        "charset": "utf-8",
    }
    if token:
        h["x-wx-token"] = token
        h["X-WX-Token"] = token
    return h


def api_post(path: str, payload: Dict[str, Any], token: str = "", wid: str = "") -> Dict[str, Any]:
    url = path if path.startswith("http") else f"{API3}{path}"
    headers = biz_headers(token, wid)
    body_str = dumps(payload)
    try:
        r = http("POST", url, headers=headers, data=body_str.encode("utf-8"))
        data = r.json() if r.content else {}
        if not isinstance(data, dict):
            data = {"errcode": -1, "errmsg": clean_line(data)}
        data["_http"] = r.status_code
        return data
    except Exception as e:
        msg = clean_line(e) or str(e)
        kind = "网络不可达"
        if re.search(r"ssl|certificate", msg, re.I):
            kind = "HTTPS异常"
        elif re.search(r"timeout|timed out", msg, re.I):
            kind = "请求超时"
        return {"errcode": -1, "errmsg": f"{kind}: {msg[:80]}"}


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


def extract_session(data: Any) -> Dict[str, Any]:
    """从 loginX / login 响应里取 token/wid/openId。"""
    if not isinstance(data, dict):
        return {}
    bag = data.get("data") if isinstance(data.get("data"), dict) else data
    if not isinstance(bag, dict):
        return {}
    inner = bag.get("result") if isinstance(bag.get("result"), dict) else bag
    if not isinstance(inner, dict):
        inner = bag
    token = (
        inner.get("token")
        or inner.get("xWxToken")
        or inner.get("x-wx-token")
        or bag.get("token")
    )
    wid = inner.get("wid") or bag.get("wid") or ""
    open_id = inner.get("openId") or inner.get("openid") or bag.get("openId") or ""
    return {
        "token": str(token or ""),
        "wid": str(wid or ""),
        "openId": str(open_id or ""),
        "raw": data,
    }


def login_by_code(openid: str) -> Tuple[Optional[str], Dict[str, Any]]:
    print("🔐 使用 code 登录")
    code = get_wx_code(openid)
    print(f"ℹ️ 取码结果 code={'有' if code else '无'}")
    payload = form_login_payload(code)
    data = api_post(LOGIN_URL, payload, token="")
    sess = extract_session(data)
    token = sess.get("token") or ""
    _LOGIN_SESSION.update(sess)
    print(
        f"ℹ️ 登录响应 errcode={data.get('errcode')} msg={clean_line(err_msg(data))[:40] or ('ok' if token else '无')} "
        f"openId={'有' if sess.get('openId') else '无'} wid={mask_id(sess.get('wid'), 4) if sess.get('wid') else '无'} "
        f"token={mask_token(token)}"
    )
    if not token:
        # 兜底：passport/access/access
        alt = {
            "appid": APPID,
            "basicInfo": {
                "bosId": BOS_ID,
                "cid": CID,
                "productInstanceId": EC_PRODUCT_INSTANCE_ID,
                "tcode": "weimob",
                "vid": FORM_VID,
            },
            "code": code,
        }
        data2 = api_post("/passport/access/access", alt, token="")
        sess2 = extract_session(data2)
        if sess2.get("token"):
            print(f"ℹ️ passport/access 兜底成功 token={mask_token(sess2['token'])}")
            _LOGIN_SESSION.update(sess2)
            return sess2["token"], data2
        print(f"ℹ️ passport/access errcode={data2.get('errcode')} msg={clean_line(err_msg(data2))[:40]}")
        return None, data if isinstance(data, dict) else {"errmsg": "登录响应未返回 token"}
    return token, data if isinstance(data, dict) else {}


def query_user(token: str, wid: str) -> Tuple[str, str]:
    payload = form_biz_payload(wid, refer="onecrm-usercenter")
    data = api_post("/onecrm/user/center/usercenter/queryUserInfo", payload, token, wid)
    if not weimob_ok(data):
        say(f"⚠️ 用户信息: errcode={data.get('errcode')} msg={err_msg(data)[:40]}")
        return "未知用户", ""
    bag = data.get("data") if isinstance(data.get("data"), dict) else {}
    customer = bag.get("customer") or bag.get("userInfo") or bag
    if not isinstance(customer, dict):
        customer = {}
    name = str(customer.get("customerNick") or customer.get("nickName") or customer.get("nickname") or "未知用户")
    mobile = str(customer.get("customerMobile") or customer.get("mobile") or customer.get("phone") or "")
    return name, mobile


def query_sign_info(token: str, wid: str) -> Dict[str, Any]:
    payload = form_biz_payload(wid)
    data = api_post(SIGN_MAIN_URL, payload, token, wid)
    if not weimob_ok(data):
        say(f"⚠️ 签到查询: errcode={data.get('errcode')} msg={err_msg(data)[:50]}")
        return {}
    return data.get("data") if isinstance(data.get("data"), dict) else {}


def do_sign(token: str, wid: str) -> Tuple[bool, str, Optional[float]]:
    info = query_sign_info(token, wid)
    has = info.get("hasSign") if info else None
    if has is True:
        say("ℹ️ 今日已签到")
        return True, "今日已签到 ✅", None
    if info:
        rewards = info.get("signForwardMsg") or []
        if isinstance(rewards, list) and rewards:
            print(f"ℹ️ 签到规则 {rewards}")
    payload = form_biz_payload(wid)
    data = api_post(SIGN_DO_URL, payload, token, wid)
    msg = err_msg(data) or ""
    print(f"ℹ️ 签到响应 errcode={data.get('errcode')} msg={msg[:60] or 'ok'}")
    if not weimob_ok(data):
        return False, f"{msg[:40] or data.get('errcode')} ❌", None
    bag = data.get("data") if isinstance(data.get("data"), dict) else {}
    fixed = bag.get("fixedReward") if isinstance(bag.get("fixedReward"), dict) else {}
    points = fixed.get("points")
    point_name = bag.get("pointName") or "积分"
    try:
        gain = float(points) if points is not None else None
    except (TypeError, ValueError):
        gain = None
    if gain is not None:
        return True, f"签到成功 ✅ {point_name}+{gain:g}", gain
    return True, f"签到成功 ✅ {point_name}", None


def query_points_hint(info: Dict[str, Any]) -> str:
    if not isinstance(info, dict):
        return "-"
    for key in ("monthCumulativeSignDays", "activityCumulativeSignDays"):
        if info.get(key) is not None:
            return f"累计签到{info.get(key)}天"
    return "-"


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
        _LOGIN_SESSION.clear()
        token, _raw = login_by_code(openid)
        if not token:
            return _fail("登录失败", "登录失败 ❌")
        say(f"✅ 登录成功 token={mask_token(token)}")
        wid = str(_LOGIN_SESSION.get("wid") or "")
        if not wid:
            say("⚠️ 登录未返回 wid，业务请求可能失败")
        name, mobile = query_user(token, wid)
        if not (name or "").strip() or (name or "").strip() in {"ㅤ", "-", "未知用户"}:
            name = f"账号{index}"
        acc["account"] = name or acc["account"]
        acc["phone"] = mobile
        extras.append(f"用户 {name} {mask_phone(mobile) if mobile else '-'}")
        time.sleep(0.5)
        info = query_sign_info(token, wid)
        extras.append(f"签到状态 {query_points_hint(info)}")
        ok, status, gain = do_sign(token, wid)
        acc["status"] = status
        acc["success"] = ok
        if gain is not None:
            acc["reward"] = f"+{gain:g} 积分"
        elif ok:
            acc["reward"] = "已签到"
        if not ok:
            acc["error"] = status
            if re.search(r"登录|token|未授权|请登录", status, re.I):
                say("⚠️ 业务鉴权失败：请确认 tebu 为 wx40915 小程序下的 openid，且手机端已完成会员授权")
        return acc
    except Exception as e:
        msg = clean_line(e) or str(e)
        say(f"❌ {msg}")
        return _fail(msg, f"失败 ❌ ({msg[:40]})")


def main() -> int:
    started = time.time()
    openids = split_openids(os.getenv("tebu", "").strip())
    if not openids:
        print("❌ 未配置 tebu 环境变量（openid，多账号换行或 &）")
        return 1
    print(f"==============================\n🛒 任务名称：{APP_NAME}签到\n📌 平台：微盟 OneCRM appid={APPID}\n------------------------------")
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
