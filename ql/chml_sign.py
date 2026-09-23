#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# /*
# ------------------------------------------
# @Author: onijiang0
# @Date: 2026.09.23
# @Description: 长虹美菱会员服务 - 小程序每日签到（聚合活动）
# cron: 33 18 * * *
# #定时使用10-19点 随机时间 每天
# ------------------------------------------
# 变量名：chml
# 变量值：微信 openid，多账号换行或 & 分隔，可加 #备注
#
# 依赖变量：
# wx_server_url  必填，取码服务地址（使用者自备，勿写进仓库）
# wx_auth        必填，取码服务鉴权
# chml_aggr_id   选填，活动 id；留空则自动从「我的活动」发现当前进行中的签到活动
# chmlck         选填，抓包 token（&/换行分隔）；填了则跳过 code 登录
# QL_NOTIFY      选填，0 关闭推送
# ------------------------------------------
# 契约（appid wx36c3413e8fe39263，hongke.changhong.com/gw）★ 2026-09-23 实测：
#
# 取码  POST {wx_server_url}/wx/code  header auth  json {openid,appid}
# 登录  POST /gw/applet/appletUser/getTokenByJsCode
#       body {"jsCode":"<code>"}   ← ★ jsCode 必须在**顶层**，
#       嵌套进 userInfo 会报「微信登录失败：map为空或者openid为空！」
#       resp data.token（长度约 167）
# 头    token / smarthome 都填同一个 token；content-type: application/json
#
# 活动列表 GET /gw/applet/mine/getMyActivity        （★只支持 GET，POST 报 405）
#          → data.records[] {name,startTime,endTime,status,linkAddress}
#            status "1"=进行中 / "2"=已结束
#            linkAddress "/pages/arger/arger?activityData=<aggrId>,<uid>,10"
#            → 第一段即 aggrId
# 活动详情 GET /gw/applet/aggr/aggregationInfo?aggrId=<id>
#          → data.status=1 / isCan=1 / startTime / endTime
#            data.aggrAssemblyList[].aggrAssemblySignin.signinRuleList[]
#              ruleType=3 每日签到（每天 +rewardNum 分，isSignin 标记该日）
#              ruleType=1 连签里程碑（dayNum 天 +rewardNum 分）
# 可签校验 GET /gw/applet/aggr/aggregationCheck?aggrId=<id>
#          → data.isCan=1 可签 / prompt 为不可签原因
# 签到    POST /gw/applet/aggr/signin?aggrId=<id>   body {}
#          → code 200 成功；code 400 + message「已签到」= 今天签过了（视为成功）
#
# 踩坑：
# 1. ★ 别用 /gw/applet/signin/signin —— 那是另一套独立签到，实测返回
#    「签到配置没有启用」。真正的入口是 /gw/applet/aggr/signin（聚合活动）。
# 2. ★ 域名是 hongke.changhong.com/gw/applet/**，不是 gateway.mymlsoft.com。
#    api/urls.js 把所有接口统一拼成 https://hongke.changhong.com/gw/{接口}；
#    gateway.mymlsoft.com/gateway/mluser 只是 request.js 里不会命中的兜底分支，
#    打过去全 500，极易误判成「服务已下线」。
# 3. chmlck / openid 等账号信息不进 Git；日志脱敏。
# 4. ★★「网络不可达」的真相是**容器里的 IPv6 残废**，不是域名解析不了、也不是服务挂了。
#    面板容器是 musl（Alpine），musl 的 getaddrinfo 在未限定 family 时会**同时查
#    A 和 AAAA**；该网络没有 IPv6 出口，AAAA 查询必然卡满超时 → [Errno -3] Try again，
#    requests 整体失败并包装成「网络不可达」（极具误导性）。
#    实测（面板容器内，同一时刻同域名）：
#        getaddrinfo(host, port)               → FAIL 5.01s [Errno -3]
#        getaddrinfo(host, port, AF_INET, ...) → OK   0.004s ×10/10  → 119.3.183.149
#        getaddrinfo(host, port, AF_INET6,...) → FAIL 5.01s [Errno -3]   ← 真凶
#        requests 原始           → FAIL 5.01s
#        requests + 强制 IPv4    → OK   HTTP 400 0.15s
#        curl（happy-eyeballs）  → 400 但 2.13s（先等 AAAA 才回退）；curl -4 → 0.12s
#    本脚本把 socket.getaddrinfo 包一层强制 AF_INET 根治，**不硬编码 IP**，
#    厂商换 IP 也自动跟随（旧的 CHML_HOST_IP 硬编码兜底已废弃）。
#    辨识特征：失败耗时**稳定≈5s**（DNS 超时）而非 30s（连接超时）。
# ------------------------------------------
# */

from __future__ import annotations

import json
import os
import re
import socket
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import requests
import urllib3

urllib3.disable_warnings()

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    from send_notify import clean_line, format_report, mask_id, notify_and_format
except Exception:
    def clean_line(line: Any) -> str:
        s = str(line or "").strip()
        return "" if s.startswith(("{", "[")) else s[:180]

    def mask_id(value: Any, keep: int = 6) -> str:
        s = str(value or "")
        return (s[:keep] + "***") if len(s) > keep else (s or "-")

    def format_report(task, accounts, push_result="", cost_s=None):
        return task

    def notify_and_format(task, accounts, **kwargs):
        print("🔔 推送结果：跳过（send_notify 不可用）")


APP_NAME = "长虹美菱"
APPID = "wx36c3413e8fe39263"
HOST = "hongke.changhong.com"
BASE_URL = f"https://{HOST}/gw"
REQUEST_TIMEOUT = 30

# ── 强制 IPv4 解析 ──────────────────────────────────────────────
# 面板容器是 musl（Alpine），且容器内**没有 IPv6 出口**。
# musl 的 getaddrinfo 在未限定 family 时会**同时查 A 和 AAAA**，
# 而 AAAA 查询在这个网络里必然卡满超时 → [Errno -3] Try again，
# requests/urllib 因此整体失败，被包装成「网络不可达」。
# 实测（2026-09-23 面板容器内，同一时刻）：
#   getaddrinfo(host, port)                → FAIL 5.01s [Errno -3]
#   getaddrinfo(host, port, AF_INET, ...)  → OK   0.004s ×10/10
#   getaddrinfo(host, port, AF_INET6, ...) → FAIL 5.01s [Errno -3]  ← 真凶
#   requests 原始            → FAIL 5.01s
#   requests + 强制 IPv4     → OK   HTTP 400 0.15s
#   curl（happy-eyeballs）   → 400 但 2.13s（先等 AAAA 才回退）
#   curl -4                  → 400 0.12s
# 故把 socket.getaddrinfo 包一层强制 AF_INET。**不需要硬编码 IP**，
# 域名换 IP 也自动跟随；比 XXX_HOST_IP 兜底更干净、更根本。
_orig_getaddrinfo = socket.getaddrinfo


def _getaddrinfo_ipv4(host, port, family=0, type=0, proto=0, flags=0):
    return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)


socket.getaddrinfo = _getaddrinfo_ipv4
# ────────────────────────────────────────────────────────────────

UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5_1 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.50 NetType/WIFI Language/zh_CN"
)

# 活动 id：优先取环境变量，否则自动发现
FIXED_AGGR_ID = os.getenv("chml_aggr_id", "").strip()


def say(msg: str) -> None:
    line = clean_line(msg)
    if line:
        print(line)


def split_ids(raw: str) -> List[str]:
    out: List[str] = []
    for part in (raw or "").replace("&", "\n").splitlines():
        s = part.strip()
        if not s or s.startswith("#"):
            continue
        out.append(s.split("#")[0].strip())
    return out


def get_env(*names: str) -> str:
    for n in names:
        v = os.getenv(n, "").strip()
        if v:
            return v
    return ""


def api(method: str, path: str, *, body: Any = None,
        headers: Optional[Dict[str, str]] = None) -> Tuple[bool, Any, str]:
    url = BASE_URL + path
    hdrs = {
        "content-type": "application/json",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "User-Agent": UA,
    }
    if headers:
        hdrs.update(headers)

    last_err = ""
    for attempt in range(3):
        try:
            r = requests.request(
                method, url,
                data=json.dumps(body) if body is not None else None,
                headers=hdrs, timeout=REQUEST_TIMEOUT, verify=False,
            )
            try:
                return True, r.json(), ""
            except Exception:
                return False, None, f"非 JSON 响应 HTTP {r.status_code}: {r.text[:200]}"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
    return False, None, last_err


def get_code(openid: str) -> Tuple[Optional[str], str]:
    svc = get_env("wx_server_url", "WX_SERVER_URL")
    auth = get_env("wx_auth", "WX_AUTH")
    if not svc:
        return None, "缺变量 wx_server_url"
    if not auth:
        return None, "缺变量 wx_auth"
    try:
        r = requests.post(
            svc.rstrip("/") + "/wx/code",
            json={"openid": openid, "appid": APPID},
            headers={"auth": auth, "User-Agent": UA},
            timeout=REQUEST_TIMEOUT, verify=False,
        )
        j = r.json()
    except Exception as e:
        return None, f"取码请求异常 {type(e).__name__}"
    if not j.get("status"):
        return None, f"取码失败: {j.get('message') or j.get('error') or '未知'}"
    code = ((j.get("data") or {}).get("code") or "").strip()
    if not code:
        return None, "取码返回空 code"
    return code, ""


def login_by_code(code: str) -> Tuple[Optional[str], str]:
    ok, j, err = api("POST", "/applet/appletUser/getTokenByJsCode",
                     body={"jsCode": code})
    if not ok:
        return None, f"登录请求失败 {err}"
    if str(j.get("code")) != "200":
        return None, f"登录失败: {j.get('message') or j.get('code')}"
    token = ((j.get("data") or {}).get("token") or "").strip()
    if not token:
        return None, "登录未返回 token"
    return token, ""


def discover_aggr_id(auth: Dict[str, str]) -> Tuple[Optional[str], str]:
    """从「我的活动」里找当前进行中的签到活动，返回 aggrId。

    linkAddress: /pages/arger/arger?activityData=<aggrId>,<uid>,10
    status: "1"=进行中
    """
    ok, j, err = api("GET", "/applet/mine/getMyActivity", headers=auth)
    if not ok:
        return None, f"活动列表请求失败 {err}"
    if str(j.get("code")) != "200":
        return None, f"活动列表异常: {j.get('message') or j.get('code')}"
    records = ((j.get("data") or {}).get("records") or [])
    now = datetime.now()
    candidates: List[Tuple[int, str, str]] = []
    for rec in records:
        link = str(rec.get("linkAddress") or "")
        m = re.search(r"activityData=([^,&]+)", link)
        if not m:
            continue
        aggr_id = m.group(1)
        name = str(rec.get("name") or "")
        status = str(rec.get("status") or "")
        # 名字里带「签到」优先
        score = 0
        if "签到" in name:
            score += 10
        if status == "1":
            score += 5
        try:
            st = datetime.strptime(str(rec.get("startTime")), "%Y-%m-%d %H:%M:%S")
            et = datetime.strptime(str(rec.get("endTime")), "%Y-%m-%d %H:%M:%S")
            if st <= now <= et:
                score += 8
        except Exception:
            pass
        candidates.append((score, aggr_id, name))
    if not candidates:
        return None, "未在「我的活动」里找到带 activityData 的活动"
    candidates.sort(key=lambda x: -x[0])
    score, aggr_id, name = candidates[0]
    say(f"🔎 自动发现活动：{name}（aggrId={aggr_id}）")
    return aggr_id, ""


def check_signable(auth: Dict[str, str], aggr_id: str) -> Tuple[bool, str]:
    ok, j, err = api("GET", f"/applet/aggr/aggregationCheck?aggrId={aggr_id}",
                     headers=auth)
    if not ok:
        return False, f"校验失败 {err}"
    data = j.get("data") or {}
    if str(j.get("code")) == "200" and str(data.get("isCan")) in ("1", "True", "true"):
        return True, ""
    return False, str(data.get("prompt") or j.get("message") or "当前不可签到")


def do_sign(auth: Dict[str, str], aggr_id: str) -> Tuple[bool, str]:
    ok, j, err = api("POST", f"/applet/aggr/signin?aggrId={aggr_id}",
                     body={}, headers=auth)
    if not ok:
        return False, f"签到请求失败 {err}"
    code = str(j.get("code"))
    msg = str(j.get("message") or "")
    if code == "200":
        return True, msg or "签到成功"
    if code == "400" and ("已签到" in msg or "已经签到" in msg):
        return True, "今天已签到"
    if code == "401" or "登录" in msg:
        return False, f"token 失效: {msg}"
    return False, f"{code}: {msg}"


def get_point(auth: Dict[str, str]) -> Optional[int]:
    ok, j, err = api("GET", "/applet/homePage/getUserPoint", headers=auth)
    if ok and str(j.get("code")) == "200":
        try:
            return int(j.get("data"))
        except Exception:
            return None
    return None


def run_account(openid: str, idx: int) -> Dict[str, Any]:
    tag = f"[账号{idx}]"
    result: Dict[str, Any] = {"name": f"{tag} {mask_id(openid)}", "ok": False, "msg": ""}

    token = get_env("chmlck")
    tokens = split_ids(token) if token else []

    if len(tokens) >= idx:
        tk = tokens[idx - 1]
        say(f"{tag} 使用环境变量 chmlck 中的 token")
    else:
        code, err = get_code(openid)
        if not code:
            say(f"{tag} ❌ {err}")
            result["msg"] = err
            return result
        say(f"{tag} 取码成功")
        tk, err = login_by_code(code)
        if not tk:
            say(f"{tag} ❌ {err}")
            result["msg"] = err
            return result
        say(f"{tag} 登录成功 token len={len(tk)}")

    auth = {"token": tk, "smarthome": tk}

    aggr_id = FIXED_AGGR_ID
    if not aggr_id:
        aggr_id, err = discover_aggr_id(auth)
        if not aggr_id:
            say(f"{tag} ❌ {err}")
            result["msg"] = err
            return result

    before = get_point(auth)

    can, why = check_signable(auth, aggr_id)
    if not can:
        say(f"{tag} ⏭️ {why}")
        result["ok"] = True
        result["msg"] = why
        return result

    ok, msg = do_sign(auth, aggr_id)
    after = get_point(auth)

    if ok:
        delta = ""
        if before is not None and after is not None and after != before:
            delta = f"（积分 {before}→{after}）"
        say(f"{tag} ✅ {msg}{delta}")
        result["ok"] = True
        result["msg"] = f"{msg}{delta}"
    else:
        say(f"{tag} ❌ {msg}")
        result["msg"] = msg
    return result


def main() -> int:
    raw = get_env("chml", "CHML_OPENIDS")
    openids = split_ids(raw)
    if not openids:
        say("❌ 缺变量 chml（微信 openid，多账号换行或 & 分隔）")
        return 1

    say(f"📮 任务名称：{APP_NAME} 会员签到")
    say(f"👥 共 {len(openids)} 个账号")
    start = time.time()

    accounts: List[Dict[str, Any]] = []
    for i, oid in enumerate(openids, 1):
        try:
            accounts.append(run_account(oid, i))
        except Exception as e:
            say(f"[账号{i}] ❌ 异常 {type(e).__name__}: {e}")
            accounts.append({"name": f"[账号{i}] {mask_id(oid)}",
                             "ok": False, "msg": f"{type(e).__name__}"})
        if i < len(openids):
            time.sleep(1)

    cost = time.time() - start
    say(f"⏱️ 执行耗时：{int(cost)} 秒")
    task = f"{APP_NAME} 会员签到"
    try:
        notify_and_format(task, [a["name"] + " " + a["msg"] for a in accounts],
                          cost_s=int(cost))
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
