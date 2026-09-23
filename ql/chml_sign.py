#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# /*
# ------------------------------------------
# @Author: onijiang0
# @Date: 2026.09.21
# @Description: 长虹美菱 - 小程序每日签到
# cron: 33 18 * * *
# #定时使用10-19点 随机时间 每天
# ------------------------------------------
# 变量名：chml
# 变量值：业务 openid，多账号换行或 & 分隔，可加 #备注
#
# 依赖变量：
# wx_server_url  必填，取码服务地址（使用者自备，勿写进仓库）
# wx_auth        必填，取码服务鉴权
# chmlck         选填，抓包 token（&/换行分隔）；填了则跳过 code 登录
# QL_NOTIFY      选填，0 关闭推送
# ------------------------------------------
# 已实现：
# 1. 多账号；缺变量报错；单号失败不中断
# 2. code 换 token（或 chmlck 直填 token）；本地缓存；失效删缓存后 code 重登并重跑
# 3. 每日签到；签到结果写入统一简报
# 4. send_notify 推送；平台参数写脚本默认值；账号类只走环境变量
#
# 契约（appid wx36c3413e8fe39263，hongke.changhong.com）：
# code     POST {wx_server_url}/wx/code auth:{wx_auth} json:{openid,appid}
# 登录     POST /gw/applet/login  body {code,appId} -> token/smarthome
#          （源脚本注明登录为推断端点；失败可 chmlck 抓包 token）
# 签到     POST /gw/applet/aggr/signin?aggrId=661
#          headers token/smarthome；status_code=200 或 message 含「成功」
# 日历     POST /gw/applet/aggr/signin/calendar?aggrId=661  （缓存 token 校验）
#
# 踩坑：
# 1. 原脚本有 token 缓存：失效必须删缓存再 code 重登后重跑
# 2. aggrId=661 为平台活动参数，已写入脚本
# 3. 登录接口为推断，失败时用 chmlck 或抓包核对
# 4. chmlck/手机号等账号信息不进 Git；日志脱敏
# 5. ★「网络不可达」的真相可能是 **DNS 解析失败**，不是网络不通、更不是 token 问题！
#    2026-09-23 面板侧实测：
#      DNS 解析 hongke.changhong.com → 连续 4 次 gaierror [Errno -3] Try again（每次卡满 5s）
#      TCP 直连该域名解析出的 IP:443  → OK 0.03s
#      IP+SNI 直接发 HTTPS            → HTTP 400 {"code":401,...,"必填参数token不存在!"}（0.12s）
#      baidu.com 对照                 → 200 0.13s
#    → 面板所在网络的解析器（容器 /etc/resolv.conf 继承宿主）解析该域名失败，
#      服务本身完全正常。requests 的报错文案会写成「网络不可达」，极具误导性。
#    本脚本已支持 CHML_HOST_IP 兜底：解析失败时把域名钉到指定 IP 直连（仍发原 Host/SNI）。
#
# 变量补充：
#   CHML_HOST_IP  选填，hongke.changhong.com 的 IP，DNS 挂掉时兜底直连
# ------------------------------------------
# */

from __future__ import annotations

import json
import os
import re
import socket
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
    from send_notify import clean_line, format_report, mask_id, notify_and_format
except Exception:
    def clean_line(line: Any) -> str:
        s = str(line or "").strip()
        return "" if s.startswith(("{", "[")) else s[:180]

    def mask_id(value: Any, keep: int = 6) -> str:
        s = str(value or "")
        return (s[:keep] + "***") if len(s) > keep else (s or "-")

    def format_report(task, accounts, push_result="", cost_s=None):
        return task

    def notify_and_format(task, accounts, **kwargs):
        print("🔔 推送结果：跳过（send_notify 不可用）")


APP_NAME = "长虹美菱"
APPID = "wx36c3413e8fe39263"
BASE_URL = "https://hongke.changhong.com"
LOGIN_URL = f"{BASE_URL}/gw/applet/login"
AGGR_ID = "661"  # 平台签到活动 id
SIGN_URL = f"{BASE_URL}/gw/applet/aggr/signin?aggrId={AGGR_ID}"
CALENDAR_URL = f"{BASE_URL}/gw/applet/aggr/signin/calendar?aggrId={AGGR_ID}"
REQUEST_TIMEOUT = 30

UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5_1 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.50 NetType/WIFI Language/zh_CN"
)


def say(msg: str) -> None:
    line = clean_line(msg)
    if line:
        print(line)


def split_openids(raw: str) -> List[str]:
    out: List[str] = []
    for part in (raw or "").replace("&", "\n").splitlines():
        s = part.strip()
        if not s or s.startswith("#"):
            continue
        out.append(s.split("#")[0].strip())
    return out


def mask_token(token: str) -> str:
    s = str(token or "")
    return f"有(len={len(s)})" if s else "无"


def direct_session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    return s


# ---------------- DNS 兜底（应对面板解析器故障） ----------------

HOST_IP_OVERRIDE = os.getenv("CHML_HOST_IP", "").strip()
_dns_state: Dict[str, Any] = {"checked": False, "resolvable": True, "used_fallback": False}


def _resolve_target() -> Tuple[str, bool]:
    """返回 (要连接的主机, 是否走了 IP 兜底)。

    面板实测过 DNS 解析该域名持续失败（gaierror Errno -3），而服务本身正常。
    配置 CHML_HOST_IP 后，解析失败即把请求钉到该 IP，避免整轮白跑。
    """
    if not HOST_IP_OVERRIDE:
        return BASE_URL, False
    if not _dns_state["checked"]:
        _dns_state["checked"] = True
        try:
            socket.getaddrinfo("hongke.changhong.com", 443, proto=socket.IPPROTO_TCP)
            _dns_state["resolvable"] = True
        except Exception as e:
            _dns_state["resolvable"] = False
            say(f"⚠️ DNS 解析 hongke.changhong.com 失败（{type(e).__name__}），改用 IP 兜底")
    if _dns_state["resolvable"]:
        return BASE_URL, False
    _dns_state["used_fallback"] = True
    return BASE_URL.replace("hongke.changhong.com", HOST_IP_OVERRIDE), True


def _request(method: str, url: str, **kwargs) -> requests.Response:
    """带 IP 兜底的请求。

    走 IP 时仍带 Host 头（verify 已关闭，故不涉及证书域名校验），
    对服务端而言与走域名等价。
    """
    target, pinned = _resolve_target()
    if pinned:
        headers = kwargs.setdefault("headers", {})
        headers.setdefault("Host", "hongke.changhong.com")
        return direct_session().request(method, url.replace(BASE_URL, target), **kwargs)
    return direct_session().request(method, url, **kwargs)


def http(method: str, url: str, **kwargs) -> requests.Response:
    kwargs.setdefault("timeout", REQUEST_TIMEOUT)
    kwargs.setdefault("verify", False)
    return _request(method, url, **kwargs)


def common_headers(token: str = "") -> Dict[str, str]:
    h = {
        "User-Agent": UA,
        "content-type": "application/json",
        "Referer": f"https://servicewechat.com/{APPID}/212/page-frame.html",
    }
    if token:
        h["token"] = token
        h["smarthome"] = token
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


def extract_token(data: Any) -> Optional[str]:
    if not isinstance(data, dict):
        return None
    cands = [data.get("token"), data.get("accessToken"), data.get("access_token"), data.get("smarthome")]
    inner = data.get("data")
    if isinstance(inner, dict):
        cands += [
            inner.get("token"),
            inner.get("accessToken"),
            inner.get("access_token"),
            inner.get("smarthome"),
        ]
    for c in cands:
        if c and str(c) != "null":
            return str(c).strip()
    return None


def cache_path() -> Path:
    configured = os.getenv("CHML_TOKEN_DIR", "").strip()
    if configured:
        folder = Path(configured)
    elif Path("/ql/data/config").is_dir():
        folder = Path("/ql/data/config/chml")
    else:
        folder = Path(__file__).resolve().parent / "chml_cache"
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
        inner = raw_login.get("data") if isinstance(raw_login.get("data"), dict) else {}
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
    cache[cache_key(openid)] = {
        "token": token,
        "expireTime": expire,
        "updateTime": datetime.now().isoformat(),
    }
    save_cache(cache)


def clear_cached_token(openid: str) -> None:
    cache = load_cache()
    cache.pop(cache_key(openid), None)
    save_cache(cache)
    print("🗑️ 已删除本地 token 缓存")


def api_post(url: str, token: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    try:
        r = http("POST", url, headers=common_headers(token), json=payload or {})
        status = r.status_code
        try:
            data = r.json()
        except Exception:
            data = {"message": clean_line(r.text)[:80]}
        if not isinstance(data, dict):
            data = {"message": str(data)}
        data.setdefault("status_code", status)
        return data
    except Exception as e:
        msg = clean_line(e) or str(e)
        kind = "网络不可达"
        # 解析失败必须单独标出来：它常被 requests 笼统写成 ConnectionError，
        # 让人误以为「服务挂了」，实际只是 DNS 不通。
        # 实测 requests 的真实文案（urllib3 v2）：
        #   NameResolutionError("... Failed to resolve 'host' ([Errno -3] Try again)")
        if re.search(
            r"NameResolutionError|Failed to resolve|name resolution|"
            r"Name or service not known|nodename|gaierror|-?3\] Try again",
            msg,
            re.I,
        ):
            kind = "DNS解析失败"
        elif re.search(r"SSLError|ssl|certificate", msg, re.I):
            kind = "HTTPS/证书异常"
        elif re.search(r"timed out|timeout", msg, re.I):
            kind = "请求超时"
        elif re.search(r"Connection refused|拒绝", msg, re.I):
            kind = "连接被拒"
        hint = "" if HOST_IP_OVERRIDE else "（可设 CHML_HOST_IP 兜底）"
        return {"status_code": -1, "message": f"{kind}: {msg[:80]}{hint}"}


def is_success(resp: Dict[str, Any]) -> bool:
    if resp.get("code") in (0, "0", 200, "200") and resp.get("success") in (True, None):
        if resp.get("success") is False:
            return False
        if resp.get("status_code") in (200, 2000, None) or resp.get("code") in (0, "0", 200, "200"):
            msg = str(resp.get("message") or "")
            if re.search(r"失败|过期|错误", msg):
                return False
            return True
    if resp.get("status_code") == 200 or str(resp.get("statusCode")) == "200":
        msg = str(resp.get("message") or "")
        if re.search(r"token过期|解析失败|重新登录", msg):
            return False
        return True
    return "成功" in str(resp.get("message") or "")


def is_token_expired(resp: Dict[str, Any]) -> bool:
    msg = str(resp.get("message") or resp.get("msg") or "")
    code = str(resp.get("code") if resp.get("code") is not None else "")
    return bool(
        re.search(r"token过期|解析失败|请重新登录|未登录|401", msg)
        or code in ("401", "40101", "100401")
        or resp.get("status_code") == 401
    )


def token_valid(token: str) -> bool:
    resp = api_post(CALENDAR_URL, token, {})
    if is_token_expired(resp):
        return False
    if resp.get("status_code") == 200:
        return True
    if resp.get("status_code") in (404, 405) and not str(resp.get("message") or "").startswith("JSON"):
        return True
    if is_success(resp):
        return True
    # 网络失败时不把缓存判死
    if resp.get("status_code") == -1:
        return True
    return False


def login_by_code(openid: str) -> Tuple[Optional[str], Dict[str, Any]]:
    print("🔐 使用 code 登录")
    code = get_wx_code(openid)
    print(f"ℹ️ 取码结果 code={'有' if code else '无'}")
    try:
        r = http("POST", LOGIN_URL, headers=common_headers(), json={"code": code, "appId": APPID})
        try:
            data = r.json()
        except Exception:
            data = {"raw": clean_line(r.text)[:80], "status_code": r.status_code}
    except Exception as e:
        return None, {"message": clean_line(e)}
    if isinstance(data, dict) and "status_code" not in data:
        data.setdefault("status_code", r.status_code)
    msg = clean_line((data or {}).get("message") or (data or {}).get("msg") or "")
    print(f"ℹ️ 登录响应 status_code={(data or {}).get('status_code')} msg={msg[:60] or '无'}")
    token = extract_token(data)
    if not token:
        return None, {"message": msg or "登录响应未返回 token（可配置 chmlck）"}
    return token, data if isinstance(data, dict) else {}


def load_manual_tokens() -> List[str]:
    raw = os.getenv("chmlck", "").strip()
    if not raw:
        return []
    return [item.strip() for item in re.split(r"[\n&]", raw) if item.strip()]


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


def do_sign(token: str) -> Tuple[bool, str]:
    resp = api_post(SIGN_URL, token, {})
    msg = clean_line(resp.get("message") or resp.get("msg") or "")
    print(
        f"ℹ️ 签到响应 status_code={resp.get('status_code')} "
        f"code={resp.get('code')} msg={msg[:60] or '无'}"
    )
    if is_token_expired(resp):
        return False, f"{msg[:40] or 'token过期'} ❌"
    if resp.get("status_code") == -1:
        return False, f"{msg[:50] or '网络不可达'} ❌"
    if is_success(resp):
        text = msg or "签到成功"
        return True, f"{text} ✅" if ("成功" in text or "已签" in text) else f"{text} ✅"
    return False, f"{(msg or '签到失败')[:40]} ❌"


def run_account(openid: str, index: int, total: int, manual_token: Optional[str] = None) -> Dict[str, Any]:
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

    def _finish(ok: bool, status: str, mode: str) -> Dict[str, Any]:
        extras.append(mode)
        acc["status"] = status
        acc["success"] = ok
        if not ok:
            acc["error"] = status
        return acc

    def _try_code_login() -> Tuple[Optional[str], str]:
        token, mode = login_with_cache(openid)
        return token, mode

    # 1) chmlck 抓包 token（可能过期）
    if manual_token:
        print("🔑 使用 chmlck 抓包 token")
        say(f"✅ token={mask_token(manual_token)}（未校验有效性）")
        ok, status = do_sign(manual_token)
        if ok:
            return _finish(True, status, "chmlck token")
        if re.search(r"token过期|解析失败|重新登录", status):
            extras.append("chmlck 已过期")
            print("⚠️ chmlck token 已过期，尝试 openid code 登录")
            if openid and not str(openid).startswith("manual:"):
                try:
                    token, mode = _try_code_login()
                    if token:
                        say(f"✅ 重新登录成功 token={mask_token(token)}")
                        ok2, status2 = do_sign(token)
                        return _finish(ok2, status2, f"chmlck过期→{mode}")
                    return _finish(False, "chmlck过期且 code 登录失败 ❌", "chmlck过期")
                except Exception as e2:
                    return _finish(False, f"chmlck过期 code登录异常 ❌ ({clean_line(e2)[:30]})", "chmlck过期")
            return _finish(False, "chmlck 已过期，请更新抓包 token ❌", "chmlck token")
        return _finish(False, status, "chmlck token")

    # 2) code + 缓存登录
    def _sign_flow(token: str, mode: str) -> Dict[str, Any]:
        say(f"✅ 登录成功 token={mask_token(token)}")
        ok, status = do_sign(token)
        return _finish(ok, status, mode)

    try:
        token, mode = _try_code_login()
        if not token:
            return _finish(False, "登录失败 ❌（可配置 chmlck）", "code登录失败")
        return _sign_flow(token, mode)
    except Exception as e:
        msg = clean_line(e)
        say(f"❌ {msg}")
        if re.search(r"token|登录|401|未登录", msg, re.I):
            say("⚠️ 登录态失效，删除缓存 → code 重登 → 重跑")
            extras.append("登录态失效 code重登")
            try:
                clear_cached_token(openid)
                token, mode = _try_code_login()
                if not token:
                    return _finish(False, "重登失败 ❌", "重登失败")
                return _sign_flow(token, mode)
            except Exception as e2:
                msg2 = clean_line(e2)
                return _finish(False, f"重登重跑失败 ❌ ({msg2[:40]})", "重登重跑失败")
        return _finish(False, f"失败 ❌ ({msg[:40]})", "异常")


def main() -> int:
    started = time.time()
    openids = split_openids(os.getenv("chml", "").strip())
    manual_tokens = load_manual_tokens()
    if not openids and not manual_tokens:
        print("❌ 未配置 chml（openid）或 chmlck（抓包 token）")
        return 1
    if not openids and manual_tokens:
        # 仅 chmlck 时，openid 位用 token 摘要占位（不打印完整 token）
        openids = [f"manual:{i + 1}" for i in range(len(manual_tokens))]
    if not os.getenv("wx_server_url", "").strip() or not os.getenv("wx_auth", "").strip():
        if not manual_tokens:
            print("❌ 未配置 wx_server_url / wx_auth")
            return 1
    print(f"{APP_NAME} | {len(openids)}账号")

    results: List[Dict[str, Any]] = []
    for i, openid in enumerate(openids, 1):
        manual = manual_tokens[i - 1] if manual_tokens and i - 1 < len(manual_tokens) else None
        try:
            results.append(run_account(openid, i, len(openids), manual_token=manual))
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
