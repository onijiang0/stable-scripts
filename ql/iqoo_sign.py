#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# /*
# ------------------------------------------
# @Description: iQOO社区 - 签到/浏览任务/幸运抽奖/积分查询
# cron: 10 14 * * *
# ------------------------------------------
# 变量名：iqoo
# 变量值：openid，多账号用换行分隔
# 示例：owNAX6j2...DOLtPc
#
# 依赖变量：
# wx_auth           必填，smallcat 调用 API AUTH
# wx_server_url     默认 https://smallcat.myffa.ccwu.cc
# iqoo_appid        默认 wxcf4266fbc9463132
# iqoo_browse       默认 3，浏览帖子篇数
# iqoo_draw         默认 1，抽奖次数（有次数才抽）
# ------------------------------------------
# 契约（bbs-api.iqoo.com + smallcat）：
# 登录  POST smallcat /wx/getphonenumber -> raw.encryptedData/iv
#       POST smallcat /wx/code -> code
#       POST api/v3/users/vivo/mini {code,encryptedData,iv,from:46}
#       -> Data.accessToken / userId
# 签到  POST api/v3/sign
# 任务  GET  api/v5/users/tasks  -> Data.perDayData
# 进度  GET  api/v5/users/tasks/today-progress
# 浏览  GET  api/v5/recommend/thread/list + GET api/v3/thread.detail
# 抽奖  GET  api/v3/today.draw.count -> Data.count
#       POST api/v3/luck.draw
# 积分  GET  api/v3/user?userId= -> Data.score
# 签名  SIGN: IQOO-HMAC-SHA256 appid=1002,timestamp=..,signature=..
#       raw = METHOD&/api/path&sortedQs&jsonBody&appid=1002&timestamp=
#       HMAC-SHA256(appKey=2618194b0ebb620055e19cf9811d3c13) -> base64
# 头    X-Visitor / X-Platform=mini / Authorization Bearer
# 响应  {Code,Message,Data}；Code==0 成功
# ------------------------------------------
# */

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Dict, List, Optional, Tuple

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("IQOO")

APP_KEY = "2618194b0ebb620055e19cf9811d3c13"
API_BASE = "https://bbs-api.iqoo.com/api/"
UA = (
    "Mozilla/5.0 (Linux; Android 17) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Version/4.0 Chrome/150.0.0 Mobile Safari/537.36 "
    "MicroMessenger/8.0.76 MiniProgramEnv/android"
)
ALREADY = ("已签", "已经签", "重复", "今日已签", "already")


def enc_qs(s: Any) -> str:
    return (
        urllib.parse.quote(str(s), safe="")
        .replace("!", "%21")
        .replace("'", "%27")
        .replace("(", "%28")
        .replace(")", "%29")
        .replace("*", "%2A")
    )


class IqooApi:
    def __init__(self, appid: str, token: str = ""):
        self.appid = appid
        self.token = token
        self.visitor = uuid.uuid4().hex
        self.uid: Optional[int] = None

    def _sign(self, method: str, path: str, params: Optional[dict], body: Optional[dict]) -> str:
        ts = int(time.time())
        qs = ""
        if method == "GET" and params:
            qs = "&".join(f"{enc_qs(k)}={enc_qs(params[k])}" for k in sorted(params.keys()))
        body_s = ""
        if method != "GET" and body is not None:
            body_s = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
        raw = f"{method}&{path}&{qs}&{body_s}&appid=1002&timestamp={ts}"
        dig = hmac.new(APP_KEY.encode(), raw.encode("utf-8"), hashlib.sha256).digest()
        sig = base64.b64encode(dig).decode()
        return f"IQOO-HMAC-SHA256 appid=1002,timestamp={ts},signature={sig}"

    def call(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        url = API_BASE + endpoint
        headers = {
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "content-type": "application/json",
            "Referer": f"https://servicewechat.com/{self.appid}/16/page-frame.html",
            "X-Visitor": self.visitor,
            "X-Platform": "mini",
            "SIGN": self._sign(method, "/api/" + endpoint, params if method == "GET" else None, body),
            "Content-Nonce": "",
            "Authorization": ("Bearer " + self.token) if self.token else "Bearer ",
        }
        data = None
        if method == "GET":
            if params:
                url += "?" + urllib.parse.urlencode(params)
        else:
            data = json.dumps(body if body is not None else {}, separators=(",", ":"), ensure_ascii=False).encode()
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                return json.loads(raw)
            except Exception:
                return {"Code": e.code, "Message": raw[:200]}
        except Exception as e:
            return {"Code": -1, "Message": str(e)}

    def sc_post(self, sc_base: str, auth: str, path: str, body: dict) -> dict:
        req = urllib.request.Request(
            sc_base.rstrip("/") + path,
            data=json.dumps(body).encode(),
            method="POST",
            headers={"auth": auth, "Content-Type": "application/json", "User-Agent": UA},
        )
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                return json.loads(raw)
            except Exception:
                return {"status": False, "message": raw[:200]}
        except Exception as e:
            return {"status": False, "message": str(e)}

    def login(self, sc_base: str, auth: str, openid: str) -> Tuple[bool, str]:
        ph = self.sc_post(sc_base, auth, "/wx/getphonenumber", {"openid": openid, "appid": self.appid})
        raw = (ph.get("data") or {}).get("raw") or {}
        if not raw.get("encryptedData"):
            return False, f"getphonenumber失败: {ph.get('message') or ph.get('error') or ph}"
        time.sleep(1)
        cr = self.sc_post(sc_base, auth, "/wx/code", {"openid": openid, "appid": self.appid})
        code = (cr.get("data") or {}).get("code")
        if not code:
            return False, f"获取code失败: {cr.get('message') or cr.get('error') or cr}"
        d = self.call(
            "POST",
            "v3/users/vivo/mini",
            body={"code": code, "encryptedData": raw.get("encryptedData") or "", "iv": raw.get("iv") or "", "from": 46},
        )
        data = d.get("Data") if isinstance(d, dict) else None
        if not isinstance(data, dict) or not data.get("accessToken"):
            return False, f"登录失败 Code={d.get('Code')} {d.get('Message')}"
        self.token = data.get("accessToken") or ""
        self.uid = data.get("userId")
        return True, f"userId={self.uid}"

    def sign(self) -> str:
        d = self.call("POST", "v3/sign", body={})
        code = d.get("Code")
        if code == 0:
            data = d.get("Data") or {}
            tips = ""
            meta = d.get("Meta") or {}
            for t in meta.get("tips") or []:
                tips = t.get("message") or tips
            return f"签到成功 连签{data.get('serialDays')}天 +{data.get('score')}酷币 余额{data.get('scoreCount')} {tips}".strip()
        msg = str(d.get("Message") or "")
        if any(x in msg for x in ALREADY):
            return msg or "今日已签到"
        return f"签到失败 Code={code} {msg}"

    def tasks(self) -> List[dict]:
        d = self.call("GET", "v5/users/tasks")
        return list((d.get("Data") or {}).get("perDayData") or [])

    def progress(self) -> dict:
        d = self.call("GET", "v5/users/tasks/today-progress")
        return d.get("Data") or {}

    def browse(self, n: int) -> List[str]:
        lines: List[str] = []
        d = self.call("GET", "v5/recommend/thread/list", params={"page": 1, "perPage": max(n, 5)})
        lst = (d.get("Data") or {}).get("data") or []
        if not isinstance(lst, list) or not lst:
            d = self.call("GET", "v3/thread.list", params={"page": 1, "perPage": max(n, 5)})
            lst = (d.get("Data") or {}).get("pageData") or []
        viewed = 0
        for t in lst[:n]:
            tid = t.get("id") or t.get("threadId")
            if not tid:
                continue
            r = self.call("GET", "v3/thread.detail", params={"threadId": tid})
            ok = r.get("Code") == 0
            title = (t.get("title") or "")[:24]
            lines.append(f"浏览#{tid} {'OK' if ok else r.get('Message')} {title}".strip())
            if ok:
                viewed += 1
            time.sleep(1)
        p = self.progress()
        lines.append(f"浏览完成{viewed}篇 进度view={p.get('viewCount')}/{p.get('viewUpperLimit')}")
        return lines

    def draw(self, max_times: int) -> List[str]:
        lines: List[str] = []
        if max_times <= 0:
            return ["抽奖次数0，跳过"]
        d = self.call("GET", "v3/today.draw.count")
        cnt = int((d.get("Data") or {}).get("count") or 0)
        lines.append(f"剩余抽奖次数={cnt}")
        times = min(max_times, max(cnt, 1 if max_times >= 1 else 0))
        # count=0 时仍有每日免费1抽，最多抽 max_times
        times = min(max_times, cnt + 1) if cnt == 0 else min(max_times, cnt)
        for i in range(times):
            r = self.call("POST", "v3/luck.draw", body={})
            if r.get("Code") == 0:
                data = r.get("Data") or {}
                lines.append(f"抽奖{i + 1}: {data.get('prize_name') or data.get('prize_id')}")
            else:
                lines.append(f"抽奖{i + 1}失败: {r.get('Code')} {r.get('Message')}")
                break
            time.sleep(1)
        return lines

    def score(self) -> str:
        if not self.uid:
            return "无userId"
        d = self.call("GET", "v3/user", params={"userId": self.uid})
        data = d.get("Data") or {}
        return f"酷币余额={data.get('score')}"


def parse_openids(raw: str) -> List[str]:
    out: List[str] = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("ow") or line.startswith("o"):
            out.append(line.split("#")[0].strip())
    return out


def run_account(idx: int, openid: str, sc: str, auth: str, appid: str, browse_n: int, draw_n: int) -> List[str]:
    lines = [f"—— 账号{idx} {openid[:12]}... ——"]
    api = IqooApi(appid)
    ok, msg = api.login(sc, auth, openid)
    lines.append(("登录OK " + msg) if ok else ("登录失败 " + msg))
    if not ok:
        return lines
    lines.append(api.sign())
    try:
        lines.extend(api.browse(browse_n))
    except Exception as e:
        lines.append(f"浏览异常 {e}")
    try:
        lines.extend(api.draw(draw_n))
    except Exception as e:
        lines.append(f"抽奖异常 {e}")
    lines.append(api.score())
    return lines


def main() -> int:
    raw = os.getenv("iqoo", "").strip()
    auth = os.getenv("wx_auth", "").strip()
    sc = os.getenv("wx_server_url", "https://smallcat.myffa.ccwu.cc").strip()
    appid = os.getenv("iqoo_appid", "wxcf4266fbc9463132").strip()
    browse_n = int(os.getenv("iqoo_browse", "3").strip() or "3")
    draw_n = int(os.getenv("iqoo_draw", "1").strip() or "1")

    if not raw:
        log.error("缺少 iqoo（openid，多账号换行分隔）")
        return 1
    if not auth:
        log.error("缺少 wx_auth")
        return 1

    openids = parse_openids(raw)
    if not openids:
        log.error("iqoo 未解析出 openid")
        return 1

    all_lines: List[str] = []
    for i, oid in enumerate(openids, 1):
        all_lines.extend(run_account(i, oid, sc, auth, appid, browse_n, draw_n))
        if i < len(openids):
            time.sleep(3)

    print("\n" + "=" * 36)
    print("      iQOO社区任务简报")
    print("=" * 36)
    print("\n".join(all_lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
