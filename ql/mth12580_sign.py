#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# /*
# ------------------------------------------
# @Description: 12580mth(大参林/ddwhcb) - smallcat openid 换业务登录态 + 每日签到
# cron: 25 14 * * *
# ------------------------------------------
# 变量名：mth12580
# 变量值：wx_server 里的 openid/账号标识，多账号用 & 或换行分隔（可加 #备注）
# 示例：owNAX6hm...etI6o&owNAX6j2...LDPc#小号
#
# 依赖变量：
# wx_server_url    默认 https://smallcat.myffa.ccwu.cc
# wx_auth          必填，wx_server 鉴权值（/wx/code 用）
# mth12580_appid   可选，默认 wx1d6ad6c2412dea5a
# ------------------------------------------
# 契约（appid wx1d6ad6c2412dea5a，host https://gateway.ddwhcb.com/）：
# （自反编译主包；channelId=mth，routeFix=12580mth/api/wx，client=4）
#
# 响应壳：{code:int, msg, data}  code==0 成功；body 可能 AES 再包一层
# 登录参数  POST smallcat /wx/code  json:{openid, appid} -> data.code
# 业务登录  POST .../wechatMiniLogin  form: code=&token=&act=wechatMiniLogin&...
#           -> data.token / data.uid / data.mobile
# 签到状态  POST .../memberSignPage  form: token=&act=memberSignPage&...
# 签到      POST .../memberSign      form: token=&act=memberSign&...
#
# 网关公共字段 + 签名：
#   mth_noncestr / mth_timestamp / mth_act
#   mth_sign = md5(sorted("k=v&"...) + "mth_key=" + md5(signtSecret)).UPPER()
#   encrypt=true 时 body = {mth_str: AES_ECB_PKCS7(JSON, key=md5(encryptKey)的hex字符串utf8)}
#   encryptKey=AKUEMGNTOMSF9H5LP7JKFMSJTXFWDIDF
#   signtSecret=DLA0NTRXTDNPHEUREZEGIM6YJ8YGJSOC
#   （仅 gateway.ddwhcb.com / gateway-pre 配置）
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
        payload["client_type"] = "windows"
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


def run_one(sm: Smallcat, openid: str, appid: str) -> str:
    name = openid[-8:]
    try:
        jcode = sm.wx_code(openid, appid)
        token, uid = login_with_code(jcode)
        api = DclApi(token)

        page = api.call("memberSignPage")
        signed = False
        hint = ""
        if page.get("code") == 0:
            pd = page.get("data") or {}
            # 常见字段：is_sign / sign_status / status
            for k in ("is_sign", "sign_status", "signed", "isSign"):
                if k in pd:
                    signed = bool(pd.get(k) in (1, True, "1", "已签"))
                    break
            hint = str(pd.get("msg") or pd.get("tip") or "")
        if signed:
            return f"✅ [{name}] 今日已签到 uid={uid}"

        res = api.call("memberSign")
        msg = str(res.get("msg") or res.get("message") or "")
        if res.get("code") == 0:
            return f"✅ [{name}] 签到成功 uid={uid} {msg}".strip()
        if any(x in msg for x in ALREADY):
            return f"✅ [{name}] {msg}"
        return f"❌ [{name}] code={res.get('code')} {msg}"
    except Exception as e:
        return f"❌ [{name}] 异常: {e}"


def main() -> int:
    auth = os.getenv("wx_auth", "").strip()
    base = os.getenv("wx_server_url", "https://smallcat.myffa.ccwu.cc").strip()
    openids_raw = os.getenv("mth12580", "").strip()
    appid = os.getenv("mth12580_appid", APPID_DEFAULT).strip()

    if not auth:
        log.error("缺少 wx_auth")
        return 1
    if not openids_raw:
        log.error("缺少 mth12580（openid，多个用 & 分隔）")
        return 1

    sm = Smallcat(base, auth)
    lines = []
    for oid in split_openids(openids_raw):
        line = run_one(sm, oid, appid)
        log.info(line)
        lines.append(line)
        time.sleep(1.2)

    print("\n" + "=" * 36)
    print("      12580mth 签到简报")
    print("=" * 36)
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
