#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# /*
# ------------------------------------------
# @Author: onijiang0
# @Date: 2026.09.21
# @Description: 特步会员中心 - 每日签到/积分
# cron: 16 14 * * *
# #定时使用10-19点 随机时间 每天
# ------------------------------------------
# 变量名：tebu
# 变量值：业务 openid，多账号换行或 & 分隔，可加 #备注
#
# 依赖变量：
# wx_server_url  必填，取码服务地址（使用者自备，勿写进仓库）
# wx_auth        必填，取码服务鉴权
# QL_NOTIFY      选填，0 关闭推送
# ------------------------------------------
# 已实现：
# 1. 多账号；缺变量报错；单号失败不中断
# 2. code 换 token；本地缓存；失效删缓存后 code 重登并重跑
# 3. 用户信息脱敏展示；个人中心模板解析签到 activityId；签到+积分
# 4. send_notify 统一简报；平台参数写脚本默认值
#
# 契约（appid wx12e1cb3b09a0e6f0，mall-mobile-v6.vecrp.com）：
# code     POST {wx_server_url}/wx/code auth:{wx_auth} json:{openid,appid}
# 登录     POST /mobile/wxAppLogin
#          body {code,appid,shopId,envVersion,isEnterpriseWx,...} -> token
#          （源脚本：同平台 vecrp 推断端点，失败需抓包）
# 用户     GET  /mobile/customer/initMy?shopId=100656040
# 模板     GET  /mobile/customer/queryMobilePersonCenterTemplateByShopId?shopId=
#          componentId=WHXG8614883，link.path 签到页取 activityId
# 签到     POST /mobile/activity/sign/sign
#          body {shopId,activityId,signDate:YYYY-MM-DD}
#          success==true 为成功；result.integral 为本次积分
# 积分     GET  /mobile/customer/getMyAllPoint?shopId= -> result[0].score
# 判定     vecrp：data.success == true
#
# 踩坑：
# 1. 原脚本有 token 缓存：失效必须删缓存再 code 重登后重跑
# 2. shopId/componentId/签到页 path 为平台配置，已写入脚本
# 3. activityId 运行时从模板解析，不写死（活动会变）
# 4. 登录接口为推断；失败看日志 msg，必要时抓包 /mobile/wxAppLogin
# 5. 账号相关只改环境变量；日志手机 ****，openid 截断
# ------------------------------------------
# */

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
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
APPID = "wx12e1cb3b09a0e6f0"
BASE_URL = "https://mall-mobile-v6.vecrp.com"
LOGIN_URL = f"{BASE_URL}/mobile/wxAppLogin"
USER_INFO_URL = f"{BASE_URL}/mobile/customer/initMy"
TEMPLATE_URL = f"{BASE_URL}/mobile/customer/queryMobilePersonCenterTemplateByShopId"
SIGN_URL = f"{BASE_URL}/mobile/activity/sign/sign"
POINT_URL = f"{BASE_URL}/mobile/customer/getMyAllPoint"
SHOP_ID = "100656040"
SIGN_COMPONENT_ID = "WHXG8614883"
SIGN_PAGE_PATH = "/pages/ehd/activities/signIn/index"
REQUEST_TIMEOUT = 30

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 "
    "MicroMessenger/7.0.20.1781 NetType/WIFI MiniProgramEnv/Windows WindowsWechat"
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


def common_headers(token: str = "") -> Dict[str, str]:
    h = {
        "User-Agent": UA,
        "Content-Type": "application/json;charset=UTF-8",
        "Accept": "*/*",
        "appid": APPID,
        "ts": str(int(time.time() * 1000)),
        "Referer": f"https://servicewechat.com/{APPID}/132/page-frame.html",
    }
    if token:
        h["token"] = token
    return h


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


def is_ok(data: Any) -> bool:
    return isinstance(data, dict) and data.get("success") is True


def err_msg(data: Any) -> str:
    if isinstance(data, dict):
        return clean_line(data.get("msg") or data.get("message") or data.get("error") or "失败")
    return clean_line(data)


def is_token_expired(data: Any) -> bool:
    msg = str(err_msg(data) or "")
    return bool(re.search(r"token|登录失效|未登录|未授权|请重新登录|过期|401", msg, re.I))


def extract_token(data: Any) -> Optional[str]:
    if not isinstance(data, dict):
        return None
    cands = [data.get("token"), data.get("mobileToken"), data.get("accessToken"), data.get("access_token")]
    for key in ("data", "result"):
        inner = data.get(key)
        if isinstance(inner, dict):
            cands += [
                inner.get("token"),
                inner.get("mobileToken"),
                inner.get("accessToken"),
                inner.get("access_token"),
            ]
            user = inner.get("user") or inner.get("customer")
            if isinstance(user, dict):
                cands += [
                    user.get("token"),
                    user.get("mobileToken"),
                    user.get("accessToken"),
                    user.get("access_token"),
                ]
    for c in cands:
        if c and c != "null":
            return str(c)
    return None


def cache_path() -> Path:
    configured = os.getenv("TEBU_TOKEN_DIR", "").strip()
    if configured:
        folder = Path(configured)
    elif Path("/ql/data/config").is_dir():
        folder = Path("/ql/data/config/tebu")
    else:
        folder = Path(__file__).resolve().parent / "tebu_cache"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / "token_cache.json"


def load_cache() -> Dict[str, Any]:
    try:
        p = cache_path()
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:
        return {}


def save_cache(cache: Dict[str, Any]) -> None:
    try:
        cache_path().write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        say("⚠️ token 缓存写入失败")


def cache_key(openid: str) -> str:
    return str(openid or "")


def read_cached_token(openid: str) -> Optional[str]:
    item = load_cache().get(cache_key(openid)) or {}
    token = str(item.get("token") or "")
    expire = item.get("expireTime")
    if not token:
        return None
    try:
        if expire:
            if isinstance(expire, (int, float)):
                exp_ms = float(expire)
            else:
                exp_ms = datetime.fromisoformat(str(expire)).timestamp() * 1000
            if time.time() * 1000 >= exp_ms - 3600 * 1000:
                return None
    except Exception:
        pass
    return token


def write_cached_token(openid: str, token: str, raw_login: Dict[str, Any]) -> None:
    expire = None
    if isinstance(raw_login, dict):
        inner = raw_login.get("data") or raw_login.get("result")
        if isinstance(inner, dict):
            expire = inner.get("expireTime") or inner.get("expire_time")
            expires_in = inner.get("expiresIn")
            if not expire and isinstance(expires_in, (int, float)) and expires_in > 0:
                expire = datetime.fromtimestamp(time.time() + expires_in).isoformat()
    if not expire:
        expire = datetime.fromtimestamp(time.time() + 24 * 3600).isoformat()
    elif not isinstance(expire, str):
        try:
            expire = datetime.fromtimestamp(float(expire) / 1000.0).isoformat()
        except Exception:
            expire = datetime.fromtimestamp(time.time() + 24 * 3600).isoformat()
    cache = load_cache()
    cache[cache_key(openid)] = {"token": token, "expireTime": expire, "updateTime": datetime.now().isoformat()}
    save_cache(cache)


def clear_cached_token(openid: str) -> None:
    cache = load_cache()
    cache.pop(cache_key(openid), None)
    save_cache(cache)
    print("🗑️ 已删除本地 token 缓存")


def api_get(url: str, token: str) -> Dict[str, Any]:
    try:
        r = http("GET", url, headers=common_headers(token))
        data = r.json()
        return data if isinstance(data, dict) else {"success": False, "msg": clean_line(data)}
    except Exception as e:
        msg = clean_line(e) or str(e)
        kind = "网络不可达"
        if re.search(r"ssl|certificate", msg, re.I):
            kind = "HTTPS异常"
        elif re.search(r"timeout|timed out", msg, re.I):
            kind = "请求超时"
        return {"success": False, "msg": f"{kind}: {msg[:80]}"}


def api_post(url: str, token: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    try:
        r = http("POST", url, headers=common_headers(token), json=payload)
        data = r.json()
        return data if isinstance(data, dict) else {"success": False, "msg": clean_line(data)}
    except Exception as e:
        msg = clean_line(e) or str(e)
        kind = "网络不可达"
        if re.search(r"ssl|certificate", msg, re.I):
            kind = "HTTPS异常"
        elif re.search(r"timeout|timed out", msg, re.I):
            kind = "请求超时"
        return {"success": False, "msg": f"{kind}: {msg[:80]}"}


def token_valid(token: str) -> bool:
    resp = api_get(f"{USER_INFO_URL}?shopId={SHOP_ID}", token)
    return is_ok(resp)


def login_by_code(openid: str) -> Tuple[Optional[str], Dict[str, Any]]:
    print("🔐 使用 code 登录")
    code = get_wx_code(openid)
    print(f"ℹ️ 取码结果 code={'有' if code else '无'}")
    payload = {
        "code": code,
        "appid": APPID,
        "shopId": None,
        "envVersion": "",
        "isEnterpriseWx": False,
        "scene": "",
        "referrerInfo": "",
    }
    data = api_post(LOGIN_URL, "", payload)
    print(f"ℹ️ 登录响应 success={data.get('success')} msg={err_msg(data)[:60] if not data.get('success') else 'ok'}")
    token = extract_token(data)
    if not token:
        return None, {"message": err_msg(data) or "登录响应未返回 token"}
    return token, data


def login_with_cache(openid: str) -> Tuple[Optional[str], str]:
    cached = read_cached_token(openid)
    if cached:
        print("ℹ️ token缓存登录")
        if token_valid(cached):
            return cached, "token缓存登录"
        print("⚠️ 缓存 token 失效，自动删除并 code 重登")
        clear_cached_token(openid)
        token, raw = login_by_code(openid)
        if token:
            write_cached_token(openid, token, raw or {})
            return token, "缓存失效 code重登"
        return None, "缓存失效 code重登失败"
    token, raw = login_by_code(openid)
    if token:
        write_cached_token(openid, token, raw or {})
        return token, "code登录"
    return None, "code登录失败"


def query_user(token: str) -> Tuple[str, str]:
    """返回 (显示名, 脱敏手机)"""
    resp = api_get(f"{USER_INFO_URL}?shopId={SHOP_ID}", token)
    if not is_ok(resp):
        return "未知用户", ""
    customer = (resp.get("result") or {}).get("customer") or {}
    name = str(customer.get("customerName") or "未知用户")
    mobile = str(customer.get("mobile") or "")
    return name, mobile


def get_activity_id(token: str) -> str:
    resp = api_get(f"{TEMPLATE_URL}?shopId={SHOP_ID}", token)
    if not is_ok(resp):
        say(f"⚠️ 获取模板失败: {err_msg(resp)}")
        return ""
    views = (resp.get("result") or {}).get("views") or []
    component = next(
        (v for v in views if isinstance(v, dict) and v.get("componentId") == SIGN_COMPONENT_ID),
        None,
    )
    if not component:
        say(f"⚠️ 未找到 componentId={SIGN_COMPONENT_ID}")
        return ""
    imgs = ((component.get("defaultConfig") or {}).get("img")) or []
    for img in imgs:
        if not isinstance(img, dict):
            continue
        link = img.get("link") or {}
        if link.get("path") == SIGN_PAGE_PATH:
            activity_id = str(link.get("id") or "")
            if activity_id:
                return activity_id
    say("⚠️ 未找到签到活动 activityId")
    return ""


def query_points(token: str) -> str:
    resp = api_get(f"{POINT_URL}?shopId={SHOP_ID}", token)
    if not is_ok(resp):
        return err_msg(resp) or "-"
    rows = resp.get("result") or []
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        return str(rows[0].get("score", "-"))
    return "-"


def do_sign(token: str) -> Tuple[bool, str, Optional[float]]:
    activity_id = get_activity_id(token)
    if not activity_id:
        return False, "未获取到 activityId ❌", None
    payload = {
        "shopId": SHOP_ID,
        "activityId": activity_id,
        "signDate": datetime.now().strftime("%Y-%m-%d"),
    }
    resp = api_post(SIGN_URL, token, payload)
    msg = err_msg(resp)
    print(f"ℹ️ 签到响应 success={resp.get('success')} msg={msg[:60] or 'ok'}")
    if is_ok(resp):
        integral = (resp.get("result") or {}).get("integral")
        try:
            gain = float(integral) if integral is not None else None
        except (TypeError, ValueError):
            gain = None
        if gain is not None:
            return True, f"签到成功 ✅ 积分+{gain:g}", gain
        return True, "签到成功 ✅", None
    return False, f"{msg[:40]} ❌", None


def run_account(openid: str, index: int, total: int) -> Dict[str, Any]:
    extras: List[str] = [f"openid：{mask_id(openid, 6)}"]
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

    try:
        token, mode = login_with_cache(openid)
        extras.append(mode)
        if not token:
            acc["status"] = "登录失败 ❌（登录接口为推断，可抓包核对）"
            acc["error"] = "登录失败"
            return acc
        say(f"✅ 登录成功 token={mask_token(token)}")
        name, mobile = query_user(token)
        acc["account"] = name or acc["account"]
        acc["phone"] = mobile
        extras.append(f"用户 {name} {mask_phone(mobile) if mobile else '-'}")

        time.sleep(1)
        ok, status, gain = do_sign(token)
        acc["status"] = status
        acc["success"] = ok
        time.sleep(1)
        points = query_points(token)
        extras.append(f"当前积分 {points}")
        if gain is not None:
            acc["reward"] = f"+{gain:g} 积分"
        elif points not in ("-", "", None):
            acc["reward"] = f"积分 {points}"
        if not ok:
            acc["error"] = status
        return acc
    except Exception as e:
        msg = clean_line(e)
        say(f"❌ {msg}")
        if re.search(r"token|登录|401|未登录|过期", msg, re.I):
            say("⚠️ 登录态失效，删除缓存 → code 重登 → 重跑")
            extras.append("登录态失效 code重登")
            try:
                clear_cached_token(openid)
                token, mode = login_with_cache(openid)
                extras.append(mode)
                if not token:
                    acc["status"] = "重登失败 ❌"
                    acc["error"] = "重登失败"
                    return acc
                ok, status, gain = do_sign(token)
                acc["status"] = status
                acc["success"] = ok
                points = query_points(token)
                extras.append(f"当前积分 {points}")
                if gain is not None:
                    acc["reward"] = f"+{gain:g} 积分"
                return acc
            except Exception as e2:
                msg2 = clean_line(e2)
                acc["status"] = f"重登重跑失败 ❌ ({msg2[:40]})"
                acc["error"] = msg2
                return acc
        acc["status"] = f"失败 ❌ ({msg[:40]})"
        acc["error"] = msg
        return acc


def main() -> int:
    started = time.time()
    openids = split_openids(os.getenv("tebu", "").strip())
    if not openids:
        print("❌ 未配置 tebu 环境变量（openid，多账号换行或 &）")
        return 1
    if not os.getenv("wx_server_url", "").strip() or not os.getenv("wx_auth", "").strip():
        print("❌ 未配置 wx_server_url / wx_auth")
        return 1
    print(f"{APP_NAME} | {len(openids)}账号 | shopId={SHOP_ID}")

    results: List[Dict[str, Any]] = []
    for i, openid in enumerate(openids, 1):
        try:
            results.append(run_account(openid, i, len(openids)))
        except Exception as e:
            results.append({
                "account": f"账号{i}",
                "phone": "",
                "status": f"执行失败 ❌ ({clean_line(e)[:40]})",
                "reward": "-",
                "extra": [f"openid：{mask_id(openid, 6)}"],
                "error": clean_line(e),
                "success": False,
            })
        if i < len(openids):
            time.sleep(2)

    ok_n = sum(1 for r in results if r.get("success"))
    try:
        notify_and_format(
            APP_NAME,
            results,
            title=f"{APP_NAME}签到 {ok_n}/{len(results)}",
            start_ts=started,
        )
    except Exception:
        print(format_report(APP_NAME, results, push_result="推送模块异常", cost_s=time.time() - started))
    return 0 if ok_n == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
