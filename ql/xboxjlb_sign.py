#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# /*
# ------------------------------------------
# @Description: Xbox俱乐部 - 有赞H5每日签到（Cookie 模式，非 wx_server openid）
# cron: 40 14 * * *
# ------------------------------------------
# 变量名：xboxjlb_cookie
# 变量值：浏览器 F12 完整 Cookie（需含 KDTSESSIONID / open_token）
# 示例：KDTSESSIONID=...; open_token={...}; _kdt_id_=100464643; ...
#
# 可选变量：
# xboxjlb_base     默认 https://shop100656811.youzan.com
# xboxjlb_kdt      默认 100464643
# xboxjlb_checkin  默认 1597464
# ------------------------------------------
# 契约（有赞 wscump checkin）：
# 状态  GET /wscump/checkin/get_activity_by_yzuid_v2.json?checkinId=&kdt_id=
#       -> data.isCheckin / isOpen / dailyRewards
# 月历  GET /wscump/checkin/find_checkin_info_by_month.json?checkin_id=&kdt_id=&year=&month=
#       -> data.checkin_date[]
# 签到  GET /wscump/checkin/checkinV2.json?checkinId=&kdt_id=
#       （注意大写 V2；旧路径 checkin.json 会 160540410）
# 响应壳 {code,msg,data}；code==0 成功
# Cookie 过期需浏览器重新登录后更新变量
# ------------------------------------------
# */

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("XboxJlb")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36 Edg/152.0.0.0"
)
ALREADY = ("已签", "已经签", "签到过", "重复", "already")


def gmt8() -> datetime:
    return datetime.now(timezone(timedelta(hours=8)))


class YzCheckin:
    def __init__(self, base: str, kdt: str, checkin_id: str, cookie: str):
        self.base = base.rstrip("/")
        self.kdt = kdt
        self.checkin_id = checkin_id
        self.s = requests.Session()
        self.s.headers.update(
            {
                "User-Agent": UA,
                "Accept": "application/json, text/plain, */*",
                "Cookie": cookie,
                "Referer": f"{self.base}/wscump/checkin/result?kdt_id={kdt}",
            }
        )

    def _get(self, path: str, params: Dict[str, Any]) -> Tuple[int, str, Optional[dict]]:
        url = self.base + path
        r = self.s.get(url, params=params, timeout=20)
        try:
            data = r.json()
        except Exception:
            return r.status_code, r.text[:80], None
        return r.status_code, str(data.get("msg") or ""), data

    def activity(self) -> Dict[str, Any]:
        code, msg, data = self._get(
            "/wscump/checkin/get_activity_by_yzuid_v2.json",
            {"checkinId": self.checkin_id, "kdt_id": self.kdt},
        )
        if code != 200 or not data or data.get("code") != 0:
            raise RuntimeError(f"活动查询失败 HTTP{code} {msg}")
        return data.get("data") or {}

    def month_days(self) -> list:
        now = gmt8()
        code, msg, data = self._get(
            "/wscump/checkin/find_checkin_info_by_month.json",
            {
                "checkin_id": self.checkin_id,
                "kdt_id": self.kdt,
                "year": now.year,
                "month": now.month,
            },
        )
        if code != 200 or not data or data.get("code") != 0:
            return []
        d = data.get("data") or {}
        return list(d.get("checkin_date") or [])

    def do_checkin(self) -> Tuple[bool, str]:
        code, msg, data = self._get(
            "/wscump/checkin/checkinV2.json",
            {"checkinId": self.checkin_id, "kdt_id": self.kdt},
        )
        if not data:
            return False, f"HTTP{code} {msg}"
        c = data.get("code")
        m = str(data.get("msg") or "")
        if c == 0:
            return True, m or "签到成功"
        if any(x in m for x in ALREADY):
            return True, m
        return False, f"code={c} {m}"


def main() -> int:
    cookie = os.getenv("xboxjlb_cookie", "").strip()
    base = os.getenv("xboxjlb_base", "https://shop100656811.youzan.com").strip()
    kdt = os.getenv("xboxjlb_kdt", "100464643").strip()
    cid = os.getenv("xboxjlb_checkin", "1597464").strip()

    if not cookie:
        log.error("缺少 xboxjlb_cookie（浏览器登录有赞后复制完整 Cookie）")
        return 1

    api = YzCheckin(base, kdt, cid, cookie)
    lines = []
    try:
        act = api.activity()
        signed = bool(act.get("isCheckin"))
        opened = bool(act.get("isOpen"))
        cont = act.get("continuesDay", "?")
        daily = ""
        for r in act.get("dailyRewards") or []:
            daily = r.get("desc") or daily
        head = f"活动开启={opened} 已签={signed} 连签={cont} {daily}".strip()
        lines.append(head)

        if signed:
            lines.append("✅ 今日已签到，跳过提交")
        elif not opened:
            lines.append("❌ 签到活动未开启")
        else:
            ok, msg = api.do_checkin()
            lines.append(("✅ " if ok else "❌ ") + msg)
            # 复核
            try:
                act2 = api.activity()
                lines.append("复核 isCheckin=" + str(act2.get("isCheckin")))
            except Exception:
                pass

        days = api.month_days()
        lines.append("本月已签 " + str(len(days)) + " 天")
    except Exception as e:
        lines.append("❌ " + str(e))

    print("\n" + "=" * 36)
    print("      Xbox俱乐部签到简报")
    print("=" * 36)
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
