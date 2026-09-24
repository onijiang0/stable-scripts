#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# /*
# ------------------------------------------
# @Author: onijiang0
# @Date: 2026.09.24
# @Description: 望潮App（台州）· 阅读有礼 readingLuck-v5 —— 每日阅读任务 + 在线时长 + 抽奖
# cron: 42 9 * * *
# #定时使用 9-10 点随机时间 每天（脚本内置随机启动延迟，见 WC_START_DELAY）
# ------------------------------------------
# 变量名：wc
# 变量值：手机号#密码，多账号用 & 或换行分隔
#   例：<手机号>#<密码>
#   ★ 是 # 不是 & —— & 是多账号分隔符，写错会被拆成两个无效账号
#   ★ 因密码分隔符占用了 #，本脚本**不支持 # 备注**
#   兼容别名：WC（脚本旧版变量名）、WC_ACCOUNT
#
# 依赖变量（全部选填）：
# ONLINE_MIN / WC_ONLINE_MIN  在线挂机分钟数，默认 15；设 0 跳过挂机
# WC_SKIP_DRAW=1              跳过抽奖（调试用）
# WC_START_DELAY              启动前随机延迟上限秒数，默认 300（错峰，设 0 关闭）
# WC_CACHE_DIR                设备指纹缓存目录，默认=脚本同目录
# QL_NOTIFY=0                 关闭推送
# ------------------------------------------
# 契约（实测 2026-09-24，完整跑通 100%）：
#
# [登录] POST https://vapp.taizhou.com.cn/api/account/init      (form)
#        头 X-SESSION-ID:"" X-REQUEST-ID X-TIMESTAMP X-SIGNATURE X-TENANT-ID
#        → data.session.id（匿名会话）
# [登录] POST https://passport.tmuyun.com/web/oauth/credential_auth   (form)
#        body client_id=10019&password=<RSA密文URL编码>&phone_number=<手机号>
#        头 User-Agent: UA_PASSPORT
#        → data.authorization_code.code
# [登录] POST https://vapp.taizhou.com.cn/api/zbtxz/login       (form)
#        body check_token=&code=<code>&token=&type=-1&union_id=
#        头 X-SESSION-ID:<匿名会话>
#        → data.account.id + data.session.id
#
# 签名 X-SIGNATURE = sha256("path&&session&&reqId&&ts&&SIGN_KEY&&TENANT")
#   ★ session 为空时（如 /api/account/init）会形成 "path&&&&reqId&&..."（4 个连续 &）
#     —— 这是契约本身，不是 bug，别"修正"它
#
# [阅读侧] GET https://{xmt|maidian}.taizhou.com.cn/prod-api/user-read/app/login
#          ?id=<accountId>&sessionId=<sessionId>&deviceId=1
#          → 200 + Set-Cookie JSESSIONID（**两个域只需一个通**，实跑 maidian 常返 302）
# [任务]  GET /prod-api/user-read/list/{YYYYMMDD}   → data.{sum,completedCount,articleIsReadList[]}
# [上报]  GET /prod-api/already-read/article/new?signature=<SM2密文>
#         SM2 明文 = compact json {"timestamp":ms,"articleId":<id>,"accountId":<accountId>}
#         密文格式 C1C3C2 **不带 04 前缀**（128+64+len(plain)*2 hex 字符）
# [时长]  GET https://vapp.taizhou.com.cn/api/article/read_time
#         ?channel_article_id=<newsId>&is_end=<true|false>&read_time=<ms>
# [统计]  GET /prod-api/user-read-count/count/{YYYYMMDD}
# [抽奖]  GET https://srv-app.taizhou.com.cn/tzrb/user/loginWC  ?accountId&sessionId
#          → JSESSIONID；再 POST /tzrb/userAwardRecordUpgrade/saveUpdate (form)
#            body activityId=67&sessionId=undefined&sig=undefined&token=undefined
#            ★ 三个 "undefined" 是**原样照抄抓包**，实测服务端**不校验**、会话从 Cookie 取，
#              改成真实值反而可能失败 —— 别"优化"它
#          → 记录 GET /tzrb/userAwardRecordUpgrade/pageList?pageSize=10&pageNum=1&activityId=67
# [挂机]  POST https://maidian.taizhou.com.cn/api/online/start  json {activityId,accountId}
#          → sessionId；循环 POST /api/online/heartbeat json {sessionId}，30s 一次
#          → 结束后 POST /api/online/end json {sessionId}
#         活动 id ACTIVITY_ID = nfxn004v1692086390803
#
# 已实现：多账号 · 逐篇阅读上报(SM2) · 阅读时长心跳 · 抽奖 + 中奖记录 · 15 分钟在线挂机 · 推送简报
#
# 踩坑：
# 1. ★ 账号分隔符是 # 不是 &（见上）。原脚本注释写"手机号#密码, 多账号 & 分隔"，
#    实际喂 & 进去会被拆成两个都没有 # 的无效账号 → 直接报"账号格式错误"。
# 2. ★★「网络不可达」真因是**容器 IPv6 残废**（与长虹美菱同源）。面板容器是 musl（Alpine）
#    且无 IPv6 出口，musl 的 getaddrinfo 未限定 family 时**同时查 A + AAAA**，AAAA 必然
#    卡满超时 → [Errno -3] Try again，被 requests 包装成"网络不可达"。
#    原脚本用 FALLBACK_IP 硬编码 5 个 IPv4 兜底 —— **治标不治本**：每个域名首次请求白等
#    约 5s，且 IP 会漂移、换域名即废。本版改成 monkeypatch getaddrinfo 强制 AF_INET 根治，
#    **不硬编码任何 IP**。
# 3. ★ 原脚本 set_device_by_account() 生成的 UUID 第 3 段只有 2 字符
#    （b[6:7].hex()[1:] 把 2 个 hex 砍成 1 个）→ 段长 (8,4,2,4,12) 违反 RFC 4122。
#    实测服务端不校验，但已修成标准 v4（首段 4 位且首位=4，variant 段首字符 ∈ 8/9/a/b）。
# 4. ★★ **反风控核心：设备指纹必须持久化**。原脚本每次运行都 random.choice 换设备 + 换
#    UA 版本 —— 同一账号在服务端表现为"天天换手机"，是最典型的风控信号。
#    本版把设备指纹按账号持久化到 .wc_device.json（key = sha256(手机号)[:16]），
#    首轮生成后**永久复用**，与该账号的历史会话一致。
# 5. ★ 原脚本 http_req 里 `global SSL_VERIFY`，任一请求 SSL 失败就**全进程永久**降级为
#    verify=False 且不恢复 —— 存着账号密码的脚本这么干等于静默失去证书校验。
#    本版固定 verify=False（与仓库其它脚本一致）+ 关闭 urllib3 告警，行为显式可预期。
# 6. ★ 原脚本 keep_online 心跳数 = max(30, minutes*60/30)，**下限写死 30** ——
#    连 ONLINE_MIN=1 都会跑满 30 次＝15 分钟，无法缩短。本版改为 max(1, ...)。
# 7. 隐私：账号密码只进 env；日志全程 mask 手机号，不打印密码 / sessionId / 设备指纹全值。
# ------------------------------------------
# */

from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import socket
import sys
import time
import urllib.parse
from datetime import datetime, timedelta
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
        import re
        d = re.sub(r"\D", "", str(phone or ""))
        return (d[:3] + "****" + d[-4:]) if len(d) >= 11 else (d or "-")

    def format_report(task, accounts, push_result="", cost_s=None):
        return task

    def notify_and_format(task, accounts, **kwargs):
        print("🔔 推送结果：跳过（send_notify 不可用）")


APP_NAME = "望潮阅读有礼"

# ── 强制 IPv4 解析（根治容器 IPv6 残废，见头注释踩坑 2）──────────
_orig_getaddrinfo = socket.getaddrinfo


def _getaddrinfo_ipv4(host, port, family=0, type=0, proto=0, flags=0):
    return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)


socket.getaddrinfo = _getaddrinfo_ipv4
# ────────────────────────────────────────────────────────────────

# ========== 平台常量（第三方 App 常量，非个人隐私）==========
TENANT = "64"
CLIENT_ID = "10019"
SIGN_KEY = "FR*r!isE5W"
ACTIVITY_ID = "nfxn004v1692086390803"
XRW = "com.shangc.tiennews.taizhou"
UA_PASSPORT = "ANDROID;13;10019;6.0.2;1.0;null;MEIZU 20"
REF_LUCK = "https://xmt.taizhou.com.cn/readingLuck-v5/?gaze_control=01"
UA_NATIVE_TPL = "7.2.0;%s;%s;Android;%s;other;7.2.0"

SM2_PUB = ("04A50803A27F000D6B310607EBA2A1C899E82872C0B538CA41DB6F0183B4C7E164"
           "DAFC6946ABF93C8AF1C0AD96D0E770D29264EF9F907DDBAE97A2A0BB1036D4AC")
RSA_PUB_PEM = """-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQD6XO7e9YeAOs+cFqwa7ETJ+WXiz
PqQeXv68i5vqw9pFREsrqiBTRcg7wB0RIp3rJkDpaeVJLsZqYm5TW7FWx/iOiXFc+z
CPvaKZric2dXCw27EvlH5rq+zwIPDAJHGAfnn1nmQH7wR3PCatEIb8pz5GFlTHMllu
w4ZYmnOwg+thwIDAQAB
-----END PUBLIC KEY-----"""

READ_HOSTS = ["xmt.taizhou.com.cn", "maidian.taizhou.com.cn"]
VAPP = "https://vapp.taizhou.com.cn"
PASSPORT = "https://passport.tmuyun.com"
SRV_APP = "https://srv-app.taizhou.com.cn"
MAIDIAN = "https://maidian.taizhou.com.cn"

REQUEST_TIMEOUT = 25

DEVICE_POOL = [
    {"model": "Xiaomi 2203121C", "dev": "2203121C", "os": "14", "build": "UKQ1.231003.002"},
    {"model": "HUAWEI LIO-AL00", "dev": "LIO-AL00", "os": "12", "build": "HUAWEILIO-AL00"},
    {"model": "vivo V2183A", "dev": "V2183A", "os": "13", "build": "SP1A.210812.003"},
    {"model": "OPPO PGJM10", "dev": "PGJM10", "os": "13", "build": "SP1A.210812.016"},
    {"model": "OnePlus IN2020", "dev": "IN2020", "os": "13", "build": "RP1A.200720.011"},
    {"model": "samsung SM-S9110", "dev": "SM-S9110", "os": "14", "build": "UP1A.231005.007"},
    {"model": "HONOR ELZ-AN20", "dev": "ELZ-AN20", "os": "12", "build": "HONORELZ-AN20"},
    {"model": "Xiaomi M2102J2SC", "dev": "M2102J2SC", "os": "12", "build": "RKQ1.200826.002"},
    {"model": "Redmi K60", "dev": "23013RK75C", "os": "14", "build": "UKQ1.230804.001"},
    {"model": "vivo iQOO 11", "dev": "V2243A", "os": "14", "build": "SP1A.210812.003"},
    {"model": "OPPO PGFM10", "dev": "PGFM10", "os": "14", "build": "UKQ1.230924.001"},
    {"model": "realme RMX3708", "dev": "RMX3708", "os": "13", "build": "SP1A.210812.016"},
    {"model": "Xiaomi 22127RK46C", "dev": "22127RK46C", "os": "14", "build": "UKQ1.231003.002"},
    {"model": "HUAWEI ALN-AL80", "dev": "ALN-AL80", "os": "12", "build": "HUAWEIALN-AL80"},
    {"model": "HONOR FNE-AN00", "dev": "FNE-AN00", "os": "13", "build": "HONORFNE-AN00"},
]
ANDROID_VERSIONS = ["12", "13", "14"]
CHROME_VERSIONS = ["998.0.0.0", "1010.0.0.0", "1024.0.0.0", "1035.0.0.0"]
WEBKIT_VERSIONS = ["537.36", "537.36", "605.1.15"]


# ========== 工具 ==========
def say(msg: Any) -> None:
    line = clean_line(msg)
    if line:
        print(line)


def get_env(*names: str) -> str:
    for n in names:
        v = (os.getenv(n) or "").strip()
        if v:
            return v
    return ""


def gen_uuid_v4() -> str:
    """标准 RFC 4122 v4 UUID（8-4-4-4-12，第 3 段首位=4，第 4 段首位∈8/9/a/b）。"""
    b = bytearray(os.urandom(16))
    b[6] = (b[6] & 0x0F) | 0x40
    b[8] = (b[8] & 0x3F) | 0x80
    h = b.hex()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def ymd(d: datetime) -> str:
    return d.strftime("%Y%m%d")


def jitter(lo: float, hi: float) -> None:
    """人类化随机停顿。"""
    if hi > lo:
        time.sleep(random.uniform(lo, hi))


NOW = datetime.now()
TODAY = ymd(NOW)
YESTERDAY = ymd(NOW - timedelta(days=1))


# ========== 设备指纹（按账号持久化 —— 反风控核心）==========
def _cache_path() -> str:
    d = get_env("WC_CACHE_DIR") or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(d, ".wc_device.json")


def _load_cache() -> Dict[str, Any]:
    try:
        with open(_cache_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_cache(cache: Dict[str, Any]) -> None:
    p = _cache_path()
    try:
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    except Exception as e:
        say(f"⚠️ 设备指纹持久化失败（不影响本次运行）: {type(e).__name__}")


def _new_device() -> Dict[str, Any]:
    d = random.choice(DEVICE_POOL)
    return {
        "model": d["model"], "dev": d["dev"], "os": d["os"], "build": d["build"],
        "uuid": gen_uuid_v4(),
        "android": random.choice(ANDROID_VERSIONS),
        "chrome": random.choice(CHROME_VERSIONS),
        "webkit": random.choice(WEBKIT_VERSIONS),
        "created": int(time.time()),
    }


def device_for(account_id: str) -> Dict[str, Any]:
    """同一账号永远复用同一套设备指纹（首次生成后写入缓存）。"""
    key = sha256_hex(str(account_id))[:16]
    cache = _load_cache()
    dev = cache.get(key)
    if not isinstance(dev, dict) or not dev.get("uuid") or not dev.get("model"):
        dev = _new_device()
        cache[key] = dev
        _save_cache(cache)
        say(f"📱 首次为该账号生成设备指纹（{dev['model']} / Android {dev['android']}），已持久化复用")
    return dev


def web_ua(dev: Dict[str, Any]) -> str:
    return ("Mozilla/5.0 (Linux; Android %s; %s Build/%s; wv) "
            "AppleWebKit/%s (KHTML, like Gecko) Version/4.0 Chrome/%s "
            "Mobile Safari/537.36;xsb_wangchao;xsb_wangchao;7.2.0;native_app;7.2.0"
            % (dev["android"], dev["model"], dev["build"], dev["webkit"], dev["chrome"]))


def native_ua(dev: Dict[str, Any]) -> str:
    return UA_NATIVE_TPL % (dev["uuid"], dev["model"], dev["android"])


def api_signature(path: str, session: str, req_id: str, ts: str) -> str:
    return sha256_hex("%s&&%s&&%s&&%s&&%s&&%s" % (path, session, req_id, ts, SIGN_KEY, TENANT))


# ========== RSA PKCS#1 v1.5（X.509 DER 解析）==========
def _parse_rsa_pubkey(pem: str) -> Tuple[int, int]:
    b64 = "".join(l for l in pem.strip().splitlines() if "-----" not in l).strip()
    der = base64.b64decode(b64)

    def read_len(data: bytes, i: int) -> Tuple[int, int]:
        b = data[i]
        if b < 0x80:
            return b, i + 1
        n = b & 0x7F
        return int.from_bytes(data[i + 1:i + 1 + n], "big"), i + 1 + n

    i = 0
    if der[i] != 0x30:
        raise ValueError("RSA DER 外层异常 (0x%02x)" % der[i])
    _, i = read_len(der, i + 1)
    if der[i] != 0x30:
        raise ValueError("AlgorithmIdentifier 异常 (0x%02x)" % der[i])
    alg_len, i = read_len(der, i + 1)
    i += alg_len
    if der[i] != 0x03:
        raise ValueError("BIT STRING 异常 (0x%02x)" % der[i])
    _, i = read_len(der, i + 1)
    i += 1
    if der[i] != 0x30:
        raise ValueError("RSA 公钥 SEQUENCE 异常 (0x%02x)" % der[i])
    _, i = read_len(der, i + 1)
    if der[i] != 0x02:
        raise ValueError("RSA 模数标记异常 (0x%02x)" % der[i])
    ln, i = read_len(der, i + 1)
    n_bytes = der[i:i + ln]
    i += ln
    if der[i] != 0x02:
        raise ValueError("RSA 指数标记异常 (0x%02x)" % der[i])
    le, i = read_len(der, i + 1)
    e_bytes = der[i:i + le]
    return int.from_bytes(n_bytes, "big"), int.from_bytes(e_bytes, "big")


def rsa_encrypt(plain: str) -> str:
    n, e = _parse_rsa_pubkey(RSA_PUB_PEM)
    msg = plain.encode("utf-8")
    k = (n.bit_length() + 7) // 8
    ps_len = k - len(msg) - 3
    if ps_len < 8:
        raise ValueError("RSA 消息过长")
    ps = bytearray()
    while len(ps) < ps_len:
        b = os.urandom(1)
        if b != b"\x00":
            ps += b
    em = b"\x00\x02" + bytes(ps) + b"\x00" + msg
    c = pow(int.from_bytes(em, "big"), e, n)
    return urllib.parse.quote(base64.b64encode(c.to_bytes(k, "big")).decode(), safe="")


# ========== SM3（GB/T 32905-2016）==========
def _rotl(x: int, n: int) -> int:
    n &= 31
    return ((x << n) | (x >> (32 - n))) & 0xFFFFFFFF


SM3_IV = [0x7380166F, 0x4914B2B9, 0x172442D7, 0xDA8A0600,
          0xA96F30BC, 0x163138AA, 0xE38DEE4D, 0xB0FB0E4E]


def _ff(x: int, y: int, z: int, j: int) -> int:
    return (x ^ y ^ z) if j < 16 else ((x & y) | (x & z) | (y & z))


def _gg(x: int, y: int, z: int, j: int) -> int:
    return (x ^ y ^ z) if j < 16 else ((x & y) | ((~x & 0xFFFFFFFF) & z))


def _p0(x: int) -> int:
    return (x ^ _rotl(x, 9) ^ _rotl(x, 17)) & 0xFFFFFFFF


def _p1(x: int) -> int:
    return (x ^ _rotl(x, 15) ^ _rotl(x, 23)) & 0xFFFFFFFF


def sm3(data: bytes) -> str:
    msg = bytearray(data)
    bit_len = len(data) * 8
    msg.append(0x80)
    while len(msg) % 64 != 56:
        msg.append(0)
    msg += bit_len.to_bytes(8, "big")

    v = list(SM3_IV)
    for off in range(0, len(msg), 64):
        block = msg[off:off + 64]
        w = [0] * 68
        for j in range(16):
            w[j] = int.from_bytes(block[j * 4:j * 4 + 4], "big")
        for j in range(16, 68):
            w[j] = (_p1(w[j - 16] ^ w[j - 9] ^ _rotl(w[j - 3], 15))
                    ^ _rotl(w[j - 13], 7) ^ w[j - 6]) & 0xFFFFFFFF

        a, b, c, d, e, f, g, h = v
        for j in range(64):
            tj = _rotl(a, 12)
            ss1 = _rotl((tj + e + _rotl(0x79CC4519 if j < 16 else 0x7A879D8A, j)) & 0xFFFFFFFF, 7)
            ss2 = ss1 ^ tj
            tt1 = (_ff(a, b, c, j) + d + ss2 + (w[j] ^ w[j + 4])) & 0xFFFFFFFF
            tt2 = (_gg(e, f, g, j) + h + ss1 + w[j]) & 0xFFFFFFFF
            d, c, b, a = c, _rotl(b, 9), a, tt1
            h, g, f, e = g, _rotl(f, 19), e, _p0(tt2)

        v[0] ^= a; v[1] ^= b; v[2] ^= c; v[3] ^= d
        v[4] ^= e; v[5] ^= f; v[6] ^= g; v[7] ^= h

    return "".join("%08x" % x for x in v)


# ========== SM2 加密（GB/T 32918.5-2016，C1C3C2 无 04 前缀）==========
P = 0xFFFFFFFEFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF00000000FFFFFFFFFFFFFFFF
A = 0xFFFFFFFEFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF00000000FFFFFFFFFFFFFFFC
N = 0xFFFFFFFEFFFFFFFFFFFFFFFFFFFFFFFF7203DF6B21C6052B53BBF40939D54123
GX = 0x32C4AE2C1F1981195F9904466A39C9948FE30BBFF2660BE1715A4589334C74C7
GY = 0xBC3736A2F4F6779C59BDCEE36B692153D0A9877CC62A474002DF32E52139F0A0


def _modinv(a: int, m: int) -> int:
    a %= m
    if a == 0:
        raise ValueError("modinv(0) 无解")
    old_r, r = a, m
    old_s, s = 1, 0
    while r:
        q = old_r // r
        old_r, r = r, old_r - q * r
        old_s, s = s, old_s - q * s
    if old_r != 1:
        raise ValueError("模逆不存在")
    return old_s % m


def _pt_add(p1, p2):
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2 and (y1 + y2) % P == 0:
        return None
    if x1 == x2:
        lam = ((3 * x1 * x1 + A) * _modinv(2 * y1, P)) % P
    else:
        lam = ((y2 - y1) * _modinv(x2 - x1, P)) % P
    x3 = (lam * lam - x1 - x2) % P
    return (x3, (lam * (x1 - x3) - y1) % P)


def _pt_mul(k: int, pt):
    res = None
    ad = pt
    kk = k % N
    while kk > 0:
        if kk & 1:
            res = _pt_add(res, ad)
        ad = _pt_add(ad, ad)
        kk >>= 1
    return res


def _kdf(z_hex: str, klen_bytes: int) -> str:
    ct = (klen_bytes + 31) // 32
    out = ""
    for i in range(1, ct + 1):
        out += sm3(bytes.fromhex(z_hex + format(i, "08x")))
    return out[:klen_bytes * 2]


def sm2_encrypt(msg: str, pub_hex: str) -> str:
    h = pub_hex.lower().lstrip("0x")
    pub = (int(h[-128:-64], 16), int(h[-64:], 16))
    msg_hex = msg.encode("utf-8").hex()
    while True:
        k = int.from_bytes(os.urandom(32), "big") % (N - 1) + 1
        if 0 < k < N:
            break
    c1 = _pt_mul(k, (GX, GY))
    s = _pt_mul(k, pub)
    x2 = format(s[0], "064x")
    y2 = format(s[1], "064x")
    c3 = sm3(bytes.fromhex(x2) + bytes.fromhex(msg_hex) + bytes.fromhex(y2))
    t = _kdf(x2 + y2, len(msg_hex) // 2)
    c2 = ""
    for i in range(0, len(msg_hex), 2):
        c2 += "%02x" % (int(msg_hex[i:i + 2], 16) ^ int(t[i:i + 2], 16))
    return format(c1[0], "064x") + format(c1[1], "064x") + c3 + c2


# ========== HTTP（重试 + 限流退避 + 抖动）==========
def http_req(method: str, url: str, headers: Optional[Dict[str, str]] = None,
             params: Optional[Dict[str, Any]] = None, body: Any = None,
             form: bool = False, timeout: int = REQUEST_TIMEOUT,
             retry: int = 3, tag: str = ""):
    hdrs = dict(headers or {})
    kwargs: Dict[str, Any] = {
        "headers": hdrs, "timeout": timeout,
        "allow_redirects": False,   # 靠 302 判"会话失效"，不跟随
        "verify": False,            # 显式关闭（面板证书链常不完整）；不再全局突变
    }
    if params:
        kwargs["params"] = params
    if body is not None:
        if form:
            if isinstance(body, dict):
                kwargs["data"] = body
            else:
                kwargs["data"] = body.encode("utf-8") if isinstance(body, str) else body
            hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
        else:
            if isinstance(body, str):
                kwargs["data"] = body.encode("utf-8")
            else:
                kwargs["data"] = json.dumps(body, ensure_ascii=False,
                                            separators=(",", ":")).encode("utf-8")
                hdrs.setdefault("Content-Type", "application/json")

    last_err: Any = None
    for attempt in range(retry + 1):
        try:
            resp = requests.request(method, url, **kwargs)
        except requests.RequestException as e:
            last_err = e
            if attempt < retry:
                wait = 3 + attempt * 5 + random.uniform(0, 2)
                say(f"⚠️ {tag} 网络异常({type(e).__name__})，{wait:.0f}s 后重试 {attempt + 1}/{retry}")
                time.sleep(wait)
                continue
            raise RuntimeError(f"请求失败 {url}: {type(e).__name__}")

        try:
            data = resp.json()
        except Exception:
            data = None

        if data and data.get("code") == 10400:
            if attempt < retry:
                wait = 8 + attempt * 12 + random.uniform(0, 4)
                say(f"🚦 {tag} 被限流(10400)，{wait:.0f}s 后重试 {attempt + 1}/{retry}")
                time.sleep(wait)
                continue
            raise RuntimeError("接口限流(10400)：请等待 5-15 分钟后再运行")

        return resp, data

    raise RuntimeError(f"请求失败 {url}: {last_err}")


def grab_cookie(resp, name: str) -> str:
    try:
        if name in resp.cookies:
            return "%s=%s" % (name, resp.cookies[name])
    except Exception:
        pass
    try:
        for c in resp.cookies:
            if c.name == name:
                return "%s=%s" % (c.name, c.value)
    except Exception:
        pass
    return ""


# ========== 登录 ==========
def login(phone: str, password: str, dev: Dict[str, Any]) -> Dict[str, str]:
    jitter(1.5, 3.5)
    rid = gen_uuid_v4()
    ts = str(int(time.time() * 1000))
    _, data = http_req(
        "POST", VAPP + "/api/account/init",
        headers={
            "User-Agent": native_ua(dev),
            "Content-Type": "application/x-www-form-urlencoded",
            "X-SESSION-ID": "",
            "X-REQUEST-ID": rid,
            "X-TIMESTAMP": ts,
            "X-SIGNATURE": api_signature("/api/account/init", "", rid, ts),
            "X-TENANT-ID": TENANT,
        }, tag="init")
    anon_sid = data and data.get("data", {}).get("session", {}).get("id")
    if not anon_sid:
        raise RuntimeError("初始化会话失败")

    enc = rsa_encrypt(password)
    _, data = http_req(
        "POST", PASSPORT + "/web/oauth/credential_auth",
        headers={
            "User-Agent": UA_PASSPORT,
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            "Connection": "Keep-Alive",
        },
        form=True,
        body="client_id=%s&password=%s&phone_number=%s" % (CLIENT_ID, enc, phone),
        tag="passport")
    code = data and data.get("data", {}).get("authorization_code", {}).get("code")
    if not code:
        raise RuntimeError("通行证登录失败（账号/密码错误？）")

    rid = gen_uuid_v4()
    ts = str(int(time.time() * 1000))
    _, data = http_req(
        "POST", VAPP + "/api/zbtxz/login",
        headers={
            "User-Agent": native_ua(dev),
            "Content-Type": "application/x-www-form-urlencoded",
            "X-SESSION-ID": anon_sid,
            "X-REQUEST-ID": rid,
            "X-TIMESTAMP": ts,
            "X-SIGNATURE": api_signature("/api/zbtxz/login", anon_sid, rid, ts),
            "X-TENANT-ID": TENANT,
        },
        form=True,
        body="check_token=&code=%s&token=&type=-1&union_id=" % urllib.parse.quote(code),
        tag="zbtxz_login")
    acc = data and data.get("data", {}).get("account")
    ses = data and data.get("data", {}).get("session")
    if not acc or not ses:
        raise RuntimeError("业务登录失败")
    return {"accountId": str(acc["id"]), "sessionId": str(ses["id"]),
            "nick": str(acc.get("nick_name") or "")}


# ========== 阅读任务 ==========
class ReadSide:
    """阅读侧会话（xmt / maidian 两域，只要一个通即可）。"""

    def __init__(self, dev: Dict[str, Any]) -> None:
        self.dev = dev
        self.jsid: Dict[str, str] = {}

    def login(self, account_id: str, session_id: str) -> bool:
        q = {"id": account_id, "sessionId": session_id, "deviceId": "1"}
        for host in READ_HOSTS:
            try:
                resp, data = http_req("GET", "https://%s/prod-api/user-read/app/login" % host,
                                      params=q, headers=self.headers(), tag="read_login")
                ok = resp.status_code == 200 and data and data.get("code") == 200
                ck = grab_cookie(resp, "JSESSIONID")
                if ok and ck:
                    self.jsid[host.split(".")[0]] = ck
            except Exception as e:
                say(f"   · {host} 登录异常: {type(e).__name__}")
        return bool(self.jsid)

    def headers(self, cookie: str = "") -> Dict[str, str]:
        h = {
            "User-Agent": web_ua(self.dev),
            "Accept": "*/*",
            "X-Requested-With": XRW,
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
            "Referer": REF_LUCK,
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        if cookie:
            h["Cookie"] = cookie
        return h

    def call(self, api_path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        last = ""
        for key in ("maidian", "xmt"):
            if not self.jsid.get(key):
                continue
            host = "%s.taizhou.com.cn" % key
            try:
                resp, data = http_req("GET", "https://%s/prod-api%s" % (host, api_path),
                                      params=params, headers=self.headers(self.jsid[key]),
                                      tag="read_api")
                if resp.status_code == 200 and data and data.get("code") == 200:
                    return data
                if resp.status_code == 302 or (data and data.get("code") == 401):
                    last = "%s 会话失效" % host
                    continue
                if resp.status_code == 200 and data:
                    return data
                last = "%s HTTP%s" % (host, resp.status_code)
            except Exception as e:
                last = "%s: %s" % (key, type(e).__name__)
        raise RuntimeError("prod-api%s 请求失败: %s" % (api_path, last))


def read_task_list(rs: ReadSide) -> Dict[str, Any]:
    j = rs.call("/user-read/list/%s" % TODAY)
    return j.get("data") or {"sum": 0, "completedCount": 0, "articleIsReadList": []}


def report_read(rs: ReadSide, article_id: str, account_id: str) -> Dict[str, Any]:
    plain = json.dumps({"timestamp": int(time.time() * 1000),
                        "articleId": article_id,
                        "accountId": account_id},
                       ensure_ascii=False, separators=(",", ":"))
    return rs.call("/already-read/article/new", {"signature": sm2_encrypt(plain, SM2_PUB)})


def report_read_time(news_id: str, total_ms: int, is_end: bool,
                     session_id: str, account_id: str, dev: Dict[str, Any]) -> bool:
    rid = gen_uuid_v4()
    ts = str(int(time.time() * 1000))
    path = "/api/article/read_time"
    resp, _ = http_req(
        "GET", VAPP + path,
        params={"channel_article_id": news_id,
                "is_end": "true" if is_end else "false",
                "read_time": str(total_ms if is_end else 5000)},
        headers={
            "User-Agent": native_ua(dev),
            "Content-Type": "application/x-www-form-urlencoded",
            "X-SESSION-ID": session_id,
            "X-REQUEST-ID": rid,
            "X-TIMESTAMP": ts,
            "X-SIGNATURE": api_signature(path, session_id, rid, ts),
            "X-TENANT-ID": TENANT,
            "X-ACCOUNT-ID": account_id,
        }, tag="read_time")
    return resp.status_code == 200


def run_read_task(rs: ReadSide, acc: Dict[str, str], dev: Dict[str, Any]) -> Dict[str, Any]:
    """返回 {done, total, pending}。"""
    task = read_task_list(rs)
    lst = task.get("articleIsReadList") or []
    total, completed = task.get("sum", 0), task.get("completedCount", 0)
    say(f"📄 今日任务 {total} 篇，已完成 {completed}，待读 {max(0, total - completed)} 篇")

    done = 0
    todo = [a for a in lst if not a.get("isRead")]
    for idx, art in enumerate(todo, 1):
        title = (art.get("title") or art.get("newsId") or "")[:40]
        try:
            rr = report_read(rs, art.get("id"), acc["accountId"])
            if rr.get("code") != 200:
                say(f"   [{idx}/{len(todo)}] ⚠️ 上报被拒: {rr.get('msg') or rr.get('code')}")
                continue
        except Exception as e:
            if "会话失效" in str(e):
                raise
            say(f"   [{idx}/{len(todo)}] ❌ 上报异常: {type(e).__name__}")
            continue

        done += 1
        say(f"   [{idx}/{len(todo)}] ✅ 已读 {title}")

        # 阅读时长心跳：22-30s，5s 一拍（带抖动）
        stay = random.randint(22000, 30000)
        spent = 0
        while spent < stay:
            step = random.uniform(4.5, 5.5)
            time.sleep(step)
            spent += int(step * 1000)
            try:
                report_read_time(art.get("newsId"), 5000, False,
                                 acc["sessionId"], acc["accountId"], dev)
            except Exception:
                pass
        try:
            report_read_time(art.get("newsId"), stay, True,
                             acc["sessionId"], acc["accountId"], dev)
        except Exception:
            pass

        # 篇间随机停顿（人类化，避免"6 分钟 12 篇"的机械节奏）
        if idx < len(todo):
            jitter(3, 15)

    # 统计 + 昨日在线
    try:
        c1 = rs.call("/user-read-count/count/%s" % TODAY)
        say(f"📊 今日完成统计: {c1.get('msg')}（可抽奖次数: {c1.get('data') or 0}）")
    except Exception:
        pass
    try:
        s1 = rs.call("/user-read/list/summary/%s" % YESTERDAY)
        arr = (s1 and s1.get("data")) or []
        rec = next((x for x in arr if x and x.get("summaryDate") == YESTERDAY), None)
        dur = (rec.get("duration", 0) // 60000) if rec else 0
        say(f"⏱️ 昨日在线 {dur} 分钟" + ("（已满 15 分钟，今日有额外抽奖）" if dur > 15 else ""))
    except Exception:
        pass

    return {"done": done, "total": total, "pending": max(0, len(todo) - done)}


# ========== 抽奖 ==========
def draw(acc: Dict[str, str], dev: Dict[str, Any]) -> str:
    def hd(ref: str) -> Dict[str, str]:
        return {
            "User-Agent": web_ua(dev),
            "Accept": "*/*",
            "Content-type": "application/x-www-form-urlencoded",
            "Origin": SRV_APP,
            "X-Requested-With": XRW,
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
            "Referer": ref,
            "Accept-Language": "zh-CN,zh;q=0.9",
        }

    resp, _ = http_req("GET", SRV_APP + "/tzrb/user/loginWC",
                       params={"accountId": acc["accountId"], "sessionId": acc["sessionId"]},
                       headers=hd(SRV_APP + "/luckdraw-ra-1/"), tag="draw_login")
    ck = grab_cookie(resp, "JSESSIONID")
    if not ck:
        return "抽奖侧登录失败"
    jitter(2, 4)

    h2 = hd(SRV_APP + "/luckdraw-ra-1/")
    h2["Cookie"] = ck
    resp, data = http_req(
        "POST", SRV_APP + "/tzrb/userAwardRecordUpgrade/saveUpdate",
        headers=h2, form=True,
        # ★ 这三个 "undefined" 是原样抓包值，实测服务端不校验（会话走 Cookie），别改
        body={"activityId": "67", "sessionId": "undefined",
              "sig": "undefined", "token": "undefined"},
        tag="draw")
    msg = str((data and (data.get("message") or data.get("msg"))) or (resp.text or ""))[:80]
    say(f"🎰 抽奖结果: {msg}")

    jitter(1.5, 3)
    try:
        _, d2 = http_req("GET", SRV_APP + "/tzrb/userAwardRecordUpgrade/pageList",
                         params={"pageSize": 3, "pageNum": 1, "activityId": 67},
                         headers=h2, tag="draw_list")
        recs = (d2 and d2.get("data", {}).get("records")) or []
        if recs:
            top = recs[0]
            say(f"🎁 最新记录: {top.get('createTime')} {top.get('awardName') or ''}")
    except Exception:
        pass
    return msg


# ========== 在线挂机 ==========
def keep_online(acc: Dict[str, str], dev: Dict[str, Any], minutes: int) -> str:
    if minutes <= 0:
        return "跳过"
    say(f"🕐 开始在线挂机 {minutes} 分钟（今日在线 → 明日额外抽奖）")
    h = {
        "User-Agent": web_ua(dev),
        "Content-Type": "application/json",
        "Origin": "https://xmt.taizhou.com.cn",
        "Referer": "https://xmt.taizhou.com.cn/",
    }
    _, data = http_req("POST", MAIDIAN + "/api/online/start", headers=h,
                       body={"activityId": ACTIVITY_ID, "accountId": acc["accountId"]},
                       tag="online_start")
    sid = data and data.get("sessionId")
    if not sid:
        say("⚠️ 在线会话建立失败（挂机跳过）")
        return "会话建立失败"

    # ★ 修正原脚本下限写死 30 的 bug：现在 minutes=1 就是 2 拍
    interval = 30
    total = max(1, int(round(minutes * 60 / interval)))
    ok = 0
    for i in range(total):
        time.sleep(interval + random.uniform(-3, 3))
        try:
            _, hb = http_req("POST", MAIDIAN + "/api/online/heartbeat", headers=h,
                             body={"sessionId": sid}, retry=1, tag="online_hb")
            if hb and hb.get("success"):
                ok += 1
        except Exception:
            pass
    try:
        http_req("POST", MAIDIAN + "/api/online/end", headers=h,
                 body={"sessionId": sid}, retry=1, tag="online_end")
    except Exception:
        pass
    say(f"🏁 挂机结束（心跳 {ok}/{total}）")
    return f"心跳 {ok}/{total}"


# ========== 单账号 ==========
def run_account(phone: str, password: str, idx: int) -> Dict[str, Any]:
    tag = f"[账号{idx}] {mask_phone(phone)}"
    res: Dict[str, Any] = {"account": mask_phone(phone), "phone": phone,
                           "status": "", "reward": "", "extra": [], "success": False}
    dev = device_for(phone)
    say(f"{tag} 设备 {dev['model']} / Android {dev['android']} / "
        f"UUID {dev['uuid'][:13]}…（持久化复用）")

    acc = login(phone, password, dev)
    nick = acc.get("nick") or mask_phone(phone)
    res["account"] = nick
    say(f"{tag} ✅ 登录成功（{nick}）")

    rs = ReadSide(dev)
    if not rs.login(acc["accountId"], acc["sessionId"]):
        raise RuntimeError("阅读侧登录失败（两个域都没拿到 JSESSIONID）")
    say(f"{tag} ✅ 阅读侧登录成功（域: {'/'.join(rs.jsid)}）")

    r = run_read_task(rs, acc, dev)
    res["extra"].append(f"阅读 {r['done']}/{r['total']} 篇")

    if not SKIP_DRAW:
        res["reward"] = draw(acc, dev)
    else:
        res["reward"] = "已跳过"

    res["extra"].append("挂机 " + keep_online(acc, dev, ONLINE_MIN))

    res["status"] = "完成"
    res["success"] = r["done"] > 0
    return res


def split_accounts(raw: str) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    for part in (raw or "").replace("&", "\n").splitlines():
        s = part.strip()
        if not s or s.startswith("#"):
            continue
        if "#" not in s:
            continue
        phone, _, pwd = s.partition("#")
        phone, pwd = phone.strip(), pwd.strip()
        if phone and pwd:
            out.append((phone, pwd))
    return out


# ========== 环境变量 ==========
ONLINE_MIN = 15
try:
    _om = get_env("ONLINE_MIN", "WC_ONLINE_MIN")
    if _om:
        ONLINE_MIN = max(0, int(_om))
except Exception:
    pass
SKIP_DRAW = get_env("WC_SKIP_DRAW").lower() in ("1", "true", "yes")
START_DELAY = 300
try:
    _sd = get_env("WC_START_DELAY")
    if _sd:
        START_DELAY = max(0, int(_sd))
except Exception:
    pass


def main() -> int:
    raw = get_env("wc", "WC", "WC_ACCOUNT")
    if not raw:
        say("❌ 缺变量 wc（手机号#密码，多账号 & 或换行分隔）")
        return 1

    accounts = split_accounts(raw)
    if not accounts:
        say("❌ 变量 wc 格式错误：需要「手机号#密码」，多账号用 & 或换行分隔"
            "（★ 是 # 不是 &）")
        return 1

    # 错峰：启动前随机延迟，避免每天同一秒打
    if START_DELAY > 0:
        d = random.randint(0, START_DELAY)
        if d > 5:
            say(f"⏳ 错峰延迟 {d}s 后开始…")
            time.sleep(d)

    say(f"📮 任务名称：{APP_NAME}")
    say(f"👥 共 {len(accounts)} 个账号 | 挂机 {ONLINE_MIN} 分钟 | 抽奖 {'跳过' if SKIP_DRAW else '执行'}")
    started = time.time()

    results: List[Dict[str, Any]] = []
    for i, (phone, pwd) in enumerate(accounts, 1):
        try:
            results.append(run_account(phone, pwd, i))
        except Exception as e:
            say(f"[账号{i}] {mask_phone(phone)} ❌ {type(e).__name__}: {e}")
            results.append({"account": mask_phone(phone), "phone": phone,
                            "status": "异常", "error": f"{type(e).__name__}: {e}",
                            "success": False})
        if i < len(accounts):
            jitter(15, 40)

    ok_n = sum(1 for r in results if r.get("success"))
    cost = int(time.time() - started)
    say(f"🏁 完成 {ok_n}/{len(results)} | 耗时 {cost} 秒")
    try:
        notify_and_format(APP_NAME, results,
                          title=f"{APP_NAME} {ok_n}/{len(results)}",
                          start_ts=started)
    except Exception:
        print(format_report(APP_NAME, results, push_result="推送模块异常", cost_s=cost))
    return 0 if ok_n == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
