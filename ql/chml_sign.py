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


def http(method: str, url: str, **kwargs) -> requests.Response:
    kwargs.setdefault("timeout", REQUEST_TIMEOUT)
    kwargs.setdefault("verify", False)
    return direct_session().request(method, url, **kwargs)


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
        data = r.json()
        if isinstance(data, dict) and "status_code" not in data:
            data = dict(data)
            data.setdefault("status_code", r.status_code)
        return data if isinstance(data, dict) else {"status_code": r.status_code, "message": str(data)}
    except Exception as e:
        return {"status_code": -1, "message": clean_line(e)}


def is_success(resp: Dict[str, Any]) -> bool:
    if resp.get("status_code") == 200 or str(resp.get("statusCode")) == "200":
        return True
    return "成功" in str(resp.get("message") or "")


def token_valid(token: str) -> bool:
    resp = api_post(CALENDAR_URL, token, {})
    if resp.get("status_code") == 200:
        return True
    if resp.get("status_code") in (404, 405) and not str(resp.get("message") or "").startswith("JSON"):
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
    print(f"ℹ️ 签到响应 status_code={resp.get('status_code')} msg={clean_line(resp.get('message') or resp.get('msg') or '')[:60]}")
    if is_success(resp):
        msg = clean_line(resp.get("message") or resp.get("msg") or "签到成功")
        return True, f"{msg} ✅" if "成功" in msg or "已签" in msg else f"{msg} ✅"
    msg = clean_line(resp.get("message") or resp.get("msg") or "签到失败")
    return False, f"{msg[:40]} ❌"


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

    def _sign(token: str, mode: str) -> Dict[str, Any]:
        extras.append(mode)
        say(f"✅ 登录成功 token={mask_token(token)}")
        ok, status = do_sign(token)
        acc["status"] = status
        acc["success"] = ok
        if not ok:
            acc["error"] = status
        return acc

    def _fail(msg: str, status: str) -> Dict[str, Any]:
        acc["status"] = status
        acc["error"] = msg
        acc["success"] = False
        return acc

    try:
        if manual_token:
            print("🔑 使用 chmlck 抓包 token")
            return _sign(manual_token, "chmlck token")
        token, mode = login_with_cache(openid)
        if not token:
            return _fail("登录失败", "登录失败 ❌（可配置 chmlck）")
        return _sign(token, mode)
    except Exception as e:
        msg = clean_line(e)
        say(f"❌ {msg}")
        if re.search(r"token|登录|401|未登录", msg, re.I) and not manual_token:
            say("⚠️ 登录态失效，删除缓存 → code 重登 → 重跑")
            extras.append("登录态失效 code重登")
            try:
                clear_cached_token(openid)
                token, mode = login_with_cache(openid)
                if not token:
                    return _fail("重登失败", "重登失败 ❌")
                return _sign(token, mode)
            except Exception as e2:
                msg2 = clean_line(e2)
                return _fail(msg2, f"重登重跑失败 ❌ ({msg2[:40]})")
        return _fail(msg, f"失败 ❌ ({msg[:40]})")


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
