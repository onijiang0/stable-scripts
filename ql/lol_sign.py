#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# /*
# ------------------------------------------
# @Author: onijiang0
# @Date: 2026.10.10
# @Description: 掌上英雄联盟 · 签到 + 自动补签（原生App，QQ登录）
# cron: 23 9 * * *
# ------------------------------------------
# 变量名：lol
# 变量值：单账号 JSON（& 或换行分隔多账号，可加 #备注）：
#   {"cookie":"clientType=9; uin=o<qq>; appid=100543809; acctype=qc; openid=<32HEX>; access_token=<32HEX>; userId=<uuid>; accountType=5; tid=<长token>;","clientInfo":{...get_main_role_raw 请求体里的 clientInfo 对象...}}#大号
#   cookie/clientInfo 抓包来源：掌盟 App 内"福利中心-签到"页（reqable 抓一次即可）
#
# 依赖变量：
# LOL_DRY_RUN=1        只查签到状态，不提交签到/补签（调试）
# LOL_AUTO_ADDSIGN=0   关闭自动补签（默认开，消耗补签卡，用户已确认）
# LOL_START_DELAY      启动前随机延迟上限秒数，默认 60（错峰，0 关闭）
# LOL_UA               覆盖默认 UA（一般不用）
# QL_NOTIFY=0          关闭推送
# ------------------------------------------
# 契约（reqable 实测 2026-10-09/10 + 本机重放验证，笔记 docs/guides/lol/notes.md）：
#
# [鉴权] 纯 cookie（tid 为主要载体），无 g_tk；http/1.1 + requests 可调通
# [scene] 绑定态凭证：get_main_role_raw 响应里 gameId=lol 角色项的 scene 字段
#         （v3_ 开头，各游戏各有 scene，LOL端游/tft/lgame/jgame 别拿错）
# [查状态] POST https://mlol.qt.qq.com/go/mlol_store/lol/sign_calendar?source_game_zone=lol&game_zone=lol
#          body {"scene":<scene>,"source_game_zone":"lol","game_zone":"lol"}
#          → data.sign_info.today_has_sign 0=未签/1=已签（scene="" 时是未绑定态，判重无效！）
#          → data.sign_info.add_sign_num 补签卡张数(string，月上限15，每日登录LOL得1张)
#          → data.calendar.days[] date(1起)/is_sign：0=未来 1=已签 2=漏签可补 3=今日可签
# [签到]   POST https://mlol.qt.qq.com/go/mlol_store/lol/sign_in?...（与补签同接口）
#          body {"scene":<scene>,"day":<1-based日期>,...}
#          → result==0 成功（err_msg:"6" 也出现在成功响应里，别当错误）
#          → result==1002 "没有补签资格"（0卡补签，实测无副作用）
#          → 一次请求只签一天（day=今日 联动两天是 App 双发假象，已复测排除）
# [角色]   POST https://mlol.qt.qq.com/go/account/get_main_role_raw?source_game_zone=lol&game_zone=lol
#          body {"clientInfo":{...},"source_game_zone":"lol","game_zone":"lol"}
#
# 已实现：多账号 · scene 自取（免手工维护）· 自动补签（可关）· 随机延迟 · 日志脱敏 · 推送简报
# 隐私：cookie/openid/uin 全程 mask，不打印 tid/access_token
# ------------------------------------------
# */

from __future__ import annotations

import json
import os
import random
import socket
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import requests
import urllib3

urllib3.disable_warnings()

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from send_notify import clean_line, mask_id, notify_and_format
except Exception:
    def clean_line(line: Any) -> str:
        s = str(line or "").strip()
        return "" if s.startswith(("{", "[")) else s[:180]

    def mask_id(value: Any, keep: int = 8) -> str:
        v = str(value or "")
        return (v[:keep] + "****" + v[-4:]) if len(v) > keep + 4 else (v or "-")

    def notify_and_format(task, accounts, **kwargs):
        print("🔔 推送结果：跳过（send_notify 不可用）")


APP_NAME = "掌盟签到"

# ── 强制 IPv4 解析（容器 IPv6 残废根治，与 chml/md/wc 同源问题）──
_orig_getaddrinfo = socket.getaddrinfo


def _getaddrinfo_ipv4(host, port, family=0, type=0, proto=0, flags=0):
    return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)


socket.getaddrinfo = _getaddrinfo_ipv4
# ────────────────────────────────────────────────────────────────

HOST = "https://mlol.qt.qq.com"
QS = "source_game_zone=lol&game_zone=lol"

UA_DEFAULT = (
    "QTL/12.8.2.11900 Channel/5 Manufacturer/Xiaomi  Mozilla/5.0 "
    "(Linux; Android 17; 2509FPN0BC Build/CP2A.260605.016; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/153.0.8010.36 "
    "Mobile Safari/537.36"
)

TIMEOUT = 25


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


def split_accounts(raw: str) -> List[Dict[str, Any]]:
    """& 或换行分隔；单账号为 JSON，支持 #备注。"""
    out: List[Dict[str, Any]] = []
    for chunk in (raw or "").replace("\n", "&").split("&"):
        chunk = chunk.strip()
        if not chunk:
            continue
        remark = ""
        if "#" in chunk:
            chunk, remark = chunk.split("#", 1)
            remark = remark.strip()
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            ent = json.loads(chunk)
            if not isinstance(ent, dict) or not str(ent.get("cookie") or "").strip():
                raise ValueError("缺少 cookie")
            ent["remark"] = remark
            out.append(ent)
        except Exception as e:
            out.append({"_error": f"JSON 解析失败: {e}", "remark": remark})
    return out


def account_name(ent: Dict[str, Any], idx: int) -> str:
    if ent.get("remark"):
        return str(ent["remark"])
    import re

    m = re.search(r"uin=o?(\d+)", str(ent.get("cookie") or ""))
    if m:
        uin = m.group(1)
        return f"QQ{uin[-4:]}"
    return f"账号{idx}"


class MlolClient:
    def __init__(self, cookie: str, ua: str):
        self.verify = not bool(get_env("LOL_INSECURE"))  # 仅本机 MITM 调试用，勿在面板开启
        self.s = requests.Session()
        self.s.headers.update({
            "cookie": cookie,
            "user-agent": ua,
            "content-type": "application/json; charset=utf-8",
        })

    def _post(self, path: str, body: dict) -> Dict[str, Any]:
        r = self.s.post(f"{HOST}{path}?{QS}", json=body, timeout=TIMEOUT, verify=self.verify)
        r.raise_for_status()
        return r.json()

    def fetch_scene(self, client_info: dict) -> Tuple[str, str]:
        """从角色列表取 gameId=lol 的 scene。返回 (scene, 描述)。"""
        j = self._post("/go/account/get_main_role_raw",
                       {"clientInfo": client_info, "source_game_zone": "lol", "game_zone": "lol"})
        if j.get("result") != 0:
            raise RuntimeError(f"get_main_role_raw result={j.get('result')} {j.get('msg')}")
        for it in (j.get("data") or {}).get("list") or []:
            if it.get("gameId") == "lol":
                scene = str(it.get("scene") or "")
                if scene:
                    desc = f"{it.get('mlol_area_name', '')} {it.get('role_name', '')}".strip()
                    return scene, desc
        raise RuntimeError("角色列表里没有 gameId=lol 的角色（scene 取不到）")

    def calendar(self, scene: str) -> Dict[str, Any]:
        return self._post("/go/mlol_store/lol/sign_calendar",
                          {"scene": scene, "source_game_zone": "lol", "game_zone": "lol"})

    def sign_in(self, scene: str, day: int) -> Dict[str, Any]:
        return self._post("/go/mlol_store/lol/sign_in",
                          {"scene": scene, "day": day, "source_game_zone": "lol", "game_zone": "lol"})


def summarize_calendar(j: dict) -> Tuple[Optional[int], int, int, List[int], str]:
    """→ (today_has_sign, add_sign_num, month_signed, missed_days, err)"""
    data = j.get("data") or {}
    info = data.get("sign_info") or {}
    try:
        cards = int(str(info.get("add_sign_num") or "0"))
    except Exception:
        cards = 0
    days = ((data.get("calendar") or {}).get("days")) or []
    signed, missed = 0, []
    for c in days:
        v = str(c.get("is_sign"))
        if v == "1":
            signed += 1
        elif v == "2":
            missed.append(int(c.get("date")))
    missed.sort()
    ths = info.get("today_has_sign")
    ths = int(ths) if str(ths).isdigit() else None
    return ths, cards, signed, missed, ""


def run_account(idx: int, ent: Dict[str, Any]) -> Dict[str, Any]:
    name = account_name(ent, idx)
    blk: Dict[str, Any] = {"account": name}
    extra: List[str] = []
    if "_error" in ent:
        blk.update(status="env 格式错误", error=ent["_error"])
        return blk

    dry = bool(get_env("LOL_DRY_RUN"))
    auto_add = (get_env("LOL_AUTO_ADDSIGN") or "1") not in ("0", "false", "no")

    try:
        cli = MlolClient(str(ent["cookie"]).strip(),
                         get_env("LOL_UA") or UA_DEFAULT)

        # 1) scene 自取
        scene, role_desc = cli.fetch_scene(ent.get("clientInfo") or {})
        say(f"[{name}] 角色：{role_desc}")

        # 2) 状态
        j = cli.calendar(scene)
        if j.get("result") != 0:
            blk.update(status="查询失败", error=f"result={j.get('result')} {j.get('msg')}")
            return blk
        ths, cards, signed, missed, _ = summarize_calendar(j)
        say(f"[{name}] 今日{'已签' if ths == 1 else '未签'} | 本月已签 {signed} 天 | 补签卡 {cards} | 漏签 {missed or '无'}")

        if dry:
            blk.update(status="DRY_RUN 未提交",
                       month_days=signed, patch_card=cards,
                       extra=[f"漏签日: {missed or '无'}"])
            return blk

        rewards: List[str] = []

        # 3) 今日签到
        if ths != 1:
            import datetime
            today = datetime.date.today().day
            j2 = cli.sign_in(scene, today)
            if j2.get("result") == 0:
                d2 = j2.get("data") or {}
                items = d2.get("items") or []
                got = "、".join(str(i.get("content")) for i in items if i.get("content")) or "成功"
                rewards.append(got)
                ths = 1
                signed += 1
                say(f"[{name}] 今日签到成功：{got}")
            else:
                err = f"result={j2.get('result')} {j2.get('msg')} {j2.get('err_msg')}"
                say(f"[{name}] 今日签到失败：{err}")
                blk["error"] = err

        # 4) 自动补签（从早到晚，卡用完或 1002 即停）
        add_used = 0
        if auto_add and cards > 0 and missed:
            for day in missed:
                if cards <= 0:
                    break
                j3 = cli.sign_in(scene, day)
                if j3.get("result") == 0:
                    cards -= 1
                    add_used += 1
                    signed += 1
                    d3 = j3.get("data") or {}
                    got = "、".join(str(i.get("content")) for i in (d3.get("items") or []) if i.get("content")) or "成功"
                    rewards.append(got)
                    say(f"[{name}] 补签 {day} 日成功（余卡 {cards}）：{got}")
                    time.sleep(random.uniform(1.5, 3.0))
                else:
                    code = j3.get("result")
                    msg = j3.get("msg")
                    say(f"[{name}] 补签 {day} 日未成功：result={code} {msg}")
                    if code == 1002:   # 没有补签资格
                        break
                    # 其它错误也停，避免连续异常请求
                    break

        if add_used:
            extra.append(f"自动补签 {add_used} 天（消耗补签卡 {add_used} 张）")

        status = "今日已签" if ths == 1 else "今日未签"
        if ths == 1 and not rewards:
            status = "今日已签(此前完成)"
        blk.update(
            status=status,
            reward="、".join(rewards) if rewards else "",
            month_days=signed,
            patch_card=cards,
            extra=extra or None,
        )
        # 补签后复核一次剩余卡（服务器口径）
        try:
            j4 = cli.calendar(scene)
            t4, c4, s4, m4, _ = summarize_calendar(j4)
            blk["patch_card"] = c4
            blk["month_days"] = s4
            if m4:
                extra.append(f"仍有漏签: {m4}（补签卡不足，登录LOL游戏得卡）")
                blk["extra"] = extra
        except Exception:
            pass
        return blk

    except Exception as e:
        blk.update(status="异常", error=clean_line(str(e))[:120] or type(e).__name__)
        return blk


def main() -> int:
    start = time.time()

    delay_max = int(get_env("LOL_START_DELAY") or 60)
    if delay_max > 0:
        d = random.uniform(0, delay_max)
        say(f"⏳ 随机延迟 {d:.0f}s（错峰）")
        time.sleep(d)

    raw = os.getenv("lol", "") or os.getenv("LOL", "") or os.getenv("LOL_ACCOUNT", "")
    if not raw.strip():
        say("未找到变量 lol")
        notify_and_format(APP_NAME, [], title="掌盟 未配置账号")
        return 0

    accounts = split_accounts(raw)
    say(f"共 {len(accounts)} 个账号")

    results: List[Dict[str, Any]] = []
    for i, ent in enumerate(accounts, 1):
        results.append(run_account(i, ent))
        if i < len(accounts):
            time.sleep(random.uniform(3, 8))

    notify_and_format(APP_NAME, results, start_ts=start)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"脚本异常: {e}")
        notify_and_format(APP_NAME, [], title="掌盟 脚本异常")
        sys.exit(1)
