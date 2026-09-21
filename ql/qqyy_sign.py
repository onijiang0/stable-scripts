#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# /*
# ------------------------------------------
# @Description: QQ音乐签到小程序 - code 换 musickey + 绿钻/金币/任务/抽奖
# cron: 20 14 * * *
# ------------------------------------------
# 变量名：YYB_SERVER
# 变量值：yyb-go 地址@账号ID或OpenID，多账号换行
#
# 依赖变量：
# YYB_SERVER        推荐，YYB Go 地址@账号ID/OpenID，换行分隔
# QQ_SERVERS        可选，旧版本地 code 服务，换行；仅未配 YYB_SERVER 时用
# PROXY_API         可选，HTTP/socks5 代理提取地址
# PROXY_TYPE        可选，http / socks5
# QQ_ENABLE_ACTIVITY  可选，抽奖/红包，默认 1
# QQ_ENABLE_FAVORITE  可选，临时收藏/关注任务，默认 1
# QQ_DEBUG          可选，调试日志，默认 0
# QL_NOTIFY         可选，设为 0 关闭推送
# 注：推送走 send_notify（tools/sendNotify.js），结果只打短文案
#
# 契约（appid wxada7aab80ba27074）：
# code     YYB {server}/wxapp/getCode  json:{ref,app_id}
#          或旧版 GET http://{server}/login?appId=
# 登录     POST https://u.y.qq.com/cgi-bin/musicu.fcg
#          music.login.LoginServer.Login  body.comm+login.param.code
#          -> login.data.musickey / musicid
# 绿钻     music.lvz.MuFest13TaskSvr.EveryDaySignLvzScore
# 金币签到 music.actCenter.ActCenterSignNewSvr GetSignInSummary/SignIn/AwardPrize
# 任务/抽奖/红包雨 见脚本内 module.method
# ------------------------------------------
# */

from __future__ import annotations

import json
import os
import random
import re
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import requests

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    from send_notify import (
        clean_line,
        format_report,
        mask_id,
        notify_and_format,
        send_notify,
    )
except Exception:
    def clean_line(line: Any) -> str:
        s = str(line or "").strip()
        return "" if (s.startswith("{") or s.startswith("[")) else s[:180]

    def mask_id(value: Any, keep: int = 8) -> str:
        s = str(value or "")
        return (s[:keep] + "***") if len(s) > keep else (s or "-")

    def format_report(task, accounts, push_result="", cost_s=None):
        return task

    def notify_and_format(task, accounts, **kwargs):
        return send_notify(task, str(accounts))

    def send_notify(title: str, content: str) -> str:
        print(f"🔔 推送结果：跳过（send_notify 不可用）")
        return "跳过推送"

try:
    from yyb_account_guard import filter_accounts, update_from_result
except Exception:
    def filter_accounts(accounts, key_fn, app_id=None, log=None):  # type: ignore
        return list(accounts or [])

    def update_from_result(ref, result, app_id=None):  # type: ignore
        return None


APP_NAME = "QQ音乐签到小程序"
APPID = "wxada7aab80ba27074"

# 本地 code 服务地址一律走环境变量，仓库内不写真实 IP
SERVERS: List[str] = []


@dataclass
class AccountTarget:
    server: str
    ref: str = ""
    remark: str = ""
    nickname: str = ""
    index: int = 0

    @property
    def label(self) -> str:
        display = self.remark or self.nickname
        if display and self.ref:
            base = self.ref if self.ref.isdigit() else str(self.index or "?")
            return f"{display}（{base}）"
        if self.ref:
            return f"账号 {self.ref if self.ref.isdigit() else (self.index or '?')}"
        return display or f"账号 {self.index or '?'}"


PROXY_API = os.getenv("PROXY_API", "")
PROXY_TYPE = os.getenv("PROXY_TYPE", "http").lower()
ENABLE_ACTIVITY = os.getenv("QQ_ENABLE_ACTIVITY", "1") not in ("0", "false", "False")
ENABLE_FAVORITE = os.getenv("QQ_ENABLE_FAVORITE", "1") not in ("0", "false", "False")
IS_DEBUG = os.getenv("QQ_DEBUG", "0") in ("1", "true", "True")

CURRENT_ACCOUNTS: List[AccountTarget] = []

PROXY_RETRY_TIMES = 3
PROXY_VALIDATE_URL = "http://httpbin.org/ip"
PROXY_FETCH_INTERVAL = 3
ENABLE_DIRECT_FALLBACK = True
REQUEST_TIMEOUT = 30

MUSIC_API_URL = "https://u.y.qq.com/cgi-bin/musicu.fcg"
APP_API_URL = "https://u6.y.qq.com/cgi-bin/musics.fcg"

COIN_SIGN_ACT_ID = "Z25hHGi"
COIN_SIGN_SCENE_ID = "2"
DAILY_TASK_ACT_ID = "Z1NRf2o"
LOTTERY_SIGN_ACT_ID = "Z156KEu"
COIN_LOTTERY_PLAY_ID = "PR-Lottery-20240408-33489273491"
RED_PACKET_RAIN_KEY = "1joIuy"
TIMER_TASK_MODULE_ID = "ZGp4ja"
AUDIOBOOK_CATEGORY_ID = "42800344"
AUDIOBOOK_CANDIDATES = [93654004]
PLAYLIST_CANDIDATES = [9611383852]
SINGER_CANDIDATES = ["0039zms40xSD5K"]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36 "
    "MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI "
    "MiniProgramEnv/Windows WindowsWechat"
)


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def sleep(seconds: float) -> None:
    time.sleep(seconds)


def mask(value: Any) -> str:
    return mask_id(value, 8)


def json_preview(data: Any, limit: int = 120) -> str:
    if not IS_DEBUG:
        return clean_line(str(data))[:limit]
    try:
        text = json.dumps(data, ensure_ascii=False)
    except Exception:
        text = str(data)
    return redact_sensitive(text[:limit])


def to_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def safe_data(resp: Dict[str, Any]) -> Dict[str, Any]:
    return resp.get("data") or {}


def say(msg: str) -> None:
    line = clean_line(msg)
    if line:
        print(line)


def normalize_server(value: str) -> str:
    value = value.strip()
    if value and not value.startswith(("http://", "https://")):
        value = "http://" + value
    return value.rstrip("/")


def load_account_targets() -> List[AccountTarget]:
    raw = os.getenv("YYB_SERVER", "").strip()
    if not raw:
        legacy = os.getenv("QQ_SERVERS", "").strip()
        servers = [line.strip() for line in legacy.splitlines() if line.strip()] if legacy else list(SERVERS)
        if not servers:
            raise RuntimeError("未配置 YYB_SERVER 或 QQ_SERVERS")
        return [AccountTarget(server=normalize_server(server), index=index) for index, server in enumerate(servers, 1)]

    accounts: List[AccountTarget] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "@" not in line:
            say(f"⚠️ 配置忽略无账号标识行")
            continue
        server, ref = (part.strip() for part in line.split("@", 1))
        if server and ref:
            accounts.append(AccountTarget(normalize_server(server), ref=ref, index=len(accounts) + 1))
    if not accounts:
        raise RuntimeError("YYB_SERVER 未读取到有效账号，格式：地址@账号ID")
    load_account_labels(accounts)
    return accounts


def load_account_labels(accounts: List[AccountTarget]) -> None:
    grouped: Dict[str, List[AccountTarget]] = {}
    for account in accounts:
        grouped.setdefault(account.server, []).append(account)
    for server, rows in grouped.items():
        try:
            response = direct_session().get(server + "/accounts", timeout=10)
            payload = response.json()
            items = payload.get("data") if isinstance(payload, dict) else payload
            if isinstance(items, dict):
                items = items.get("accounts") or items.get("items") or []
            if not response.ok or not isinstance(items, list):
                continue
        except (requests.RequestException, ValueError):
            continue
        for account in rows:
            for item in items:
                if not isinstance(item, dict):
                    continue
                identifiers = {
                    str(item.get("id") or ""),
                    str(item.get("openid") or ""),
                    str(item.get("uin") or ""),
                }
                if account.ref not in identifiers:
                    continue
                account.remark = str(item.get("remark") or item.get("alias") or "").strip()
                account.nickname = str(item.get("nickname") or "").strip()
                break


def apply_account_label(account: AccountTarget, item: Any) -> None:
    if not isinstance(item, dict):
        return
    account.remark = str(item.get("remark") or item.get("alias") or account.remark).strip()
    account.nickname = str(item.get("nickname") or account.nickname).strip()


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


def get_valid_proxy(account_name: str) -> Tuple[Optional[Dict[str, str]], str]:
    if not PROXY_API:
        return None, ""
    for index in range(PROXY_RETRY_TIMES):
        try:
            response = direct_session().get(PROXY_API, timeout=15)
            proxies = build_proxy_dict(parse_proxy_response(response.text))
            if not proxies:
                continue
            try:
                r = requests.get(PROXY_VALIDATE_URL, proxies=proxies, timeout=15)
                if r.status_code == 200:
                    try:
                        ip = r.json().get("origin", "")
                    except Exception:
                        ip = ""
                    say(f"🌐 代理可用 {ip or 'OK'}")
                    return proxies, str(ip or "-")
            except Exception:
                pass
        except Exception:
            pass
        if index < PROXY_RETRY_TIMES - 1:
            sleep(2)
    say("🌐 代理不可用，直连")
    return None, ""


def request_with_proxy(
    method: str,
    url: str,
    *,
    proxies: Optional[Dict[str, str]] = None,
    server: str = "",
    **kwargs,
) -> requests.Response:
    kwargs.setdefault("timeout", REQUEST_TIMEOUT)
    if proxies:
        try:
            return requests.request(method, url, proxies=proxies, **kwargs)
        except Exception as exc:
            say(f"⚠️ 代理请求失败: {clean_line(exc)}")
            if not ENABLE_DIRECT_FALLBACK:
                raise
    return direct_session().request(method, url, **kwargs)


def get_code(account: AccountTarget) -> Optional[str]:
    server = account.server
    ref = account.ref
    if ref:
        url = server.rstrip("/") + "/wxapp/getCode"
        method = "POST"
        request_kwargs = {"json": {"ref": ref, "app_id": APPID}}
    else:
        url = f"{server}/login"
        method = "GET"
        request_kwargs = {"params": {"appId": APPID}}
    say("🔐 获取 code")
    for attempt in range(1, 3):
        try:
            response = direct_session().request(method, url, timeout=20, **request_kwargs)
            data = response.json()
            if ref:
                nested = data.get("data") if isinstance(data, dict) else None
                if isinstance(nested, dict):
                    apply_account_label(account, nested.get("account"))
                    nested = nested.get("result") or nested.get("data") or nested
                code = str(
                    (nested.get("code") if isinstance(nested, dict) else None)
                    or (data.get("code") if isinstance(data, dict) else "")
                    or ""
                ).strip()
                valid = response.ok and bool(code) and code.lower() != "null"
            else:
                code = str(data.get("code") or "").strip()
                valid = data.get("err") == 0 and bool(code) and code.lower() != "null"
            if valid:
                say("✅ code 获取成功")
                return code
            say(f"❌ code 获取失败（第 {attempt} 次）")
            if IS_DEBUG:
                say(json_preview(data, 200))
            if attempt < 2:
                sleep(3)
        except Exception as exc:
            say(f"❌ code 获取异常（第 {attempt} 次）: {clean_line(exc)}")
            if attempt < 2:
                sleep(3)
    return None


def common_headers(auth: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
        "Accept": "*/*",
        "xweb_xhr": "1",
        "Referer": f"https://servicewechat.com/{APPID}/175/page-frame.html",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if auth:
        headers["Cookie"] = f"uin=o{auth['uin']}; qm_keyst={auth['authst']}"
    return headers


def extract_musickey(data: Any) -> Tuple[Optional[str], Optional[str]]:
    if not isinstance(data, dict):
        return None, None
    login = data.get("login")
    if not isinstance(login, dict):
        return None, None
    inner = login.get("data")
    if not isinstance(inner, dict):
        return None, None
    musickey = inner.get("musickey")
    musicid = inner.get("musicid") or inner.get("str_musicid")
    if musickey and musicid:
        return str(musickey), str(musicid)
    return None, None


def login_by_code(
    server: str,
    code: str,
    proxies: Optional[Dict[str, str]],
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    try:
        say("🔐 code 换 musickey")
        payload = {
            "comm": {
                "uin": "0",
                "authst": "",
                "mina": 1,
                "appid": APPID,
                "ct": 25,
                "tmeAppID": "qqmusic",
                "tmeLoginType": "1",
            },
            "login": {
                "module": "music.login.LoginServer",
                "method": "Login",
                "param": {"code": code, "strAppid": APPID},
            },
        }
        response = request_with_proxy(
            "POST",
            MUSIC_API_URL,
            headers=common_headers(),
            json=payload,
            proxies=proxies,
            server=server,
        )
        try:
            data = response.json()
        except Exception:
            data = {"raw": response.text[:200]}
        musickey, musicid = extract_musickey(data)
        if musickey:
            say(f"✅ 登录成功 uin={mask(musicid)}")
            return {"uin": musicid, "authst": musickey}, data
        say("❌ 登录未返回 musickey")
        if IS_DEBUG:
            say(json_preview(data, 200))
        return None, data
    except Exception as exc:
        say(f"❌ 登录异常: {clean_line(exc)}")
        return None, None


def hash33(text: str) -> int:
    h = 5381.0
    for ch in text:
        i32 = int(h) & 0xFFFFFFFF
        shifted = i32 << 5
        if shifted >= 0x80000000:
            shifted -= 0x100000000
        h = h + shifted + ord(ch)
    return int(h) & 0x7FFFFFFF


def sha1_hex_utf8(text: str) -> str:
    import hashlib

    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def zzc_sign(payload: str) -> str:
    import base64

    h = sha1_hex_utf8(payload).upper()
    part1_indexes = [23, 14, 6, 36, 16, 7, 19]
    part2_indexes = [16, 1, 32, 12, 19, 27, 8, 5]
    scramble = [89, 39, 179, 150, 218, 82, 58, 252, 177, 52, 186, 123, 120, 64, 242, 133, 143, 161, 121, 179]
    part1 = "".join(h[i] for i in part1_indexes)
    part2 = "".join(h[i] for i in part2_indexes)
    raw = bytes(scramble[i] ^ int(h[i * 2:i * 2 + 2], 16) for i in range(20))
    middle = base64.b64encode(raw).decode().replace("\\", "").replace("/", "").replace("+", "").replace("=", "")
    return ("zzc" + part1 + middle + part2).lower()


def make_comm(auth: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "uin": int(auth["uin"]),
        "authst": auth["authst"],
        "mina": 1,
        "appid": APPID,
        "ct": 29,
        "cv": 0,
        "format": "json",
    }


def make_app_comm(auth: Dict[str, Any], ct: int, cv: int, mesh: str) -> Dict[str, Any]:
    return {
        "g_tk": hash33(auth["authst"]),
        "uin": int(auth["uin"]),
        "format": "json",
        "inCharset": "utf-8",
        "outCharset": "utf-8",
        "notice": 0,
        "platform": "h5",
        "needNewCode": 1,
        "ct": ct,
        "cv": cv,
        "mesh_devops": mesh,
    }


def redact_sensitive(text: str) -> str:
    text = re.sub(
        r'("(?:authst|musickey|refresh_key|session_key|uin|musicid|str_musicid|cookie|openid|unionid|encryptUin|userip|phoneNo|encryptedPhoneNo)"\s*:\s*")[^"]*',
        r"\1<redacted>",
        text,
        flags=re.I,
    )
    text = re.sub(r'("(?:uin|musicid)"\s*:\s*)\d+', r"\1<redacted>", text, flags=re.I)
    text = re.sub(r"(qm_keyst=)[^;\s]+", r"\1<redacted>", text, flags=re.I)
    return text


def debug_log(content: Any, title: str = "debug") -> None:
    if not IS_DEBUG:
        return
    print(f"----- {title} -----")
    text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    print(redact_sensitive(text)[:800])
    print("----- end -----\n")


def post_musicu(
    server: str,
    auth: Dict[str, Any],
    payload: Dict[str, Any],
    proxies: Optional[Dict[str, str]],
) -> Optional[Dict[str, Any]]:
    try:
        response = request_with_proxy(
            "POST",
            MUSIC_API_URL,
            headers=common_headers(auth),
            json=payload,
            proxies=proxies,
            server=server,
        )
        return response.json()
    except Exception as exc:
        say(f"❌ musicu 异常: {clean_line(exc)}")
        return None


def app_post(
    server: str,
    auth: Dict[str, Any],
    cgi_key: str,
    payload: Dict[str, Any],
    proxies: Optional[Dict[str, str]],
) -> Optional[Dict[str, Any]]:
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    sign = zzc_sign(body)
    url = f"{APP_API_URL}?_webcgikey={quote(cgi_key)}&_={int(time.time() * 1000)}&sign={sign}"
    headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "*/*",
        "xweb_xhr": "1",
        "Referer": f"https://servicewechat.com/{APPID}/175/page-frame.html",
        "Cookie": f"uin=o{auth['uin']}; qm_keyst={auth['authst']}",
    }
    try:
        response = request_with_proxy(
            "POST",
            url,
            headers=headers,
            data=body,
            proxies=proxies,
            server=server,
        )
        return response.json()
    except Exception as exc:
        say(f"❌ musics({cgi_key}) 异常: {clean_line(exc)}")
        return None


def app_request_succeeded(res: Optional[Dict[str, Any]], req_key: str = "req_0") -> bool:
    if not res or res.get("code") != 0:
        return False
    req = res.get(req_key)
    if not req or req.get("code") != 0:
        return False
    data = req.get("data")
    if not isinstance(data, dict):
        return True
    for key in ("retCode", "RetCode", "ret", "Ret", "code"):
        if key in data and to_float(data[key]) != 0:
            return False
    return True


def music_request_succeeded(res: Optional[Dict[str, Any]], req_key: str = "req_0") -> bool:
    if not res or res.get("code") != 0:
        return False
    req = res.get(req_key)
    data = req.get("data") if req else None
    return bool(req and req.get("code") == 0 and data and to_float(data.get("retCode", 0)) == 0)


def red_packet_request_succeeded(res: Optional[Dict[str, Any]]) -> bool:
    if not app_request_succeeded(res):
        return False
    data = (res.get("req_0") or {}).get("data")
    if not data or "Code" not in data:
        return True
    return to_float(data["Code"]) in (0, 10000)


def read_red_packet_rest_chance(data: Any, now: Optional[int] = None) -> int:
    if now is None:
        now = int(time.time())
    if not isinstance(data, dict):
        return 0
    config = data.get("BaseConfig")
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except Exception:
            return 0
    if not isinstance(config, dict):
        return 0
    session = config.get("session")
    segments = session.get("timeSegment") if isinstance(session, dict) else None
    if not isinstance(segments, list):
        return 0
    active = None
    for item in segments:
        if isinstance(item, dict) and to_float(item.get("status")) == 2:
            active = item
            break
    if active is None:
        for item in segments:
            if not isinstance(item, dict):
                continue
            rng = item.get("timeRangeTs")
            if isinstance(rng, list) and len(rng) >= 2 and to_float(rng[0]) <= now <= to_float(rng[1]):
                active = item
                break
    if not active:
        return 0
    return max(0, int(to_float(active.get("restChance"))))


def read_song_add_status(res: Optional[Dict[str, Any]], song_id: int) -> Optional[bool]:
    if not music_request_succeeded(res):
        return None
    data = (res.get("req_0") or {}).get("data") or {}
    result = data.get("result")
    entries = result.get("songlist") if isinstance(result, dict) else None
    if not isinstance(entries, list):
        return None
    entry = None
    for item in entries:
        if isinstance(item, dict) and int(to_float(item.get("songId") or item.get("backendSongId"))) == int(song_id):
            entry = item
            break
    if not entry or "existed" not in entry:
        return None
    existed = to_float(entry["existed"])
    if existed == 0:
        return True
    if existed == 1:
        return False
    return None


def collect_values_by_keys(root: Any, keys: List[str], predicate: Any = None, limit: int = 50) -> List[Any]:
    wanted = set(str(k) for k in keys)
    result: List[Any] = []

    def walk(value: Any) -> None:
        if len(result) >= limit or value is None:
            return
        if isinstance(value, list):
            for item in value:
                walk(item)
            return
        if not isinstance(value, dict):
            return
        for key, item in value.items():
            if key in wanted and (predicate is None or predicate(item)):
                result.append(item)
            walk(item)
            if len(result) >= limit:
                return

    walk(root)
    return unique_values(result)


def collect_singer_mids(root: Any, limit: int = 20) -> List[str]:
    body = root.get("body") if isinstance(root, dict) else root
    if not isinstance(body, dict):
        return []
    singers: List[str] = []

    def add_singer(singer: Any) -> None:
        if not isinstance(singer, dict):
            return
        mid = singer.get("mid") or singer.get("singerMID") or singer.get("singer_mid")
        if mid and re.match(r"^[A-Za-z0-9]{10,20}$", str(mid)):
            singers.append(str(mid))

    def add_song(song: Any) -> None:
        if not isinstance(song, dict) or not isinstance(song.get("singer"), list):
            return
        for singer in song["singer"]:
            add_singer(singer)

    for song in body.get("item_song") or []:
        add_song(song)
    for singer in body.get("singer") or []:
        add_singer(singer)
    for singer in body.get("item_singer") or []:
        add_singer(singer)
    return unique_values(singers)[:limit]


def unique_values(values: Any) -> List[Any]:
    seen = set()
    result = []
    for value in values or []:
        key = str(value)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def find_first_value(root: Any, keys: List[str]) -> Any:
    wanted = set(str(k) for k in keys)
    found = [None]

    def walk(value: Any) -> None:
        if found[0] is not None or value is None:
            return
        if isinstance(value, list):
            for item in value:
                walk(item)
            return
        if not isinstance(value, dict):
            return
        for key, item in value.items():
            if key in wanted:
                found[0] = item
                return
            walk(item)
            if found[0] is not None:
                return

    walk(root)
    return found[0]


def normalize_status(value: Any) -> Optional[bool]:
    if value is True or value == 1 or value == "1" or value == "true":
        return True
    if value is False or value == 0 or value == "0" or value == "false":
        return False
    return None


def read_single_mapped_status(root: Any, map_keys: List[str]) -> Optional[bool]:
    if not isinstance(root, dict):
        return None
    statuses = []
    for key in map_keys:
        mapping = root.get(key)
        if not isinstance(mapping, dict):
            continue
        for value in mapping.values():
            status = normalize_status(value)
            if status is not None:
                statuses.append(status)
    return statuses[0] if len(statuses) == 1 else None


def read_target_status(root: Any, target: str) -> Optional[bool]:
    found = [None]

    def walk(value: Any) -> None:
        if found[0] is not None or value is None:
            return
        if isinstance(value, list):
            for item in value:
                walk(item)
            return
        if not isinstance(value, dict):
            return
        if target in value:
            direct = normalize_status(value[target])
            if direct is not None:
                found[0] = direct
                return
        object_id = value.get("id") or value.get("userid") or value.get("userId") or value.get("mid") or value.get("singerMID")
        if str(object_id or "") == target:
            for key in ("status", "follow", "followed", "isFollow", "fav", "isFav", "operation", "oper"):
                if key not in value:
                    continue
                status = normalize_status(value[key])
                if status is not None:
                    found[0] = status
                    return
        for item in value.values():
            walk(item)
            if found[0] is not None:
                return

    walk(root)
    return found[0]


def format_lottery_gift(data: Any) -> str:
    gift = find_first_value(data, ["lotteryGift"])
    if isinstance(gift, str):
        try:
            gift = json.loads(gift)
        except Exception:
            if gift.strip():
                return gift.strip()
    source = gift if isinstance(gift, dict) else data
    name = find_first_value(source, ["giftName", "prizeName", "PrizeName", "name"])
    value = to_float(find_first_value(source, ["awardValue", "RewardGold", "rewardGold", "coinNum", "coin"]))
    if name and value:
        return f"{name} +{int(value)}"
    if name:
        return str(name)
    if value:
        return f"金币 +{int(value)}"
    return "已领取"


def format_coin_reward(task: Optional[Dict[str, Any]]) -> str:
    prize_list = task.get("PrizeList") if task else None
    prize = prize_list[0] if isinstance(prize_list, list) and prize_list else None
    if not prize:
        return ""
    if prize.get("Name"):
        return f"{prize['Name']}"
    if prize.get("Value"):
        return f"+{prize['Value']} 金币"
    return ""


def check_lvz_score(server: str, auth: Dict[str, Any], proxies: Optional[Dict[str, str]]) -> str:
    payload = {
        "comm": make_comm(auth),
        "req_0": {
            "module": "music.lvz.MuFest13TaskSvr",
            "method": "EveryDaySignLvzScore",
            "param": {"Uin": auth["uin"], "Cmd": "get"},
        },
    }
    res = post_musicu(server, auth, payload, proxies)
    debug_log(res, "EveryDaySignLvzScore")
    if not res:
        return "绿钻成长值签到无响应"
    r0 = res.get("req_0") or {}
    data = r0.get("data") or {}
    if res.get("code") != 0 or (r0.get("code") not in (0, None) and "Ret" not in data):
        return f"绿钻成长值签到失败 (code={res.get('code')})"
    ret = data.get("Ret")
    msg = data.get("Msg") or ""
    if ret == 0:
        info = data.get("Info") or {}
        score = info.get("Score") or 0
        return f"绿钻成长值签到成功" + (f" +{score}" if score else "")
    if ret == 20019 or re.search(r"已.*领取|已签|重复", msg):
        return f"绿钻成长值今日已签到"
    return f"绿钻成长值已处理 (Ret={ret})"


def get_coin_sign_state(
    server: str,
    auth: Dict[str, Any],
    proxies: Optional[Dict[str, str]],
    act_id: str,
    scene_id: str,
) -> Optional[Dict[str, Any]]:
    payload = {
        "comm": make_comm(auth),
        "req_0": {
            "module": "music.actCenter.ActCenterSignNewSvr",
            "method": "GetSignInSummary",
            "param": {"ActID": act_id},
        },
        "req_1": {
            "module": "music.actCenter.ActCenterSignNewSvr",
            "method": "GetSignInTaskList",
            "param": {"ActID": act_id, "ScenesID": scene_id},
        },
    }
    res = post_musicu(server, auth, payload, proxies)
    debug_log(res, "Coin Sign State")
    summary = res.get("req_0") if res else None
    tasks = res.get("req_1") if res else None
    summary_data = summary.get("data") if summary else None
    task_data = tasks.get("data") if tasks else None
    if (
        not res
        or res.get("code") != 0
        or not summary
        or summary.get("code") != 0
        or not tasks
        or tasks.get("code") != 0
        or not summary_data
        or summary_data.get("retCode") != 0
        or not task_data
        or task_data.get("retCode") != 0
    ):
        say("❌ 金币状态查询失败")
        return None
    task_list_info = task_data.get("TaskListInfo") or {}
    task_list = ((task_list_info.get("TaskList") or {}).get("ContinueTaskList")) or {}
    return {
        "info": task_data.get("Info") or summary_data.get("Info") or {},
        "taskList": task_list,
    }


def check_coin_sign_in(server: str, auth: Dict[str, Any], proxies: Optional[Dict[str, str]]) -> str:
    act_id = COIN_SIGN_ACT_ID
    scene_id = COIN_SIGN_SCENE_ID
    state = get_coin_sign_state(server, auth, proxies, act_id, scene_id)
    if not state:
        return "金币中心签到失败"
    signed_now = False
    if not state["info"].get("IsSignIn"):
        payload = {
            "comm": make_comm(auth),
            "req_0": {
                "module": "music.actCenter.ActCenterSignNewSvr",
                "method": "SignIn",
                "param": {"ActID": act_id, "ScenesID": scene_id},
            },
        }
        res = post_musicu(server, auth, payload, proxies)
        debug_log(res, "Coin SignIn")
        sign_req = res.get("req_0") if res else None
        sign_data = sign_req.get("data") if sign_req else None
        if (
            not res
            or res.get("code") != 0
            or not sign_req
            or sign_req.get("code") != 0
            or not sign_data
            or sign_data.get("retCode") != 0
            or not (sign_data.get("Info") or {}).get("IsSignIn")
        ):
            return "金币中心签到失败"
        signed_now = True
        state = get_coin_sign_state(server, auth, proxies, act_id, scene_id)
        if not state:
            return "金币中心已签到,状态刷新失败"

    day = int(to_float(state["info"].get("ContinueSignInCount")))
    task_map = state["taskList"] or {}
    task = next((item for item in task_map.values() if isinstance(item, dict) and item.get("State") == 2), None)
    if task is None:
        task = task_map.get(str(day))
    reward = format_coin_reward(task)

    if isinstance(task, dict) and task.get("State") == 2:
        payload = {
            "comm": make_comm(auth),
            "req_0": {
                "module": "music.actCenter.ActCenterSignNewSvr",
                "method": "AwardPrize",
                "param": {"ActID": act_id, "TaskID": task.get("ID")},
            },
        }
        res = post_musicu(server, auth, payload, proxies)
        debug_log(res, "Coin AwardPrize")
        award_req = res.get("req_0") if res else None
        award_data = award_req.get("data") if award_req else None
        if (
            res
            and res.get("code") == 0
            and award_req
            and award_req.get("code") == 0
            and award_data
            and award_data.get("retCode") in (0, 100004)
        ):
            base = "金币中心签到成功" if signed_now else "金币中心奖励已领取"
            return f"{base} ✅" + (f"（{reward}）" if reward else "")
        return "金币中心已签到,领奖失败"
    if state["info"].get("IsSignIn"):
        return f"金币中心今日已签到 ✅" + (f"（{reward}）" if reward else "")
    return "金币中心签到状态未确认"


def get_lottery_sign_state(
    server: str,
    auth: Dict[str, Any],
    proxies: Optional[Dict[str, str]],
) -> Optional[Dict[str, Any]]:
    payload = {
        "comm": make_app_comm(auth, 1, 200605, "DevopsCoinCenter3"),
        "req_0": {
            "module": "music.actCenter.ActCenterSignNewSvr",
            "method": "GetSignInSummary",
            "param": {"ActID": LOTTERY_SIGN_ACT_ID},
        },
        "req_1": {
            "module": "music.actCenter.ActCenterSignNewSvr",
            "method": "GetSignInTaskList",
            "param": {"ActID": LOTTERY_SIGN_ACT_ID},
        },
    }
    res = app_post(server, auth, "GetSignInSummary", payload, proxies)
    debug_log(res, "Lottery Sign State")
    if not app_request_succeeded(res, "req_0") or not app_request_succeeded(res, "req_1"):
        return None
    summary_data = (res.get("req_0") or {}).get("data") or {}
    task_data = (res.get("req_1") or {}).get("data") or {}
    task_list_info = task_data.get("TaskListInfo") or {}
    task_list = ((task_list_info.get("TaskList") or {}).get("ContinueTaskList")) or {}
    return {
        "info": task_data.get("Info") or summary_data.get("Info") or {},
        "taskList": task_list,
    }


def check_lottery_sign_in(server: str, auth: Dict[str, Any], proxies: Optional[Dict[str, str]]) -> str:
    state = get_lottery_sign_state(server, auth, proxies)
    if not state:
        return "金币抽奖签到失败"
    signed_now = False
    if not state["info"].get("IsSignIn"):
        payload = {
            "comm": make_app_comm(auth, 1, 200605, "DevopsCoinCenter3"),
            "req_0": {
                "module": "music.actCenter.ActCenterSignNewSvr",
                "method": "SignIn",
                "param": {"ActID": LOTTERY_SIGN_ACT_ID},
            },
        }
        res = app_post(server, auth, "SignIn", payload, proxies)
        debug_log(res, "Lottery SignIn")
        if not app_request_succeeded(res):
            return "金币抽奖签到失败"
        signed_now = True
        state = get_lottery_sign_state(server, auth, proxies)
        if not state:
            return "金币抽奖已签到,状态刷新失败"
    task = next(
        (item for item in (state["taskList"] or {}).values() if isinstance(item, dict) and item.get("State") == 2),
        None,
    )
    if isinstance(task, dict):
        payload = {
            "comm": make_app_comm(auth, 1, 200605, "DevopsCoinCenter3"),
            "req_0": {
                "module": "music.actCenter.ActCenterSignNewSvr",
                "method": "AwardPrize",
                "param": {"ActID": LOTTERY_SIGN_ACT_ID, "TaskID": task.get("ID")},
            },
        }
        res = app_post(server, auth, "AwardPrize", payload, proxies)
        if app_request_succeeded(res):
            reward = format_coin_reward(task)
            return "金币抽奖签到成功 ✅" + (f"（{reward}）" if reward else "")
        return "金币抽奖签到已完成,领奖失败"
    if signed_now:
        return "金币抽奖签到已完成 ✅"
    return "金币抽奖今日已签到 ✅"


def draw_coin_lottery(server: str, auth: Dict[str, Any], proxies: Optional[Dict[str, str]]) -> Tuple[str, int]:
    def query() -> Optional[Dict[str, Any]]:
        payload = {
            "comm": make_app_comm(auth, 1, 200605, "DevopsCoinCenter3"),
            "req_0": {
                "module": "music.actCenter.CoinLotterySvr",
                "method": "GetCoinUserInfo",
                "param": {"Param": 1, "Playid": COIN_LOTTERY_PLAY_ID},
            },
        }
        return app_post(server, auth, "GetCoinUserInfo", payload, proxies)

    info_res = query()
    if not app_request_succeeded(info_res):
        return "金币抽奖状态查询失败", 0
    remain = int(to_float(find_first_value((info_res.get("req_0") or {}).get("data"), ["lotteryRemain"])))
    if remain <= 0:
        return "金币抽奖机会已用完", 0
    gifts: List[str] = []
    coins = 0
    for _ in range(min(remain, 10)):
        payload = {
            "comm": make_app_comm(auth, 1, 200605, "DevopsCoinCenter3"),
            "req_0": {
                "module": "music.actCenter.CoinLotterySvr",
                "method": "UserCoinLottery",
                "param": {"Param": 1, "Playid": COIN_LOTTERY_PLAY_ID},
            },
        }
        res = app_post(server, auth, "UserCoinLottery", payload, proxies)
        if not app_request_succeeded(res):
            break
        gift = format_lottery_gift((res.get("req_0") or {}).get("data"))
        gifts.append(gift)
        m = re.search(r"\+(\d+)", gift)
        if m:
            coins += int(m.group(1))
        sleep(0.35)
    if gifts:
        return f"金币抽奖 {len(gifts)} 次：{'、'.join(gifts[:4])}", coins
    return "金币抽奖未完成", 0


def read_prize_coins(data: Any) -> int:
    prize_infos = find_first_value(data, ["PrizeInfos", "prizeInfos"])
    if isinstance(prize_infos, str):
        try:
            prize_infos = json.loads(prize_infos)
        except Exception:
            prize_infos = None
    if isinstance(prize_infos, dict):
        total = 0
        for item in prize_infos.get("results") or []:
            if not isinstance(item, dict):
                continue
            info = item.get("info")
            if not isinstance(info, dict):
                continue
            for prize in info.get("thePrize") or []:
                if isinstance(prize, dict) and re.search(r"金币", str(prize.get("prizeName") or "")):
                    total += int(to_float(prize.get("sendPrizeNum") or prize.get("prizeNum")))
        if total:
            return total
    return int(to_float(find_first_value(data, ["awardValue", "RewardGold", "rewardGold", "coinNum", "coin"])))


def run_red_packet_rain(server: str, auth: Dict[str, Any], proxies: Optional[Dict[str, str]]) -> Tuple[str, int]:
    payload = {
        "comm": make_app_comm(auth, 1, 200605, "DevopsCoinCenter3"),
        "req_0": {
            "module": "music.actCenter.RedPacketRainSvr",
            "method": "Raining",
            "param": {"RainKey": RED_PACKET_RAIN_KEY},
        },
    }
    res = app_post(server, auth, "Raining", payload, proxies)
    if not red_packet_request_succeeded(res):
        data = ((res or {}).get("req_0") or {}).get("data")
        if isinstance(data, dict) and to_float(data.get("Code")) == 20001:
            return "红包雨当前无可领次数", 0
        return "红包雨状态查询失败", 0
    rest_chance = read_red_packet_rest_chance((res.get("req_0") or {}).get("data"))
    if rest_chance <= 0:
        return "红包雨当前无可领次数", 0
    completed = 0
    coins = 0
    for _ in range(min(rest_chance, 6)):
        chance_payload = {
            "comm": make_app_comm(auth, 1, 200605, "DevopsCoinCenter3"),
            "req_0": {
                "module": "music.actCenter.RedPacketRainSvr",
                "method": "IncrChance",
                "param": {"RainKey": RED_PACKET_RAIN_KEY, "IncrType": 2},
            },
        }
        chance_res = app_post(server, auth, "IncrChance", chance_payload, proxies)
        if not red_packet_request_succeeded(chance_res):
            break
        sleep(0.8)
        draw_payload = {
            "comm": make_app_comm(auth, 1, 200605, "DevopsCoinCenter3"),
            "req_0": {
                "module": "music.actCenter.RedPacketRainSvr",
                "method": "DrawPrizes",
                "param": {"RainKey": RED_PACKET_RAIN_KEY, "HitNum": 10, "HitStreakNum": 0},
            },
        }
        draw_res = app_post(server, auth, "DrawPrizes", draw_payload, proxies)
        if not red_packet_request_succeeded(draw_res):
            break
        completed += 1
        coins += read_prize_coins((draw_res.get("req_0") or {}).get("data"))
    if completed:
        return f"红包雨 {completed} 次" + (f" +{coins} 金币" if coins else ""), coins
    return "红包雨未完成", 0


def query_coin_balance(server: str, auth: Dict[str, Any], proxies: Optional[Dict[str, str]]) -> str:
    url = "https://i2.y.qq.com/n3/coin_center/pages/client_v1/sign.html?_hidehd=1&_hdct=1&_miniplayer=1"
    try:
        response = request_with_proxy(
            "GET",
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Cookie": f"uin=o{auth['uin']}; qm_keyst={auth['authst']}",
                "Referer": "https://i2.y.qq.com/n3/coin_center/pages/client_v1/index.html",
            },
            proxies=proxies,
            server=server,
        )
        match = re.search(r'__ssrFirstPageData__="((?:[^"\\]|\\.)*)"', response.text)
        if not match:
            return "-"
        raw = match.group(1).replace('\\"', '"').replace("\\\\", "\\")
        data = json.loads(raw)
        coin = data.get("coin") if isinstance(data, dict) else None
        return str(int(to_float(coin))) if coin is not None else "-"
    except Exception as exc:
        if IS_DEBUG:
            say(f"余额查询失败: {clean_line(exc)}")
        return "-"


def query_daily_tasks(
    server: str,
    auth: Dict[str, Any],
    proxies: Optional[Dict[str, str]],
    page_id: str,
    floor_ids: List[int],
) -> Optional[List[Dict[str, Any]]]:
    payload = {
        "comm": make_app_comm(auth, 1, 200605, "DevopsCoinCenter3"),
        "req_0": {
            "module": "music.activeCenter.FloorManagerSvr",
            "method": "GetFloors",
            "param": {"Release": 1, "PageID": page_id, "PersonalityMode": 1, "FloorIDs": floor_ids},
        },
    }
    res = app_post(server, auth, "GetFloors", payload, proxies)
    debug_log(res, f"Daily Tasks {page_id}")
    req = res.get("req_0") if res else None
    data = req.get("data") if req else None
    if not res or res.get("code") != 0 or not req or req.get("code") != 0 or not data or data.get("RetCode") != 0:
        return None
    tasks: List[Dict[str, Any]] = []
    for floor in data.get("Floors") or []:
        if not isinstance(floor, dict):
            continue
        for item in floor.get("ItemList") or []:
            if not isinstance(item, dict):
                continue
            try:
                conf = item.get("ResourceConf")
                if isinstance(conf, str):
                    conf = json.loads(conf)
                if not isinstance(conf, dict):
                    continue
                task_list = ((conf.get("ActTaskModule") or {}).get("TaskList")) or []
                for task in task_list:
                    if not isinstance(task, dict):
                        continue
                    task = dict(task)
                    task["_actID"] = conf.get("ActID") or DAILY_TASK_ACT_ID
                    tasks.append(task)
            except Exception:
                continue
    return tasks


def get_daily_tasks(server: str, auth: Dict[str, Any], proxies: Optional[Dict[str, str]]) -> Optional[List[Dict[str, Any]]]:
    tasks = query_daily_tasks(server, auth, proxies, "18NtBy", [193])
    if not tasks:
        tasks = query_daily_tasks(server, auth, proxies, "songpopup", [85])
    return tasks


def find_daily_task(tasks: List[Dict[str, Any]], predicate: Any) -> Optional[Dict[str, Any]]:
    for task in tasks:
        if predicate(task):
            return task
    return None


def task_reached_ready(tasks: List[Dict[str, Any]], target: Dict[str, Any]) -> bool:
    for task in tasks:
        if task.get("ID") == target.get("ID") and task.get("State") == 2:
            return True
    return False


def is_timed_floor_task(task: Dict[str, Any]) -> bool:
    return bool(
        task.get("ID") == "26EIHk"
        or int(to_float(task.get("Type"))) == 600
        or re.search(r"定时领金币", task.get("Name") or "")
    )


def unique_tasks(tasks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    result = []
    for task in tasks:
        if not isinstance(task, dict) or not task.get("ID"):
            continue
        key = f"{task.get('_actID') or ''}:{task.get('ID')}"
        if key in seen:
            continue
        seen.add(key)
        result.append(task)
    return result


def claim_ready_task_rewards(
    server: str,
    auth: Dict[str, Any],
    proxies: Optional[Dict[str, str]],
    tasks: List[Dict[str, Any]],
) -> str:
    ready = []
    for task in tasks:
        if not isinstance(task, dict) or task.get("State") != 2:
            continue
        prize_list = task.get("PrizeList")
        if not isinstance(prize_list, list):
            continue
        if any(isinstance(p, dict) and int(to_float(p.get("Type"))) == 12 and to_float(p.get("Value")) > 0 for p in prize_list):
            ready.append(task)
    claimed = []
    for task in ready:
        payload = {
            "comm": make_app_comm(auth, 23, 0, "DevopsBase"),
            "req_0": {
                "module": "music.activeCenter.ActTaskNewSvr",
                "method": "AwardTaskPrize",
                "param": {"actID": task.get("_actID"), "TaskID": task.get("ID")},
            },
        }
        res = app_post(server, auth, "AwardTaskPrize", payload, proxies)
        award_req = res.get("req_0") if res else None
        award_data = award_req.get("data") if award_req else None
        if (
            res
            and res.get("code") == 0
            and award_req
            and award_req.get("code") == 0
            and award_data
            and award_data.get("retCode") == 0
        ):
            value = int(to_float(award_data.get("awardValue")))
            claimed.append(f"{task.get('Name') or task.get('ID')}{' +' + str(value) if value else ''}")
            if isinstance(award_data.get("taskStatusInfo"), dict):
                task.update(award_data["taskStatusInfo"])
        else:
            say(f"⚠️ 任务领奖失败: {clean_line(task.get('Name') or task.get('ID'))}")
    if claimed:
        return "任务领奖：" + "、".join(claimed)
    return ""


def report_daily_task_action(
    server: str,
    auth: Dict[str, Any],
    proxies: Optional[Dict[str, str]],
    task: Dict[str, Any],
) -> bool:
    payload = {
        "comm": make_app_comm(auth, 23, 0, "DevopsBase"),
        "req_0": {
            "module": "music.activeCenter.ActTaskNewSvr",
            "method": "TaskActDataReport",
            "param": {"actID": task.get("_actID") or DAILY_TASK_ACT_ID, "taskID": task.get("ID"), "actData": 1},
        },
    }
    res = app_post(server, auth, "TaskActDataReport", payload, proxies)
    return app_request_succeeded(res)


def refresh_tasks_after_action(
    server: str,
    auth: Dict[str, Any],
    proxies: Optional[Dict[str, str]],
    target: Dict[str, Any],
) -> List[Dict[str, Any]]:
    refreshed: List[Dict[str, Any]] = []
    for _ in range(4):
        sleep(1.5)
        refreshed = get_daily_tasks(server, auth, proxies) or refreshed
        if task_reached_ready(refreshed, target):
            break
    return refreshed


def add_temporary_song_favorite(
    server: str,
    auth: Dict[str, Any],
    proxies: Optional[Dict[str, str]],
) -> Optional[Dict[str, Any]]:
    payload = {
        "comm": make_comm(auth),
        "req_0": {
            "module": "music.musicToplist.Toplist",
            "method": "GetDetail",
            "param": {"topId": 26, "offset": 0, "num": 10, "withTags": True},
        },
    }
    res = post_musicu(server, auth, payload, proxies)
    data = ((res or {}).get("req_0") or {}).get("data") or {}
    songs = (data.get("data") or {}).get("song") or []
    random.shuffle(songs)
    for song in songs:
        if not isinstance(song, dict):
            continue
        candidate = {"songId": int(to_float(song.get("songId"))), "songType": int(to_float(song.get("songType")))}
        if not candidate["songId"]:
            continue
        if add_favorite_song_if_new(server, auth, proxies, candidate):
            return candidate
    return None


def add_favorite_song_if_new(
    server: str,
    auth: Dict[str, Any],
    proxies: Optional[Dict[str, str]],
    song: Dict[str, Any],
) -> bool:
    payload = {
        "comm": make_comm(auth),
        "req_0": {
            "module": "music.musicasset.PlaylistDetailWrite",
            "method": "AddSonglist",
            "param": {
                "dirId": 201,
                "tid": 0,
                "bFmtUtf8": True,
                "v_songInfo": [{"songId": song["songId"], "songType": song["songType"]}],
            },
        },
    }
    res = post_musicu(server, auth, payload, proxies)
    added = read_song_add_status(res, song["songId"])
    return bool(added)


def update_favorite_song(
    server: str,
    auth: Dict[str, Any],
    proxies: Optional[Dict[str, str]],
    method: str,
    song: Dict[str, Any],
) -> bool:
    payload = {
        "comm": make_comm(auth),
        "req_0": {
            "module": "music.musicasset.PlaylistDetailWrite",
            "method": method,
            "param": {
                "dirId": 201,
                "tid": 0,
                "bFmtUtf8": True,
                "v_songInfo": [{"songId": song["songId"], "songType": song["songType"]}],
            },
        },
    }
    res = post_musicu(server, auth, payload, proxies)
    return music_request_succeeded(res)


def search_content(
    server: str,
    auth: Dict[str, Any],
    proxies: Optional[Dict[str, str]],
    search_type: int,
) -> Optional[Dict[str, Any]]:
    payload = {
        "comm": make_app_comm(auth, 1, 200605, "DevopsBase"),
        "req_0": {
            "module": "music.search.SearchCgiService",
            "method": "DoSearchForQQMusicMobile",
            "param": {
                "query": "音乐",
                "highlight": 1,
                "searchid": "",
                "sub_searchid": 0,
                "search_type": search_type,
                "sin": 0,
                "ein": 29,
                "page_num": 1,
                "num_per_page": 15,
                "cat": 2,
                "grp": 1,
                "remoteplace": "txt.mqq.all",
                "multi_zhida": 1,
            },
        },
    }
    res = app_post(server, auth, "DoSearchForQQMusicMobile", payload, proxies)
    if not app_request_succeeded(res):
        return None
    return (res.get("req_0") or {}).get("data")


def search_content_candidates(
    server: str,
    auth: Dict[str, Any],
    proxies: Optional[Dict[str, str]],
    search_type: int,
    keys: List[str],
) -> List[Any]:
    data = search_content(server, auth, proxies, search_type)
    return collect_values_by_keys(data, keys, lambda value: value != "" and value != 0, 20)


def search_singer_candidates(server: str, auth: Dict[str, Any], proxies: Optional[Dict[str, str]]) -> List[str]:
    data = search_content(server, auth, proxies, 7)
    return collect_singer_mids(data, 20)


def query_follow_status(
    server: str,
    auth: Dict[str, Any],
    proxies: Optional[Dict[str, str]],
    follow_type: int,
    target_id: str,
) -> Optional[bool]:
    payload = {
        "comm": make_app_comm(auth, 1, 200605, "DevopsBase"),
        "req_0": {
            "module": "music.follow.FollowStatus",
            "method": "QueryFollowStatus",
            "param": {"type": follow_type, "id": str(target_id)},
        },
    }
    res = app_post(server, auth, "QueryFollowStatus", payload, proxies)
    if not app_request_succeeded(res):
        return None
    data = (res.get("req_0") or {}).get("data")
    direct = read_target_status(data, str(target_id))
    if direct is not None:
        return direct
    return read_single_mapped_status(data, ["m_user_status", "m_singer_status"])


def update_favorite_playlist(
    server: str,
    auth: Dict[str, Any],
    proxies: Optional[Dict[str, str]],
    add: bool,
    playlist: Dict[str, Any],
) -> bool:
    method = "FavPlaylist" if add else "CancelFavPlaylist"
    payload = {
        "comm": make_comm(auth),
        "req_0": {
            "module": "music.musicasset.PlaylistFavWrite",
            "method": method,
            "param": {"v_playlistId": [int(playlist["playlistID"])]},
        },
    }
    res = post_musicu(server, auth, payload, proxies)
    return music_request_succeeded(res)


def add_temporary_playlist_favorite(
    server: str,
    auth: Dict[str, Any],
    proxies: Optional[Dict[str, str]],
) -> Optional[Dict[str, Any]]:
    dynamic_ids = search_content_candidates(server, auth, proxies, 4, ["dissid"])
    candidates = unique_values([*PLAYLIST_CANDIDATES, *dynamic_ids])
    for item in candidates:
        candidate = {"playlistID": int(to_float(item))}
        if candidate["playlistID"] <= 0:
            continue
        status = query_follow_status(server, auth, proxies, 500, str(candidate["playlistID"]))
        if status is True:
            continue
        if status is not False:
            continue
        if update_favorite_playlist(server, auth, proxies, True, candidate):
            return candidate
    return None


def update_favorite_audiobook(
    server: str,
    auth: Dict[str, Any],
    proxies: Optional[Dict[str, str]],
    add: bool,
    audiobook: Dict[str, Any],
) -> bool:
    payload = {
        "comm": make_comm(auth),
        "req_0": {
            "module": "music.favorSystemWrite.FavorSystem",
            "method": "do_favor",
            "param": {
                "reqtype": 1 if add else 2,
                "fav_type": 1,
                "vec_id": [str(audiobook["bookID"])],
            },
        },
    }
    res = post_musicu(server, auth, payload, proxies)
    return music_request_succeeded(res)


def add_temporary_audiobook_favorite(
    server: str,
    auth: Dict[str, Any],
    proxies: Optional[Dict[str, str]],
) -> Optional[Dict[str, Any]]:
    payload = {
        "comm": make_app_comm(auth, 1, 200605, "DevopsBase"),
        "req_0": {
            "module": "music.longRadio.LongRadioContent",
            "method": "GetChannelPageV2",
            "param": {
                "tabIndex": -1,
                "abt": "39445_39445003",
                "splashEndInterval": -1,
                "categoryId": AUDIOBOOK_CATEGORY_ID,
                "page": 1,
            },
        },
    }
    res = app_post(server, auth, "GetChannelPageV2", payload, proxies)
    dynamic_ids = collect_values_by_keys(
        res,
        ["albumID", "albumId", "album_id", "radioID", "radioId", "radio_id"],
        lambda value: re.match(r"^\d{6,12}$", str(value)),
        20,
    )
    candidates = unique_values([*AUDIOBOOK_CANDIDATES, *dynamic_ids])
    for item in candidates:
        candidate = {"bookID": str(item)}
        status = query_follow_status(server, auth, proxies, 400, candidate["bookID"])
        if status is True:
            continue
        if status is not False:
            continue
        if update_favorite_audiobook(server, auth, proxies, True, candidate):
            return candidate
    return None


def query_singer_follow_status(
    server: str,
    auth: Dict[str, Any],
    proxies: Optional[Dict[str, str]],
    mid: str,
) -> Optional[bool]:
    payload = {
        "comm": make_app_comm(auth, 1, 200605, "DevopsBase"),
        "req_0": {
            "module": "music.concern.ConcernSystem",
            "method": "cgi_qry_concern_status",
            "param": {"vec_userinfo": [{"usertype": 1, "userid": mid}]},
        },
    }
    res = app_post(server, auth, "cgi_qry_concern_status", payload, proxies)
    if not app_request_succeeded(res):
        return None
    data = (res.get("req_0") or {}).get("data")
    mapping = data.get("map_singer_status") if isinstance(data, dict) else None
    if isinstance(mapping, dict) and mid in mapping:
        return normalize_status(mapping[mid])
    return read_target_status(data, mid)


def update_singer_follow(
    server: str,
    auth: Dict[str, Any],
    proxies: Optional[Dict[str, str]],
    add: bool,
    singer: Dict[str, Any],
) -> bool:
    payload = {
        "comm": make_app_comm(auth, 23, 0, "DevopsBase"),
        "req_0": {
            "module": "music.concern.ConcernSystem",
            "method": "cgi_concern_user_v2",
            "param": {
                "bussinesstype": "",
                "source": 137,
                "opertype": 0 if add else 1,
                "bussinessid": "",
                "userinfo": {"userid": singer["mid"], "usertype": 1},
            },
        },
    }
    res = app_post(server, auth, "cgi_concern_user_v2", payload, proxies)
    return app_request_succeeded(res)


def add_temporary_singer_follow(
    server: str,
    auth: Dict[str, Any],
    proxies: Optional[Dict[str, str]],
) -> Optional[Dict[str, Any]]:
    dynamic_mids = search_singer_candidates(server, auth, proxies)
    candidates = unique_values([*SINGER_CANDIDATES, *dynamic_mids])
    for item in candidates:
        mid = str(item)
        if not re.match(r"^[A-Za-z0-9]{10,20}$", mid):
            continue
        candidate = {"mid": mid}
        status = query_singer_follow_status(server, auth, proxies, mid)
        if status is True:
            continue
        if status is not False:
            continue
        if update_singer_follow(server, auth, proxies, True, candidate):
            return candidate
    return None


def claim_daily_task_rewards(server: str, auth: Dict[str, Any], proxies: Optional[Dict[str, str]]) -> str:
    tasks = get_daily_tasks(server, auth, proxies)
    if not tasks:
        return "每日任务查询失败"
    messages: List[str] = []
    cleanups: List[Dict[str, Any]] = []
    try:
        quiz_task = find_daily_task(tasks, lambda t: t.get("ID") == "Zff1WO" or re.search(r"皇宫身份", t.get("Name") or ""))
        if quiz_task and quiz_task.get("State") == 1 and ENABLE_ACTIVITY:
            if report_daily_task_action(server, auth, proxies, quiz_task):
                tasks = refresh_tasks_after_action(server, auth, proxies, quiz_task)

        money_tree_task = find_daily_task(tasks, lambda t: t.get("ID") == "5E9TC" or re.search(r"摇钱树", t.get("Name") or ""))
        if money_tree_task and money_tree_task.get("State") == 1 and ENABLE_ACTIVITY:
            if report_daily_task_action(server, auth, proxies, money_tree_task):
                tasks = refresh_tasks_after_action(server, auth, proxies, money_tree_task)

        if ENABLE_FAVORITE:
            favorite_song_task = find_daily_task(
                tasks,
                lambda t: t.get("ID") == "Z1mKlEI" or t.get("TaskActTypID") == "2tuNRp" or int(to_float(t.get("Type"))) == 8,
            )
            if favorite_song_task and favorite_song_task.get("State") == 1:
                song = add_temporary_song_favorite(server, auth, proxies)
                if song:
                    cleanups.append(
                        {
                            "name": "临时收藏歌曲",
                            "run": lambda: update_favorite_song(server, auth, proxies, "DelSonglist", song),
                        }
                    )
                    tasks = refresh_tasks_after_action(server, auth, proxies, favorite_song_task)
                    if not task_reached_ready(tasks, favorite_song_task):
                        messages.append("收藏歌曲任务状态未更新,已恢复临时收藏")

            favorite_playlist_task = find_daily_task(
                tasks,
                lambda t: t.get("ID") == "ZBiJs9" or int(to_float(t.get("Type"))) == 9,
            )
            if favorite_playlist_task and favorite_playlist_task.get("State") == 1:
                playlist = add_temporary_playlist_favorite(server, auth, proxies)
                if playlist:
                    cleanups.append(
                        {
                            "name": "临时收藏歌单",
                            "run": lambda: update_favorite_playlist(server, auth, proxies, False, playlist),
                        }
                    )
                    tasks = refresh_tasks_after_action(server, auth, proxies, favorite_playlist_task)
                    if not task_reached_ready(tasks, favorite_playlist_task) and report_daily_task_action(
                        server, auth, proxies, favorite_playlist_task
                    ):
                        tasks = refresh_tasks_after_action(server, auth, proxies, favorite_playlist_task)

            favorite_audiobook_task = find_daily_task(
                tasks,
                lambda t: t.get("ID") == "Z5bnvq" or t.get("TaskActTypID") == "CeYSX" or int(to_float(t.get("Type"))) == 36,
            )
            if favorite_audiobook_task and favorite_audiobook_task.get("State") == 1:
                audiobook = add_temporary_audiobook_favorite(server, auth, proxies)
                if audiobook:
                    cleanups.append(
                        {
                            "name": "临时收藏有声书",
                            "run": lambda: update_favorite_audiobook(server, auth, proxies, False, audiobook),
                        }
                    )
                    tasks = refresh_tasks_after_action(server, auth, proxies, favorite_audiobook_task)

            follow_singer_task = find_daily_task(
                tasks,
                lambda t: t.get("ID") == "2wgcMV" or int(to_float(t.get("Type"))) == 13,
            )
            if follow_singer_task and follow_singer_task.get("State") == 1:
                singer = add_temporary_singer_follow(server, auth, proxies)
                if singer:
                    cleanups.append(
                        {
                            "name": "临时关注歌手",
                            "run": lambda: update_singer_follow(server, auth, proxies, False, singer),
                        }
                    )
                    tasks = refresh_tasks_after_action(server, auth, proxies, follow_singer_task)

        reward_msg = claim_ready_task_rewards(server, auth, proxies, [t for t in tasks if not is_timed_floor_task(t)])
        if reward_msg:
            messages.append(reward_msg)
    finally:
        for cleanup in cleanups:
            try:
                ok = cleanup["run"]()
                say(f"🧹 {cleanup['name']}{'已恢复' if ok else '恢复失败'}")
            except Exception as exc:
                say(f"⚠️ {cleanup['name']}恢复异常: {clean_line(exc)}")
    return "；".join(messages) if messages else "每日任务处理完成"


def get_timer_tasks(server: str, auth: Dict[str, Any], proxies: Optional[Dict[str, str]]) -> Optional[List[Dict[str, Any]]]:
    payload = {
        "comm": make_app_comm(auth, 23, 0, "DevopsBase"),
        "req_0": {
            "module": "music.activeCenter.ActTaskNewSvr",
            "method": "GetTaskModules",
            "param": {"actID": DAILY_TASK_ACT_ID, "taskModuleIDs": [TIMER_TASK_MODULE_ID]},
        },
    }
    res = app_post(server, auth, "GetTaskModules", payload, proxies)
    if not app_request_succeeded(res):
        return None
    modules = ((res.get("req_0") or {}).get("data") or {}).get("taskModules") or {}
    tasks: List[Dict[str, Any]] = []
    for module in modules.values():
        if not isinstance(module, dict):
            continue
        for task in module.get("TaskList") or []:
            if not isinstance(task, dict):
                continue
            task = dict(task)
            task["_actID"] = DAILY_TASK_ACT_ID
            tasks.append(task)
    return tasks


def claim_timed_task_rewards(server: str, auth: Dict[str, Any], proxies: Optional[Dict[str, str]]) -> str:
    messages: List[str] = []
    for source in ("floor", "treasure"):
        queried = (
            query_daily_tasks(server, auth, proxies, "18NtBy", [193])
            if source == "floor"
            else get_timer_tasks(server, auth, proxies)
        )
        timed_tasks = unique_tasks([t for t in (queried or []) if source != "floor" or is_timed_floor_task(t)])
        if not timed_tasks:
            continue
        reward_msg = claim_ready_task_rewards(server, auth, proxies, timed_tasks)
        if reward_msg:
            messages.append(reward_msg)
    return "；".join(messages) if messages else ""


def is_daily_flow_due(now: Optional[datetime] = None) -> bool:
    now = now or datetime.now()
    return now.hour > 7 or (now.hour == 7 and now.minute >= 30)


def is_timer_window(now: Optional[datetime] = None) -> bool:
    now = now or datetime.now()
    return now.hour in (9, 10)


def get_red_packet_slot(now: Optional[datetime] = None) -> str:
    now = now or datetime.now()
    return str(now.hour) if now.hour in (0, 8, 12, 16, 20, 22) else ""


def run_account(index: int, total: int, account: AccountTarget) -> Dict[str, Any]:
    server = account.server
    extras: List[str] = []
    result: Dict[str, Any] = {
        "account": account.label,
        "phone": "",
        "status": "-",
        "reward": "-",
        "extra": extras,
        "error": "",
        "success": False,
        "server": server,
        "balance": "-",
        "uin": "",
        "coin_gain": 0,
    }

    say(f"—— 账号 {index}/{total} {account.label} ——")

    proxies, proxy_ip = get_valid_proxy(account.label)
    if proxy_ip and proxy_ip != "-":
        extras.append(f"出口IP {mask_id(proxy_ip, 6)}")
    extras.append("代理" if proxies else "直连")

    sleep(PROXY_FETCH_INTERVAL)
    delay = random.randint(2, 6)
    sleep(delay)

    code = get_code(account)
    if not code:
        result["error"] = "获取 code 失败"
        result["status"] = "获取 code 失败 ❌"
        return result

    result["account"] = account.label
    auth, _raw_login = login_by_code(server, code, proxies)
    if not auth:
        result["error"] = "登录失败"
        result["status"] = "登录失败 ❌"
        return result

    result["uin"] = str(auth.get("uin") or "")
    extras.append(f"uin {mask(auth.get('uin'))}")

    try:
        now = datetime.now()
        coin_gain = 0
        status_parts: List[str] = []

        if not is_daily_flow_due(now):
            result["status"] = "⏰ 未到每日签到时段(7:30 后)"
            result["success"] = True
            return result

        lvz = check_lvz_score(server, auth, proxies)
        say(f"💎 {lvz}")
        status_parts.append(lvz)

        coin = check_coin_sign_in(server, auth, proxies)
        say(f"🪙 {coin}")
        status_parts.append(coin)

        task_msg = claim_daily_task_rewards(server, auth, proxies)
        say(f"📋 {task_msg}")
        extras.append(task_msg)

        lottery_msgs: List[str] = []
        if ENABLE_ACTIVITY:
            lot_sign = check_lottery_sign_in(server, auth, proxies)
            say(f"🎰 {lot_sign}")
            lottery_msgs.append(lot_sign)
            lot_draw, lot_coins = draw_coin_lottery(server, auth, proxies)
            say(f"🎰 {lot_draw}")
            lottery_msgs.append(lot_draw)
            coin_gain += lot_coins
        else:
            lottery_msgs.append("活动已关闭")
        extras.extend(lottery_msgs)

        if ENABLE_ACTIVITY and is_timer_window(now):
            timed_msg = claim_timed_task_rewards(server, auth, proxies)
            if timed_msg:
                extras.append(timed_msg)
                say(f"⏰ {timed_msg}")

        red_msg = "当前时段无红包雨"
        if ENABLE_ACTIVITY and get_red_packet_slot(now):
            red_msg, red_coins = run_red_packet_rain(server, auth, proxies)
            coin_gain += red_coins
        say(f"🧧 {red_msg}")
        extras.append(red_msg)

        balance = query_coin_balance(server, auth, proxies)
        result["balance"] = balance
        say(f"💰 余额 {balance}")
        extras.append(f"金币余额 {balance}")

        result["coin_gain"] = coin_gain
        if coin_gain:
            result["reward"] = f"金币 +{coin_gain}"
        elif balance not in ("-", "", None):
            result["reward"] = f"余额 {balance}"

        # 签到状态取绿钻/金币主结果
        main_status = "、".join([p for p in status_parts if p][:2])
        result["status"] = (main_status or "任务已执行") + " ✅"
        result["success"] = True
        result["extra"] = extras
        return result
    except Exception as exc:
        result["error"] = clean_line(exc) or str(exc)[:80]
        result["status"] = f"执行异常 ❌ ({result['error'][:40]})"
        if IS_DEBUG:
            say(traceback.format_exc())
        result["extra"] = extras
        return result


def main() -> int:
    global CURRENT_ACCOUNTS
    started = time.time()
    try:
        CURRENT_ACCOUNTS = load_account_targets()
        CURRENT_ACCOUNTS = filter_accounts(CURRENT_ACCOUNTS, lambda account: account.ref, app_id=APPID, log=say)
    except Exception as exc:
        say(f"❌ 配置错误: {clean_line(exc)}")
        return 1

    say(f"{APP_NAME} | {len(CURRENT_ACCOUNTS)}账号 | {now_text()}")

    results: List[Dict[str, Any]] = []
    for index, account in enumerate(CURRENT_ACCOUNTS, 1):
        try:
            result = run_account(index, len(CURRENT_ACCOUNTS), account)
            update_from_result(account.ref, result, app_id=APPID)
        except Exception as exc:
            result = {
                "account": account.label,
                "phone": "",
                "status": f"脚本异常 ❌ ({clean_line(exc)[:40]})",
                "reward": "-",
                "extra": [],
                "error": clean_line(exc) or "异常",
                "success": False,
            }
            if IS_DEBUG:
                say(traceback.format_exc())
        results.append(result)
        if index < len(CURRENT_ACCOUNTS):
            sleep(2)

    ok_n = sum(1 for r in results if r.get("success"))
    try:
        notify_and_format(
            APP_NAME,
            results,
            title=f"QQ音乐签到 {ok_n}/{len(results)}",
            start_ts=started,
        )
    except Exception:
        print(format_report(APP_NAME, results, push_result="推送模块异常", cost_s=time.time() - started))
        try:
            send_notify(f"QQ音乐签到 {ok_n}/{len(results)}", format_report(APP_NAME, results))
        except Exception:
            pass
    return 0 if ok_n == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
