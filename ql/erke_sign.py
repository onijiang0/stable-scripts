#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# /*
# ------------------------------------------
# @Author: onijiang0
# @Date: 2026.09.12
# @Description: 鸿星尔克会员签到 - 取码服务 openid 换业务登录态 + 每日签到
# cron: 5 14 * * *
# ------------------------------------------
# 变量名：hxek
# 变量值：wx_server 里的 openid/账号标识，多账号用 & 或换行分隔（可加 #备注）
# 示例：openid，多账号换行或 &，可 #备注
#
# 依赖变量：
# wx_server_url  必填，取码服务地址（勿写进仓库）
# wx_auth        必填，wx_server 鉴权值（/wx/code 用）
# hxek_appid     可选，默认 wxa1f1fa3785a47c7d（鸿星尔克）
# hxek_scene     可选，默认 1001
# ------------------------------------------
# 契约（appid wxa1f1fa3785a47c7d，host hope.demogic.com/gic-wx-app）：
# （自反编译主包；GIC 会员体系；签名密钥可用 ERKE_SECRET 覆盖）
#
# 响应：业务接口带 code/errcode，0000/1001/0 视为成功
# 登录参数  POST {wx_server_url}/wx/code  json:{openid, appid} -> data.code (= jcode)
# 业务登录  POST /on_login.json  form: jcode & openid & scene + 空会员字段 + sign
#           -> response.memberInfo / enterpriseInfo
#           含 memberId / enterpriseId / unionid / openid / wxOpenid / cliqueId
# 签名      md5("timestamp={ts}transId={appid+ts}secret={ERKE_SECRET}" \
#               "random={r}memberId={memberId}")
# 业务头    sign=<enterpriseId>  channelEntrance=wx_app
# 签到      POST /sign/member_sign.json  json: source=wxapp & 会员字段 & sign
# 积分      POST /integral_record.json   form: currentPage & pageSize & 会员字段 & sign
#           -> response.accumulatPoints
#
# 登录态缓存 6 小时；失败自动强刷 jcode 重登一次。
# 无需手填 ERKE_CONF（member_id#enterprise_id#...）。
# ------------------------------------------
# */

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
log = logging.getLogger("ErkeSM")

APPID_DEFAULT = "wxa1f1fa3785a47c7d"
BASE_URL = "https://hope.demogic.com/gic-wx-app"
SECRET = os.getenv("ERKE_SECRET", "damogic8888").strip()  # 小程序签名密钥；账号类勿写仓库
GIC_VERSION = "3.9.93"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 "
    "MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI "
    "MiniProgramEnv/Windows WindowsWechat/WMPF"
)
SUCCESS_CODES = {"0000", "1001", 0, "0"}
ALREADY_RE = ("已签", "已经签", "签到过", "重复", "already", "重复签到")


def gmt8_now() -> datetime:
    return datetime.now(timezone(timedelta(hours=8)))


def fmt_time(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def build_sign(member_id: str, appid: str, timestamp: Optional[str] = None) -> Dict[str, Any]:
    ts = timestamp or fmt_time(gmt8_now())
    rand = random.randint(0, 9_999_999)
    trans_id = appid + ts
    if not SECRET:
        raise RuntimeError("缺少 ERKE_SECRET")
    raw = f"timestamp={ts}transId={trans_id}secret={SECRET}random={rand}memberId={member_id}"
    return {
        "sign": md5(raw),
        "timestamp": ts,
        "transId": trans_id,
        "random": rand,
        "appid": appid,
        "memberId": member_id,
    }


def cache_path() -> Path:
    env = os.getenv("erke_cache", "").strip()
    if env:
        return Path(env)
    return Path(__file__).resolve().parent / "erke_login_cache.json"


def load_cache() -> Dict[str, Any]:
    p = cache_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_cache(data: Dict[str, Any]) -> None:
    p = cache_path()
    try:
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning("缓存写入失败: %s", e)


class CodeService:
    def __init__(self, base: str, auth: str, timeout: int = 20):
        self.base = base.rstrip("/")
        self.auth = auth
        self.timeout = timeout
        self.s = requests.Session()
        self.s.headers.update({"auth": auth, "User-Agent": UA})

    def wx_code(self, openid: str, appid: str) -> str:
        r = self.s.post(
            f"{self.base}/wx/code",
            json={"openid": openid, "appid": appid},
            timeout=self.timeout,
        )
        r.raise_for_status()
        data = r.json()
        if not data.get("status"):
            raise RuntimeError(f"取码服务 /wx/code 失败: {data.get('message')}")
        code = (data.get("data") or {}).get("code")
        if not code:
            raise RuntimeError("取码服务未返回 code（可能限流）")
        return code


class Erke:
    def __init__(self, appid: str, scene: str = "1001"):
        self.appid = appid
        self.scene = scene
        self.s = requests.Session()
        self.s.headers.update(
            {
                "User-Agent": UA,
                "channelEntrance": "wx_app",
                "Referer": f"https://servicewechat.com/{appid}/85/page-frame.html",
            }
        )

    def _post(self, path: str, payload: Dict[str, Any], enterprise_id: str = "", json_body: bool = True):
        url = f"{BASE_URL}/{path}"
        if not path.endswith(".json"):
            url += ".json"
        headers = {"sign": enterprise_id or ""}
        if json_body:
            headers["Content-Type"] = "application/json;charset=UTF-8"
            r = self.s.post(url, headers=headers, json=payload, timeout=15)
        else:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            r = self.s.post(url, headers=headers, data=payload, timeout=15)
        r.raise_for_status()
        return r.json()

    def on_login(self, jcode: str, openid: str) -> Dict[str, Any]:
        """业务登录，返回会员信息（memberId / enterpriseId / unionid / wxOpenid 等）。"""
        # 首次登录时 memberId 为空
        base = build_sign("-1", self.appid)
        body = {
            "jcode": jcode,
            "openid": openid,
            "scene": self.scene,
            "memberId": "-1",
            "cliqueId": "-1",
            "cliqueMemberId": "-1",
            "useClique": 0,
            "enterpriseId": "",
            "unionid": "",
            "wxOpenid": "",
            "sign": base["sign"],
            "random": base["random"],
            "appid": self.appid,
            "transId": base["transId"],
            "timestamp": base["timestamp"],
            "gicWxaVersion": GIC_VERSION,
            "launchOptions": "{}",
        }
        # 源码 on_login 用 form（network_util 默认 x-www-form-urlencoded）
        data = self._post("on_login", body, enterprise_id="", json_body=False)
        resp = data.get("response") or data.get("result") or {}
        if isinstance(resp, dict) and "memberInfo" not in resp and data.get("code") not in SUCCESS_CODES:
            # 有的版本直接把字段放在 response 上
            if "memberId" in data.get("response", {}):
                resp = data["response"]
        info: Dict[str, Any] = {}
        if isinstance(resp, dict):
            info.update(resp)
            info.update(resp.get("memberInfo") or {})
            info.update(resp.get("enterpriseInfo") or {})
        if not info.get("openid"):
            info["openid"] = openid
        return info

    def sign_in(self, profile: Dict[str, Any]) -> Tuple[bool, str]:
        member_id = str(profile.get("memberId") or "-1")
        enterprise_id = str(profile.get("enterpriseId") or "")
        payload = {
            "source": "wxapp",
            "memberId": member_id,
            "cliqueId": str(profile.get("cliqueId") or "-1"),
            "enterpriseId": enterprise_id,
            "unionid": profile.get("unionid") or "",
            "openid": profile.get("openid") or "",
            "wxOpenid": profile.get("wxOpenid") or "",
            "gicWxaVersion": GIC_VERSION,
        }
        sign = build_sign(member_id, self.appid)
        payload.update(
            {
                "sign": sign["sign"],
                "random": sign["random"],
                "appid": self.appid,
                "transId": sign["transId"],
                "timestamp": sign["timestamp"],
            }
        )
        data = self._post("sign/member_sign", payload, enterprise_id=enterprise_id, json_body=True)
        msg = str(data.get("message") or data.get("msg") or "无返回")
        code = data.get("code", data.get("errcode"))
        if code in SUCCESS_CODES:
            return True, msg
        low = msg.lower()
        if any(k in msg for k in ALREADY_RE) or "already" in low:
            return True, msg
        return False, msg

    def get_points(self, profile: Dict[str, Any]) -> str:
        member_id = str(profile.get("memberId") or "-1")
        enterprise_id = str(profile.get("enterpriseId") or "")
        payload = {
            "currentPage": 1,
            "pageSize": 5,
            "memberId": member_id,
            "cliqueId": str(profile.get("cliqueId") or "-1"),
            "enterpriseId": enterprise_id,
            "unionid": profile.get("unionid") or "",
            "openid": profile.get("openid") or "",
            "wxOpenid": profile.get("wxOpenid") or "",
            "gicWxaVersion": GIC_VERSION,
        }
        sign = build_sign(member_id, self.appid)
        payload.update(
            {
                "sign": sign["sign"],
                "random": sign["random"],
                "appid": self.appid,
                "transId": sign["transId"],
                "timestamp": sign["timestamp"],
            }
        )
        try:
            data = self._post("integral_record", payload, enterprise_id=enterprise_id, json_body=False)
            resp = data.get("response") or {}
            return str(resp.get("accumulatPoints", "?"))
        except Exception as e:
            log.debug("积分查询失败: %s", e)
            return "?"


def split_openids(raw: str) -> List[str]:
    parts = raw.replace("&", "\n").replace(",", "\n").splitlines()
    return [p.strip() for p in parts if p.strip()]


def ensure_login(
    sm: CodeService,
    erke: Erke,
    openid: str,
    cache: Dict[str, Any],
) -> Dict[str, Any]:
    key = f"{erke.appid}:{openid}"
    hit = cache.get(key)
    # 缓存 24 小时；未命中只调一次 /wx/code，本轮不 force 重试
    if hit:
        ts = hit.get("_ts") or 0
        if time.time() - ts < 24 * 3600 and hit.get("memberId") and str(hit.get("memberId")) != "-1":
            return hit
    code = sm.wx_code(openid, erke.appid)
    profile = erke.on_login(code, openid)
    profile["_ts"] = time.time()
    cache[key] = profile
    save_cache(cache)
    return profile


def run_one(sm: CodeService, erke: Erke, openid: str, cache: Dict[str, Any]) -> Dict[str, Any]:
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
    try:
        profile = ensure_login(sm, erke, openid, cache)
        mid = profile.get("memberId")
        if not mid or str(mid) == "-1":
            acc["status"] = "登录失败 ❌（无 memberId，下轮再试）"
            acc["error"] = "无 memberId"
            return acc

        points_before = erke.get_points(profile)
        ok, msg = erke.sign_in(profile)
        points_after = erke.get_points(profile)
        acc["success"] = bool(ok)
        acc["status"] = (msg or ("签到成功" if ok else "签到失败")) + (" ✅" if ok else " ❌")
        delta = None
        try:
            delta = int(points_after) - int(points_before)
        except Exception:
            delta = None
        if delta is not None:
            acc["reward"] = f"+{delta}" if delta >= 0 else str(delta)
        acc["extra"] = [f"积分 {points_before} → {points_after}"]
        if not ok:
            acc["error"] = msg
        return acc
    except Exception as e:
        acc["status"] = f"异常 ❌ ({str(e)[:60]})"
        acc["error"] = str(e)[:80]
        return acc


def main() -> int:
    started = time.time()
    auth = os.getenv("wx_auth", "").strip()
    base = os.getenv("wx_server_url", "").strip()
    openids_raw = os.getenv("hxek", "").strip()
    appid = os.getenv("hxek_appid", APPID_DEFAULT).strip()
    scene = os.getenv("hxek_scene", "1001").strip()

    if not auth:
        log.error("缺少 wx_auth")
        return 1
    if not base:
        log.error("缺少 wx_server_url（取码服务地址，勿写进仓库）")
        return 1
    if not openids_raw:
        log.error("缺少 hxek（openid，多个用 & 分隔）")
        return 1

    openids = split_openids(openids_raw)
    sm = CodeService(base, auth)
    erke = Erke(appid, scene)
    cache = load_cache()

    accounts: List[Dict[str, Any]] = []
    for oid in openids:
        acc = run_one(sm, erke, oid, cache)
        accounts.append(acc)
        time.sleep(3)

    ok_n = sum(1 for a in accounts if a.get("success"))
    try:
        from send_notify import notify_and_format

        notify_and_format(
            "鸿星尔克",
            accounts,
            title=f"鸿星尔克签到 {ok_n}/{len(accounts)}",
            start_ts=started,
        )
    except Exception as _ne:
        print("[notify] 使用旧格式:", _ne)
        lines = []
        for a in accounts:
            mark = "✅" if a.get("success") else "❌"
            lines.append(f"{mark} [{a.get('account')}] {a.get('status')} {a.get('reward')}")
        _report = "\n".join(lines)
        print(_report)
        try:
            from send_notify import send_notify

            send_notify("鸿星尔克签到简报", _report)
        except Exception as _ne2:
            print("[notify] 跳过:", _ne2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
