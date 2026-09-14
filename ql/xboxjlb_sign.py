#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# /*
# ------------------------------------------
# @Description: Xbox俱乐部 - 有赞小程序每日签到（access_token + sid）
# cron: 40 14 * * *
# ------------------------------------------
# 变量名：xboxjlb
# 变量值：token&sid，多账号用换行分隔
# 示例：0e9a148c2ffced9a36ddf90bed1ca6&YZ1548674722890866688YZQNVJtmPJ
#
# 可选变量：
# xboxjlb_appid     默认 wx7f4f694622875202（有赞小程序）
# xboxjlb_kdt       默认 100464643
# xboxjlb_checkin   默认 1597464
# xboxjlb_uuid      可选，extra-data.uuid
# ------------------------------------------
# 契约（h5.youzan.com 小程序接口）：
# 状态  GET /wscump/checkin/get_activity_by_yzuid_v2.json
#       ?checkinId=&app_id=&kdt_id=&access_token=
#       头 extra-data: {is_weapp:1,sid,version,client:weapp,bizEnv:wsc,uuid,ftime}
# 签到  GET /wscump/checkin/checkinV2.json  （大写 V2）
# 月历  GET /wscump/checkin/find_checkin_info_by_month.json
# 响应 {code,msg,data}；code==0 成功；已签/已达次数也算成功
# token 过期需手机重新签到/打开小程序后抓新包更新变量
# ------------------------------------------
# */

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("XboxJlb")

BASE = "https://h5.youzan.com"
ALREADY = ("已签", "已经签", "签到过", "重复", "已达最大", "already")


def gmt8() -> datetime:
    return datetime.now(timezone(timedelta(hours=8)))


def parse_accounts(raw: str) -> List[Tuple[str, str]]:
    """解析 xboxjlb：每行 token&sid，支持 # 注释与空行。"""
    accounts: List[Tuple[str, str]] = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "&" not in line:
            log.warning("跳过非法行（缺 &）: %s", line[:40])
            continue
        token, sid = line.split("&", 1)
        token, sid = token.strip(), sid.strip()
        if token and sid:
            accounts.append((token, sid))
        else:
            log.warning("跳过非法行（token/sid 为空）: %s", line[:40])
    return accounts


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
        self.s = requests.Session()
        extra = json.dumps(
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
        self.s.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Linux; Android 17) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Version/4.0 Chrome/150.0.0 Mobile Safari/537.36 "
                    "MicroMessenger/8.0.76 MiniProgramEnv/android"
                ),
                "Referer": f"https://servicewechat.com/{appid}/16/page-frame.html",
                "extra-data": extra,
                "content-type": "application/json",
                "charset": "utf-8",
            }
        )

    def _get(self, path: str, params: Dict[str, Any]) -> Tuple[int, str, Optional[dict]]:
        url = BASE + path
        r = self.s.get(url, params=params, timeout=20)
        try:
            data = r.json()
        except Exception:
            return r.status_code, r.text[:120], None
        return r.status_code, str(data.get("msg") or data.get("message") or ""), data

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
        p.update(
            {
                "checkin_id": self.checkin_id,
                "year": now.year,
                "month": now.month,
            }
        )
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
    token: str,
    sid: str,
    appid: str,
    kdt: str,
    cid: str,
    uuid: str,
) -> List[str]:
    api = YzWeappCheckin(token, sid, appid, kdt, cid, uuid)
    lines: List[str] = [f"—— 账号{idx} ——"]
    try:
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
        lines.append(str(e))
    return lines


def main() -> int:
    raw = os.getenv("xboxjlb", "").strip()
    appid = os.getenv("xboxjlb_appid", "wx7f4f694622875202").strip()
    kdt = os.getenv("xboxjlb_kdt", "100464643").strip()
    cid = os.getenv("xboxjlb_checkin", "1597464").strip()
    uuid = os.getenv("xboxjlb_uuid", "").strip()

    if not raw:
        log.error("缺少 xboxjlb（格式 token&sid，多账号换行分隔）")
        return 1

    accounts = parse_accounts(raw)
    if not accounts:
        log.error("xboxjlb 未解析出有效账号")
        return 1

    all_lines: List[str] = []
    for i, (token, sid) in enumerate(accounts, 1):
        all_lines.extend(run_account(i, token, sid, appid, kdt, cid, uuid))
        if i < len(accounts):
            time.sleep(3)

    print("\n" + "=" * 36)
    print("   Xbox俱乐部签到简报(小程序)")
    print("=" * 36)
    print("\n".join(all_lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
