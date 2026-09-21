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
# 契约（appid wx12e1cb3b09a0e6f0，mall-mobile-v6.vecrp.com/mobile）：
# code     POST {wx_server_url}/wx/code auth:{wx_auth} json:{openid,appid}
# 登录     POST /mobile/wxAppLogin
#          body {code,appid,shopId,envVersion,isEnterpriseWx,scene,referrerInfo}
#          -> result.mobileToken / result.openId / result.shopId
# 请求头   appid / token / ts / startTime / sign / X-TracedId
# sign     POST: sha1_hex(排序拼接 body=JSON.stringify(req)+secretKey+ts)
#          密钥写在脚本默认值（包内逆向，非账号密钥）
# 用户     GET  /mobile/customer/initMy?shopId=100656040
# 模板     GET  /mobile/customer/queryMobilePersonCenterTemplateByShopId?shopId=
#          componentId=WHXG8614883，link.path 签到页取 activityId
# 签到     POST /mobile/activity/sign/sign
#          body {shopId,activityId,signDate} -> success + result.integral
# 积分     GET  /mobile/customer/getMyAllPoint?shopId= -> result[0].score
# 判定     success == true；token 字段为 mobileToken
#
# 踩坑：
# 1. 无 sign/ts 头时接口回「当前请求异常」
# 2. 原脚本有 token 缓存：失效删缓存再 code 重登后重跑
# 3. shopId/componentId/secretKey 已从包内回填脚本默认值
# 4. activityId 运行时从模板解析，不写死
# 5. 账号相关只改环境变量；日志脱敏
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
# 包内逆向请求签名密钥（非账号密钥）
SECRET_KEY = "R6WbJ830wNsEdjH9GumwKYiYxHz0K9QD"
REQUEST_TIMEOUT = 30

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 "
    "MicroMessenger/7.0.20.1781 NetType/WIFI MiniProgramEnv/Windows WindowsWechat"
)


def _sha1_hex(text: str) -> str:
    import hashlib
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def compute_sign(payload: Dict[str, Any], ts: int, is_post: bool = True) -> Tuple[str, str]:
    """返回 (sign, signed_body_str)。POST body 与签名所用字符串必须完全一致。"""
    if is_post:
        body_str = json.dumps(payload if payload is not None else {}, separators=(",", ":"), ensure_ascii=False)
        bag = {"body": body_str, "secretKey": SECRET_KEY, "ts": ts}
        parts = sorted(f"{k}{bag[k]}" for k in bag)
        return _sha1_hex("".join(parts)), body_str
    bag = dict(payload or {})
    bag["secretKey"] = SECRET_KEY
    bag["ts"] = ts
    parts = sorted(f"{k}{bag[k]}" for k in bag)
    return _sha1_hex("".join(parts)), ""


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


def common_headers(token: str = "", *, sign: str = "", ts: str = "", open_id: str = "") -> Dict[str, str]:
    import uuid as _uuid
    h = {
        "User-Agent": UA,
        "Content-Type": "application/json;charset=UTF-8",
        "Accept": "*/*",
        "appid": APPID,
        "ts": ts or str(int(time.time() * 1000)),
        "startTime": str(int(time.time() * 1000)),
        "Referer": f"https://servicewechat.com/{APPID}/132/page-frame.html",
        "X-TracedId": str(_uuid.uuid4()),
    }
    if sign:
        h["sign"] = sign
    if token:
        h["token"] = token
    if open_id:
        h["openId"] = open_id
        h["openid"] = open_id
    return h


# 模块级：登录返回的 openId（部分网关校验）
_LOGIN_OPENID = ""


def extract_open_id(data: Any) -> str:
    if isinstance(data, dict):
        for src in (data.get("result"), data.get("data"), data):
            if isinstance(src, dict):
                for k in ("openId", "openid", "unionId"):
                    if src.get(k):
                        return str(src.get(k))
    return ""


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


def extract_token(data: Any) -> Optional[str]:
    """包内为 result.mobileToken；兼容 data.token 等。"""
    if not isinstance(data, dict):
        return None
    cands = [
        data.get("mobileToken"),
        data.get("token"),
        data.get("accessToken"),
        data.get("access_token"),
    ]
    for key in ("result", "data"):
        inner = data.get(key)
        if isinstance(inner, dict):
            cands += [
                inner.get("mobileToken"),
                inner.get("token"),
                inner.get("accessToken"),
                inner.get("access_token"),
            ]
    for c in cands:
        if c and c != "null":
            return str(c)
    return None


def extract_shop_id(data: Any) -> str:
    """优先登录返回的 shopId（包内 UserShopId），否则默认 SHOP_ID。"""
    if isinstance(data, dict):
        for src in (data.get("result"), data.get("data"), data):
            if isinstance(src, dict):
                sid = src.get("shopId") or src.get("UserShopId")
                if sid not in (None, "", "null"):
                    return str(sid)
    return SHOP_ID


def api_get(path: str, token: str, shop_id: str = "") -> Dict[str, Any]:
    """GET：先与包内一致 path+data(shopId) 签名；URL 拼 shopId 查询串。"""
    sid = str(shop_id or SHOP_ID)
    ts = int(time.time() * 1000)
    params = {"shopId": sid}
    sign, _ = compute_sign(params, ts, is_post=False)
    url = path if path.startswith("http") else f"{BASE_URL}{path}"
    sep = "&" if "?" in url else "?"
    url_q = f"{url}{sep}shopId={sid}"
    headers = common_headers(token, sign=sign, ts=str(ts), open_id=_LOGIN_OPENID)
    try:
        r = http("GET", url_q, headers=headers)
        data = r.json() if r.content else {}
        if not isinstance(data, dict):
            data = {"success": False, "msg": clean_line(data)}
        data["_http"] = r.status_code
        return data
    except Exception as e:
        msg = clean_line(e) or str(e)
        kind = "网络不可达"
        if re.search(r"ssl|certificate", msg, re.I):
            kind = "HTTPS异常"
        elif re.search(r"timeout|timed out", msg, re.I):
            kind = "请求超时"
        return {"success": False, "msg": f"{kind}: {msg[:80]}"}


def api_post(path: str, token: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """POST：body 与签名 JSON 完全一致。"""
    try:
        url = path if path.startswith("http") else f"{BASE_URL}{path}"
        ts = int(time.time() * 1000)
        sign, body_str = compute_sign(payload or {}, ts, is_post=True)
        headers = common_headers(token, sign=sign, ts=str(ts))
        r = http("POST", url, headers=headers, data=body_str.encode("utf-8"))
        try:
            data = r.json()
        except Exception:
            data = {"success": False, "msg": clean_line(r.text)[:80]}
        return data if isinstance(data, dict) else {"success": False, "msg": clean_line(data)}
    except Exception as e:
        msg = clean_line(e) or str(e)
        return {"success": False, "msg": f"请求失败: {msg[:80]}"}


def token_valid(token: str, shop_id: str = "") -> bool:
    resp = api_get("/mobile/customer/initMy", token, shop_id)
    return is_ok(resp)


def login_by_code(openid: str) -> Tuple[Optional[str], Dict[str, Any]]:
    print("🔐 使用 code 登录")
    code = get_wx_code(openid)
    print(f"ℹ️ 取码结果 code={'有' if code else '无'}")
    payload = {
        "code": code,
        "appid": APPID,
        "shopId": SHOP_ID,
        "envVersion": "",
        "isEnterpriseWx": False,
        "scene": "",
        "referrerInfo": "",
    }
    data = api_post("/mobile/wxAppLogin", "", payload)
    token = extract_token(data)
    global _LOGIN_OPENID
    _LOGIN_OPENID = extract_open_id(data)
    code_v = data.get("code") if data.get("code") is not None else ""
    result = data.get("result") if isinstance(data.get("result"), dict) else {}
    result_keys = list(result.keys())[:12]
    has_auth_url = bool(result.get("authCenterUrl") or result.get("qyAuthCenterUrl"))
    print(
        f"ℹ️ 登录响应 success={data.get('success')} code={code_v} "
        f"resultKeys={result_keys} openId={'有' if _LOGIN_OPENID else '无'} "
        f"authCenter={'有' if has_auth_url else '无'} "
        f"msg={clean_line(data.get('msg') or data.get('message') or '')[:40] or ('ok' if token else '无')}"
    )
    if has_auth_url:
        print("⚠️ 登录返回 authCenterUrl：需在小程序内完成授权/开卡后，业务接口才能调用")
    if not token:
        return None, {"message": clean_line(data.get("msg") or "登录响应未返回 mobileToken")}
    # 包内：授权后会拉 getNascentId；纯 HTTP 先尝试一次
    try:
        nas = api_get("/mobile/customer/getNascentId", token, extract_shop_id(data))
        print(f"ℹ️ getNascentId success={nas.get('success')} code={nas.get('code')} msg={err_msg(nas)[:40] or 'ok'}")
    except Exception:
        pass
    return token, data


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
            if time.time() * 1000 >= exp_ms - 600 * 1000:
                return None
    except Exception:
        pass
    return token


def write_cached_token(openid: str, token: str, raw_login: Dict[str, Any]) -> None:
    expire = datetime.fromtimestamp(time.time() + 6000).isoformat()
    cache = load_cache()
    cache[cache_key(openid)] = {"token": token, "expireTime": expire, "updateTime": datetime.now().isoformat()}
    save_cache(cache)


def clear_cached_token(openid: str) -> None:
    cache = load_cache()
    cache.pop(cache_key(openid), None)
    save_cache(cache)
    print("🗑️ 已删除本地 token 缓存")


def login_with_cache(openid: str) -> Tuple[Optional[str], Dict[str, Any]]:
    cached = read_cached_token(openid)
    if cached:
        if token_valid(cached, SHOP_ID):
            print("ℹ️ token缓存登录")
            return cached, {"shopId": SHOP_ID}
        print("⚠️ 缓存 token 失效，删除后 code 重登")
        clear_cached_token(openid)
        token, raw = login_by_code(openid)
        if token:
            write_cached_token(openid, token, raw or {})
            return token, raw or {}
        return None, {}
    token, raw = login_by_code(openid)
    if token:
        write_cached_token(openid, token, raw or {})
        return token, raw or {}
    return None, {}


def query_user(token: str, shop_id: str = "") -> Tuple[str, str]:
    """返回 (显示名, 脱敏手机)"""
    resp = api_get("/mobile/customer/initMy", token, shop_id)
    if not is_ok(resp):
        code = str(resp.get("code") if resp.get("code") is not None else "")
        msg = err_msg(resp)
        if code == "2025" or "授权失败" in msg:
            say("⚠️ code=2025 授权失败：请打开特步会员中心小程序完成授权/开卡后重试")
        else:
            say(f"⚠️ 用户信息: code={code} msg={msg} http={resp.get('_http')}")
        return "未知用户", ""
    customer = (resp.get("result") or {}).get("customer") or {}
    name = str(customer.get("customerName") or "未知用户")
    mobile = str(customer.get("mobile") or "")
    return name, mobile


def get_activity_id(token: str, shop_id: str = "") -> str:
    resp = api_get("/mobile/customer/queryMobilePersonCenterTemplateByShopId", token, shop_id)
    if not is_ok(resp):
        code = str(resp.get("code") if resp.get("code") is not None else "")
        msg = err_msg(resp)
        if code == "2025" or "授权失败" in msg:
            say("⚠️ 模板接口 code=2025 授权失败：需在小程序完成授权/开卡")
        else:
            say(f"⚠️ 获取模板失败: code={code} msg={msg}")
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


def query_points(token: str, shop_id: str = "") -> str:
    resp = api_get("/mobile/customer/getMyAllPoint", token, shop_id)
    if not is_ok(resp):
        return err_msg(resp) or "-"
    rows = resp.get("result") or []
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        return str(rows[0].get("score", "-"))
    return "-"


def do_sign(token: str, shop_id: str = "") -> Tuple[bool, str, Optional[float]]:
    activity_id = get_activity_id(token, shop_id)
    if not activity_id:
        return False, "未获取到 activityId ❌", None
    sid = str(shop_id or SHOP_ID)
    payload = {
        "shopId": sid,
        "activityId": activity_id,
        "signDate": datetime.now().strftime("%Y-%m-%d"),
    }
    resp = api_post("/mobile/activity/sign/sign", token, payload)
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

    def _biz(token: str, raw: Dict[str, Any], mode: str) -> Dict[str, Any]:
        extras.append(mode)
        shop_id = extract_shop_id(raw)
        extras.append(f"shopId {shop_id}")
        say(f"✅ 登录成功 token={mask_token(token)}")
        name, mobile = query_user(token, shop_id)
        acc["account"] = name or acc["account"]
        acc["phone"] = mobile
        extras.append(f"用户 {name} {mask_phone(mobile) if mobile else '-'}")
        time.sleep(1)
        ok, status, gain = do_sign(token, shop_id)
        acc["status"] = status
        acc["success"] = ok
        time.sleep(1)
        points = query_points(token, shop_id)
        extras.append(f"当前积分 {points}")
        if gain is not None:
            acc["reward"] = f"+{gain:g} 积分"
        elif points not in ("-", "", None) and "失败" not in str(points) and "授权" not in str(points):
            acc["reward"] = f"积分 {points}"
        if not ok:
            acc["error"] = status
        return acc

    def _fail(msg: str, status: str) -> Dict[str, Any]:
        acc["status"] = status
        acc["error"] = msg
        acc["success"] = False
        return acc

    try:
        token, raw = login_with_cache(openid)
        if not token:
            return _fail("登录失败", "登录失败 ❌")
        return _biz(token, raw or {}, "code登录")
    except Exception as e:
        msg = clean_line(e)
        say(f"❌ {msg}")
        if re.search(r"token|登录|401|未登录|过期", msg, re.I):
            say("⚠️ 登录态失效，删除缓存 → code 重登 → 重跑")
            extras.append("登录态失效 code重登")
            try:
                clear_cached_token(openid)
                token, raw = login_with_cache(openid)
                if not token:
                    return _fail("重登失败", "重登失败 ❌")
                return _biz(token, raw or {}, "缓存失效 code重登")
            except Exception as e2:
                msg2 = clean_line(e2)
                return _fail(msg2, f"重登重跑失败 ❌ ({msg2[:40]})")
        return _fail(msg, f"失败 ❌ ({msg[:40]})")


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
