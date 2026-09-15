#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# /*
# ------------------------------------------
# @Description: 中国移动10086签到 - smallcat /wx/code 换 QWHD_SESSION_TOKEN + 每日签到
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
# smallcat  POST /wx/code  json:{openid, appid} -> data.code
# OAuth     GET https://wx.10086.cn/qwhdhub/qwhdmark/{id}?code={code}&yx=..&touch_id=..
#           跟随重定向累加 Cookie -> Set-Cookie: QWHD_SESSION_TOKEN
# 签到      POST /qwhdhub/api/mark/do/mark  Cookie: QWHD_SESSION_TOKEN=...
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
from urllib.parse import quote, urljoin, urlparse

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
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
    max_hops: int = 20,
    timeout: int = 15,
) -> Tuple[Dict[str, str], int]:
    cookie_map = parse_cookie_str(base_cookie)
    current = start_url
    http_status = 0

    for _ in range(max_hops + 1):
        cookie_header = "; ".join(f"{k}={v}" for k, v in cookie_map.items())
        host = urlparse(current).netloc
        resp = requests.get(
            current,
            headers={
                "Host": host, "User-Agent": UA, "Cookie": cookie_header,
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
            allow_redirects=False, timeout=timeout,
        )
        http_status = resp.status_code
        for k, v in resp.cookies.get_dict().items():
            cookie_map[k] = v
        loc = resp.headers.get("Location", "")
        if 300 <= resp.status_code < 400 and loc:
            current = urljoin(current, loc)
            continue
        break
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
            json={}, timeout=15,
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
    用 smallcat /wx/code 拿 code，拼到活动页 URL，跟随重定向拿 Cookie。
    10086 活动页收到 code 后由服务端完成 SSO 并签发 QWHD_SESSION_TOKEN。
    """
    code = sm.wx_code(openid, appid)
    log.info("wx/code 获取成功: %s...", code[:12])

    # 拼活动页 URL，带上 code
    activity = (
        f"{BASE}/qwhdhub/qwhdmark/{ACTIVITY_ID}"
        f"?code={quote(code)}"
        f"&ys=&yx={cmcc.yx}&touch_id={cmcc.touch_id}"
    )

    cm, status = follow_redirects(activity, timeout=timeout)

    if "QWHD_SESSION_TOKEN" not in cm:
        # 可能需要走 authorize 跳转，再试一次带 state
        oauth_url = (
            f"https://open.weixin.qq.com/connect/oauth2/authorize"
            f"?appid={appid}"
            f"&redirect_uri={quote(activity, safe='')}"
            f"&response_type=code&scope=snsapi_base&state=STATE#wechat_redirect"
        )
        cm2, status2 = follow_redirects(oauth_url, timeout=timeout)
        cm.update(cm2)
        status = status2

    if "QWHD_SESSION_TOKEN" not in cm:
        raise RuntimeError(
            f"未获取到 QWHD_SESSION_TOKEN (HTTP {status})。"
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
    appid = os.getenv("cmcc_appid", APPID_DEFAULT).strip()
    yx = os.getenv("cmcc_yx", "JHT042591F0005").strip()
    touch_id = os.getenv("cmcc_touch_id", "26-05-10005-2007-A01").strip()

    if not base:
        log.error("缺少 wx_server_url")
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

    cmcc = CMCC(yx, touch_id)
    cache = load_cache()

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
