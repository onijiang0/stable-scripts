#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# /*
# ------------------------------------------
# @Description: Xbox俱乐部 - 有赞小程序每日签到（openid→code换token）
# cron: 40 14 * * *
# ------------------------------------------
# 变量名：xboxjlb
# 变量值：wx_server 里的 openid，多账号用换行分隔
# 示例：owNAX6hm...etI6o
#
# 依赖变量：
# wx_server_url  必填，wx_server 地址（勿写进仓库）
# wx_auth        必填，wx_server 鉴权值（/wx/code 用）
#
# 可选变量：
# xboxjlb_appid     默认 wx7f4f694622875202（有赞小程序）
# xboxjlb_kdt       默认 100464643
# xboxjlb_checkin   默认 1597464
# xboxjlb_uuid      可选，extra-data.uuid
# ------------------------------------------
# 契约（youzan）：
# 登录  POST https://uic.youzan.com/passport/general/auth.json
#       ?app_id=&kdt_id=
#       body {appId,code,platformName:weapp,signature:android,clientId,
#             grantType:yz_union,inWsc:true,kdtId,extraBizData}
#       头 extra-data / page-path / Referer
#       → {sessionId,accessToken,refreshToken,userId,mobile,openId,unionId}
# 签到  GET h5.youzan.com/wscump/checkin/checkinV2.json（大写 V2）
#       ?checkinId=&app_id=&kdt_id=&access_token=
#       头 extra-data: {is_weapp:1,sid:sessionId,version,client:weapp,bizEnv:wsc,uuid,ftime}
# 状态  GET /wscump/checkin/get_activity_by_yzuid_v2.json
# 月历  GET /wscump/checkin/find_checkin_info_by_month.json
# 响应 {code,msg,data}；code==0 成功
# ------------------------------------------
# 踩坑：
# 1. signature 固定 "android"（平台标识，非哈希）；clientId=md5(uuid)[:18]
# 2. sessionId 即 extra-data.sid（KDTWEAPPSESSIONID）
# 3. access_token 走 URL query，sid 走 extra-data 头
# 4. 签到接口是 checkinV2.json 大写 V2
# ------------------------------------------
# */

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("XboxJlb")

BASE = "https://h5.youzan.com"
AUTH_BASE = "https://uic.youzan.com"
ALREADY = ("已签", "已经签", "签到过", "重复", "已达最大", "already")
UA = (
    "Mozilla/5.0 (Linux; Android 17) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Version/4.0 Chrome/150.0.0 Mobile Safari/537.36 "
    "MicroMessenger/8.0.76 MiniProgramEnv/android"
)

WX_SERVER_URL = (os.getenv("wx_server_url") or "").strip().rstrip("/")
WX_AUTH = (os.getenv("wx_auth") or "").strip()


def gmt8() -> datetime:
    return datetime.now(timezone(timedelta(hours=8)))


def parse_openids(raw: str) -> List[str]:
    out: List[str] = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # 兼容旧格式 token&sid：取 & 前半段当 openid 会不对，直接跳过
        if "&" in line:
            log.warning("检测到旧格式 token&sid，请改成 openid，跳过: %s", line[:30])
            continue
        if line.startswith("o"):
            out.append(line.split("#")[0].strip())
    return out


def get_wx_code(openid: str, appid: str) -> str:
    if not WX_SERVER_URL:
        raise RuntimeError("缺少 wx_server_url")
    if not WX_AUTH:
        raise RuntimeError("缺少 wx_auth")
    body = json.dumps({"openid": openid, "appid": appid}).encode()
    req = urllib.request.Request(
        WX_SERVER_URL + "/wx/code",
        data=body,
        method="POST",
        headers={
            "auth": WX_AUTH,
            "Content-Type": "application/json",
            "User-Agent": UA,
            "Referer": f"https://servicewechat.com/{appid}/16/page-frame.html",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            d = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"/wx/code HTTP{e.code}: {e.read().decode()[:150]}")
    except Exception as e:
        raise RuntimeError(f"/wx/code 失败: {e}")
    code = (d.get("data") or {}).get("code") or d.get("code")
    if not code:
        raise RuntimeError(f"/wx/code 无code: {json.dumps(d, ensure_ascii=False)[:200]}")
    return code


def youzan_auth(code: str, appid: str, kdt: str, uuid: str = "") -> Dict[str, Any]:
    """微信 code → 有赞 accessToken + sessionId"""
    url = f"{AUTH_BASE}/passport/general/auth.json?app_id={appid}&kdt_id={kdt}"
    device_uuid = uuid or "g3aOWshQWjGjuby1714673097798"
    client_id = hashlib.md5(device_uuid.encode()).hexdigest()[:18]
    extra_data = json.dumps(
        {
            "sid": "",
            "version": "2.149.9.101",
            "clientType": "weapp-miniprogram",
            "client": "weapp",
            "bizEnv": "",
            "uuid": device_uuid,
            "ftime": 1714673097793,
        },
        separators=(",", ":"),
    )
    body = json.dumps(
        {
            "appId": appid,
            "code": code,
            "platformName": "weapp",
            "signature": "android",
            "clientId": client_id,
            "grantType": "yz_union",
            "inWsc": True,
            "kdtId": int(kdt) if str(kdt).isdigit() else kdt,
            "extraBizData": {
                "enterOptions": {
                    "extKdtId": int(kdt) if str(kdt).isdigit() else kdt,
                    "path": "pages/home/dashboard/index",
                    "query": {},
                    "scene": 1089,
                    "referrerInfo": {},
                    "sessionId": f"host=&version=671108145&device=2",
                    "mode": "default",
                    "apiCategory": "default",
                },
                "guideBizDataMap": {"from_params": ""},
                "sceneData": {},
            },
        },
        separators=(",", ":"),
    ).encode()
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "User-Agent": UA,
            "Content-Type": "application/json",
            "charset": "utf-8",
            "extra-data": extra_data,
            "page-path": "pages/home/dashboard/index",
            "Referer": f"https://servicewechat.com/{appid}/16/page-frame.html",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            d = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"auth.json HTTP{e.code}: {e.read().decode()[:200]}")
    except Exception as e:
        raise RuntimeError(f"auth.json 失败: {e}")
    data = d.get("data") if isinstance(d.get("data"), dict) else d
    token = data.get("accessToken") or ""
    sid = data.get("sessionId") or ""
    if not token or not sid:
        raise RuntimeError(f"auth.json 缺字段: {json.dumps(d, ensure_ascii=False)[:250]}")
    return {
        "accessToken": token,
        "sessionId": sid,
        "userId": data.get("userId"),
        "mobile": data.get("mobile"),
        "openId": data.get("openId"),
    }


class YzWeappCheckin:
    def __init__(
        self,
        token: str,
        sid: str,
        appid: str,
        kdt: str,
        checkin_id: str,
        uuid: str = "",
    ):
        self.token = token
        self.sid = sid
        self.appid = appid
        self.kdt = kdt
        self.checkin_id = checkin_id
        self.uuid = uuid or "g3aOWshQWjGjuby1714673097798"
        self.extra = json.dumps(
            {
                "is_weapp": 1,
                "sid": sid,
                "version": "2.149.9.101",
                "client": "weapp",
                "bizEnv": "wsc",
                "uuid": self.uuid,
                "ftime": 1714673097793,
            },
            separators=(",", ":"),
        )

    def _headers(self) -> Dict[str, str]:
        return {
            "User-Agent": UA,
            "Referer": f"https://servicewechat.com/{self.appid}/16/page-frame.html",
            "extra-data": self.extra,
            "content-type": "application/json",
            "charset": "utf-8",
        }

    def _get(self, path: str, params: Dict[str, Any]) -> Tuple[int, str, Optional[dict]]:
        url = BASE + path + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers=self._headers(), method="GET")
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read().decode("utf-8", "replace")
                code = r.status
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            code = e.code
        except Exception as e:
            return 0, str(e)[:120], None
        try:
            data = json.loads(raw)
        except Exception:
            return code, raw[:120], None
        return code, str(data.get("msg") or data.get("message") or ""), data

    def _base_params(self) -> Dict[str, str]:
        return {
            "app_id": self.appid,
            "kdt_id": self.kdt,
            "access_token": self.token,
        }

    def activity(self) -> Dict[str, Any]:
        p = self._base_params()
        p["checkinId"] = self.checkin_id
        code, msg, data = self._get("/wscump/checkin/get_activity_by_yzuid_v2.json", p)
        if code != 200 or not data or data.get("code") != 0:
            raise RuntimeError(f"活动查询失败 HTTP{code} {msg}")
        return data.get("data") or {}

    def month_days(self) -> list:
        now = gmt8()
        p = self._base_params()
        p.update({"checkin_id": self.checkin_id, "year": now.year, "month": now.month})
        code, msg, data = self._get("/wscump/checkin/find_checkin_info_by_month.json", p)
        if code != 200 or not data or data.get("code") != 0:
            return []
        d = data.get("data") or {}
        return list(d.get("checkin_date") or [])

    def do_checkin(self) -> Tuple[bool, str]:
        p = self._base_params()
        p["checkinId"] = self.checkin_id
        code, msg, data = self._get("/wscump/checkin/checkinV2.json", p)
        if not data:
            return False, f"HTTP{code} {msg}"
        c = data.get("code")
        m = str(data.get("msg") or "")
        if c == 0:
            return True, m or "签到成功"
        if any(x in m for x in ALREADY):
            return True, m
        return False, f"code={c} {m}"


def run_account(
    idx: int,
    openid: str,
    appid: str,
    kdt: str,
    cid: str,
    uuid: str,
) -> List[str]:
    lines: List[str] = [f"—— 账号{idx} {openid[:12]}... ——"]
    try:
        code = get_wx_code(openid, appid)
        lines.append("获取code OK")
        auth = youzan_auth(code, appid, kdt, uuid)
        mobile = auth.get("mobile") or ""
        masked = (mobile[:3] + "****" + mobile[-4:]) if mobile else "未知"
        lines.append(f"登录成功 userId={auth.get('userId')} 手机: {masked}")

        api = YzWeappCheckin(auth["accessToken"], auth["sessionId"], appid, kdt, cid, uuid)
        act = api.activity()
        signed = bool(act.get("isCheckin"))
        opened = bool(act.get("isOpen"))
        cont = act.get("continuesDay", "?")
        daily = ""
        for r in act.get("dailyRewards") or []:
            daily = r.get("desc") or daily
        lines.append(f"活动开启={opened} 已签={signed} 连签={cont} {daily}".strip())

        if signed:
            lines.append("今日已签到，跳过提交")
        elif not opened:
            lines.append("签到活动未开启")
        else:
            ok, msg = api.do_checkin()
            lines.append(msg)
            try:
                act2 = api.activity()
                lines.append("复核 isCheckin=" + str(act2.get("isCheckin")))
            except Exception:
                pass

        days = api.month_days()
        lines.append("本月已签 " + str(len(days)) + " 天")
    except Exception as e:
        lines.append(f"失败: {e}")
    return lines


def main() -> int:
    raw = os.getenv("xboxjlb", "").strip()
    appid = os.getenv("xboxjlb_appid", "wx7f4f694622875202").strip()
    kdt = os.getenv("xboxjlb_kdt", "100464643").strip()
    cid = os.getenv("xboxjlb_checkin", "1597464").strip()
    uuid = os.getenv("xboxjlb_uuid", "").strip()

    if not raw:
        log.error("缺少 xboxjlb（openid，多账号换行分隔）")
        return 1
    if not WX_SERVER_URL:
        log.error("缺少 wx_server_url")
        return 1
    if not WX_AUTH:
        log.error("缺少 wx_auth")
        return 1

    openids = parse_openids(raw)
    if not openids:
        log.error("xboxjlb 未解析出有效 openid")
        return 1

    all_lines: List[str] = []
    for i, oid in enumerate(openids, 1):
        all_lines.extend(run_account(i, oid, appid, kdt, cid, uuid))
        if i < len(openids):
            time.sleep(3)

    print("\n" + "=" * 36)
    print("   Xbox俱乐部签到简报(小程序)")
    print("=" * 36)
    print("\n".join(all_lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
