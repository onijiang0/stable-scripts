#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# /*
# ------------------------------------------
# @Description: 中国移动10086签到 - smallcat /wx/oauth 公众号OAuth + 每日签到
# cron: 30 8 * * *
# ------------------------------------------
# 变量名：cmcc
# 变量值：smallcat openid，多账号 & 分隔；留空自动读 smallcat 全部账号
#
# 依赖变量：
# wx_server_url  必填，smallcat 地址
# wx_auth        必填，smallcat 调用 API AUTH
# cmcc_appid     可选，默认 wx43a850f87498127d（10086公众号）
# cmcc_yx        可选，默认 JHT042591F0005
# cmcc_touch_id  可选，默认 26-05-10005-2007-A01
# ------------------------------------------
# 契约：
# 两段式公众号 OAuth（复刻傻妞 container.SmallCat.oauth）：
#   ① POST {smallcat}/wx/oauth -> data.full_url
#   ② GET full_url 跟随重定向，遇微信 authorize 停下，提取 bindAccount 回跳
#   ③ POST {smallcat}/wx/oauth (redirect_uri=bindAccount回跳) -> full_url
#   ④ GET full_url 跟随重定向 -> Set-Cookie: QWHD_SESSION_TOKEN
# 签到 POST /qwhdhub/api/mark/do/mark  Cookie: QWHD_SESSION_TOKEN=...
# 已签到关键词: 已签|重复|already|TODAY_MARKED
# 缓存 6h；失败自动重登；多账号 sleep 3s
# ------------------------------------------
# */

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote, urljoin, urlparse

import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(level=logging.DEBUG if os.getenv("CMCC_DEBUG") else logging.INFO,
                    format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("CMCC")

APPID_DEFAULT = "wx43a850f87498127d"
ACTIVITY_ID = "1021122301"
BASE = "https://wx.10086.cn"
MARK_URL = f"{BASE}/qwhdhub/api/mark/do/mark"
PRIZE_URL = f"{BASE}/qwhdhub/api/mark/info/prizeInfo"

UA = (
    "Mozilla/5.0 (Linux; Android 17; 2509FPN0BC Build/CP2A.260605.016; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
    "Chrome/150.0.7871.189 Mobile Safari/537.36 XWEB/1500117 "
    "MMWEBSDK/20260502 MMWEBID/9885 "
    "MicroMessenger/8.0.76.3141(0x28004C31) WeChat/arm64 Weixin "
    "NetType/WIFI Language/zh_CN ABI/arm64 miniProgram/wx43aab19a93a3a6f2"
)
ALREADY_RE = re.compile(r"已签|已经签|签到过|重复|already|TODAY_MARKED", re.I)


class Smallcat:
    def __init__(self, base: str, auth: str, timeout: int = 20):
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.s = requests.Session()
        self.s.verify = False  # smallcat 自签证书
        self.s.headers.update({"auth": auth, "User-Agent": UA})

    def accounts(self) -> List[Dict[str, Any]]:
        r = self.s.get(f"{self.base}/api/accounts", timeout=self.timeout)
        r.raise_for_status()
        return (r.json().get("data") or {}).get("items") or []

    def wx_code(self, openid: str, appid: str) -> str:
        """POST /wx/code 获取微信 code（限流 ~8次/90秒）"""
        r = self.s.post(
            f"{self.base}/wx/code",
            json={"openid": openid, "appid": appid},
            timeout=self.timeout,
        )
        r.raise_for_status()
        data = r.json()
        if not data.get("status"):
            raise RuntimeError(f"smallcat /wx/code 失败: {data.get('message')}")
        code = (data.get("data") or {}).get("code") or ""
        if not code:
            raise RuntimeError("smallcat 未返回 code（可能限流）")
        return code

    def oauth(self, appid: str, openid: str, redirect_uri: str,
              scope: str = "snsapi_base", state: str = "") -> str:
        """
        公众号 OAuth：POST /wx/oauth -> data.full_url
        对应傻妞 container.SmallCat.oauth()，用于需要 wmhToken 的 H5 活动页。
        注意：status=false 时 full_url 仍可能有效（预验证 redirect_uri 失败不影响实际 OAuth）。
        """
        r = self.s.post(
            f"{self.base}/wx/oauth",
            json={"appid": appid, "openid": openid, "scope": scope,
                  "redirect_uri": redirect_uri, "state": state},
            timeout=self.timeout,
        )
        r.raise_for_status()
        data = r.json()
        full_url = (data.get("data") or {}).get("full_url") or ""
        if not full_url:
            err = (data.get("data") or {}).get("error", "")
            raise RuntimeError(f"smallcat /wx/oauth 未返回 full_url: {data.get('message')} {err}")
        if not data.get("status"):
            log.debug(f"oauth status=false 但有 full_url: {data.get('message')}")
        return full_url
        return full_url


def parse_cookie_str(s: str) -> Dict[str, str]:
    m: Dict[str, str] = {}
    for p in (s or "").split(";"):
        p = p.strip()
        if not p:
            continue
        i = p.find("=")
        if i > 0:
            m[p[:i].strip()] = p[i + 1:].strip()
    return m


def follow_redirects(
    start_url: str,
    base_cookie: str = "",
    stop_at_authorize: bool = False,
    max_hops: int = 20,
    timeout: int = 15,
) -> Tuple[Dict[str, str], int]:
    cookie_map = parse_cookie_str(base_cookie)
    current = start_url
    http_status = 0
    authorize_marker = "open.weixin.qq.com/connect/oauth2/authorize"

    for hop in range(max_hops + 1):
        cookie_header = "; ".join(f"{k}={v}" for k, v in cookie_map.items())
        host = urlparse(current).netloc
        resp = requests.get(
            current,
            headers={
                "Host": host, "User-Agent": UA, "Cookie": cookie_header,
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
            allow_redirects=False, timeout=timeout, verify=False,
        )
        http_status = resp.status_code
        new_cookies = resp.cookies.get_dict()
        for k, v in new_cookies.items():
            cookie_map[k] = v
        loc = resp.headers.get("Location", "")
        log.debug(f"  hop{hop}: {resp.status_code} {current[:100]} -> loc={loc[:100] if loc else '-'} cookies={list(new_cookies.keys())}")
        if resp.status_code == 200 and not loc:
            body = resp.text or ""
            log.debug(f"  body({len(body)} bytes): {body[:3000]}")
            # 尝试从页面提取 redirect URL 或表单 action
            import re as _re
            # 方法1: JS redirect
            m = _re.search(r'(?:window\.location(?:\.href)?|location\.href)\s*=\s*["\']([^"\']+)["\']', body)
            if m:
                log.info(f"  从页面提取到 redirect: {m.group(1)[:100]}")
                current = urljoin(current, m.group(1))
                continue
            # 方法2: 表单提交（微信授权确认页）
            m2 = _re.search(r'<form[^>]*action=["\']([^"\']+)["\']', body)
            if m2:
                action = m2.group(1)
                log.info(f"  发现表单 action: {action[:100]}")
                # 提取表单字段
                inputs = _re.findall(r'<input[^>]*name=["\']([^"\']+)["\'][^>]*value=["\']([^"\']*)["\']', body)
                if not inputs:
                    inputs = _re.findall(r'<input[^>]*value=["\']([^"\']*)["\'][^>]*name=["\']([^"\']+)["\']', body)
                    inputs = [(v, n) for n, v in inputs]
                form_data = dict(inputs)
                log.debug(f"  表单字段: {list(form_data.keys())}")
                # POST 表单
                resp2 = requests.post(
                    urljoin(current, action),
                    data=form_data,
                    headers={"User-Agent": UA, "Referer": current,
                             "Content-Type": "application/x-www-form-urlencoded"},
                    allow_redirects=False, timeout=timeout, verify=False,
                )
                for k, v in resp2.cookies.get_dict().items():
                    cookie_map[k] = v
                loc2 = resp2.headers.get("Location", "")
                log.info(f"  表单提交: {resp2.status_code} -> loc={loc2[:100] if loc2 else '-'}")
                if 300 <= resp2.status_code < 400 and loc2:
                    current = urljoin(current, loc2)
                    continue
                # 如果返回 200，可能是成功页面，检查 cookies
                if "QWHD_SESSION_TOKEN" in cookie_map or "code=" in current:
                    break
        if stop_at_authorize and loc and authorize_marker in loc:
            cookie_map["_authorize_url"] = loc
            break
        if 300 <= resp.status_code < 400 and loc:
            current = urljoin(current, loc)
            continue
        break
    log.info(f"  跳转结束: HTTP {http_status}, cookies={list(cookie_map.keys())}")
    return cookie_map, http_status


class CMCC:
    def __init__(self, yx: str, touch_id: str):
        self.yx = yx
        self.touch_id = touch_id

    def _referer(self) -> str:
        return f"{BASE}/qwhdhub/qwhdmark/{ACTIVITY_ID}?ys=&yx={self.yx}&touch_id={self.touch_id}#/"

    def _cookie(self, cm: Dict[str, str]) -> str:
        parts = [f"yx={self.yx}", f"touch_id={self.touch_id}"]
        parts.extend(f"{k}={v}" for k, v in cm.items() if k not in ("yx", "touch_id"))
        return "; ".join(parts)

    def _post(self, url: str, cm: Dict[str, str]) -> Dict[str, Any]:
        resp = requests.post(
            url,
            headers={
                "Host": "wx.10086.cn",
                "Content-Type": "application/json;charset=UTF-8",
                "Accept": "*/*", "Origin": BASE, "Referer": self._referer(),
                "x-requested-with": "XMLHttpRequest", "login-check": "1",
                "User-Agent": UA, "Cookie": self._cookie(cm),
            },
            json={}, timeout=15, verify=False,
        )
        try:
            return resp.json() or {}
        except Exception:
            return {}

    def do_mark(self, cm: Dict[str, str]) -> Dict[str, Any]:
        return self._post(MARK_URL, cm)

    def prize_info(self, cm: Dict[str, str]) -> Dict[str, Any]:
        return self._post(PRIZE_URL, cm)


def interpret_sign(raw: Dict[str, Any]) -> Tuple[bool, str]:
    if not raw or not isinstance(raw, dict):
        return False, "未知返回"
    code = raw.get("code")
    msg = str(raw.get("msg") or "").strip()
    success = raw.get("success") is True
    is_done = bool(ALREADY_RE.search(msg)) or code == "TODAY_MARKED"
    if success or code == "SUCCESS" or code in (0, "0") or is_done:
        prize = (raw.get("data") or {}).get("prizeName")
        label = "今日已签到" if is_done else (msg or "签到成功")
        if prize:
            label += f" | 奖品: {prize}"
        return True, label
    return False, msg or f"code={code}"


def acquire_cookie(sm: Smallcat, cmcc: CMCC, appid: str, openid: str,
                   timeout: int = 30) -> Dict[str, str]:
    """
    公众号 OAuth -> 跟随 SSO 跳转链 -> 拿 QWHD_SESSION_TOKEN
    """
    activity = f"{BASE}/qwhdhub/qwhdmark/{ACTIVITY_ID}?ys=&yx={cmcc.yx}&touch_id={cmcc.touch_id}"

    # 1. smallcat OAuth 拿业务回调 URL（带 code）
    full1 = sm.oauth(appid, openid, activity)
    log.info(f"oauth full_url: {full1}")

    # 2. 跟随 full_url，不阻止 authorize，看完整跳转链
    cm, status = follow_redirects(full1, max_hops=20, timeout=timeout)
    log.info(f"跟随后 cookies: {list(cm.keys())}, HTTP {status}")

    if "QWHD_SESSION_TOKEN" in cm:
        return cm

    # 3. 如果有 d.sid，试试从 qwhdsso/redirect 拿 session
    if "d.sid" in cm:
        sid_url = f"{BASE}/qwhdsso/redirect?sid={cm['d.sid']}"
        log.info(f"尝试 qwhdsso/redirect: {sid_url[:80]}...")
        cm2, status2 = follow_redirects(sid_url, timeout=timeout)
        cm.update(cm2)
        log.info(f"qwhdsso 后 cookies: {list(cm.keys())}")

    if "QWHD_SESSION_TOKEN" not in cm:
        raise RuntimeError(
            f"未获取到 QWHD_SESSION_TOKEN。cookies: {list(cm.keys())}。"
            f"请确认 smallcat 账号已授权该公众号 (appid={appid})"
        )
    return cm


# ========== 缓存 ==========
def cache_path() -> Path:
    env = os.getenv("cmcc_cache", "").strip()
    return Path(env) if env else Path(__file__).resolve().parent / "cmcc_login_cache.json"


def load_cache() -> Dict[str, Any]:
    p = cache_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_cache(data: Dict[str, Any]) -> None:
    try:
        cache_path().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning("缓存写入失败: %s", e)


def split_openids(raw: str) -> List[str]:
    return [p.strip() for p in raw.replace("&", "\n").replace(",", "\n").splitlines()
            if p.strip() and not p.strip().startswith("#")]


def mask(o: str) -> str:
    o = str(o or "")
    return o[:8] + "***" if len(o) > 8 else o


def run_one(sm: Smallcat, cmcc: CMCC, appid: str, openid: str,
            cache: Dict[str, Any], force: bool = False) -> str:
    key = f"{appid}:{openid}"
    hit = cache.get(key)
    cm: Dict[str, str] = {}

    if hit and not force:
        ts = hit.get("_ts") or 0
        if time.time() - ts < 6 * 3600 and hit.get("cookie"):
            cm = parse_cookie_str(hit["cookie"])

    if not cm or "QWHD_SESSION_TOKEN" not in cm:
        log.info("获取 Cookie (openid=%s)...", mask(openid))
        cm = acquire_cookie(sm, cmcc, appid, openid)
        cache[key] = {
            "openid": openid,
            "cookie": "; ".join(f"{k}={v}" for k, v in cm.items()),
            "_ts": time.time(),
        }
        save_cache(cache)
        log.info("Cookie 获取成功")

    try:
        before = cmcc.prize_info(cm)
        bd = before.get("data") or {}
        log.info("状态: 已签 %s/%s | 今日已签: %s",
                 bd.get("markedTimes", "?"), bd.get("totalMarkTimes", "?"), bd.get("todayMarked"))
    except Exception:
        pass

    log.info(">>> 执行签到 <<<")
    raw = cmcc.do_mark(cm)
    ok, msg = interpret_sign(raw)

    if not ok and not force:
        log.warning("签到失败(%s)，尝试重登...", msg)
        return run_one(sm, cmcc, appid, openid, cache, force=True)

    return ("✅" if ok else "❌") + f" [{mask(openid)}] {msg}"


def main() -> int:
    auth = os.getenv("wx_auth", "").strip()
    base = os.getenv("wx_server_url", "").strip()
    openids_raw = os.getenv("cmcc", "").strip()
    manual_cookie = os.getenv("cmcc_cookie", "").strip()
    appid = os.getenv("cmcc_appid", APPID_DEFAULT).strip()
    yx = os.getenv("cmcc_yx", "JHT042591F0005").strip()
    touch_id = os.getenv("cmcc_touch_id", "26-05-10005-2007-A01").strip()

    cmcc = CMCC(yx, touch_id)
    cache = load_cache()

    # 手动 Cookie 模式：跳过 OAuth，直接用提供的 Cookie 签到
    if manual_cookie:
        log.info("=" * 40)
        log.info("中国移动10086签到 (手动 Cookie 模式)")
        log.info("=" * 40)
        cm = parse_cookie_str(manual_cookie)
        if "QWHD_SESSION_TOKEN" not in cm:
            log.error("cmcc_cookie 缺少 QWHD_SESSION_TOKEN")
            return 1
        try:
            before = cmcc.prize_info(cm)
            bd = before.get("data") or {}
            log.info("状态: 已签 %s/%s | 今日已签: %s",
                     bd.get("markedTimes", "?"), bd.get("totalMarkTimes", "?"), bd.get("todayMarked"))
        except Exception:
            pass
        log.info(">>> 执行签到 <<<")
        raw = cmcc.do_mark(cm)
        ok, msg = interpret_sign(raw)
        label = ("✅" if ok else "❌") + f" {msg}"
        log.info(label)
        print("\n" + "=" * 40)
        print(f"  {label}")
        print("=" * 40)
        return 0 if ok else 1

    # OAuth 模式需要 smallcat
    if not base:
        log.error("缺少 wx_server_url（OAuth 模式）或 cmcc_cookie（手动模式）")
        return 1
    if not auth:
        log.error("缺少 wx_auth")
        return 1

    sm = Smallcat(base, auth)

    if openids_raw:
        openids = split_openids(openids_raw)
    else:
        log.info("未设置 cmcc 变量，自动读取 smallcat 全部账号")
        try:
            accounts = sm.accounts()
            openids = [a["openid"] for a in accounts if a.get("openid") and not a.get("disabled")]
        except Exception as e:
            log.error("读取 smallcat 账号失败: %s", e)
            return 1

    if not openids:
        log.error("无可用 openid")
        return 1

    log.info("=" * 40)
    log.info("中国移动10086签到 (共 %d 个账号)", len(openids))
    log.info("=" * 40)

    lines = []
    for oid in openids:
        try:
            line = run_one(sm, cmcc, appid, oid, cache)
        except Exception as e:
            line = f"❌ [{mask(oid)}] 异常: {e}"
        log.info(line)
        lines.append(line)
        time.sleep(3)

    ok_count = sum(1 for l in lines if "✅" in l)
    print("\n" + "=" * 40)
    print(f"  中国移动签到简报  {ok_count}/{len(lines)} 成功")
    print("=" * 40)
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
