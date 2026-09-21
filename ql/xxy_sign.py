#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# /*
# ------------------------------------------
# @Author: onijiang0
# @Date: 2026.09.20
# @Description: 芯享会（心相印）- openid 换微信 code 登录 + 每日签到
# cron: 35 14 * * *
# ------------------------------------------
# 变量名：xxy
# 变量值：wx_server 里的 openid，多账号用换行或 & 分隔
#
# 依赖变量：
# wx_server_url    必填，wx_server 地址（勿写进仓库）
# wx_auth          必填，wx_server 鉴权值（/wx/code 用）
# xxy_appid        可选，默认 wxfc766f1e9a63b01f
# PROXY_API        可选，HTTP/socks5 代理提取地址
# PROXY_TYPE       可选，http / socks5
# QL_NOTIFY        可选，设为 0 关闭推送
# 注：推送结果由 send_notify 统一短文案输出（如「企业微信推送成功」）
#
# 契约（appid wxfc766f1e9a63b01f）：
# code     POST {wx_server_url}/wx/code  auth:{wx_auth}  json:{openid,appid}
# 登录     POST https://mshopapi.hengan.cn/auth/app/anon/oauth/wxappLogin
#          body {code, spread:0, labelValue:null} -> data.token
# 校验     GET  /mall/app/anon/api/auth/checkAppToken?token=
# 签到     POST /mall/app/sign/user  Bearer token  body {sign:1,integral:1,all:1}
# 积分     POST /mall/app/api/sign/v2/integral
# 本月签到 GET  /mall/app/api/sign/v2/currentMonthSignInfo
# 活动配置 GET  /mall/app/api/sign/v2/currentActiveSignConfig
# 我的奖励 GET  /mall/app/api/sign/v2/mySignRewardList
# 补签次数 GET  /mall/app/api/sign/v2/supplementaryCount
# 注：主包未出现独立「领取累计奖励」写接口；累计档位可能随签到自动发放
# ------------------------------------------
# */

from __future__ import annotations

import json
import logging
import os
import random
import re
import sys
import time
import traceback
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import requests
import urllib3
urllib3.disable_warnings()

logging.basicConfig(
    level=logging.DEBUG if os.getenv("xxy_debug") else logging.WARNING,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
log = logging.getLogger("XXY")

APP_NAME = "芯享会（心相印）"
APPID = (os.getenv("xxy_appid") or "wxfc766f1e9a63b01f").strip()

PROXY_API = os.getenv("PROXY_API", "")
PROXY_TYPE = os.getenv("PROXY_TYPE", "http").lower()
PROXY_RETRY_TIMES = 3
ENABLE_DIRECT_FALLBACK = True
REQUEST_TIMEOUT = 30

HENGAN_BASE = os.getenv("HENGAN_BASE", "https://mshopapi.hengan.cn/mall/app").rstrip("/")
HENGAN_OAUTH_BASE = os.getenv("HENGAN_OAUTH_BASE", "https://mshopapi.hengan.cn").rstrip("/")
HENGAN_OAUTH_LOGIN = "/auth/app/anon/oauth/wxappLogin"
HENGAN_CHECK_TOKEN = "/anon/api/auth/checkAppToken"
HENGAN_SIGN = "/sign/user"
HENGAN_SIGN_INTEGRAL = "/api/sign/v2/integral"
HENGAN_SIGN_CALENDAR = "/api/sign/v2/currentMonthSignInfo"
HENGAN_SIGN_CONFIG = "/api/sign/v2/currentActiveSignConfig"
HENGAN_SIGN_REWARDS = "/api/sign/v2/mySignRewardList"
HENGAN_SIGN_SUPP = "/api/sign/v2/supplementaryCount"
HENGAN_USERINFO = "/userinfo?login=true"
HENGAN_APP_VERSION = "1.2.11"

CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "xxy_token_cache.json")

USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 13; SM-G9910 Build/TP1A.220624.014) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Mobile Safari/537.36 "
    "MicroMessenger/8.0.49.2600(0x28003137) NetType/WIFI Language/zh_CN "
    "miniProgram/" + APPID
)


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def mask(value: Any) -> str:
    value = str(value or "")
    return value[:8] + "***" if len(value) > 8 else value


def split_openids(raw: str) -> List[str]:
    out: List[str] = []
    for line in (raw or "").replace("&", "\n").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line.split("#")[0].strip())
    return out


try:
    from send_notify import (
        format_report,
        mask_phone,
        notify_and_format,
        send_notify,
    )
except Exception:
    from send_notify import send_notify  # type: ignore

    def mask_phone(phone):
        s = re.sub(r"\D", "", str(phone or ""))
        return (s[:3] + "****" + s[-4:]) if len(s) >= 11 else (s or "-")

    def format_report(task, accounts, push_result="", cost_s=None):
        return task

    def notify_and_format(task, accounts, **kwargs):
        return send_notify(task, str(accounts))


class CodeService:
    def __init__(self, base: str, auth: str):
        self.base = base.rstrip("/")
        self.s = requests.Session()
        self.s.verify = False
        self.s.headers.update({"auth": auth, "User-Agent": USER_AGENT})

    def wx_code(self, openid: str, appid: str) -> str:
        r = self.s.post(
            f"{self.base}/wx/code",
            json={"openid": openid, "appid": appid},
            timeout=20,
        )
        data = r.json()
        if not data.get("status"):
            raise RuntimeError(f"/wx/code 失败: {data.get('message')}")
        code = ((data.get("data") or {}).get("code") or "").strip()
        if not code:
            raise RuntimeError("/wx/code 未返回 code")
        return code


def direct_session() -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    return session


def parse_proxy_response(text: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False)
    text = text.strip()
    if not text:
        return None
    try:
        data = json.loads(text)
        proxy_obj = None
        if isinstance(data.get("data"), list) and data["data"]:
            proxy_obj = data["data"][0]
        elif isinstance(data.get("data"), dict):
            proxy_obj = data["data"]
        elif data.get("ip") and data.get("port"):
            proxy_obj = data
        elif isinstance(data.get("result"), dict):
            proxy_obj = data["result"]
        if proxy_obj:
            host = proxy_obj.get("ip") or proxy_obj.get("host")
            port = proxy_obj.get("port")
            if host and port:
                return {
                    "host": str(host),
                    "port": int(port),
                    "username": proxy_obj.get("user") or proxy_obj.get("username") or "",
                    "password": proxy_obj.get("pass") or proxy_obj.get("password") or "",
                }
    except Exception:
        pass
    return None


def build_proxy_dict(proxy_info: Optional[Dict[str, Any]]) -> Optional[Dict[str, str]]:
    if not proxy_info:
        return None
    auth = ""
    if proxy_info.get("username") and proxy_info.get("password"):
        auth = f"{quote(proxy_info['username'])}:{quote(proxy_info['password'])}@"
    scheme = "socks5" if PROXY_TYPE == "socks5" else "http"
    proxy_url = f"{scheme}://{auth}{proxy_info['host']}:{proxy_info['port']}"
    return {"http": proxy_url, "https": proxy_url}


def get_valid_proxy() -> Optional[Dict[str, str]]:
    if not PROXY_API:
        return None
    for index in range(PROXY_RETRY_TIMES):
        try:
            response = direct_session().get(PROXY_API, timeout=15)
            proxies = build_proxy_dict(parse_proxy_response(response.text))
            if proxies:
                try:
                    r = requests.get("http://httpbin.org/ip", proxies=proxies, timeout=15)
                    if r.status_code == 200:
                        return proxies
                except Exception:
                    pass
        except Exception:
            pass
        time.sleep(2)
    return None


def request_with_proxy(method: str, url: str, *, proxies: Optional[Dict[str, str]] = None, **kwargs):
    kwargs.setdefault("timeout", REQUEST_TIMEOUT)
    if proxies:
        try:
            return requests.request(method, url, proxies=proxies, **kwargs)
        except Exception:
            if not ENABLE_DIRECT_FALLBACK:
                raise
    return direct_session().request(method, url, **kwargs)


def json_preview(data: Any, limit: int = 200) -> str:
    try:
        return json.dumps(data, ensure_ascii=False)[:limit]
    except Exception:
        return str(data)[:limit]


def extract_token(data: Any) -> Optional[str]:
    if not isinstance(data, dict):
        return None
    candidates = [data.get("token"), data.get("accessToken"), data.get("access_token"), data.get("jwt")]
    inner = data.get("data")
    if isinstance(inner, dict):
        candidates += [
            inner.get("token"),
            inner.get("accessToken"),
            inner.get("access_token"),
            inner.get("jwt"),
        ]
    for item in candidates:
        if item and item != "null":
            return str(item)
    return None


def hengan_headers(token: str = "") -> Dict[str, str]:
    h = {
        "User-Agent": USER_AGENT,
        "appVersion": HENGAN_APP_VERSION,
        "envVersion": "release",
        "Content-Type": "application/json",
        "Accept": "*/*",
        "xweb_xhr": "1",
        "Referer": f"https://servicewechat.com/{APPID}/340/page-frame.html",
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def hengan_get(path: str, token: str = "", proxies=None, params=None) -> Dict[str, Any]:
    response = request_with_proxy(
        "GET", f"{HENGAN_BASE}{path}",
        headers=hengan_headers(token), params=params, proxies=proxies,
    )
    try:
        return response.json()
    except Exception:
        return {"status": -1, "msg": response.text[:200]}


def hengan_post(path: str, token: str, body: Any, proxies=None) -> Dict[str, Any]:
    response = request_with_proxy(
        "POST", f"{HENGAN_BASE}{path}",
        headers=hengan_headers(token), json=body, proxies=proxies,
    )
    try:
        return response.json()
    except Exception:
        return {"status": -1, "msg": response.text[:200]}


def hengan_check_token(token: str, proxies=None) -> bool:
    try:
        data = hengan_get(HENGAN_CHECK_TOKEN, token, proxies, {"token": token})
        return bool(data.get("data")) and data.get("success") is True
    except Exception:
        return False


def login_by_code(code: str, proxies=None) -> Tuple[Optional[str], Dict[str, Any]]:
    url = HENGAN_OAUTH_BASE + HENGAN_OAUTH_LOGIN
    body = {"code": code, "spread": 0, "labelValue": None}
    response = request_with_proxy(
        "POST", url, headers=hengan_headers(),
        data=json.dumps(body, separators=(",", ":"), ensure_ascii=False),
        proxies=proxies,
    )
    try:
        result = response.json()
    except Exception:
        result = {"raw": response.text[:300]}
    return extract_token(result), result


def load_token_cache() -> Dict[str, Any]:
    try:
        if os.path.exists(CACHE_PATH):
            with open(CACHE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def save_token_cache(cache: Dict[str, Any]) -> None:
    try:
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def get_cached_token(openid: str) -> Optional[str]:
    data = load_token_cache().get(openid)
    if data and data.get("token") and data.get("expireTime"):
        try:
            expire = datetime.fromisoformat(data["expireTime"]).timestamp() * 1000
            if time.time() * 1000 < expire - 3600 * 1000:
                return data["token"]
        except Exception:
            pass
    return None


def set_cached_token(openid: str, token: str) -> None:
    cache = load_token_cache()
    cache[openid] = {
        "token": token,
        "expireTime": datetime.fromtimestamp(time.time() + 24 * 3600).isoformat(),
    }
    save_token_cache(cache)


def _pick_name(item: dict) -> str:
    for k in (
        "rewardName", "name", "title", "prizeName", "goodsName",
        "integral", "score", "desc", "description",
    ):
        v = item.get(k)
        if v not in (None, "", 0, "0"):
            return str(v)
    return ""


def _is_claimable(item: dict) -> bool:
    """判断累计奖励是否可能可领取。"""
    if not isinstance(item, dict):
        return False
    # 明确不可领
    for k in ("isReceive", "received", "isReceived", "is_lock", "isLock", "isLockStyle"):
        if item.get(k) in (True, 1, "1"):
            return False
    for k in ("status", "receiveStatus", "awardStatus", "state"):
        v = item.get(k)
        if v in (True, 1, "1", "received", "RECEIVED", "已领取", "done"):
            return False
        if v in (0, "0", 2, "2", "claimable", "CLAIMABLE", "可领取", "pending", "PENDING"):
            return True
    # itemVoList 子档位
    for sub in item.get("itemVoList") or []:
        if isinstance(sub, dict) and not sub.get("isLockStyle") and not sub.get("isReceive"):
            name = _pick_name(sub)
            if name:
                return True
    return False


def parse_sign_extras(cfg: Any, month: Any, rewards: Any, supp: Any) -> Dict[str, Any]:
    """汇总月累计天数 / 可领奖励 / 配置摘要。"""
    out = {
        "signNum": None,
        "isDaySign": None,
        "claimable": [],
        "reward_summary": "",
        "supp": None,
        "config_days": [],
    }

    def walk_days(obj):
        if isinstance(obj, dict):
            for k in ("signNum", "sumSignDay", "totalSignDay", "continuousSignDays"):
                if obj.get(k) is not None:
                    try:
                        out["signNum"] = int(obj[k])
                        return True
                    except Exception:
                        pass
            if obj.get("isDaySign") is not None:
                out["isDaySign"] = bool(obj.get("isDaySign"))
            for v in obj.values():
                if walk_days(v):
                    return True
        elif isinstance(obj, list):
            for it in obj:
                if walk_days(it):
                    return True
        return False

    walk_days(month or {})
    walk_days(cfg or {})

    # 配置里的累计档位天数
    def walk_cfg(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                lk = str(k).lower()
                if lk in ("signday", "signdays", "day", "days", "continuousday", "needday", "reachday"):
                    try:
                        out["config_days"].append(int(v))
                    except Exception:
                        pass
                if isinstance(v, (dict, list)):
                    walk_cfg(v)
        elif isinstance(obj, list):
            for it in obj:
                walk_cfg(it)
    walk_cfg(cfg or {})
    out["config_days"] = sorted(set(out["config_days"]))[:12]

    # 我的奖励列表
    items = []
    raw = rewards
    if isinstance(raw, dict):
        data = raw.get("data") if isinstance(raw.get("data"), (dict, list)) else raw
        if isinstance(data, dict):
            for k in ("list", "items", "records", "rewardList", "signRewardList"):
                if isinstance(data.get(k), list):
                    items = data[k]
                    break
            if not items:
                for v in data.values():
                    if isinstance(v, list) and v and isinstance(v[0], dict):
                        items = v
                        break
        elif isinstance(data, list):
            items = data
    elif isinstance(raw, list):
        items = raw

    names = []
    for it in items[:20]:
        if not isinstance(it, dict):
            continue
        nm = _pick_name(it) or json.dumps(it, ensure_ascii=False)[:40]
        names.append(nm)
        if _is_claimable(it):
            out["claimable"].append({"name": nm, "raw": {k: it.get(k) for k in list(it)[:12]}})
    out["reward_summary"] = "、".join([n for n in names if n][:6])

    if isinstance(supp, dict):
        d = supp.get("data")
        if isinstance(d, dict):
            out["supp"] = d.get("count") or d.get("supplementaryCount") or d.get("num")
        elif isinstance(d, (int, str)):
            out["supp"] = d
    return out


def try_claim_rewards(token: str, claimable: list, proxies=None) -> List[str]:
    """主包无独立领取接口；仅在响应里带 id 时尝试积分兑换/领取类端点。"""
    lines: List[str] = []
    if not claimable:
        return lines
    for item in claimable[:5]:
        raw = item.get("raw") or {}
        rid = raw.get("id") or raw.get("rewardId") or raw.get("signRewardId") or raw.get("activityId")
        name = item.get("name") or rid
        if not rid:
            lines.append(f"奖励[{name}] 无 id，需小程序内手动领取")
            continue
        # 源码可见的唯一写接口：pointExchangeSign（积分兑签到卡，未必是累计奖励）
        body = {"id": rid, "rewardId": rid, "signRewardId": rid}
        resp = hengan_post("/api/sign/v2/pointExchangeSign", token, body, proxies)
        ok = resp.get("success") is True
        msg = resp.get("msg") or json.dumps(resp, ensure_ascii=False)[:80]
        lines.append(f"尝试领取[{name}]: {'成功' if ok else msg}")
    return lines


def hengan_run(token: str, proxies=None) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "account": "微信用户",
        "phone": "",
        "status": "-",
        "reward": "-",
        "month_days": None,
        "patch_card": None,
        "extra": [],
        "success": False,
        "error": "",
        "userMsg": "-",
        "signMsg": "-",
        "pointsMsg": "-",
        "rewardMsg": "-",
    }
    info = hengan_get(HENGAN_USERINFO, token, proxies)
    d = info.get("data") or {}
    if info.get("success") is not True:
        out["error"] = f"读取用户失败: {(info.get('msg') or '')[:80]}"
        return out
    out["account"] = str(d.get("nickname") or "微信用户")
    out["phone"] = str(d.get("phone") or "")

    sign = hengan_post(HENGAN_SIGN, token, {"sign": 1, "integral": 1, "all": 1}, proxies)
    sd = sign.get("data") or {}
    cont = sd.get("sumSignDay", sd.get("signNum"))
    if sign.get("success") is True:
        if sd.get("isDaySign") is True:
            out["status"] = f"今日已签到 ✅ (连续 {cont if cont is not None else '?'} 天)"
        else:
            out["status"] = f"签到成功 ✅ (连续 {cont if cont is not None else '?'} 天)"
    else:
        msg = sign.get("msg") or "签到失败"
        if "已签" in str(msg) or "重复" in str(msg):
            out["status"] = f"{msg} ✅ (连续 {cont if cont is not None else '?'} 天)"
        else:
            out["status"] = f"签到失败 ❌ ({str(msg)[:60]})"
            out["error"] = str(msg)[:80]

    gained = sd.get("integral") or sd.get("score")
    pts = hengan_post(HENGAN_SIGN_INTEGRAL, token, {}, proxies)
    if pts.get("success") is True:
        g2 = (pts.get("data") or {}).get("integral")
        if g2 is not None:
            gained = g2
        if gained is None:
            out["reward"] = pts.get("msg") or "已领取"
        else:
            out["reward"] = f"+{gained}"
    else:
        if gained is not None:
            out["reward"] = f"+{gained}"
        else:
            out["reward"] = pts.get("msg") or "-"

    try:
        cfg = hengan_get(HENGAN_SIGN_CONFIG, token, proxies)
        month = hengan_get(HENGAN_SIGN_CALENDAR, token, proxies)
        rewards = hengan_get(HENGAN_SIGN_REWARDS, token, proxies)
        supp = hengan_get(HENGAN_SIGN_SUPP, token, proxies)
        ex = parse_sign_extras(cfg, month, rewards, supp)
        days = ex.get("signNum") or cont or d.get("signNum")
        try:
            out["month_days"] = int(days) if days is not None else None
        except Exception:
            out["month_days"] = None
        if ex.get("supp") is not None:
            out["patch_card"] = ex["supp"]
        extras: List[str] = []
        if ex.get("reward_summary"):
            extras.append("奖励: " + ex["reward_summary"])
        if ex.get("claimable"):
            extras.append(f"可领候选 {len(ex['claimable'])} 项")
            claim_lines = try_claim_rewards(token, ex["claimable"], proxies)
            extras.extend(claim_lines[:2])
        out["extra"] = extras
        if os.getenv("xxy_debug"):
            print("[debug] rewards keys", list(rewards.keys())[:8] if isinstance(rewards, dict) else type(rewards))
    except Exception as e:
        out["extra"] = [f"累计查询失败: {e}"]

    # 兼容旧字段
    out["userMsg"] = f"{out['account']} ({mask_phone(out['phone']) if out['phone'] else '-'})"
    out["signMsg"] = out["status"]
    out["pointsMsg"] = out["reward"]
    out["rewardMsg"] = f"本月已签 {out['month_days'] if out['month_days'] is not None else '?'} 天"
    if out.get("patch_card") is not None:
        out["rewardMsg"] += f" | 补签卡 {out['patch_card']}"
    if out["status"].startswith("签到失败"):
        out["success"] = False
    else:
        out["success"] = True
        out["error"] = ""
    return out


def run_account(openid: str, sm: CodeService) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "openid": openid,
        "success": False,
        "account": "微信用户",
        "phone": "",
        "status": "-",
        "reward": "-",
        "month_days": None,
        "patch_card": None,
        "extra": [],
        "error": "",
    }
    proxies = get_valid_proxy()
    token = get_cached_token(openid)
    hres: Dict[str, Any] = {}
    if token and hengan_check_token(token, proxies):
        hres = hengan_run(token, proxies)
    else:
        token = None
    if not token:
        try:
            code = sm.wx_code(openid, APPID)
        except Exception as e:
            result["error"] = str(e)
            result["status"] = f"获取 code 失败 ❌ ({str(e)[:60]})"
            return result
        time.sleep(random.uniform(0.5, 2.0))
        token, raw = login_by_code(code, proxies)
        if not token:
            result["error"] = "登录失败"
            result["status"] = "登录失败 ❌"
            return result
        set_cached_token(openid, token)
        hres = hengan_run(token, proxies)
    for k in ("account", "phone", "status", "reward", "month_days", "patch_card", "extra", "success", "error"):
        if k in hres:
            result[k] = hres[k]
    return result


def main() -> int:
    started = time.time()
    sc_base = os.getenv("wx_server_url", "").strip()
    sc_auth = os.getenv("wx_auth", "").strip()
    raw = os.getenv("xxy", "").strip()
    if not sc_base or not sc_auth:
        print("缺少 wx_server_url / wx_auth")
        return 1
    openids = split_openids(raw)
    if not openids:
        print("缺少 xxy（openid，多账号换行或 & 分隔）")
        return 1
    sm = CodeService(sc_base, sc_auth)
    print(f"{APP_NAME} | {len(openids)}账号")

    results: List[Dict[str, Any]] = []
    for openid in openids:
        if results:
            time.sleep(random.uniform(8, 20))
        try:
            res = run_account(openid, sm)
        except Exception:
            res = {
                "openid": openid,
                "success": False,
                "account": mask(openid),
                "phone": "",
                "status": "脚本异常 ❌",
                "reward": "-",
                "month_days": None,
                "patch_card": None,
                "extra": [],
                "error": traceback.format_exc().strip().splitlines()[-1][:80],
            }
        results.append(res)

    try:
        notify_and_format(
            APP_NAME,
            results,
            title=f"芯享会签到 {sum(1 for x in results if x.get('success'))}/{len(results)}",
            start_ts=started,
        )
    except Exception:
        # 兜底：至少把账号块打出来
        print(format_report(APP_NAME, results, push_result="推送模块异常", cost_s=time.time() - started))
    ok_n = sum(1 for x in results if x.get("success"))
    return 0 if ok_n == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
