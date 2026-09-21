#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# /*
# ------------------------------------------
# @Author: onijiang0
# @Date: 2026.09.21
# @Description: 沪上阿姨 - 企迈 code 登录 + 每日签到/积分
# cron: 41 15 * * *
# #定时使用10-19点 随机时间 每天
# ------------------------------------------
# 变量名：hsay
# 变量值：业务 openid，多账号换行或 & 分隔，可加 #备注
#
# 依赖变量：
# wx_server_url      必填，取码服务地址（使用者自备，勿写进仓库）
# wx_auth            必填，取码服务鉴权
# HSAY_BRAND_ID      选填，覆盖企迈 brandId（默认已写脚本，反编译 storeId）
# HSAY_ACTIVITY_ID   选填，签到活动 id（平台侧可能按月变更）
# QL_NOTIFY          选填，0 关闭推送
# ------------------------------------------
# 已实现：
# 1. 多账号；缺变量明确报错；单号失败不中断
# 2. code 换 qm-user-token；本地缓存；失效删缓存后 code 重登并重跑
# 3. 每日签到 + 积分查询；签到/积分结果写入统一简报
# 4. send_notify 推送；脱敏日志；平台密钥/brandId 写在脚本默认值
#
# 契约（appid wxd92a2d29f8022f40，企迈 qmai，品牌「沪上阿姨点单」）：
# code     POST {wx_server_url}/wx/code auth:{wx_auth} json:{openid,appid}
# 登录     POST https://webapi.qmai.cn/web/account-center/oauth/mini-app-login
#          AES-GCM body {code,eVersion:"1.0",brandId} -> token
#          brandId 默认 201424（包内 storeId/brandId 同源）
# 签到     POST /web/cmk-center/sign/takePartInSign
#          AES-GCM body {activityId,appid}
#          code=0 且 status=true 签到成功；code=400041 多为已签
# 积分     POST /web/catering2-apiserver/crm/points-info
#          AES-GCM body {appid} -> data.totalPoints
# 加密     全站 AES-256-GCM payload + 头 QM-Encrypt-Meta
#          SDK 固定串写在脚本内（非账号密钥）
#
# 踩坑：
# 1. 原脚本 brandId 默认为空，登录会报 10102「品牌Id为空」
# 2. 平台 ID（brandId/storeId/activityId）应从原脚本或反编译回填进默认值
# 3. 原脚本有 token 缓存：失效必须删缓存再 code 重登后重跑
# 4. 登录也是 AES-GCM，不是明文 JSON
# 5. 活动 id 会变，可用 HSAY_ACTIVITY_ID 覆盖
# ------------------------------------------
# */

from __future__ import annotations

import base64
import hashlib
import json
import os
import random
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

try:
    from Crypto.Cipher import AES
except ImportError:
    AES = None  # type: ignore


APP_NAME = "沪上阿姨"
APPID = "wxd92a2d29f8022f40"
# 平台参数（非账号密钥，写进脚本默认值；原脚本 brandId 为空，反编译 storeId=brandId）
KEY_RAW = "mN6KpXq8Sv2WxYz9LdFcRgHjMnBvCtDxZaS3QwE5rT0yU7I4O1A"
KEY_VERSION = "1.0.0"
META_HEADER = "QM-Encrypt-Meta"
STORE_ID = "201424"  # 包内 ext.storeId；与 brandId 同源
BRAND_ID = os.getenv("HSAY_BRAND_ID", "201424").strip() or "201424"
ACTIVITY_ID = os.getenv("HSAY_ACTIVITY_ID", "702822503017398273").strip() or "702822503017398273"
BASE_URL = "https://webapi.qmai.cn"
LOGIN_URL = f"{BASE_URL}/web/account-center/oauth/mini-app-login"
SIGN_URL = os.getenv("HSAY_SIGN_URL", f"{BASE_URL}/web/cmk-center/sign/takePartInSign").strip()
POINTS_URL = f"{BASE_URL}/web/catering2-apiserver/crm/points-info"

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


def b64relax(value: str) -> bytes:
    return base64.b64decode(value + "=" * ((4 - len(value) % 4) % 4))


def derive_key(raw: str) -> bytes:
    try:
        b = b64relax(raw)
    except Exception:
        b = raw.encode("utf-8")
    if len(b) == 32:
        return b
    out = bytearray(32)
    out[: min(len(b), 32)] = b[:32]
    return bytes(out)


KEY = derive_key(KEY_RAW)


def gcm_encrypt(plaintext: str, iv: bytes) -> str:
    cipher = AES.new(KEY, AES.MODE_GCM, nonce=iv)
    enc, tag = cipher.encrypt_and_digest(plaintext.encode("utf-8"))
    return base64.b64encode(enc + tag).decode("utf-8")


def gcm_decrypt(payload_b64: str, iv: bytes) -> str:
    buf = base64.b64decode(payload_b64)
    tag, data = buf[-16:], buf[:-16]
    cipher = AES.new(KEY, AES.MODE_GCM, nonce=iv)
    return cipher.decrypt_and_verify(data, tag).decode("utf-8")


def direct_session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    return s


def common_headers(token: str = "") -> Dict[str, str]:
    h = {
        "User-Agent": UA,
        "Accept": "v=1.0",
        "Content-Type": "application/json",
        "qm-from-type": "catering",
        "qm-from": "wechat",
        "scene": "1101",
        "store-id": STORE_ID,
        "multi-store-id": "",
        "Referer": f"https://servicewechat.com/{APPID}/1/page-frame.html",
    }
    if token:
        h["qm-user-token"] = token
    return h


def http(method: str, url: str, **kwargs) -> requests.Response:
    kwargs.setdefault("timeout", REQUEST_TIMEOUT)
    kwargs.setdefault("verify", False)
    return direct_session().request(method, url, **kwargs)


def qmai_request(method: str, url: str, body: Dict[str, Any], token: str = "") -> Dict[str, Any]:
    if AES is None:
        return {"status": False, "code": -1, "message": "缺少 pycryptodome"}
    payload = dict(body or {})
    payload.setdefault("appid", APPID)
    iv = os.urandom(12)
    ts = int(time.time() * 1000)
    meta = base64.b64encode(
        json.dumps(
            {
                "version": KEY_VERSION,
                "timestamp": ts,
                "iv": base64.b64encode(iv).decode("utf-8"),
            }
        ).encode("utf-8")
    ).decode("utf-8")
    headers = common_headers(token)
    headers[META_HEADER] = meta
    try:
        resp = http(
            method,
            url,
            headers=headers,
            json={"payload": gcm_encrypt(json.dumps(payload, ensure_ascii=False), iv)},
        )
        data = resp.json()
    except Exception as e:
        return {"status": False, "code": -1, "message": clean_line(e)}
    if isinstance(data, dict) and isinstance(data.get("payload"), str):
        rmeta = resp.headers.get(META_HEADER) or resp.headers.get(META_HEADER.lower())
        if not rmeta:
            return {"status": False, "code": -1, "message": "响应加密但缺少 QM-Encrypt-Meta"}
        try:
            meta_obj = json.loads(b64relax(str(rmeta)).decode("utf-8"))
            return json.loads(gcm_decrypt(data["payload"], b64relax(str(meta_obj["iv"]))))
        except Exception as e:
            return {"status": False, "code": -1, "message": f"响应解密失败: {clean_line(e)}"}
    return data if isinstance(data, dict) else {"status": False, "message": "非 JSON"}


def extract_token(data: Any) -> Optional[str]:
    if not isinstance(data, dict):
        return None
    cands = [data.get("token"), data.get("accessToken"), data.get("access_token")]
    inner = data.get("data")
    if isinstance(inner, dict):
        cands += [inner.get("token"), inner.get("accessToken"), inner.get("access_token")]
        user = inner.get("user")
        if isinstance(user, dict):
            cands += [user.get("token"), user.get("accessToken")]
    for c in cands:
        if c and c != "null":
            return str(c)
    return None


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


def cache_path() -> Path:
    configured = os.getenv("HSAY_TOKEN_DIR", "").strip()
    if configured:
        folder = Path(configured)
    elif Path("/ql/data/config").is_dir():
        folder = Path("/ql/data/config/hsay")
    else:
        folder = Path(__file__).resolve().parent / "hsay_cache"
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
    return hashlib.sha256(openid.encode("utf-8")).hexdigest()


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
    cache[cache_key(openid)] = {"token": token, "expireTime": expire, "updateTime": datetime.now().isoformat()}
    save_cache(cache)


def clear_cached_token(openid: str) -> None:
    cache = load_cache()
    cache.pop(cache_key(openid), None)
    save_cache(cache)
    print("🗑️ 已删除本地 token 缓存")


def login_by_code(openid: str) -> Tuple[Optional[str], Dict[str, Any]]:
    """企迈 AES-GCM 加密登录（原脚本契约）。"""
    print("🔐 使用 code 登录")
    code = get_wx_code(openid)
    print(f"ℹ️ 取码结果 code={'有' if code else '无'}")
    body = {"code": code, "eVersion": "1.0", "brandId": BRAND_ID}
    data = qmai_request("POST", LOGIN_URL, body)
    code_s = str(data.get("code") if data.get("code") is not None else "")
    msg = clean_line(data.get("message") or data.get("msg") or "")
    print(f"ℹ️ 登录响应 code={code_s or '无'} msg={msg[:80] or '无'}")
    ok = data.get("status") is True and code_s in ("0", "0.0")
    if not ok and code_s not in ("0", "0.0"):
        if re.search(r"品牌Id|brandId|品牌ID", msg):
            raise RuntimeError("登录缺少 brandId，请配置 HSAY_BRAND_ID")
        return None, {"message": msg or f"登录失败 code={code_s}"}
    token = extract_token(data)
    if not token:
        return None, {"message": msg or "登录响应未返回 token"}
    return token, data


def points_valid(token: str) -> bool:
    data = qmai_request("POST", POINTS_URL, {"appid": APPID}, token=token)
    return data.get("code") in (0, "0") or data.get("status") is True


def query_points(token: str) -> Optional[Any]:
    data = qmai_request("POST", POINTS_URL, {"appid": APPID}, token=token)
    inner = data.get("data") if isinstance(data.get("data"), dict) else data
    if not isinstance(inner, dict):
        return None
    for k in ("totalPoints", "totalPoint", "points", "point", "score"):
        if inner.get(k) is not None:
            return inner.get(k)
    return None


def do_sign(token: str) -> Tuple[bool, str]:
    data = qmai_request(
        "POST",
        SIGN_URL,
        {"activityId": ACTIVITY_ID, "appid": APPID},
        token=token,
    )
    code = data.get("code")
    msg = clean_line(data.get("message") or data.get("msg") or "")
    status = data.get("status")
    print(f"ℹ️ 签到响应 code={code} status={status} msg={msg[:60] or '无'}")
    if code in (0, "0") or code == 400041:
        if status is True:
            return True, "签到成功 ✅"
        if re.search(r"已签|重复|签到过", msg):
            return True, f"{msg or '今日已签到'} ✅"
        return True, f"{msg or '今日已签到'} ✅"
    return False, f"{msg[:40] or '签到失败'} (code={code}) ❌"


def auth_related_error(msg: str) -> bool:
    if re.search(r"brandId|品牌Id|未授权手机号|活动", msg):
        return False
    return bool(re.search(r"token|登录失效|未登录|请先登录|401", msg, re.I))


def login_with_cache(openid: str) -> Tuple[Optional[str], str]:
    cached = read_cached_token(openid)
    if cached:
        print("ℹ️ token缓存登录")
        if points_valid(cached):
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
    say(f"━━━━━━━━━━━━━━━━━━━━\n👤 账号 {index}/{total} {mask_id(openid, 6)}\n━━━━━━━━━━━━━━━━━━━━")

    def _biz(token: str) -> Dict[str, Any]:
        say("📋 签到")
        before = query_points(token)
        ok, status = do_sign(token)
        after = query_points(token)
        acc["status"] = status
        acc["success"] = ok
        extras.append(f"积分 {before if before is not None else '-'} → {after if after is not None else '-'}")
        try:
            if before is not None and after is not None:
                delta = float(after) - float(before)
                if delta:
                    acc["reward"] = f"积分 {delta:+g}"
                else:
                    acc["reward"] = f"积分 {after}"
            elif after is not None:
                acc["reward"] = f"积分 {after}"
        except (TypeError, ValueError):
            if after is not None:
                acc["reward"] = f"积分 {after}"
        if not ok:
            acc["error"] = status
        return acc

    def _fail(msg: str, status: str) -> Dict[str, Any]:
        acc["status"] = status
        acc["error"] = msg
        acc["success"] = False
        return acc

    try:
        token, login_mode = login_with_cache(openid)
        extras.append(login_mode)
        if not token:
            return _fail("登录失败", "登录失败 ❌")
        say(f"✅ 登录成功 token={mask_token(token)}")
        return _biz(token)
    except Exception as e:
        msg = clean_line(e)
        say(f"❌ {msg}")
        if auth_related_error(msg):
            say("⚠️ 登录态失效，删除缓存 → code 重登 → 重跑")
            extras.append("登录态失效 code重登")
            try:
                clear_cached_token(openid)
                token, login_mode = login_with_cache(openid)
                extras.append(login_mode)
                if not token:
                    return _fail("重登失败", "重登失败 ❌")
                return _biz(token)
            except Exception as e2:
                msg2 = clean_line(e2)
                return _fail(msg2, f"重登重跑失败 ❌ ({msg2[:40]})")
        return _fail(msg, f"失败 ❌ ({msg[:40]})")


def main() -> int:
    started = time.time()
    openids = split_openids(os.getenv("hsay", "").strip())
    if not openids:
        print("❌ 未配置 hsay 环境变量（openid，多账号换行或 &）")
        return 1
    if not os.getenv("wx_server_url", "").strip() or not os.getenv("wx_auth", "").strip():
        print("❌ 未配置 wx_server_url / wx_auth")
        return 1
    if AES is None:
        print("❌ 缺少依赖 pycryptodome（青龙：pip install pycryptodome）")
        return 1
    print(f"{APP_NAME} | {len(openids)}账号 | activityId={ACTIVITY_ID}")

    results: List[Dict[str, Any]] = []
    for i, openid in enumerate(openids, 1):
        try:
            res = run_account(openid, i, len(openids))
        except Exception as e:
            res = {
                "account": f"账号{i}",
                "phone": "",
                "status": f"执行失败 ❌ ({clean_line(e)[:40]})",
                "reward": "-",
                "extra": [f"openid：{mask_id(openid, 6)}"],
                "error": clean_line(e),
                "success": False,
            }
        results.append(res)
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
