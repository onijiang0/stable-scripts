#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
好游快爆 App · 爆米花农场每日任务（cornfarm）
================================================
覆盖: ① 爆米花签到(+5)  ② 每日分享任务链(+奖励)  ③ 预约游戏领奖(+10)
接口域: https://huodong3.3839.com/n/hykb/cornfarm/
鉴权: body 内 scookie（快爆 App 统一 H5 会话凭据，长效）+ token_sign 请求签名

签名算法（从 cornfarm act.js/function_common.js 逆向，抓包双样本验证）:
  token_sign = md5( page_token + '|' + random_str + '|' + token_time + '|visitV2' )[:10]

任务 ID 发现（不写死，活动 ID 会换）:
  - 分享任务: GET index.php HTML 解析 DailyShareLing(<id>) 与对应 data-shareype
  - 预约任务: dailyInit 返回 user_daily_Task 里 success<2 的记录，逐个 DailyYuyueLing
  - 注: 预约任务的"游戏预约"一步走 App 主 API（user_token+t 签名，未破解），
        这里只对已有任务记录尝试领奖；若服务端校验预约则该任务会拿不到，推送里可见。

每日分享链路（cornfarm_daily.js 逆向）:
  DailyShare(id, onlyc=0)   → 拿分享内容并开始任务（key 2002 + share 字段）
  DailyShareCallback(id, mode, source=ds) → 标记已分享（key ok）
  DailyShareLing(id)        → 领奖（ok / 2001 已领 / 2002 未分享 / 2003 未播种 / 2004 需收割）

环境变量:
  HYKB_APP  账号串，多账号换行分隔（也支持 & 分隔单行内多账号则不适用——含备注时按行优先），
            每行 6 段用 & 分隔: scookie&device&smdeviceid&token&page_token&random_str
            （scookie 内部含 | 和 %1，不含 &，可放心用 & 分隔；支持 备注@ 开头）

抓包方法:
  快爆 App 内打开爆米花农场点一次签到，Reqable 找
  huodong3.3839.com/n/hykb/cornfarm/ajax_sign.php，
  复制 body 里 scookie/device/smdeviceid/token/page_token/random_str 六个值。

cron: 21 8 * * *
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import sys
import time
import urllib.parse
import urllib.request
from typing import Dict, List, Optional, Tuple

BASE = "https://huodong3.3839.com/n/hykb/cornfarm/"
AJAX_SIGN = BASE + "ajax_sign.php"
AJAX_DAILY = BASE + "ajax_daily.php"
AJAX_MAIN = BASE + "ajax.php"
INDEX = BASE + "index.php?imm=0"
REFERER = BASE + "index.php?imm=0"
UA = (
    "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Version/4.0 Chrome/153.0.8010.36 Mobile Safari/537.36"
    "Androidkb/1.5.7.507(android;)"
)
VERSION = "1.5.7.507"

LING_KEY_TEXT = {
    "2001": "今日已领过",
    "2002": "未分享，领奖被拒",
    "2003": "未播种玉米，先播种才能领",
    "2004": "玉米已成熟，先收割再领",
}


def log(msg: str) -> None:
    print(msg, flush=True)


def parse_accounts(raw: str) -> List[Tuple[str, Dict[str, str]]]:
    """拆环境变量为 [(名称, 字段dict)]。每行一个账号，行内 6 段用 & 分隔；支持 备注@ 开头。"""
    accounts: List[Tuple[str, Dict[str, str]]] = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        name, body = "", line
        if "@" in line and line.split("@", 1)[1].count("&") == 5:
            name, body = line.split("@", 1)
        parts = body.split("&")
        if len(parts) != 6:
            log(f"跳过格式错误账号行（应为6段）: {line[:40]}…")
            continue
        scookie, device, smdeviceid, token, page_token, random_str = parts
        accounts.append((name or f"账号{len(accounts) + 1}", {
            "scookie": scookie, "device": device, "smdeviceid": smdeviceid,
            "token": token, "page_token": page_token, "random_str": random_str,
        }))
    return accounts


def token_sign(page_token: str, random_str: str, token_time: int) -> str:
    src = f"{page_token}|{random_str}|{token_time}|visitV2"
    return hashlib.md5(src.encode()).hexdigest()[:10]


def post_json(url: str, param: Dict[str, str]) -> Tuple[Optional[dict], str]:
    """POST 表单并解析 JSON；非 JSON 返回 (None, 原文)。"""
    data = urllib.parse.urlencode(param).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("User-Agent", UA)
    req.add_header("Content-Type", "application/x-www-form-urlencoded; charset=UTF-8")
    req.add_header("X-Requested-With", "XMLHttpRequest")
    req.add_header("Accept", "application/json, text/javascript, */*; q=0.01")
    req.add_header("Origin", "https://huodong3.3839.com")
    req.add_header("Referer", REFERER)
    req.add_header("Cookie", "cornfarm_iback_v5=ok; cornfarm_iback_mark_v2=ok")
    with urllib.request.urlopen(req, timeout=25) as resp:
        text = resp.read().decode("utf-8", "replace")
    try:
        return json.loads(text), text
    except ValueError:
        return None, text


def base_param(f: Dict[str, str]) -> Dict[str, str]:
    """AddTokenInfo 等价：时间戳组 + 签名 + 凭据。"""
    now = int(time.time() * 1000)
    return {
        "r": f"{random.random()}",
        "token": f["token"],
        "token_version": "v2",
        "page_token": f["page_token"],
        "enter_time": str(now - 3000),
        "token_time": str(now),
        "token_time2": str(now + 400),
        "serverTimeInfo[enter_set_time]": str(now - 2000),
        "serverTimeInfo[time_diff]": "0",
        "random_str": f["random_str"],
        "token_sign": token_sign(f["page_token"], f["random_str"], now),
        "scookie": f["scookie"],
        "device": f["device"],
    }


def do_sign(f: Dict[str, str]) -> str:
    """① 爆米花签到，返回结果描述。"""
    param = base_param(f)
    param.update({
        "ac": "Sign",
        "smdeviceid": f["smdeviceid"],
        "verison": VERSION,
        "OpenAutoSign": "close",
    })
    d, text = post_json(AJAX_SIGN, param)
    if d is None:
        return f"签到返回异常: {text[:120]}"
    key = str(d.get("key", ""))
    if key == "1001":
        sign = d.get("sign") or {}
        today = time.strftime("%Y%m%d")
        if str(sign.get("sign_day", "")) == today and "已领取" in str(d.get("html", "")):
            return (f"今日已签（连续{sign.get('now_sign_daynum', '?')}天，"
                    f"今日得{sign.get('sign_now_bmh', '?')}爆米花）")
        return f"签到成功，得 {sign.get('sign_now_bmh', '?')} 爆米花（连续{sign.get('now_sign_daynum', '?')}天）"
    return f"签到失败 key={key}: {text[:150]}"


def daily_init(f: Dict[str, str]) -> Tuple[List[dict], str]:
    """dailyInit：返回 (任务记录列表, 成熟度描述)。"""
    param = base_param(f)
    param.update({"ac": "dailyInit", "VersionCode": "342"})
    d, _ = post_json(AJAX_DAILY, param)
    if not d or str(d.get("key")) != "ok":
        return [], "dailyInit 失败"
    tasks = d.get("user_daily_Task") or []
    csd = f"成熟度{d.get('chengshoudu', '?')}/{d.get('banfenbi', '?')}"
    return tasks, csd


def discover_share_tasks() -> Tuple[List[Tuple[str, str]], List[str]]:
    """从 index.php HTML 运行时发现任务 ID。
    返回 (分享任务 [(id, shareype)], 预约任务 [id])。"""
    req = urllib.request.Request(INDEX)
    req.add_header("User-Agent", UA)
    req.add_header("Referer", REFERER)
    with urllib.request.urlopen(req, timeout=25) as resp:
        html = resp.read().decode("utf-8", "replace")
    share_pairs: List[Tuple[str, str]] = []
    for m in re.finditer(r"daily_before1_btn_(\d+)[\s\S]{0,600}?data-shareype=\"([^\"]*)\"", html):
        tid, shareype = m.group(1), m.group(2) or "allnoweibo"
        if f"DailyShareLing({tid})" in html and tid not in {p[0] for p in share_pairs}:
            share_pairs.append((tid, shareype))
    # 兜底：页面结构变化时至少拿到领奖函数里的 id
    if not share_pairs:
        for tid in re.findall(r"DailyShareLing\((\d+)\)", html):
            if tid not in {p[0] for p in share_pairs}:
                share_pairs.append((tid, "allnoweibo"))
    yuyue_ids: List[str] = []
    for tid in re.findall(r"DailyYuyueLing\((\d+)\)", html):
        if tid not in yuyue_ids:
            yuyue_ids.append(tid)
    return share_pairs, yuyue_ids


def reward_text(d: dict) -> str:
    """从领奖 ok 响应里提取所有 reward 字段。"""
    parts = []
    for k, v in d.items():
        if k.startswith("reward") and str(v) not in ("0", "", "None"):
            parts.append(f"{k.replace('reward_', '').replace('_num', '')}+{v}")
    return " ".join(parts) if parts else "(响应无 reward 字段)"


def share_chain(f: Dict[str, str], tid: str, shareype: str) -> str:
    """② 单个分享任务: 拿内容 → 标记已分享 → 领奖。"""
    p1 = base_param(f)
    p1.update({"ac": "DailyShare", "id": tid, "onlyc": "0"})
    d, text = post_json(AJAX_DAILY, p1)
    if d is None or str(d.get("key")) not in ("2002", "ok"):
        return f"分享任务{tid} 拿内容异常 key={d.get('key') if d else '?'}: {text[:100]}"

    p2 = base_param(f)
    p2.update({"ac": "DailyShareCallback", "id": tid, "mode": shareype, "source": "ds"})
    d2, text2 = post_json(AJAX_DAILY, p2)
    cb = str(d2.get("key")) if d2 else "?"
    if cb != "ok":
        return f"分享任务{tid} 标记已分享失败 key={cb}: {text2[:100]}"

    p3 = base_param(f)
    p3.update({"ac": "DailyShareLing", "smdeviceid": f["smdeviceid"], "verison": VERSION, "id": tid})
    d3, text3 = post_json(AJAX_DAILY, p3)
    key = str(d3.get("key")) if d3 else "?"
    if key == "ok":
        return f"分享任务{tid} 领奖成功 {reward_text(d3)}"
    if key in LING_KEY_TEXT:
        return f"分享任务{tid} {LING_KEY_TEXT[key]}"
    return f"分享任务{tid} 领奖返回 key={key}: {text3[:100]}"


def yuyue_claim(f: Dict[str, str], rwid: str) -> str:
    """③ 预约游戏领奖（依赖 App 内已完成预约的任务记录）。"""
    param = base_param(f)
    param.update({"ac": "DailyYuyueLing", "id": rwid,
                  "smdeviceid": f["smdeviceid"], "verison": VERSION})
    d, text = post_json(AJAX_DAILY, param)
    key = str(d.get("key")) if d else "?"
    if key == "ok":
        return f"预约任务{rwid} 领奖成功 {reward_text(d)}"
    if key in LING_KEY_TEXT:
        return f"预约任务{rwid} {LING_KEY_TEXT[key]}"
    return f"预约任务{rwid} 返回 key={key}: {text[:100]}"


def run_account(name: str, f: Dict[str, str]) -> Tuple[str, List[str]]:
    """跑单账号全部任务，返回 (状态, 推送行)。"""
    lines: List[str] = []
    fail = False

    try:
        lines.append(do_sign(f))
    except Exception as exc:
        fail = True
        lines.append(f"签到异常: {exc}")

    try:
        tasks, csd = daily_init(f)
        lines.append(csd)
    except Exception as exc:
        tasks, csd = [], ""
        lines.append(f"dailyInit 异常: {exc}")
    claimed = {str(t.get("rwid")): t for t in tasks
               if isinstance(t, dict) and t.get("rwid")}

    try:
        share_tasks, yuyue_ids = discover_share_tasks()
    except Exception as exc:
        share_tasks, yuyue_ids = [], []
        lines.append(f"任务发现异常: {exc}")
    if not share_tasks:
        lines.append("未发现分享任务（页面结构可能变化）")
    for tid, shareype in share_tasks:
        rec = claimed.get(tid)
        if rec and str(rec.get("success")) == "2":
            lines.append(f"分享任务{tid} 今日已完成")
            continue
        try:
            line = share_chain(f, tid, shareype)
        except Exception as exc:
            fail = True
            line = f"分享任务{tid} 异常: {exc}"
        lines.append(line)

    # 预约领奖只针对「预约类」任务记录（rwid 须在页面的 DailyYuyueLing 清单里），
    # 避免把分享任务记录误当预约任务重复领
    for rwid, rec in claimed.items():
        if str(rec.get("success")) == "2" or rwid not in yuyue_ids:
            continue
        try:
            lines.append(yuyue_claim(f, rwid))
        except Exception as exc:
            fail = True
            lines.append(f"预约任务{rwid} 异常: {exc}")

    return ("error" if fail else "ok"), lines


def main() -> int:
    raw = os.getenv("HYKB_APP", "").strip()
    if not raw:
        log("未设置 HYKB_APP，退出")
        return 1
    accounts = parse_accounts(raw)
    if not accounts:
        log("HYKB_APP 解析不到账号，退出")
        return 1
    log(f"好游快爆爆米花农场每日任务，共 {len(accounts)} 个账号")
    fail = 0
    push_lines: List[str] = []
    for name, f in accounts:
        try:
            state, lines = run_account(name, f)
        except Exception as exc:
            state, lines = "error", [f"账号级异常: {exc}"]
        if state != "ok":
            fail += 1
        push_lines.extend(f"{name}: {x}" for x in lines)
        log(f"—— {name} [{state}] ——")
        for line in lines:
            log(f"  {line}")

    if os.getenv("QL_NOTIFY", "1") != "0":
        try:
            from send_notify import send_notify  # type: ignore

            send_notify("好游快爆农场每日任务", "\n".join(push_lines))
            log("已推送")
        except Exception as exc:
            log(f"推送不可用（{type(exc).__name__}），仅控制台输出")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
