#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# /*
# ------------------------------------------
# @Author: onijiang0
# @Date: 2026.09.21
# @Description: 七彩虹商城 - 小程序每日签到/积分
# cron: 36 15 * * *
# #定时使用10-19点 随机时间 每天
# ------------------------------------------
# 变量名：qch
# 变量值：账号，多账号换行或 & 分隔，可加 #备注
#   仅 openid：脚本自动 code→token（OnLogin + 手机号 code → DecryptPhoneNumber）
#   openid#userToken 或 openid#userToken#refreshToken：直接用抓包登录态
#   也可只写 userToken（配合 qch_refresh 可选）
#
# 依赖变量：
# wx_server_url  必填，取码服务地址（使用者自备，勿写进仓库）
# wx_auth        必填，取码服务鉴权（/wx/code 与 /wx/getphonenumber）
# qch_appid      可选，默认 wx49018277e65fc3e1
# qch_refresh    可选，RefreshToken（单账号 token 模式）
# QL_NOTIFY      选填，0 关闭推送
# ------------------------------------------
# 已实现：
# 1. 多账号；缺变量报错；单号失败不中断
# 2. code 换 token 全链路；token 本地缓存；失效自动重登
# 3. 查签到状态/日历/积分；未签则签到；短日志+脱敏
# 4. send_notify 统一简报；不打印原始 JSON
#
# 契约（appid wx49018277e65fc3e1，host interface.skycolorful.com）：
# code     POST {wx_server_url}/wx/code  auth:{wx_auth}  json:{openid,appid}
#          -> data.code
# 登录     POST /api/User/OnLogin  json:{Code}
#          -> Data.OpenId（实测无 Token）
# 手机号码 POST {wx_server_url}/wx/getphonenumber  auth:{wx_auth}
#          json:{openid,appid} -> data.code / data.raw.{code,encryptedData,iv}
# 换token  POST /api/User/DecryptPhoneNumber
#          json:{OpenId, Code} 或 {OpenId, Code, Iv, encryptedData}
#          -> Data.Token / Data.RefreshToken
# 签到     POST /api/User/SignV2
# 签到状态 GET  /api/User/IsSignV2      -> Data.IsSign
# 签到日历 GET  /api/User/SignDaysV2    -> Data.DataList
# 用户     GET  /api/User/GetUserInfo   -> Data.UserExpPoint.Points / Phone / NickName
# 积分     GET  /api/User/GetUserPoint
#
# 公共请求头（每次请求）：
#   Authorization: Bearer {userToken}
#   X-Authorization: Bearer {refreshToken}
#   User-from: xcx  source: Wx  UcSource: 30  version: 2.0.0  tenant-id: 1
#   AppId / Ticks / requestId / Sign
#   Sign = md5(AppId + Ticks + requestId + signSecret)
#   AppId=815d8026-9a52-4445-a42c-a5443134232e
#   signSecret=2b5c01fb-7640-401a-8188-43a13190a626
#
# 成功判定：HTTP 200 且 body.Success==true 且 Code∈
#   {0,52001,52002,50001,51001,51002,40100,40101}
# 响应头 access-token / x-access-token 可刷新本地 token
#
# 踩坑：
# 1. OnLogin 只回 OpenId，Token 必须 DecryptPhoneNumber（手机号 code）
# 2. 业务网关偶发阿里云 405 HTML（WAF），脚本会明确报错；可稍后重试或填 token
# 3. 分包 CDN 404，任务相关接口不在主包，本脚本只做签到
# 4. 已签：IsSignV2.Data.IsSign=true 视为成功，不重复调 SignV2
# 5. 单次任务内每 openid 尽量只调 1 次 /wx/code；账号间 sleep 防限流
# 6. 请求 Sign=md5(AppId+Ticks+requestId+secret)，每次请求都要新算
# ------------------------------------------
# */

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
import urllib3
urllib3.disable_warnings()

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    from send_notify import (
        clean_line,
        format_report,
        mask_phone,
        notify_and_format,
        send_notify,
    )
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

    def send_notify(title, content):
        print("🔔 推送结果：跳过（send_notify 不可用）")
        return "跳过"


APP_NAME = "七彩虹商城"
APPID = (os.getenv("qch_appid") or "wx49018277e65fc3e1").strip()
BASE_URL = (os.getenv("qch_base") or "https://interface.skycolorful.com").rstrip("/")

# 源码内固定平台参数（vendor.js request 签名）
PLAT_APP_ID = os.getenv("qch_plat_appid") or "815d8026-9a52-4445-a42c-a5443134232e"
SIGN_SECRET = os.getenv("qch_sign_secret") or "2b5c01fb-7640-401a-8188-43a13190a626"
TENANT_ID = os.getenv("qch_tenant") or "1"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) MicroMessenger/7.0.20.1781 MiniProgramEnv/Windows "
    "WindowsWechat/WMPF WindowsWechat"
)
REQUEST_TIMEOUT = 30
CACHE_NAME = "qch_token_cache.json"
SUCCESS_CODES = {0, 52001, 52002, 50001, 51001, 51002, 40100, 40101}
ALREADY_HINTS = ("已签", "重复", "already", "IsSign", "今日已签")


def say(msg: str) -> None:
    line = clean_line(msg)
    if line:
        print(line)


def mask_id(value: Any, keep: int = 8) -> str:
    s = str(value or "")
    return (s[:keep] + "***") if len(s) > 8 else (s or "-")


def parse_accounts(raw: str) -> List[Dict[str, str]]:
    """账号行：openid | openid#token | openid#token#refresh | token"""
    out: List[Dict[str, str]] = []
    for part in (raw or "").replace("&", "\n").splitlines():
        s = part.strip()
        if not s or s.startswith("#"):
            continue
        bits = [b.strip() for b in s.split("#") if b.strip() != "" or b == ""]
        # 支持 openid#token#refresh；token 可能很长
        openid, token, refresh = "", "", ""
        if len(bits) >= 3 and len(bits[1]) > 20:
            openid, token, refresh = bits[0], bits[1], bits[2]
        elif len(bits) == 2 and len(bits[1]) > 20:
            openid, token = bits[0], bits[1]
        elif len(bits) == 1 and len(bits[0]) > 40:
            token = bits[0]
            refresh = os.getenv("qch_refresh", "").strip()
        else:
            openid = bits[0] if bits else ""
            if len(bits) >= 2 and bits[1]:
                token = bits[1]
            if len(bits) >= 3:
                refresh = bits[2]
        if not openid and not token:
            continue
        out.append({"openid": openid, "token": token, "refresh": refresh})
    return out


def sess() -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    s.verify = False
    return s


def md5_lower(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def build_sign_headers(token: str = "", refresh: str = "") -> Dict[str, str]:
    ticks = str(int(time.time() * 1000))
    request_id = str(uuid.uuid4())
    sign = md5_lower(f"{PLAT_APP_ID}{ticks}{request_id}{SIGN_SECRET}")
    h = {
        "User-Agent": UA,
        "Content-Type": "application/json;charset=UTF-8",
        "Accept": "*/*",
        "Authorization": f"Bearer {token}" if token else "",
        "X-Authorization": f"Bearer {refresh}" if refresh else "",
        "User-from": "xcx",
        "source": "Wx",
        "UcSource": "30",
        "version": "2.0.0",
        "tenant-id": TENANT_ID,
        "AppId": PLAT_APP_ID,
        "Ticks": ticks,
        "requestId": request_id,
        "Sign": sign,
        "Referer": f"https://servicewechat.com/{APPID}/1264/page-frame.html",
    }
    return h


def extract_tokens(body: Dict[str, Any], headers: Any = None) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if headers:
        for k in ("access-token", "x-access-token"):
            v = headers.get(k) or headers.get(k.title())
            if v:
                out["access-token" if k == "access-token" else "x-access-token"] = str(v)

    def walk(o: Any, depth: int = 0) -> None:
        if depth > 4 or not isinstance(o, dict):
            return
        for key in (
            "Token", "token", "AccessToken", "accessToken", "userToken",
            "RefreshToken", "refreshToken",
        ):
            if o.get(key) and key.lower().find("refresh") >= 0:
                out.setdefault("refresh", str(o.get(key)))
            elif o.get(key):
                out.setdefault("token", str(o.get(key)))
        for v in o.values():
            if isinstance(v, dict):
                walk(v, depth + 1)

    walk(body)
    data = body.get("Data") if isinstance(body.get("Data"), dict) else {}
    if isinstance(data, dict):
        for k, dest in (("Token", "token"), ("token", "token"),
                        ("RefreshToken", "refresh"), ("refreshToken", "refresh")):
            if data.get(k):
                out.setdefault(dest, str(data.get(k)))
        if data.get("OpenId") or data.get("openid"):
            out["openid"] = str(data.get("OpenId") or data.get("openid"))
    return out


def is_ok_body(body: Any) -> bool:
    if not isinstance(body, dict):
        return False
    code = body.get("Code")
    try:
        code_i = int(code) if code is not None else None
    except Exception:
        code_i = None
    success = body.get("Success")
    if success is True and (code_i in SUCCESS_CODES or code is None):
        return True
    # 兼容 status 壳
    if body.get("status") is True or body.get("Status") is True:
        return True
    return False


def body_msg(body: Any) -> str:
    if not isinstance(body, dict):
        return clean_line(body) or "失败"
    return clean_line(
        body.get("Message") or body.get("message") or body.get("Msg")
        or body.get("msg") or body.get("Code") or "失败"
    )


def is_auth_fail(body: Any, status_code: int = 0) -> bool:
    if status_code == 401:
        return True
    msg = str(body_msg(body) or "")
    return bool(re.search(r"登录|token|授权|未登录|失效|过期|401", msg, re.I))


def load_cache() -> Dict[str, Any]:
    p = Path(__file__).resolve().parent / CACHE_NAME
    try:
        if p.is_file():
            return json.loads(p.read_text(encoding="utf-8") or "{}")
    except Exception:
        pass
    return {}


def save_cache(cache: Dict[str, Any]) -> None:
    p = Path(__file__).resolve().parent / CACHE_NAME
    try:
        p.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def get_wx_code(openid: str) -> str:
    base = os.getenv("wx_server_url", "").strip().rstrip("/")
    auth = os.getenv("wx_auth", "").strip()
    if not base or not auth:
        raise RuntimeError("缺少 wx_server_url / wx_auth")
    r = sess().post(
        f"{base}/wx/code",
        json={"openid": openid, "appid": APPID},
        headers={"auth": auth, "User-Agent": UA},
        timeout=20,
    )
    data = r.json()
    if not data.get("status"):
        raise RuntimeError(f"/wx/code 失败: {data.get('message')}")
    code = str(((data.get("data") or {}).get("code") or "")).strip()
    if not code:
        raise RuntimeError("/wx/code 未返回 code")
    return code


def get_phone_payload(openid: str) -> Dict[str, str]:
    """/wx/getphonenumber -> {code, encryptedData, iv}"""
    base = os.getenv("wx_server_url", "").strip().rstrip("/")
    auth = os.getenv("wx_auth", "").strip()
    if not base or not auth:
        raise RuntimeError("缺少 wx_server_url / wx_auth")
    r = sess().post(
        f"{base}/wx/getphonenumber",
        json={"openid": openid, "appid": APPID},
        headers={"auth": auth, "User-Agent": UA},
        timeout=20,
    )
    try:
        data = r.json()
    except Exception:
        raise RuntimeError(f"/wx/getphonenumber 非 JSON: {clean_line(r.text)[:60]}")
    if not data.get("status"):
        raise RuntimeError(f"/wx/getphonenumber 失败: {data.get('message')}")
    inner = data.get("data") or {}
    if not isinstance(inner, dict):
        raise RuntimeError("/wx/getphonenumber data 异常")
    raw = inner.get("raw") if isinstance(inner.get("raw"), dict) else {}
    code = str(inner.get("code") or raw.get("code") or "").strip()
    return {
        "code": code,
        "encryptedData": str(raw.get("encryptedData") or raw.get("encryptData") or inner.get("encryptedData") or ""),
        "iv": str(raw.get("iv") or raw.get("IV") or inner.get("iv") or ""),
    }


class Colorful:
    def __init__(self, token: str = "", refresh: str = ""):
        self.s = sess()
        self.token = token
        self.refresh = refresh

    def call(self, path: str, method: str = "GET", body: Optional[Dict] = None):
        url = f"{BASE_URL}{path}"
        headers = build_sign_headers(self.token, self.refresh)
        method = method.upper()
        if method == "GET":
            r = self.s.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        else:
            r = self.s.post(url, headers=headers, data=json.dumps(body or {}).encode("utf-8"),
                            timeout=REQUEST_TIMEOUT)
        text = r.text or ""
        if r.status_code == 405 or text.lstrip()[:20].lower().startswith("<!doctype"):
            return r.status_code, {
                "Code": r.status_code,
                "Success": False,
                "Message": "网关 405/WAF 拦截，请稍后重试或改用 openid#token",
            }
        try:
            data = r.json()
        except Exception:
            data = {"Code": r.status_code, "Success": False, "Message": clean_line(text)[:80]}
        for hk, dest in (("access-token", "token"), ("x-access-token", "refresh")):
            hv = r.headers.get(hk) or r.headers.get(hk.title())
            if hv:
                setattr(self, dest, hv)
        return r.status_code, data

    def on_login(self, code: str):
        return self.call("/api/User/OnLogin", "POST", {"Code": code})

    def decrypt_phone(self, openid: str, phone_code: str,
                      encrypted_data: str = "", iv: str = ""):
        body: Dict[str, Any] = {"OpenId": openid, "Code": phone_code}
        if encrypted_data and iv:
            body["Iv"] = iv
            body["encryptedData"] = encrypted_data
        return self.call("/api/User/DecryptPhoneNumber", "POST", body)

    def user_info(self):
        return self.call("/api/User/GetUserInfo")

    def user_point(self):
        return self.call("/api/User/GetUserPoint")

    def is_sign(self):
        return self.call("/api/User/IsSignV2")

    def sign_days(self):
        return self.call("/api/User/SignDaysV2")

    def do_sign(self):
        return self.call("/api/User/SignV2", "POST", {})


def dig(data: Any, *paths: str) -> Any:
    for path in paths:
        cur = data
        ok = True
        for part in path.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                ok = False
                break
        if ok and cur not in (None, ""):
            return cur
    return None


def ensure_login(acct: Dict[str, str], cache: Dict[str, Any]) -> Colorful:
    openid = acct.get("openid") or ""
    manual_token = (acct.get("token") or "").strip()
    manual_refresh = (acct.get("refresh") or "").strip()
    item = cache.get(openid) if openid else {}
    if not isinstance(item, dict):
        item = {}
    token = manual_token or str(item.get("token") or "")
    refresh = manual_refresh or str(item.get("refresh") or "")
    api = Colorful(token, refresh)

    def try_user() -> Optional[Dict[str, Any]]:
        if not api.token:
            return None
        st, body = api.user_info()
        if st == 200 and is_ok_body(body) and isinstance(body.get("Data"), dict):
            return body["Data"]
        return None

    if token:
        info = try_user()
        if info is not None:
            return api
        if openid:
            cache.pop(openid, None)
        api = Colorful("", "")
        token, refresh = "", ""

    if not openid:
        raise RuntimeError("token 无效或已过期；请重新抓包填 qch")

    # 1) wx.login code -> OnLogin -> OpenId
    code = get_wx_code(openid)
    st, body = api.on_login(code)
    toks = extract_tokens(body, None)
    biz_openid = str(toks.get("openid") or "")
    if toks.get("token"):
        api.token = toks["token"]
        api.refresh = toks.get("refresh") or api.refresh

    # 2) 若 OnLogin 未带 token，走手机号 code 换 token
    if not api.token:
        if not biz_openid:
            # OnLogin 失败/WAF 时，仍尝试用取码服务 openid 作 OpenId
            biz_openid = openid
        phone_err = ""
        try:
            phone = get_phone_payload(openid)
        except Exception as e:
            phone = None
            phone_err = clean_line(e) or str(e)
        last_msg = phone_err or "/wx/getphonenumber 无 code"
        if phone and phone.get("code"):
            last_msg = "DecryptPhoneNumber 无 Token"
            for payload in (
                {"openid": biz_openid, "code": phone["code"],
                 "encryptedData": phone.get("encryptedData") or "", "iv": phone.get("iv") or ""},
                {"openid": biz_openid, "code": phone["code"], "encryptedData": "", "iv": ""},
            ):
                st2, body2 = api.decrypt_phone(
                    payload["openid"], payload["code"],
                    payload.get("encryptedData") or "", payload.get("iv") or "",
                )
                t2 = extract_tokens(body2, None)
                last_msg = body_msg(body2)
                if t2.get("token"):
                    api.token = t2["token"]
                    api.refresh = t2.get("refresh") or api.refresh
                    if t2.get("openid"):
                        biz_openid = t2["openid"]
                    break
        if not api.token:
            raise RuntimeError(
                f"code→token 失败：{last_msg[:60]}；"
                f"OpenId={mask_id(biz_openid, 8) or '-'}。"
                f"可小程序登录后抓包，qch 填 openid#token"
            )

    info = try_user()
    if info is None:
        raise RuntimeError(
            f"已拿到 token 但 GetUserInfo 失败（{mask_id(api.token, 8)}）；"
            f"token 可能无效，或网关 405/WAF"
        )
    cache[openid or biz_openid] = {
        "token": api.token,
        "refresh": api.refresh,
        "openid": biz_openid,
        "ts": int(time.time()),
    }
    save_cache(cache)
    return api


def parse_calendar(body: Any) -> Dict[str, Any]:
    data = dig(body, "Data") or {}
    lst = dig(data, "DataList") or dig(data, "data") or []
    days = None
    signed_days = 0
    today_signed = None
    if isinstance(lst, list):
        for it in lst:
            if not isinstance(it, dict):
                continue
            num = it.get("Num") or it.get("num") or it.get("Day")
            flag = it.get("IsSign") if it.get("IsSign") is not None else it.get("Sign")
            if flag in (1, True, "1"):
                signed_days += 1
            if it.get("IsToday") in (1, True, "1") or it.get("Today") in (1, True, "1"):
                today_signed = flag in (1, True, "1")
            if num not in (None, ""):
                try:
                    days = max(days or 0, int(num))
                except Exception:
                    pass
    for k in ("SignDays", "signDays", "Count", "continuity", "Continuity"):
        if data.get(k) not in (None, ""):
            try:
                signed_days = int(data.get(k))
                break
            except Exception:
                pass
    return {
        "list_len": len(lst) if isinstance(lst, list) else 0,
        "signed_days": signed_days,
        "max_num": days,
        "today_signed": today_signed,
    }


def extract_points(info: Optional[Dict[str, Any]], point_body: Any) -> Optional[int]:
    cands = []
    if isinstance(info, dict):
        cands.append(dig(info, "UserExpPoint.Points", "Points", "Point", "points", "integral"))
        uep = info.get("UserExpPoint")
        if isinstance(uep, dict):
            cands.append(uep.get("Points") or uep.get("Point"))
    if isinstance(point_body, dict):
        cands.append(dig(point_body, "Data", "Data.Points", "Data.Point"))
        data = point_body.get("Data")
        if isinstance(data, (int, float)):
            cands.append(data)
        elif isinstance(data, dict):
            cands.append(data.get("Points") or data.get("Point") or data.get("points"))
        elif isinstance(data, list) and data:
            first = data[0]
            if isinstance(first, dict):
                cands.append(first.get("Points") or first.get("Point") or first.get("points"))
    for c in cands:
        try:
            if c is not None and c != "":
                return int(float(c))
        except Exception:
            continue
    return None


def extract_identity(info: Optional[Dict[str, Any]]) -> Dict[str, str]:
    out = {"account": "", "phone": ""}
    if not isinstance(info, dict):
        return out
    nick = (
        dig(info, "NickName", "NickName", "nickname", "UserName", "Phone")
        or info.get("NickName")
        or info.get("NickName")
        or info.get("UserName")
        or ""
    )
    phone = dig(info, "Phone", "Mobile", "mobile") or info.get("Phone") or ""
    # NickName 可能为空
    for k in ("NickName", "Nickname", "nickName", "Name", "RealName"):
        if info.get(k):
            nick = info.get(k)
            break
    out["account"] = clean_line(nick) or ""
    out["phone"] = str(phone or "")
    return out


def run_account(acct: Dict[str, str], idx: int, total: int) -> Dict[str, Any]:
    openid = acct.get("openid") or ""
    show_id = mask_id(openid, 8) if openid else mask_id(acct.get("token"), 6)
    acc: Dict[str, Any] = {
        "account": f"账号{idx}",
        "phone": "",
        "status": "",
        "reward": "-",
        "extra": [],
        "error": "",
        "success": False,
    }
    cache = load_cache()
    try:
        api = ensure_login(acct, cache)
    except Exception as e:
        msg = clean_line(e) or str(e)
        acc["status"] = f"登录失败 ❌ ({msg[:60]})"
        acc["error"] = msg
        acc["extra"] = [f"id：{show_id}"]
        return acc

    extras = acc["extra"]
    try:
        st_info, info_body = api.user_info()
        info = dig(info_body, "Data") if is_ok_body(info_body) else None
        ident = extract_identity(info if isinstance(info, dict) else None)
        if ident["account"]:
            acc["account"] = ident["account"]
        if ident["phone"]:
            acc["phone"] = mask_phone(ident["phone"])
        elif openid and acc["account"].startswith("账号"):
            acc["account"] = f"{acc['account']} ({show_id})"

        # 签到状态
        st_is, is_body = api.is_sign()
        is_sign = bool(dig(is_body, "Data.IsSign", "IsSign", "Data.isSign"))
        if dig(is_body, "Data") in (1, True, "1", "true"):
            is_sign = True

        # 日历
        month_days = None
        try:
            st_cal, cal_body = api.sign_days()
            cal = parse_calendar(cal_body)
            if cal.get("signed_days"):
                month_days = cal["signed_days"]
            if cal.get("today_signed") is True:
                is_sign = True
        except Exception:
            pass

        # 积分（签到前）
        st_pt, pt_body = api.user_point()
        pts_before = extract_points(info if isinstance(info, dict) else None, pt_body if is_ok_body(pt_body) else None)

        status = ""
        ok = False
        reward = "-"

        pts_final = pts_before
        if is_sign:
            status = "今日已签到 ✅"
            ok = True
        else:
            st_sign, sign_body = api.do_sign()
            msg = body_msg(sign_body)
            sign_ok = is_ok_body(sign_body) or (
                st_sign == 200 and any(h in msg for h in ALREADY_HINTS)
            )
            try:
                _, is_body2 = api.is_sign()
                if dig(is_body2, "Data.IsSign") in (1, True, "1"):
                    sign_ok = True
            except Exception:
                pass
            _, info_body2 = api.user_info()
            info2 = dig(info_body2, "Data") if is_ok_body(info_body2) else info
            _, pt_body2 = api.user_point()
            pts_after = extract_points(
                info2 if isinstance(info2, dict) else None,
                pt_body2 if is_ok_body(pt_body2) else None,
            )
            if pts_after is not None:
                pts_final = pts_after
            if pts_before is not None and pts_after is not None:
                delta = pts_after - pts_before
                if delta != 0:
                    reward = f"{delta:+d} 积分"
                elif sign_ok:
                    reward = "0 积分"
            if sign_ok:
                status = "签到成功 ✅"
                if msg and "成功" not in msg and not any(h in msg for h in ALREADY_HINTS):
                    extras.append(msg[:40])
                ok = True
            else:
                status = f"签到失败 ❌ ({msg[:40]})"
                acc["error"] = msg

        if pts_final is not None:
            extras.append(f"当前积分 {pts_final}")
        if month_days is not None:
            extras.append(f"本月签到约 {month_days} 天")
        extras.append(f"id {show_id}")

        acc["status"] = status
        acc["reward"] = reward
        acc["success"] = ok
        return acc
    except Exception as e:
        msg = clean_line(e) or str(e)
        acc["status"] = f"执行异常 ❌ ({msg[:50]})"
        acc["error"] = msg
        acc["extra"] = extras + [f"id {show_id}"]
        return acc
    finally:
        time.sleep(random.uniform(8, 20))


def main() -> int:
    started = time.time()
    accounts = parse_accounts(os.getenv("qch", "").strip())
    if not accounts:
        print("❌ 未配置 qch（openid 或 openid#token，多账号换行或 & 分隔）")
        return 1
    need_code = any(a.get("openid") for a in accounts)
    if need_code and (
        not os.getenv("wx_server_url", "").strip()
        or not os.getenv("wx_auth", "").strip()
    ):
        print("❌ 未配置 wx_server_url / wx_auth（含 openid 的账号需要取码）")
        return 1
    print(f"{APP_NAME} | {len(accounts)}账号 | {APPID}")

    results: List[Dict[str, Any]] = []
    for i, acct in enumerate(accounts, 1):
        try:
            results.append(run_account(acct, i, len(accounts)))
        except Exception as e:
            show = mask_id(acct.get("openid") or acct.get("token"), 8)
            results.append({
                "account": f"账号{i}",
                "phone": "",
                "status": f"执行失败 ❌ ({clean_line(e)[:40]})",
                "reward": "-",
                "extra": [f"id {show}"],
                "error": clean_line(e),
                "success": False,
            })

    ok_n = sum(1 for r in results if r.get("success"))
    try:
        notify_and_format(
            APP_NAME,
            results,
            title=f"{APP_NAME}签到 {ok_n}/{len(results)}",
            start_ts=started,
        )
    except Exception:
        try:
            body = format_report(APP_NAME, results)
            pr = send_notify(f"{APP_NAME}签到 {ok_n}/{len(results)}", body)
            print(format_report(APP_NAME, results, push_result=pr, cost_s=time.time() - started))
        except Exception as ne:
            print("[notify] 跳过:", ne)
            print(format_report(APP_NAME, results, cost_s=time.time() - started))
    return 0 if ok_n == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
