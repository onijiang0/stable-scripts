#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# /*
# ------------------------------------------
# @Author: onijiang0
# @Date: 2026.09.21
# @Description: 益禾堂 - 企迈 code 登录 + 兑吧动态 token 签到
# cron: 17 13 * * *
# #定时使用10-19点 随机时间 每天
# ------------------------------------------
# 变量名：yht
# 变量值：业务 openid，多账号换行或 & 分隔，可加 #备注
#
# 依赖变量：
# wx_server_url  必填，取码地址（勿写进仓库）
# wx_auth        必填，取码鉴权（/wx/code）
# YHT_TOKEN_DIR  选填，token 缓存目录
# PROXY_API      选填，代理提取地址
# PROXY_TYPE     选填，http / socks5
# QL_NOTIFY      选填，0 关闭推送
# ------------------------------------------
# 已实现：
# 1. 多账号；缺变量明确报错退出
# 2. code 换 qm-user-token；本地缓存；失效删缓存后 code 重登并重跑
# 3. member/redirect 活动落地页 → Set-Cookie 会话
# 4. getToken 混淆 JS → doSign 每日签到；签到结果与积分文案
# 5. send_notify 统一简报；脱敏日志；不打印原始 JSON/完整 token
#
# 契约（appid wx4080846d0cec2fd5 / store 203009）：
# code     POST {wx_server_url}/wx/code auth:{wx_auth} json:{openid,appid}
# 登录     POST https://webapi.qmai.cn/web/account-center/oauth/mini-app-login
#          明文 body {code,eVersion:"1.0",appid} -> data.token + data.user
# 业务加密  POST 企迈接口：AES-256-GCM payload + 头 QM-Encrypt-Meta
#          SDK 固定串（非用户密钥）：key raw/derive + version 1.0.0
# 落地页   POST /web/catering/crm/member/redirect body {redirectUrl}
#          -> data 为兑吧活动 URL
# 活动会话 GET 兑吧落地页 302 Set-Cookie：wdata4/w_ts/_ac/wdata3/dcustom
# 签到token POST https://86019-activity.dexfu.cn/chw/ctoken/getToken
#          form {timestamp} -> token 为混淆 JS；eval 后取 window['3fd0cbet']
# 签到     POST https://86019-activity.dexfu.cn/sign/component/doSign
#          form {signOperatingId,token}；success=true 为成功
# 会员验证  member/redirect 可用则缓存 token 有效
#
# 踩坑：
# 1. 原脚本有 token 缓存：失效必须删缓存再 code 登录后重跑；勿只重试不删缓存
# 2. 登录接口为明文 JSON；业务 POST 需 AES-GCM（pycryptodome）
# 3. getToken 需 Node 执行混淆 JS；无 Node 时用正则兜底 window['3fd0cbet']
# 4. 键名 3fd0cbet 源自实测，若服务端更换需按抓包更新，勿凭空猜
# 5. 签到 Cookie 不完整/JS 执行失败时明确失败，不伪造已签到
# 6. 日志不打印完整 token/cookie；openid 脱敏
# ------------------------------------------
# */

from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import re
import subprocess
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

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


APP_NAME = "益禾堂"
APPID = "wx4080846d0cec2fd5"
STORE_ID = "203009"

QMAI_BASE = "https://webapi.qmai.cn/web"
QMAI_LOGIN = f"{QMAI_BASE}/account-center/oauth/mini-app-login"
QMAI_REDIRECT = f"{QMAI_BASE}/catering/crm/member/redirect"

ACTIVITY_PAGE_URL = "https://86019.activity-12.m.duiba.com.cn/chw/visual-editor/skins?id=203576"
ACTIVITY_TOKEN_URL = "https://86019-activity.dexfu.cn/chw/ctoken/getToken"
ACTIVITY_SIGN_URL = "https://86019-activity.dexfu.cn/sign/component/doSign"
SIGN_OPERATING_ID = "326649747164581"
ACTIVITY_KEY = "3fd0cbet"  # 实测固定键名；服务端若更换需抓包确认

# 企迈客户端 SDK 固定参数（解包 requestEncryptSdk，非用户密钥，勿当账号密钥外传）
KEY_RAW = os.getenv("YHT_KEY_RAW", "mN6KpXq8Sv2WxYz9LdFcRgHjMnBvCtDxZaS3QwE5rT0yU7I4O1A")
KEY_VERSION = "1.0.0"
META_HEADER = "QM-Encrypt-Meta"

PROXY_API = os.getenv("PROXY_API", "")
PROXY_TYPE = os.getenv("PROXY_TYPE", "http").lower()
REQUEST_TIMEOUT = 30
ENABLE_DIRECT_FALLBACK = True

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 "
    "MicroMessenger/7.0.20.1781 NetType/WIFI MiniProgramEnv/Windows WindowsWechat"
)
SIGN_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36 "
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


def parse_proxy(text: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False)
    try:
        data = json.loads(text.strip() or "null")
    except Exception:
        return None
    obj = None
    if isinstance(data, dict):
        if isinstance(data.get("data"), list) and data["data"]:
            obj = data["data"][0]
        elif isinstance(data.get("data"), dict):
            obj = data["data"]
        elif data.get("ip") and data.get("port"):
            obj = data
    if not isinstance(obj, dict):
        return None
    host = obj.get("ip") or obj.get("host")
    port = obj.get("port")
    if not host or not port:
        return None
    return {
        "host": str(host),
        "port": int(port),
        "username": obj.get("user") or obj.get("username") or "",
        "password": obj.get("pass") or obj.get("password") or "",
    }


def get_proxy() -> Optional[Dict[str, str]]:
    if not PROXY_API:
        return None
    try:
        info = parse_proxy(direct_session().get(PROXY_API, timeout=15).text)
        if not info:
            return None
        auth = ""
        if info.get("username") and info.get("password"):
            auth = f"{quote(info['username'])}:{quote(info['password'])}@"
        scheme = "socks5" if PROXY_TYPE == "socks5" else "http"
        url = f"{scheme}://{auth}{info['host']}:{info['port']}"
        return {"http": url, "https": url}
    except Exception:
        return None


def http(method: str, url: str, **kwargs) -> requests.Response:
    kwargs.setdefault("timeout", REQUEST_TIMEOUT)
    kwargs.setdefault("verify", False)
    proxies = kwargs.pop("proxies", None)
    if proxies:
        try:
            return requests.request(method, url, proxies=proxies, **kwargs)
        except Exception as exc:
            say(f"⚠️ 代理失败: {clean_line(exc)}")
            if not ENABLE_DIRECT_FALLBACK:
                raise
    return direct_session().request(method, url, **kwargs)


def common_headers(token: str = "") -> Dict[str, str]:
    h = {
        "User-Agent": UA,
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Qm-From-Type": "catering",
        "Qm-From": "wechat",
        "store-id": STORE_ID,
        "Accept": "v=1.0",
        "Referer": f"https://servicewechat.com/{APPID}/0/page-frame.html",
    }
    if token:
        h["qm-user-token"] = token
    return h


def qmai_post(url: str, body: Dict[str, Any], token: str = "", proxies=None) -> Dict[str, Any]:
    """企迈业务 POST：AES-GCM + QM-Encrypt-Meta。"""
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
            "POST",
            url,
            headers=headers,
            json={"payload": gcm_encrypt(json.dumps(payload, ensure_ascii=False), iv)},
            proxies=proxies,
        )
        data = resp.json()
    except Exception as e:
        return {"status": False, "code": -1, "message": clean_line(e)}
    if isinstance(data, dict) and isinstance(data.get("payload"), str):
        rmeta = resp.headers.get(META_HEADER) or resp.headers.get(META_HEADER.lower())
        if not rmeta:
            return {"status": False, "code": -1, "message": "响应加密但缺少 QM-Encrypt-Meta"}
        try:
            meta_obj = json.loads(b64relax(rmeta).decode("utf-8"))
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


def login_by_code(openid: str, proxies=None) -> Tuple[Optional[str], Dict[str, Any]]:
    """明文 mini-app-login（HAR 坐实），不走 AES。"""
    code = get_wx_code(openid)
    say(f"ℹ️ 取码结果 code={('有' if code else '无')}")
    headers = common_headers()
    headers["Accept"] = "v=1.0"
    try:
        resp = http(
            "POST",
            QMAI_LOGIN,
            headers=headers,
            json={"code": code, "eVersion": "1.0", "appid": APPID},
            proxies=proxies,
        )
        data = resp.json()
    except Exception as e:
        return None, {"message": clean_line(e)}
    code_s = str(data.get("code") if data.get("code") is not None else "")
    if code_s not in ("0", "0.0"):
        # 企迈登录有时用 code==0，兼容 status
        if not (data.get("status") is True):
            return None, {"message": clean_line(data.get("message") or f"登录失败 code={code_s}")}
    token = extract_token(data)
    if not token:
        return None, {"message": "登录响应未返回 token"}
    return token, data


def cache_path() -> Any:
    from pathlib import Path

    configured = os.getenv("YHT_TOKEN_DIR", "").strip()
    if configured:
        folder = Path(configured)
    elif Path("/ql/data/config").is_dir():
        folder = Path("/ql/data/config/yht")
    else:
        folder = Path(__file__).resolve().parent / "yht_cache"
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


def token_valid(openid: str, token: str, proxies=None) -> bool:
    """用 member/redirect 验证缓存 token（原脚本同接口）。"""
    if not token:
        return False
    try:
        data = qmai_post(QMAI_REDIRECT, {"redirectUrl": ACTIVITY_PAGE_URL}, token=token, proxies=proxies)
        return bool(data.get("status") is True and data.get("data"))
    except Exception:
        return False


def login_with_cache(openid: str, proxies=None) -> Tuple[Optional[str], str]:
    """原脚本有缓存：有效则复用；失效删缓存后 code 重登。返回 (token, login_mode)。"""
    cached = read_cached_token(openid)
    if cached:
        print("ℹ️ token缓存登录")
        if token_valid(openid, cached, proxies):
            return cached, "token缓存登录"
        print("⚠️ 缓存 token 失效，自动删除并 code 重登")
        clear_cached_token(openid)
        token, raw = login_by_code(openid, proxies)
        if token:
            write_cached_token(openid, token, raw or {})
            return token, "缓存失效 code重登"
        return None, "缓存失效 code重登失败"
    print("🔐 使用 code 登录")
    token, raw = login_by_code(openid, proxies)
    if token:
        write_cached_token(openid, token, raw or {})
        return token, "code登录"
    return None, "code登录失败"


def fetch_activity_cookie(proxies=None) -> str:
    """GET 落地页取 302 Set-Cookie（不落盘、不打印完整 cookie）。"""
    # redirect 先拿 URL
    return ""


def get_redirect_url(token: str, proxies=None) -> str:
    data = qmai_post(QMAI_REDIRECT, {"redirectUrl": ACTIVITY_PAGE_URL}, token=token, proxies=proxies)
    if data.get("status") is not True or not data.get("data"):
        raise RuntimeError(clean_line(data.get("message") or "获取活动地址失败"))
    return str(data["data"])


def extract_session_cookie(activity_url: str, proxies=None) -> str:
    resp = http(
        "GET",
        activity_url,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        },
        proxies=proxies,
        allow_redirects=False,
    )
    raw = getattr(resp, "raw", None)
    header_obj = getattr(raw, "headers", None)
    set_cookies: List[str] = []
    if header_obj is not None:
        try:
            set_cookies = header_obj.getlist("Set-Cookie")
        except Exception:
            set_cookies = []
    if not set_cookies:
        merged = resp.headers.get("Set-Cookie", "")
        if merged:
            set_cookies = [merged]
    joined = "".join(set_cookies)
    parts = re.findall(r"(?:wdata4|w_ts|_ac|wdata3|dcustom)=[^;]*;", joined)
    names = [p.split("=", 1)[0] for p in parts]
    print(f"ℹ️ 活动 Cookie 片段 {len(parts)}/5 names={names}")
    if not parts:
        return ""
    return "".join(parts)


def _node_path() -> Optional[str]:
    for name in (os.environ.get("node") or "", "node", "nodejs"):
        if not name:
            continue
        try:
            r = subprocess.run([name, "-v"], capture_output=True, timeout=8, text=True)
            if r.returncode == 0:
                return name
        except Exception:
            continue
    return None


def _decode_js_escapes(s: str) -> str:
    """还原 \\uXXXX / \\xXX，便于正则与 eval。"""
    if not s:
        return s

    def _u(m: re.Match) -> str:
        try:
            return chr(int(m.group(1), 16))
        except Exception:
            return m.group(0)

    out = re.sub(r"\\u([0-9a-fA-F]{4})", _u, s)
    out = re.sub(r"\\x([0-9a-fA-F]{2})", _u, out)
    return out


def eval_sign_token(raw_js: str) -> str:
    """执行混淆 JS 取签到 token；失败则解码后正则兜底。"""
    if not raw_js:
        print("ℹ️ getToken 返回空 token 字段")
        return ""
    print(f"ℹ️ getToken JS len={len(raw_js)} 前80={clean_line(raw_js[:80])}")
    decoded = _decode_js_escapes(raw_js)
    if decoded != raw_js:
        print(f"ℹ️ 已解码 unicode 转义 len={len(decoded)}")
    keys_raw = re.findall(r"window\[['\"]([^'\"]+)['\"]\]", decoded)
    if keys_raw:
        print(f"ℹ️ 解码后 window 键名={unique_sample(keys_raw)}")
    if "3fd0cbet" in decoded:
        print("ℹ️ 解码文本中包含 3fd0cbet 字样")

    fixed = re.sub(r"\b0([0-7]+)\b", r"0o\1", decoded)
    node = _node_path()
    print(f"ℹ️ Node 运行时: {node or '未找到'}")
    if node:
        try:
            import json as _json
            import tempfile
            from pathlib import Path

            runner = (
                "var window = global.window = {};\n"
                "var __err = '';\n"
                f"var __code = {_json.dumps(fixed)};\n"
                "try { eval(__code); } catch (e) { __err = String(e && e.message || e); }\n"
                "var keys = [];\n"
                "try { keys = Object.keys(window); } catch (e) {}\n"
                "var found = '';\n"
                f"var want = {_json.dumps(ACTIVITY_KEY)};\n"
                "if (window[want] !== undefined && window[want] !== null && String(window[want]) !== 'undefined' && String(window[want]) !== '') {\n"
                "  found = String(window[want]);\n"
                "} else {\n"
                "  for (var i = 0; i < keys.length; i++) {\n"
                "    var k = keys[i];\n"
                "    var v = window[k];\n"
                "    if (v === undefined || v === null) continue;\n"
                "    var vs = String(v);\n"
                "    if (!vs || vs === 'undefined' || vs === '[object Object]') continue;\n"
                "    if (k.indexOf('3fd0') >= 0 || k === want) { found = vs; break; }\n"
                "  }\n"
                "  if (!found && keys.length === 1) { found = String(window[keys[0]] || ''); }\n"
                "}\n"
                "process.stdout.write(JSON.stringify({keys: keys.slice(0, 30), found: found, err: __err}));\n"
            )
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as tf:
                tf.write(runner)
                tmp = tf.name
            try:
                r = subprocess.run([node, tmp], capture_output=True, timeout=25, text=True)
                out = (r.stdout or "").strip()
                err = clean_line((r.stderr or "")[:160])
                print(f"ℹ️ Node stdout={out[:240] or '空'} stderr={err or '无'}")
                if out:
                    try:
                        info = json.loads(out)
                        found = str(info.get("found") or "")
                        if found:
                            print(f"ℹ️ Node 解析成功 found_len={len(found)}")
                            return found
                        print(f"ℹ️ Node keys={info.get('keys')} err={info.get('err')}")
                    except Exception:
                        if out and out != "undefined":
                            return out
            finally:
                try:
                    Path(tmp).unlink(missing_ok=True)
                except Exception:
                    pass
        except Exception as e:
            print(f"ℹ️ Node 执行异常: {clean_line(e)[:80]}")

    for src, tag in ((decoded, "decoded"), (raw_js, "raw")):
        for pat in (
            rf"window\[['\"]{re.escape(ACTIVITY_KEY)}['\"]\]\s*=\s*['\"]([^'\"]+)['\"]",
            rf"['\"]{re.escape(ACTIVITY_KEY)}['\"]\]\s*=\s*['\"]([^'\"]+)['\"]",
        ):
            m = re.search(pat, src)
            if m:
                print(f"ℹ️ 正则命中（{tag}）")
                return m.group(1)
    m2 = re.findall(r"window\[['\"]([0-9a-zA-Z_]{4,20})['\"]\]\s*=\s*['\"]([^'\"]{4,})['\"]", decoded)
    if m2:
        print(f"ℹ️ 正则候选项={ [k for k, _ in m2[:5]] }，取首个")
        return m2[0][1]
    # 从 unicode 解码后再找 fromCharCode 结果附近的赋值
    m3 = re.findall(r"window\[['\"]([^'\"]{4,24})['\"]\]\s*=\s*([A-Za-z_$][\w$]*)\s*[;,]", decoded)
    if m3:
        print(f"ℹ️ 发现 window 键赋给变量={m3[:4]}（需 eval 才能取值）")
    print("ℹ️ 未能从 getToken 响应解析出签到 token")
    return ""


def unique_sample(keys: List[str]) -> List[str]:
    seen = []
    for k in keys:
        if k not in seen:
            seen.append(k)
        if len(seen) >= 6:
            break
    return seen


def get_sign_token(session_cookie: str, proxies=None) -> str:
    ts = int(time.time() * 1000)
    cookie_names = [p.split("=", 1)[0] for p in session_cookie.split(";") if "=" in p]
    print(f"ℹ️ getToken 请求 Cookie 键={cookie_names}")
    try:
        resp = http(
            "POST",
            ACTIVITY_TOKEN_URL,
            headers={
                "User-Agent": SIGN_UA,
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "https://86019-activity.dexfu.cn",
                "Referer": f"https://86019-activity.dexfu.cn/sign/component/page?signOperatingId={SIGN_OPERATING_ID}",
                "Cookie": session_cookie,
            },
            data={"timestamp": ts},
            proxies=proxies,
        )
    except Exception as e:
        print(f"ℹ️ getToken 网络异常: {clean_line(e)[:80]}")
        return ""
    print(f"ℹ️ getToken HTTP={resp.status_code}")
    try:
        result = resp.json()
    except Exception:
        print(f"ℹ️ getToken 非 JSON 前80={clean_line(resp.text)[:80]}")
        return ""
    if not isinstance(result, dict):
        print(f"ℹ️ getToken 结果类型={type(result).__name__}")
        return ""
    print(f"ℹ️ getToken keys={list(result.keys())[:10]} success={result.get('success')}")
    if not result.get("success"):
        print(f"ℹ️ getToken 业务失败 msg={clean_line(result.get('message') or result.get('msg') or '')[:80]}")
        return ""
    raw_js = str(result.get("token") or result.get("data") or "")
    if isinstance(result.get("data"), dict):
        raw_js = str(result["data"].get("token") or "")
    return eval_sign_token(raw_js)


def auth_related_error(msg: str) -> bool:
    """仅企迈登录态问题才触发删缓存重登；签到 token/兑换/JS 失败不算。"""
    if re.search(r"签到 token|getToken|活动会话|活动地址|JS|Node|pycryptodome", msg):
        return False
    return bool(re.search(r"登录失败|登录失效|token失效|qm-user-token|未登录|请先登录|HTTP 401", msg, re.I))


def do_sign(session_cookie: str, sign_token: str, proxies=None) -> Tuple[bool, str, Optional[float]]:
    resp = http(
        "POST",
        f"{ACTIVITY_SIGN_URL}?_={int(time.time() * 1000)}",
        headers={
            "User-Agent": SIGN_UA,
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://86019-activity.dexfu.cn",
            "Referer": f"https://86019-activity.dexfu.cn/sign/component/page?signOperatingId={SIGN_OPERATING_ID}",
            "Cookie": session_cookie,
        },
        data={"signOperatingId": SIGN_OPERATING_ID, "token": sign_token},
        proxies=proxies,
    )
    try:
        data = resp.json()
    except Exception:
        return False, clean_line(resp.text)[:80], None
    if data.get("success") is True:
        payload = data.get("data")
        gain = None
        if isinstance(payload, dict):
            sr = payload.get("signResult")
            if sr not in (None, ""):
                try:
                    gain = float(sr)
                    return True, "签到成功 ✅", gain
                except (TypeError, ValueError):
                    return True, f"签到成功 ✅（{clean_line(sr)[:30]}）", None
        return True, "签到成功 ✅", None
    msg = clean_line(data.get("message") or data.get("msg") or "")
    if re.search(r"已签|已经签|签到过|重复|已完成", msg + str(data.get("data") or "")):
        return True, "今日已签到 ✅", None
    return False, f"{msg[:40] or '签到失败'} ❌", None


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
    proxies = get_proxy()
    extras.append("代理" if proxies else "直连")

    def _biz(token: str) -> Dict[str, Any]:
        say("📋 业务流程")
        activity_url = get_redirect_url(token, proxies)
        say(f"ℹ️ 活动地址已获取（len={len(activity_url)}）")
        time.sleep(random.uniform(0.8, 1.6))
        cookie = extract_session_cookie(activity_url, proxies)
        if not cookie:
            raise RuntimeError("活动会话 Cookie 获取失败")
        extras.append("活动会话已获取")
        time.sleep(random.uniform(0.8, 1.6))
        key = get_sign_token(cookie, proxies)
        if not key:
            raise RuntimeError("签到 token 解析失败（getToken/JS）")
        say("✅ 签到 token 已获取")
        time.sleep(random.uniform(0.8, 1.6))
        ok, status, gain = do_sign(cookie, key, proxies)
        acc["status"] = status
        if gain is not None:
            acc["reward"] = f"+{gain:g} 积分"
        acc["success"] = ok
        if not ok:
            acc["error"] = status
        return acc

    def _fail(msg: str, status: str) -> Dict[str, Any]:
        acc["status"] = status
        acc["error"] = msg
        acc["success"] = False
        return acc

    login_mode = ""
    token = None
    try:
        token, login_mode = login_with_cache(openid, proxies)
        extras.append(login_mode)
        if not token:
            say("❌ 登录失败")
            return _fail("登录失败", "登录失败 ❌")
        say(f"✅ 登录成功 token={mask_token(token)}")
        return _biz(token)
    except Exception as e:
        msg = clean_line(e)
        say(f"❌ {msg}")
        # 仅企迈登录态问题才删缓存重登；getToken/签到 token 失败不重登
        if token and auth_related_error(msg):
            say("⚠️ 登录态失效，删除缓存 → code 重登 → 重跑")
            extras.append("登录态失效 code重登")
            try:
                clear_cached_token(openid)
                token, login_mode = login_with_cache(openid, proxies)
                extras.append(login_mode)
                if not token:
                    return _fail("重登失败", "重登失败 ❌")
                return _biz(token)
            except Exception as e2:
                msg2 = clean_line(e2)
                say(f"❌ 重登重跑失败: {msg2}")
                return _fail(msg2, f"重登重跑失败 ❌ ({msg2[:40]})")
        return _fail(msg, f"失败 ❌ ({msg[:40]})")


def main() -> int:
    started = time.time()
    openids = split_openids(os.getenv("yht", "").strip())
    if not openids:
        print("❌ 未配置 yht 环境变量（openid，多账号换行或 &）")
        return 1
    if not os.getenv("wx_server_url", "").strip() or not os.getenv("wx_auth", "").strip():
        print("❌ 未配置 wx_server_url / wx_auth")
        return 1
    if AES is None:
        print("❌ 缺少依赖 pycryptodome（青龙：pip install pycryptodome）")
        return 1
    print(f"{APP_NAME} | {len(openids)}账号")

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
