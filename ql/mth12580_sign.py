#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# /*
# ------------------------------------------
# @Description: 12580mth(大参林/ddwhcb) - openid 换业务登录态 + 每日签到 + 抽奖
# cron: 25 14 * * *
# ------------------------------------------
# 变量名：mth12580
# 变量值：wx_server 里的 openid/账号标识，多账号用 & 或换行分隔（可加 #备注）
#
# 依赖变量：
# wx_server_url    必填，wx_server 地址（勿写进仓库）
# wx_auth          必填，wx_server 鉴权值（/wx/code 用）
# mth12580_appid   可选，默认 wx1d6ad6c2412dea5a
# mth12580_draw    可选，0=关抽奖；默认 1
# 抽奖规则：仅当积分 >= 100 时抽奖 1 次；日志打印签到奖励
#
# 契约（appid wx1d6ad6c2412dea5a，host https://gateway.ddwhcb.com/）：
# （自反编译主包；channelId=mth，routeFix=12580mth/api/wx，client=4）
#
# 响应壳：{code:int, msg, data}  code==0 成功；body 可能 AES 再包一层
# 登录参数  POST smallcat /wx/code  json:{openid, appid} -> data.code
# 业务登录  POST .../wechatMiniLogin -> data.token / data.uid
# 签到状态  POST .../memberSignPage
# 签到      POST .../memberSign
# 抽奖页    POST .../lotteryPage
# 抽奖信息  POST .../getMemberChoujiang*（抓包名截断，脚本多 act 探测）
# 抽奖资格  POST .../qualifications
# 抽奖      POST .../luckDraw  （body 仅 mth_str，无额外业务字段）
#
# 网关公共字段 + 签名：
#   mth_noncestr / mth_timestamp / mth_act
#   mth_sign = md5(sorted("k=v&"...) + "mth_key=" + md5(signtSecret)).UPPER()
#   encrypt=true 时 body = {mth_str: AES_ECB_PKCS7(JSON, key=md5(encryptKey)的hex字符串utf8)}
#   encryptKey=AKUEMGNTOMSF9H5LP7JKFMSJTXFWDIDF
#   signtSecret=DLA0NTRXTDNPHEUREZEGIM6YJ8YGJSOC
#   （仅 gateway.ddwhcb.com / gateway-pre 配置）
# 登录态缓存 24h；**单次任务内每个 openid 至多调 1 次 /wx/code，失败不重试**。
# 多账号间隔 sleep，避免触发 smallcat 限流（约 8 code / 90s）。
# ------------------------------------------
# */

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import random
import sys
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("DclSign")

APPID_DEFAULT = "wx1d6ad6c2412dea5a"
HOST = "https://gateway.ddwhcb.com/"
ROUTE = "12580mth/api/wx"
CLIENT = 4
CHANNEL = "mth"
VERSION = "1.0.41"
ENCRYPT_KEY = "AKUEMGNTOMSF9H5LP7JKFMSJTXFWDIDF"
SIGN_SECRET = "DLA0NTRXTDNPHEUREZEGIM6YJ8YGJSOC"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 "
    "MicroMessenger/7.0.20.1781 MiniProgramEnv/Windows"
)
ALREADY = ("已签", "已经签", "签到过", "重复", "already")


def md5_hex(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def aes_key() -> bytes:
    # CryptoJS MD5(encryptKey).toString() 是 hex 字符串，再按 Utf8 当 AES key
    return md5_hex(ENCRYPT_KEY).encode("utf-8")


def aes_encrypt(plain: str) -> str:
    key = aes_key()
    cipher = AES.new(key, AES.MODE_ECB)
    raw = cipher.encrypt(pad(plain.encode("utf-8"), AES.block_size))
    return base64.b64encode(raw).decode("ascii")


def mth_sign(act: str, nonce: str, ts: int, extra: Optional[Dict[str, str]] = None) -> str:
    x = {
        "mth_noncestr": nonce,
        "mth_timestamp": ts,
        "mth_act": act,
        "mth_client": str(CLIENT),
        "mth_channel_id": CHANNEL,
        "mth_version": VERSION,
        "mth_browser_c": "",
        "mth_browser_uuid_type": "",
        "mth_browser_uuid": "",
    }
    if extra:
        x.update(extra)
    parts = []
    for k in sorted(x.keys()):
        if x[k]:
            parts.append(f"{k}={x[k]}")
    b = "&".join(parts) + "&"
    b += "mth_key=" + md5_hex(SIGN_SECRET)
    return md5_hex(b).upper()


class DclApi:
    def __init__(self, token: str = ""):
        self.token = token
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA})

    def call(self, act: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = dict(data or {})
        nonce = str(random.randint(100000, 9999999))
        ts = int(time.time())
        payload["mth_noncestr"] = nonce
        payload["mth_timestamp"] = ts
        payload["mth_sign"] = mth_sign(act, nonce, ts)
        payload["token"] = self.token
        payload["client"] = str(CLIENT)
        payload["client_type"] = "android"
        payload["channel_id"] = CHANNEL
        payload["version"] = VERSION
        payload["act"] = act
        payload["extra_parameters"] = ""
        payload["browser_c"] = ""
        payload["browser_uuid_type"] = ""
        payload["browser_uuid"] = ""

        body = {"mth_str": aes_encrypt(json.dumps(payload, ensure_ascii=False))}
        url = HOST + ROUTE + "/" + act
        r = self.s.post(
            url,
            data=body,
            headers={"content-type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        r.raise_for_status()
        try:
            resp = r.json()
        except Exception:
            raise RuntimeError(f"非 JSON 响应: {r.text[:120]}")
        if isinstance(resp, dict) and resp.get("encrypt") == 1:
            from Crypto.Cipher import AES as _AES

            enc = resp.get("data")
            if isinstance(enc, str) and enc:
                cipher = _AES.new(aes_key(), _AES.MODE_ECB)
                raw = base64.b64decode(enc)
                text = cipher.decrypt(raw)
                padlen = text[-1] if text else 0
                if 1 <= padlen <= 16:
                    text = text[:-padlen]
                try:
                    resp["data"] = json.loads(text.decode("utf-8"))
                except Exception:
                    resp["data"] = text.decode("utf-8", errors="replace")
        return resp


class Smallcat:
    def __init__(self, base: str, auth: str):
        self.base = base.rstrip("/")
        self.s = requests.Session()
        self.s.headers.update({"auth": auth, "User-Agent": UA})

    def wx_code(self, openid: str, appid: str) -> str:
        r = self.s.post(f"{self.base}/wx/code", json={"openid": openid, "appid": appid}, timeout=20)
        r.raise_for_status()
        data = r.json()
        if not data.get("status"):
            raise RuntimeError(f"/wx/code: {data.get('message')}")
        code = (data.get("data") or {}).get("code")
        if not code:
            raise RuntimeError("smallcat 未返回 code")
        return code


def split_openids(raw: str) -> List[str]:
    return [p.strip() for p in raw.replace("&", "\n").replace(",", "\n").splitlines() if p.strip()]


def cache_path():
    from pathlib import Path

    p = os.getenv("mth12580_cache", "").strip()
    if p:
        return Path(p)
    return Path(__file__).resolve().parent / "mth12580_token_cache.json"


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
        cache_path().write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def login_with_code(jcode: str) -> Tuple[str, str]:
    """返回 (token, uid)"""
    api = DclApi()
    resp = api.call("wechatMiniLogin", {"code": jcode})
    if resp.get("code") != 0:
        raise RuntimeError(f"登录失败 code={resp.get('code')} msg={resp.get('msg')}")
    data = resp.get("data") or {}
    token = data.get("token") or ""
    uid = str(data.get("uid") or "")[:16]
    if not token:
        raise RuntimeError(f"登录未返回 token: {json.dumps(data, ensure_ascii=False)[:160]}")
    return token, uid


def get_token(sm: Smallcat, openid: str, appid: str, cache: Dict[str, Any]) -> Tuple[str, str]:
    """缓存优先；未命中只调一次 /wx/code，不重试。"""
    key = f"{appid}:{openid}"
    hit = cache.get(key) or {}
    ts = hit.get("_ts") or 0
    if hit.get("token") and time.time() - ts < 24 * 3600:
        return str(hit["token"]), str(hit.get("uid") or "")
    jcode = sm.wx_code(openid, appid)
    token, uid = login_with_code(jcode)
    cache[key] = {"token": token, "uid": uid, "_ts": time.time()}
    save_cache(cache)
    return token, uid


def _walk_find(obj: Any, keys: Tuple[str, ...]) -> Any:
    if isinstance(obj, dict):
        for k in keys:
            if k in obj and obj[k] not in (None, ""):
                return obj[k]
        for v in obj.values():
            got = _walk_find(v, keys)
            if got not in (None, ""):
                return got
    elif isinstance(obj, list):
        for it in obj:
            got = _walk_find(it, keys)
            if got not in (None, ""):
                return got
    return None


def extract_sign_reward(res: Dict[str, Any]) -> str:
    """从签到响应里提取奖励文案（积分/优惠券/礼品等）。"""
    if not isinstance(res, dict):
        return ""
    data = res.get("data") if isinstance(res.get("data"), dict) else res
    # 显式字段
    for k in (
        "rewardName", "prizeName", "awardName", "giftName", "couponName",
        "signReward", "reward", "prize", "msg", "message",
    ):
        v = _walk_find(data, (k,))
        if isinstance(v, str) and v.strip() and k != "msg":
            return v.strip()
    parts = []
    gain = _walk_find(data, ("integral", "score", "point", "points", "gainIntegral", "addIntegral"))
    try:
        g = int(gain)
        if g:
            parts.append(f"积分+{g}")
    except Exception:
        pass
    coupon = _walk_find(data, ("couponName", "coupon", "couponList"))
    if isinstance(coupon, str) and coupon.strip():
        parts.append(coupon.strip())
    elif isinstance(coupon, dict):
        nm = coupon.get("name") or coupon.get("couponName")
        if nm:
            parts.append(str(nm))
    elif isinstance(coupon, list) and coupon:
        names = []
        for it in coupon[:3]:
            if isinstance(it, dict):
                names.append(str(it.get("name") or it.get("couponName") or "")[:20])
            elif isinstance(it, str):
                names.append(it[:20])
        if any(names):
            parts.append("、".join([n for n in names if n]))
    msg = str(res.get("msg") or res.get("message") or "").strip()
    if msg and not parts:
        # msg 可能本身含奖励描述
        if any(x in msg for x in ("积分", "优惠券", "券", "礼品", "红包", "+")):
            parts.append(msg)
    return " | ".join(parts)


def extract_integral(*payloads: Any) -> Optional[int]:
    """从多个响应中提取当前积分/成长值。"""
    keys = (
        "integral", "score", "point", "points", "totalIntegral",
        "usableIntegral", "balance", "memberIntegral",
    )
    for obj in payloads:
        got = _walk_find(obj, keys)
        try:
            if got is not None:
                return int(float(got))
        except Exception:
            continue
    return None


def parse_draw_remain(obj: Any) -> Optional[int]:
    """从抽奖接口响应里粗取剩余次数。"""
    if not isinstance(obj, dict):
        return None
    keys = (
        "remainNum", "remainCount", "leftCount", "leftTimes", "drawCount",
        "times", "chance", "chances", "surplus", "surplusCount",
        "lotteryCount", "drawNum", "canDrawNum", "remaining",
    )
    data = obj.get("data") if isinstance(obj.get("data"), dict) else obj
    if not isinstance(data, dict):
        return None
    for k in keys:
        v = data.get(k)
        if isinstance(v, bool):
            continue
        try:
            return int(v)
        except Exception:
            continue
    # 嵌套
    for v in data.values():
        if isinstance(v, dict):
            got = parse_draw_remain({"data": v})
            if got is not None:
                return got
    return None


def lottery_query(api: "DclApi") -> List[str]:
    """只读探测抽奖相关 act，打印剩余次数与摘要。"""
    lines: List[str] = []
    acts = [
        "lotteryPage",
        "qualifications",
        "getMemberChoujiangPage",
        "getMemberChoujiangInfo",
        "getMemberChoujiangConfig",
        "getMemberChoujiang",
        "memberChoujiang",
        "index",
    ]
    remain = None
    for act in acts:
        try:
            res = api.call(act)
        except Exception as e:
            continue
        code = res.get("code")
        msg = str(res.get("msg") or res.get("message") or "")[:40]
        if code not in (0, "0", None) and not msg:
            continue
        got = parse_draw_remain(res)
        if got is not None:
            remain = got if remain is None else min(remain, got)
        data = res.get("data")
        preview = ""
        if isinstance(data, dict):
            preview = json.dumps(data, ensure_ascii=False)[:120]
        elif data is not None:
            preview = str(data)[:80]
        if code in (0, "0") or got is not None or preview:
            lines.append(f"查询 {act}: code={code} remain={got} {msg} {preview}")
    if remain is None:
        remain = -1  # 未知
    lines.append(f"抽奖剩余: {remain if remain >= 0 else '未知'}")
    return lines, remain


def lottery_draw(api: "DclApi") -> List[str]:
    """积分达标后只抽 1 次。"""
    lines: List[str] = []
    try:
        res = api.call("luckDraw", {})
    except Exception as e:
        return [f"抽奖异常: {e}"]
    code = res.get("code")
    msg = str(res.get("msg") or res.get("message") or "")
    data = res.get("data") if isinstance(res.get("data"), dict) else {}
    prize = (
        data.get("prizeName")
        or data.get("prize_name")
        or data.get("awardName")
        or data.get("giftName")
        or data.get("name")
        or ""
    )
    if code in (0, "0"):
        lines.append(f"抽奖1次 成功 奖品={prize or data.get('prizeId') or '-'} {msg}".strip())
    else:
        low = msg.lower()
        if any(x in (msg + low) for x in ("已抽", "次数", "上限", "不足", "用完", "already", "limit")):
            lines.append(f"抽奖跳过: {msg or code}")
        else:
            lines.append(f"抽奖失败 code={code} {msg[:80]}")
    return lines


def run_one(sm: Smallcat, openid: str, appid: str, cache: Dict[str, Any]) -> Dict[str, Any]:
    name = openid[-8:]
    acc: Dict[str, Any] = {
        "account": name,
        "phone": "",
        "status": "-",
        "reward": "-",
        "extra": [],
        "error": "",
        "success": False,
    }
    extras: List[str] = []
    try:
        token, uid = get_token(sm, openid, appid, cache)
        api = DclApi(token)

        page = api.call("memberSignPage")
        signed = False
        page_data = {}
        if page.get("code") == 0:
            page_data = page.get("data") or {}
            pd = page_data if isinstance(page_data, dict) else {}
            for k in ("is_sign", "sign_status", "signed", "isSign"):
                if k in pd:
                    signed = bool(pd.get(k) in (1, True, "1", "已签"))
                    break
        if signed:
            acc["status"] = "今日已签到 ✅"
            acc["success"] = True
            extras.append(f"uid={uid}")
        else:
            res = api.call("memberSign")
            msg = str(res.get("msg") or res.get("message") or "")
            reward = extract_sign_reward(res)
            if res.get("code") == 0:
                acc["status"] = "签到成功 ✅"
                acc["success"] = True
            elif any(x in msg for x in ALREADY):
                acc["status"] = f"{msg} ✅"
                acc["success"] = True
            else:
                acc["status"] = f"code={res.get('code')} {msg} ❌"
                acc["error"] = msg[:80]
            if reward:
                acc["reward"] = reward
            extras.append(f"uid={uid}")
            if msg and msg not in acc["status"]:
                extras.append(msg[:60])
            page_data = res if isinstance(res, dict) else page_data

        page_reward = extract_sign_reward(page) if not signed else ""
        if page_reward and acc.get("reward") in ("-", "", None):
            acc["reward"] = page_reward

        q_preview = []
        try:
            q_lines, remain = lottery_query(api)
            q_preview = q_lines
        except Exception as e:
            remain = -1
            extras.append(f"抽奖查询异常: {e}")

        integral = extract_integral(page, page_data)
        for line in q_preview:
            if integral is None and "integral" in line.lower():
                integral = extract_integral({"data": line})
        if integral is None:
            try:
                integral = extract_integral(api.call("memberSignPage"))
            except Exception:
                pass
        if integral is not None:
            extras.append(f"积分 {integral}")
        extras.append(f"抽奖剩余 {remain if remain is not None and remain >= 0 else '未知'}")

        if os.getenv("mth12580_draw", "1") in ("0", "false", "no"):
            extras.append("抽奖已关闭")
        elif integral is None:
            extras.append("积分未知，跳过抽奖")
        elif integral < 100:
            extras.append(f"积分 {integral}<100，跳过抽奖")
        else:
            extras.append(f"积分 {integral}>=100，抽奖 1 次")
            try:
                d_lines = lottery_draw(api)
                extras.extend([str(x) for x in d_lines if x][:4])
            except Exception as e:
                extras.append(f"抽奖异常: {e}")

        acc["extra"] = extras
        return acc
    except Exception as e:
        acc["status"] = f"异常 ❌ ({str(e)[:60]})"
        acc["error"] = str(e)[:80]
        acc["extra"] = extras
        return acc


def main() -> int:
    started = time.time()
    auth = os.getenv("wx_auth", "").strip()
    base = os.getenv("wx_server_url", "").strip()
    openids_raw = os.getenv("mth12580", "").strip()
    appid = os.getenv("mth12580_appid", APPID_DEFAULT).strip()

    if not auth:
        log.error("缺少 wx_auth")
        return 1
    if not base:
        log.error("缺少 wx_server_url（wx_server 地址，勿写进仓库）")
        return 1
    if not openids_raw:
        log.error("缺少 mth12580（openid，多个用 & 分隔）")
        return 1

    sm = Smallcat(base, auth)
    cache = load_cache()
    accounts: List[Dict[str, Any]] = []
    for oid in split_openids(openids_raw):
        acc = run_one(sm, oid, appid, cache)
        accounts.append(acc)
        time.sleep(3)

    ok_n = sum(1 for a in accounts if a.get("success"))
    try:
        from send_notify import notify_and_format

        notify_and_format(
            "12580mth",
            accounts,
            title=f"12580mth签到 {ok_n}/{len(accounts)}",
            start_ts=started,
        )
    except Exception as _ne:
        print("[notify] 使用旧格式:", _ne)
        lines = [
            f"{'✅' if a.get('success') else '❌'} [{a.get('account')}] {a.get('status')} {a.get('reward')}"
            for a in accounts
        ]
        _report = "\n".join(lines)
        print(_report)
        try:
            from send_notify import send_notify

            send_notify("12580mth签到+抽奖简报", _report)
        except Exception as _ne2:
            print("[notify] 跳过:", _ne2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
