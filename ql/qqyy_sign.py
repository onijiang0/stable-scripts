#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# /*
# ------------------------------------------
# @Author: onijiang0
# @Date: 2026.09.20
# @Description: QQ音乐 - 小程序签到/金币/任务/抽奖（取码服务换 code）
# cron: 20 14 * * *
# ------------------------------------------
# 变量名：qqyy
# 变量值：wx_server 里的 openid，多账号用 & 或换行分隔（可加 #备注）
#
# 依赖变量：
# wx_server_url    必填，wx_server 地址（勿写进仓库）
# wx_auth          必填，wx_server 鉴权值（/wx/code 用）
# qqyy_appid       可选，默认 wxada7aab80ba27074
# PROXY_API        可选，HTTP/socks5 代理提取地址
# PROXY_TYPE       可选，http / socks5
# QQ_ENABLE_ACTIVITY  可选，抽奖/红包，默认 1
# QQ_ENABLE_FAVORITE  可选，临时收藏/关注任务，默认 1
# QQ_DEBUG          可选，调试日志，默认 0
# QL_NOTIFY         可选，设为 0 关闭推送
# 注：推送结果由 send_notify 统一短文案输出
#
# 契约（appid wxada7aab80ba27074）：
# 取码服务  POST {wx_server_url}/wx/code  auth:{wx_auth} json:{openid,appid}
#           -> data.code（每账号只调 1 次）
# 登录      POST https://u.y.qq.com/cgi-bin/musicu.fcg
#           music.login.LoginServer.Login  body.comm+login.param{code,strAppid}
#           -> login.data.musickey / musicid
# 绿钻      music.lvz.MuFest13TaskSvr.EveryDaySignLvzScore
# 金币签到  music.actCenter.ActCenterSignNewSvr GetSignInSummary/SignIn/AwardPrize
# 抽奖/红包/任务  见脚本内 module.method（actId 运行时只读）
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
    from send_notify import (
        clean_line,
        format_report,
        mask_id,
        mask_phone,
        notify_and_format,
        send_notify,
    )
except Exception:
    def clean_line(line: Any) -> str:
        s = str(line or "").strip()
        return "" if s.startswith(("{", "[")) else s[:180]

    def mask_id(value: Any, keep: int = 8) -> str:
        s = str(value or "")
        return (s[:keep] + "***") if len(s) > keep else (s or "-")

    def mask_phone(phone: Any) -> str:
        s = re.sub(r"\D", "", str(phone or ""))
        return (s[:3] + "****" + s[-4:]) if len(s) >= 11 else (s or "-")

    def format_report(task, accounts, push_result="", cost_s=None):
        return task

    def notify_and_format(task, accounts, **kwargs):
        return send_notify(task, str(accounts))

    def send_notify(title: str, content: str) -> str:
        print("🔔 推送结果：跳过（send_notify 不可用）")
        return "跳过推送"


APP_NAME = "QQ音乐"
APPID = (os.getenv("qqyy_appid") or "wxada7aab80ba27074").strip()

PROXY_API = os.getenv("PROXY_API", "")
PROXY_TYPE = os.getenv("PROXY_TYPE", "http").lower()
ENABLE_ACTIVITY = os.getenv("QQ_ENABLE_ACTIVITY", "1") not in ("0", "false", "False")
ENABLE_FAVORITE = os.getenv("QQ_ENABLE_FAVORITE", "1") not in ("0", "false", "False")
IS_DEBUG = os.getenv("QQ_DEBUG", "0") in ("1", "true", "True")

PROXY_RETRY_TIMES = 3
PROXY_VALIDATE_URL = "http://httpbin.org/ip"
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
    "MicroMessenger/7.0.20.1781 NetType/WIFI MiniProgramEnv/Windows WindowsWechat"
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


def to_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def direct_session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    return s


def sc_wx_code(openid: str) -> str:
    base = os.getenv("wx_server_url", "").strip().rstrip("/")
    auth = os.getenv("wx_auth", "").strip()
    if not base or not auth:
        raise RuntimeError("缺少 wx_server_url / wx_auth")
    r = direct_session().post(
        f"{base}/wx/code",
        json={"openid": openid, "appid": APPID},
        headers={"auth": auth, "User-Agent": USER_AGENT},
        timeout=20,
    )
    data = r.json()
    if not data.get("status"):
        raise RuntimeError(f"/wx/code 失败: {data.get('message') or data}")
    code = ((data.get("data") or {}).get("code") or "").strip()
    if not code:
        raise RuntimeError("/wx/code 未返回 code")
    return code


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


def build_proxy_dict(info: Optional[Dict[str, Any]]) -> Optional[Dict[str, str]]:
    if not info:
        return None
    auth = ""
    if info.get("username") and info.get("password"):
        auth = f"{quote(info['username'])}:{quote(info['password'])}@"
    scheme = "socks5" if PROXY_TYPE == "socks5" else "http"
    url = f"{scheme}://{auth}{info['host']}:{info['port']}"
    return {"http": url, "https": url}


def get_valid_proxy() -> Optional[Dict[str, str]]:
    if not PROXY_API:
        return None
    for _ in range(PROXY_RETRY_TIMES):
        try:
            resp = direct_session().get(PROXY_API, timeout=15)
            proxies = build_proxy_dict(parse_proxy_response(resp.text))
            if not proxies:
                continue
            try:
                ok = requests.get(PROXY_VALIDATE_URL, proxies=proxies, timeout=15)
                if ok.status_code == 200:
                    return proxies
            except Exception:
                pass
        except Exception:
            pass
    return None


def request_with_proxy(method: str, url: str, *, proxies=None, **kwargs):
    kwargs.setdefault("timeout", REQUEST_TIMEOUT)
    kwargs.setdefault("verify", False)
    if proxies:
        try:
            return requests.request(method, url, proxies=proxies, **kwargs)
        except Exception as exc:
            say(f"⚠️ 代理失败: {clean_line(exc)}")
            if not ENABLE_DIRECT_FALLBACK:
                raise
    return direct_session().request(method, url, **kwargs)


def common_headers(auth: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    h = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
        "Accept": "*/*",
        "xweb_xhr": "1",
        "Referer": f"https://servicewechat.com/{APPID}/175/page-frame.html",
    }
    if auth:
        h["Cookie"] = f"uin=o{auth['uin']}; qm_keyst={auth['authst']}"
    return h


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


def login_by_code(code: str, proxies=None) -> Optional[Dict[str, Any]]:
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
    resp = request_with_proxy("POST", MUSIC_API_URL, headers=common_headers(), json=payload, proxies=proxies)
    try:
        data = resp.json()
    except Exception:
        data = {}
    musickey, musicid = extract_musickey(data)
    if musickey:
        return {"uin": musicid, "authst": musickey}
    say("❌ 登录未返回 musickey")
    return None


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
    p1i = [23, 14, 6, 36, 16, 7, 19]
    p2i = [16, 1, 32, 12, 19, 27, 8, 5]
    scramble = [89, 39, 179, 150, 218, 82, 58, 252, 177, 52, 186, 123, 120, 64, 242, 133, 143, 161, 121, 179]
    part1 = "".join(h[i] for i in p1i)
    part2 = "".join(h[i] for i in p2i)
    raw = bytes(scramble[i] ^ int(h[i * 2:i * 2 + 2], 16) for i in range(20))
    mid = base64.b64encode(raw).decode().replace("\\", "").replace("/", "").replace("+", "").replace("=", "")
    return ("zzc" + part1 + mid + part2).lower()


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


def post_musicu(auth, payload, proxies=None) -> Optional[Dict[str, Any]]:
    try:
        return request_with_proxy(
            "POST", MUSIC_API_URL, headers=common_headers(auth), json=payload, proxies=proxies
        ).json()
    except Exception as exc:
        say(f"❌ musicu: {clean_line(exc)}")
        return None


def app_post(auth, cgi_key, payload, proxies=None) -> Optional[Dict[str, Any]]:
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    sign = zzc_sign(body)
    url = f"{APP_API_URL}?_webcgikey={quote(cgi_key)}&_={int(time.time() * 1000)}&sign={sign}"
    headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/x-www-form-urlencoded",
        "Cookie": f"uin=o{auth['uin']}; qm_keyst={auth['authst']}",
        "Referer": f"https://servicewechat.com/{APPID}/175/page-frame.html",
    }
    try:
        return request_with_proxy("POST", url, headers=headers, data=body, proxies=proxies).json()
    except Exception as exc:
        say(f"❌ musics({cgi_key}): {clean_line(exc)}")
        return None


def app_ok(res: Optional[Dict[str, Any]], req_key: str = "req_0") -> bool:
    if not res or res.get("code") != 0:
        return False
    req = res.get(req_key)
    if not req or req.get("code") != 0:
        return False
    data = req.get("data")
    if not isinstance(data, dict):
        return True
    for k in ("retCode", "RetCode", "ret", "Ret", "code"):
        if k in data and to_float(data[k]) != 0:
            return False
    return True


def music_ok(res: Optional[Dict[str, Any]]) -> bool:
    if not res or res.get("code") != 0:
        return False
    req = res.get("req_0") or {}
    data = req.get("data")
    return bool(req.get("code") == 0 and data and to_float(data.get("retCode", 0)) == 0)


def find_first(root: Any, keys: List[str]) -> Any:
    wanted = set(keys)
    found = [None]

    def walk(v):
        if found[0] is not None or v is None:
            return
        if isinstance(v, list):
            for i in v:
                walk(i)
            return
        if not isinstance(v, dict):
            return
        for k, item in v.items():
            if k in wanted:
                found[0] = item
                return
            walk(item)

    walk(root)
    return found[0]


def unique(values: Any) -> List[Any]:
    out, seen = [], set()
    for v in values or []:
        s = str(v)
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(v)
    return out


def collect_keys(root: Any, keys: List[str], pred=None, limit: int = 20) -> List[Any]:
    wanted = set(keys)
    out: List[Any] = []

    def walk(v):
        if len(out) >= limit or v is None:
            return
        if isinstance(v, list):
            for i in v:
                walk(i)
            return
        if not isinstance(v, dict):
            return
        for k, item in v.items():
            if k in wanted and (pred is None or pred(item)):
                out.append(item)
            walk(item)

    walk(root)
    return unique(out)


def check_lvz(auth, proxies=None) -> str:
    payload = {
        "comm": make_comm(auth),
        "req_0": {
            "module": "music.lvz.MuFest13TaskSvr",
            "method": "EveryDaySignLvzScore",
            "param": {"Uin": auth["uin"], "Cmd": "get"},
        },
    }
    res = post_musicu(auth, payload, proxies)
    if not res:
        return "绿钻成长值签到无响应"
    r0 = res.get("req_0") or {}
    data = r0.get("data") or {}
    ret = data.get("Ret")
    msg = str(data.get("Msg") or "")
    if ret == 0:
        score = (data.get("Info") or {}).get("Score") or 0
        return f"绿钻成长值签到成功 +{score}" if score else "绿钻成长值签到成功"
    if ret == 20019 or re.search(r"已.*领取|已签|重复", msg):
        return "绿钻成长值今日已签到"
    return f"绿钻成长值已处理 (Ret={ret})"


def get_coin_state(auth, proxies=None) -> Optional[Dict[str, Any]]:
    payload = {
        "comm": make_comm(auth),
        "req_0": {
            "module": "music.actCenter.ActCenterSignNewSvr",
            "method": "GetSignInSummary",
            "param": {"ActID": COIN_SIGN_ACT_ID},
        },
        "req_1": {
            "module": "music.actCenter.ActCenterSignNewSvr",
            "method": "GetSignInTaskList",
            "param": {"ActID": COIN_SIGN_ACT_ID, "ScenesID": COIN_SIGN_SCENE_ID},
        },
    }
    res = post_musicu(auth, payload, proxies)
    summary = res.get("req_0") if res else None
    tasks = res.get("req_1") if res else None
    sd = summary.get("data") if summary else None
    td = tasks.get("data") if tasks else None
    if not res or res.get("code") != 0 or not summary or summary.get("code") != 0:
        return None
    if not tasks or tasks.get("code") != 0 or not sd or sd.get("retCode") != 0 or not td or td.get("retCode") != 0:
        return None
    tli = td.get("TaskListInfo") or {}
    tlist = ((tli.get("TaskList") or {}).get("ContinueTaskList")) or {}
    return {"info": td.get("Info") or sd.get("Info") or {}, "taskList": tlist}


def check_coin(auth, proxies=None) -> str:
    state = get_coin_state(auth, proxies)
    if not state:
        return "金币中心签到失败"
    signed_now = False
    if not state["info"].get("IsSignIn"):
        payload = {
            "comm": make_comm(auth),
            "req_0": {
                "module": "music.actCenter.ActCenterSignNewSvr",
                "method": "SignIn",
                "param": {"ActID": COIN_SIGN_ACT_ID, "ScenesID": COIN_SIGN_SCENE_ID},
            },
        }
        res = post_musicu(auth, payload, proxies)
        sign_req = res.get("req_0") if res else None
        sign_data = sign_req.get("data") if sign_req else None
        if not res or not sign_req or not sign_data or sign_data.get("retCode") != 0:
            return "金币中心签到失败"
        if not (sign_data.get("Info") or {}).get("IsSignIn"):
            return "金币中心签到失败"
        signed_now = True
        state = get_coin_state(auth, proxies) or state
    day = int(to_float(state["info"].get("ContinueSignInCount")))
    tmap = state["taskList"] or {}
    task = next((x for x in tmap.values() if isinstance(x, dict) and x.get("State") == 2), None) or tmap.get(str(day))
    reward = ""
    if isinstance(task, dict):
        pl = task.get("PrizeList")
        if isinstance(pl, list) and pl:
            reward = str(pl[0].get("Name") or pl[0].get("Value") or "")
    if isinstance(task, dict) and task.get("State") == 2:
        payload = {
            "comm": make_comm(auth),
            "req_0": {
                "module": "music.actCenter.ActCenterSignNewSvr",
                "method": "AwardPrize",
                "param": {"ActID": COIN_SIGN_ACT_ID, "TaskID": task.get("ID")},
            },
        }
        res = post_musicu(auth, payload, proxies)
        ad = ((res or {}).get("req_0") or {}).get("data") or {}
        if res and ((res.get("req_0") or {}).get("code") == 0) and ad.get("retCode") in (0, 100004):
            base = "金币中心签到成功" if signed_now else "金币中心奖励已领取"
            return f"{base} ✅" + (f"（{reward}）" if reward else "")
        return "金币中心已签到,领奖失败"
    if state["info"].get("IsSignIn"):
        return "金币中心今日已签到 ✅"
    return "金币中心签到状态未确认"


def check_lottery_sign(auth, proxies=None) -> str:
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
    res = app_post(auth, "GetSignInSummary", payload, proxies)
    if not app_ok(res, "req_0") or not app_ok(res, "req_1"):
        return "金币抽奖签到失败"
    sd = (res.get("req_0") or {}).get("data") or {}
    td = (res.get("req_1") or {}).get("data") or {}
    info = td.get("Info") or sd.get("Info") or {}
    tli = td.get("TaskListInfo") or {}
    tmap = ((tli.get("TaskList") or {}).get("ContinueTaskList")) or {}
    signed_now = False
    if not info.get("IsSignIn"):
        payload2 = {
            "comm": make_app_comm(auth, 1, 200605, "DevopsCoinCenter3"),
            "req_0": {
                "module": "music.actCenter.ActCenterSignNewSvr",
                "method": "SignIn",
                "param": {"ActID": LOTTERY_SIGN_ACT_ID},
            },
        }
        if not app_ok(app_post(auth, "SignIn", payload2, proxies)):
            return "金币抽奖签到失败"
        signed_now = True
    task = next((x for x in tmap.values() if isinstance(x, dict) and x.get("State") == 2), None)
    if isinstance(task, dict):
        payload3 = {
            "comm": make_app_comm(auth, 1, 200605, "DevopsCoinCenter3"),
            "req_0": {
                "module": "music.actCenter.ActCenterSignNewSvr",
                "method": "AwardPrize",
                "param": {"ActID": LOTTERY_SIGN_ACT_ID, "TaskID": task.get("ID")},
            },
        }
        if app_ok(app_post(auth, "AwardPrize", payload3, proxies)):
            return "金币抽奖签到成功 ✅"
        return "金币抽奖签到已完成,领奖失败"
    return "金币抽奖签到已完成 ✅" if signed_now else "金币抽奖今日已签到 ✅"


def draw_lottery(auth, proxies=None) -> Tuple[str, int]:
    payload = {
        "comm": make_app_comm(auth, 1, 200605, "DevopsCoinCenter3"),
        "req_0": {
            "module": "music.actCenter.CoinLotterySvr",
            "method": "GetCoinUserInfo",
            "param": {"Param": 1, "Playid": COIN_LOTTERY_PLAY_ID},
        },
    }
    info_res = app_post(auth, "GetCoinUserInfo", payload, proxies)
    if not app_ok(info_res):
        return "金币抽奖状态查询失败", 0
    remain = int(to_float(find_first((info_res.get("req_0") or {}).get("data"), ["lotteryRemain"])))
    if remain <= 0:
        return "金币抽奖机会已用完", 0
    gifts, coins = [], 0
    for _ in range(min(remain, 8)):
        p = {
            "comm": make_app_comm(auth, 1, 200605, "DevopsCoinCenter3"),
            "req_0": {
                "module": "music.actCenter.CoinLotterySvr",
                "method": "UserCoinLottery",
                "param": {"Param": 1, "Playid": COIN_LOTTERY_PLAY_ID},
            },
        }
        res = app_post(auth, "UserCoinLottery", p, proxies)
        if not app_ok(res):
            break
        gift = find_first((res.get("req_0") or {}).get("data"), ["lotteryGift", "giftName", "prizeName"])
        if isinstance(gift, str) and gift.startswith("{"):
            try:
                gift = json.loads(gift)
            except Exception:
                pass
        name = find_first(gift if isinstance(gift, dict) else {}, ["giftName", "prizeName", "name"]) or "已领取"
        val = to_float(find_first(gift if isinstance(gift, dict) else {}, ["awardValue", "coinNum", "coin"]))
        label = f"{name} +{int(val)}" if val else str(name)
        gifts.append(label)
        m = re.search(r"\+(\d+)", label)
        if m:
            coins += int(m.group(1))
        time.sleep(0.35)
    if gifts:
        return f"金币抽奖 {len(gifts)} 次：{'、'.join(gifts[:4])}", coins
    return "金币抽奖未完成", 0


def run_red_packet(auth, proxies=None) -> Tuple[str, int]:
    payload = {
        "comm": make_app_comm(auth, 1, 200605, "DevopsCoinCenter3"),
        "req_0": {
            "module": "music.actCenter.RedPacketRainSvr",
            "method": "Raining",
            "param": {"RainKey": RED_PACKET_RAIN_KEY},
        },
    }
    res = app_post(auth, "Raining", payload, proxies)
    if not app_ok(res):
        return "红包雨当前无可领次数", 0
    completed, coins = 0, 0
    for _ in range(4):
        cp = {
            "comm": make_app_comm(auth, 1, 200605, "DevopsCoinCenter3"),
            "req_0": {
                "module": "music.actCenter.RedPacketRainSvr",
                "method": "IncrChance",
                "param": {"RainKey": RED_PACKET_RAIN_KEY, "IncrType": 2},
            },
        }
        if not app_ok(app_post(auth, "IncrChance", cp, proxies)):
            break
        time.sleep(0.6)
        dp = {
            "comm": make_app_comm(auth, 1, 200605, "DevopsCoinCenter3"),
            "req_0": {
                "module": "music.actCenter.RedPacketRainSvr",
                "method": "DrawPrizes",
                "param": {"RainKey": RED_PACKET_RAIN_KEY, "HitNum": 10, "HitStreakNum": 0},
            },
        }
        dr = app_post(auth, "DrawPrizes", dp, proxies)
        if not app_ok(dr):
            break
        completed += 1
        coins += int(to_float(find_first((dr.get("req_0") or {}).get("data"), ["awardValue", "RewardGold", "coin"])))
    if completed:
        return f"红包雨 {completed} 次" + (f" +{coins} 金币" if coins else ""), coins
    return "红包雨未完成", 0


def query_balance(auth, proxies=None) -> str:
    url = "https://i2.y.qq.com/n3/coin_center/pages/client_v1/sign.html?_hidehd=1&_hdct=1&_miniplayer=1"
    try:
        text = request_with_proxy(
            "GET",
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Cookie": f"uin=o{auth['uin']}; qm_keyst={auth['authst']}",
            },
            proxies=proxies,
        ).text
        m = re.search(r'__ssrFirstPageData__="((?:[^"\\]|\\.)*)"', text)
        if not m:
            return "-"
        raw = m.group(1).replace('\\"', '"').replace("\\\\", "\\")
        data = json.loads(raw)
        coin = data.get("coin") if isinstance(data, dict) else None
        return str(int(to_float(coin))) if coin is not None else "-"
    except Exception:
        return "-"


def query_daily_tasks(auth, proxies=None) -> List[Dict[str, Any]]:
    payload = {
        "comm": make_app_comm(auth, 1, 200605, "DevopsCoinCenter3"),
        "req_0": {
            "module": "music.activeCenter.FloorManagerSvr",
            "method": "GetFloors",
            "param": {"Release": 1, "PageID": "18NtBy", "PersonalityMode": 1, "FloorIDs": [193]},
        },
    }
    res = app_post(auth, "GetFloors", payload, proxies)
    if not res or res.get("code") != 0:
        return []
    req = res.get("req_0") or {}
    data = req.get("data") or {}
    if req.get("code") != 0 or data.get("RetCode") != 0:
        return []
    tasks: List[Dict[str, Any]] = []
    for floor in data.get("Floors") or []:
        if not isinstance(floor, dict):
            continue
        for item in floor.get("ItemList") or []:
            conf = item.get("ResourceConf") if isinstance(item, dict) else None
            if isinstance(conf, str):
                try:
                    conf = json.loads(conf)
                except Exception:
                    conf = None
            if not isinstance(conf, dict):
                continue
            for task in ((conf.get("ActTaskModule") or {}).get("TaskList")) or []:
                if isinstance(task, dict):
                    t = dict(task)
                    t["_actID"] = conf.get("ActID") or DAILY_TASK_ACT_ID
                    tasks.append(t)
    return tasks


def claim_tasks(auth, tasks: List[Dict[str, Any]], proxies=None) -> str:
    claimed = []
    for task in tasks:
        if not isinstance(task, dict) or task.get("State") != 2:
            continue
        pl = task.get("PrizeList")
        if not (isinstance(pl, list) and any(isinstance(p, dict) and int(to_float(p.get("Type"))) == 12 for p in pl)):
            continue
        payload = {
            "comm": make_app_comm(auth, 23, 0, "DevopsBase"),
            "req_0": {
                "module": "music.activeCenter.ActTaskNewSvr",
                "method": "AwardTaskPrize",
                "param": {"actID": task.get("_actID"), "TaskID": task.get("ID")},
            },
        }
        res = app_post(auth, "AwardTaskPrize", payload, proxies)
        ad = ((res or {}).get("req_0") or {}).get("data") or {}
        if res and ((res.get("req_0") or {}).get("code") == 0) and ad.get("retCode") == 0:
            val = int(to_float(ad.get("awardValue")))
            claimed.append(f"{task.get('Name') or task.get('ID')}{' +' + str(val) if val else ''}")
    return "任务领奖：" + "、".join(claimed) if claimed else "每日任务处理完成"


def is_due(now: datetime) -> bool:
    return now.hour > 7 or (now.hour == 7 and now.minute >= 30)


def run_account(openid: str, index: int, total: int) -> Dict[str, Any]:
    extras: List[str] = [f"openid {mask_id(openid, 8)}"]
    acc: Dict[str, Any] = {
        "account": f"账号{index}",
        "phone": "",
        "status": "-",
        "reward": "-",
        "extra": extras,
        "error": "",
        "success": False,
    }
    say(f"—— 账号 {index}/{total} ——")
    proxies = get_valid_proxy()
    extras.append("代理" if proxies else "直连")
    try:
        say("🔐 获取 code")
        code = sc_wx_code(openid)
        say("✅ code 获取成功")
    except Exception as e:
        acc["status"] = f"获取 code 失败 ❌ ({clean_line(e)[:40]})"
        acc["error"] = clean_line(e)[:80]
        return acc
    time.sleep(random.uniform(0.5, 2.0))
    try:
        say("🔐 code 换 musickey")
        auth = login_by_code(code, proxies)
        if not auth:
            acc["status"] = "登录失败 ❌"
            acc["error"] = "登录失败"
            return acc
        say(f"✅ 登录成功 uin={mask_id(auth.get('uin'), 6)}")
        extras.append(f"uin {mask_id(auth.get('uin'), 6)}")
    except Exception as e:
        acc["status"] = f"登录异常 ❌ ({clean_line(e)[:40]})"
        acc["error"] = clean_line(e)[:80]
        return acc

    now = datetime.now()
    coin_gain = 0
    status_parts: List[str] = []
    try:
        if not is_due(now):
            acc["status"] = "⏰ 未到每日签到时段(7:30 后)"
            acc["success"] = True
            return acc
        lvz = check_lvz(auth, proxies)
        say(f"💎 {lvz}")
        status_parts.append(lvz)
        coin = check_coin(auth, proxies)
        say(f"🪙 {coin}")
        status_parts.append(coin)
        if ENABLE_ACTIVITY:
            lot = check_lottery_sign(auth, proxies)
            say(f"🎰 {lot}")
            extras.append(lot)
            lot2, c2 = draw_lottery(auth, proxies)
            say(f"🎰 {lot2}")
            extras.append(lot2)
            coin_gain += c2
        tasks = query_daily_tasks(auth, proxies)
        if tasks:
            tmsg = claim_tasks(auth, tasks, proxies)
            say(f"📋 {tmsg}")
            extras.append(tmsg)
        if ENABLE_ACTIVITY and now.hour in (0, 8, 12, 16, 20, 22):
            red, rc = run_red_packet(auth, proxies)
            coin_gain += rc
        else:
            red = "当前时段无红包雨"
        say(f"🧧 {red}")
        extras.append(red)
        bal = query_balance(auth, proxies)
        extras.append(f"金币余额 {bal}")
        acc["reward"] = f"金币 +{coin_gain}" if coin_gain else (f"余额 {bal}" if bal not in ("-", "") else "-")
        acc["status"] = "、".join(status_parts[:2]) + " ✅" if status_parts else "任务已执行 ✅"
        acc["success"] = True
        return acc
    except Exception as e:
        acc["status"] = f"执行异常 ❌ ({clean_line(e)[:40]})"
        acc["error"] = clean_line(e)[:80]
        if IS_DEBUG:
            say(traceback.format_exc())
        return acc


def main() -> int:
    started = time.time()
    openids = split_openids(os.getenv("qqyy", "").strip())
    if not openids:
        say("❌ 缺少 qqyy（openid，多账号换行或 & 分隔）")
        return 1
    say(f"{APP_NAME} | {len(openids)}账号")
    accounts: List[Dict[str, Any]] = []
    for i, oid in enumerate(openids, 1):
        accounts.append(run_account(oid, i, len(openids)))
        if i < len(openids):
            time.sleep(random.randint(8, 20))
    ok_n = sum(1 for a in accounts if a.get("success"))
    try:
        notify_and_format(APP_NAME, accounts, title=f"QQ音乐签到 {ok_n}/{len(accounts)}", start_ts=started)
    except Exception:
        print(format_report(APP_NAME, accounts, push_result="推送模块异常", cost_s=time.time() - started))
    return 0 if ok_n == len(accounts) else 1


if __name__ == "__main__":
    sys.exit(main())
