#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# /*
# ------------------------------------------
# @Author: onijiang0
# @Date: 2026.09.21
# @Description: 翼支付(电信 bestpay) - 签到专区 authTokenLogin + SignInService.signIn
# cron: 22 15 * * *
# #定时使用10-19点 随机时间 每天
# ------------------------------------------
# 变量名：yzf
# 变量值：wx_server openid，多账号换行或 &，可加 #备注
#
# 依赖变量：
# wx_server_url    必填，取码地址（勿写进仓库）
# wx_auth          必填，取码鉴权
# yzf_phone        选填，翼支付手机号/productNo（查积分需要）
# YZF_SESSION      选填，登录失败时仅查询用 sessionKey
# yzf_appid        选填，默认 wx1c4a70bbdfaa2029
# YZF_SECRET       选填，网关签名密钥（默认读脚本/客户端内平台参数）
# QL_NOTIFY        选填，0 关闭推送
# ------------------------------------------
# 已实现：
# 1. 多账号；缺变量报错；单号失败不中断；不缓存 token
# 2. code/手机号凭证 → authTokenLogin（抓包唯一登录入口）
# 3. 签到链：signNewSwitch → querySignInConfigInfo → querySignInDateList
#          → SignInService.signIn → querySignInDateList 复核
# 4. send_notify 统一简报；openid/session 脱敏
#
# 契约（appid wx1c4a70bbdfaa2029 / 网关 FC1902C211615）：
# code     POST {wx_server_url}/wx/code /wx/getphonenumber
# 网关      POST https://spanner.bestpay.com.cn:10081
# 登录      com.bestpay.mbp.customer.facade.LoginFacade.authTokenLogin
#           event-context: tntId=0108 arNo=8901011101110001
#                         pdPath=appletAuthorize pdCd=01110110
# 门店回填  getIpRidAndIpTidByPhone
# 签到页    origin=https://render.bestpay.cn tntId=0101 env=PRD
# 签到      SignInService.signIn（2026-09-21 抓包实锤）
# 签名      md5(secretKey&Operation-Type=..&Request-Data=b64([data])&Ts=..)
# 加密      encryptType=2 + mgssdk（yzf_mgs_client.js）
# 判定      result-status 1000 成功 / 2000 session 过期
# 平台常量  ipTId=890120031556467340724507
#           ipRId签到=890110031555074150724502
#           ipRId商城=890810000365879820724509
#           authssucode登录=8901010699000117
#
# 踩坑：
# 1. 旧脚本猜的 appletAuthorizeLogin 抓包未出现；真实登录是 authTokenLogin
# 2. 签到必须调 SignInService.signIn，只查日历不会真签
# 3. sessionkey 过期 result-status=2000；可配 YZF_SESSION 仅查询
# 4. openid/session 只进环境变量；网关 appid/ipRId 等平台参数写默认值
# 5. 推送用 notify_and_format(title=, start_ts=)，勿传不存在的 cost_s
# ------------------------------------------
# */

from __future__ import annotations

import json
import os
import random
import re
import subprocess
import sys
import time
from typing import Any, Dict, List

try:
    from send_notify import notify_and_format, format_report
except Exception:
    def format_report(task, accounts, push_result="", cost_s=None):
        return task

    def notify_and_format(task, accounts, **kwargs):
        print("🔔 推送结果：跳过（send_notify 不可用）")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

APP_NAME = "翼支付"
APPID = (os.getenv("yzf_appid") or "wx1c4a70bbdfaa2029").strip()
UA = (
    "Mozilla/5.0 (Linux; Android 17; 2509FPN0BC Build/CP2A.260605.016) "
    f"AppleWebKit/537.36 MicroMessenger/8.0.76 miniProgram/{APPID}"
)


def _env_lines(name: str) -> List[str]:
    raw = os.environ.get(name, "") or ""
    out = []
    for part in raw.replace("&", "\n").splitlines():
        s = part.strip()
        if not s or s.startswith("#"):
            continue
        if "#" in s:
            s = s.split("#", 1)[0].strip()
        if s:
            out.append(s)
    return out


def sc_post(base: str, auth: str, path: str, body: dict) -> dict:
    import urllib.error
    import urllib.request

    url = base.rstrip("/") + path
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"auth": auth, "Content-Type": "application/json", "User-Agent": UA},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return json.loads(raw)
        except Exception:
            return {"status": False, "message": f"HTTP {e.code} {raw[:160]}"}
    except Exception as e:
        return {"status": False, "message": str(e)}


def code_service_material(openid: str) -> dict:
    """每次运行每 openid 只调 1 次 /wx/code。"""
    base = os.getenv("wx_server_url", "").strip().rstrip("/")
    auth = os.getenv("wx_auth", "").strip()
    if not base or not auth:
        raise RuntimeError("缺少 wx_server_url / wx_auth")
    ph = sc_post(base, auth, "/wx/getphonenumber", {"openid": openid, "appid": APPID})
    data_ph = ph.get("data") or {}
    raw = data_ph.get("raw") or {}
    mobile = ""
    raw_data = raw.get("data")
    if isinstance(raw_data, str):
        try:
            raw_data = json.loads(raw_data)
        except Exception:
            raw_data = {}
    if isinstance(raw_data, dict):
        mobile = raw_data.get("mobile") or raw_data.get("purePhoneNumber") or ""
    if not mobile:
        mobile = raw.get("mobile") or data_ph.get("mobile") or os.getenv("yzf_phone", "")
    time.sleep(0.5)
    cr = sc_post(base, auth, "/wx/code", {"openid": openid, "appid": APPID})
    code = ((cr.get("data") or {}).get("code") or "").strip()
    phone_code = (data_ph.get("code") or "").strip()
    return {
        "openid": openid,
        "appid": APPID,
        "wxCode": code or phone_code,
        "phoneCode": phone_code,
        "encryptedData": raw.get("encryptedData") or "",
        "iv": raw.get("iv") or "",
        "mobile": mobile,
    }


def run_node(payload: dict) -> dict:
    js_candidates = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "yzf_mgs_client.js"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "yzf_js", "yzf_mgs_client.js"),
    ]
    js = next((os.path.abspath(p) for p in js_candidates if os.path.isfile(p)), "")
    if not js:
        return {"ok": False, "err": "yzf_mgs_client.js not found"}
    node = os.environ.get("node") or os.environ.get("NODE") or "node"
    env = os.environ.copy()
    env["YZF_PAYLOAD"] = json.dumps(payload, ensure_ascii=False)
    if payload.get("sessionKey"):
        env["yzf"] = payload["sessionKey"]
        env["YZF_SESSION"] = payload["sessionKey"]
    try:
        proc = subprocess.run(
            [node, js],
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        return {"ok": False, "err": "node not found"}
    except Exception as e:
        return {"ok": False, "err": str(e)}
    out = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    data = None
    lines = []
    for line in out.splitlines():
        if line.startswith("DATA "):
            try:
                data = json.loads(line[5:])
            except Exception:
                pass
        elif line.strip():
            lines.append(line.strip())
    ok = proc.returncode == 0 and (data is not None or any("签到" in x or "✅" in x for x in lines))
    if isinstance(data, dict) and data.get("ok") is False:
        ok = False
    return {
        "ok": ok,
        "code": proc.returncode,
        "lines": lines,
        "data": data or {},
        "raw": out[-2500:],
    }


def mask_id(value: Any, keep: int = 6) -> str:
    s = str(value or "")
    return (s[:keep] + "***") if len(s) > keep else (s or "-")


def run_account(openid: str, index: int, total: int) -> Dict[str, Any]:
    extras: List[str] = [f"openid：{mask_id(openid, 6)}"]
    acc: Dict[str, Any] = {
        "account": f"账号{index}",
        "phone": "",
        "status": "-",
        "reward": "-",
        "extra": extras,
        "error": "",
        "success": False,
    }
    print(f"━━━━━━━━━━━━━━━━━━━━\n👤 账号 {index}/{total} {mask_id(openid, 6)}\n━━━━━━━━━━━━━━━━━━━━")
    try:
        material = code_service_material(openid)
        mobile = material.get("mobile") or os.getenv("yzf_phone", "")
        acc["phone"] = mobile
        extras.append(f"wxCode={'有' if material.get('wxCode') else '无'} phoneCode={'有' if material.get('phoneCode') else '无'}")
        print("🔐 使用 code 登录（authTokenLogin）")
        res = run_node(material)
        if (not res.get("ok")) or str(res.get("data", {}).get("signInRs", "")).startswith("2000"):
            session_fallback = os.getenv("YZF_SESSION", "").strip()
            if session_fallback:
                print("ℹ️ 登录未得到有效 session，回退 YZF_SESSION 查询/签到")
                extras.append("回退 YZF_SESSION")
                res = run_node({"sessionKey": session_fallback, "productNo": mobile})
    except Exception as e:
        msg = str(e)[:80]
        acc["status"] = f"失败 ❌ ({msg[:40]})"
        acc["error"] = msg
        print(f"❌ {msg}")
        return acc

    data = res.get("data") or {}
    body_lines = [x for x in (res.get("lines") or []) if x and not str(x).startswith("{")]
    for line in body_lines[:8]:
        print(f"ℹ️ {line[:120]}")
        extras.append(line[:80])

    sign_body = data.get("signIn")
    sign_rs = str(data.get("signInRs") or "")
    task_info = data.get("tasks") if isinstance(data.get("tasks"), dict) else {}
    browsed = task_info.get("browsed") or []
    awarded = task_info.get("awarded") or []
    if browsed:
        extras.append(f"浏览任务上报{len(browsed)}个")
    if awarded:
        extras.append(f"任务领奖尝试{len(awarded)}个")

    sign_ok = sign_rs.startswith("1000") or any("签到接口: 成功" in x for x in body_lines)
    if data.get("sessionExpired") or sign_rs.startswith("2000"):
        acc["status"] = "session 过期 ❌"
        acc["error"] = "result-status=2000 登录超时，更新 YZF_SESSION 后重试"
        print("⚠️ result-status=2000：sessionKey 过期，请刷新环境变量 YZF_SESSION")
        return acc

    if sign_ok or browsed or awarded:
        acc["success"] = bool(sign_ok) or any(
            str(x.get("rs") or "").startswith("1000") for x in list(browsed) + list(awarded)
        )
        parts = []
        if sign_ok:
            parts.append("签到完成 ✅")
            reward = "-"
            if isinstance(sign_body, dict):
                for k in ("integral", "points", "score", "amount", "redbagAmount"):
                    if sign_body.get(k) not in (None, "", 0, "0"):
                        reward = f"{k}+{sign_body.get(k)}"
                        break
                msg = sign_body.get("msg") or sign_body.get("memo") or ""
                if msg and reward == "-":
                    reward = str(msg)[:30]
            acc["reward"] = reward
        if browsed:
            parts.append(f"浏览任务{len(browsed)}个")
        if awarded:
            parts.append(f"领奖尝试{len(awarded)}个")
        if not acc["success"]:
            acc["status"] = "任务已执行但未确认成功 ⚠️"
            acc["error"] = "请看日志中的 rs="
        else:
            acc["status"] = " / ".join(parts)
        bag = data.get("redbag") if isinstance(data.get("redbag"), dict) else {}
        if bag.get("receivedRedbags") is not None:
            extras.append(f"红包已领{bag.get('receivedRedbags')}/{bag.get('redbagTotal')}")
        if acc.get("success") and acc.get("reward") in ("-", "", None) and browsed:
            acc["reward"] = "任务已执行"
        print(f"{'✅' if acc['success'] else '⚠️'} {acc['status']} {acc.get('reward')}")
        return acc

    if res.get("ok"):
        acc["success"] = True
        acc["status"] = "查询完成 ✅"
        acc["reward"] = "-"
        return acc

    err = res.get("err") or (body_lines[0] if body_lines else "未知错误")
    acc["status"] = f"失败 ❌ ({str(err)[:40]})"
    acc["error"] = str(err)[:80]
    print(f"❌ {acc['status']}")
    return acc


def main() -> int:
    started = time.time()
    accounts_raw = _env_lines("yzf")
    if not accounts_raw:
        print("❌ 未配置 yzf 环境变量（openid，多账号换行或 &）")
        return 1
    print(
        f"==============================\n"
        f"🛒 任务名称：{APP_NAME}签到专区\n"
        f"📌 签到 signIn + 浏览任务 sendTaskMessAge/receiveTaskAward\n"
        f"------------------------------"
    )
    accounts: List[Dict[str, Any]] = []
    for i, openid in enumerate(accounts_raw, 1):
        accounts.append(run_account(openid, i, len(accounts_raw)))
        if i < len(accounts_raw):
            time.sleep(random.randint(5, 12))
    ok_n = sum(1 for a in accounts if a.get("success"))
    print("------------------------------")
    for a in accounts:
        mark = "✅" if a.get("success") else "❌"
        print(f"{mark} {a.get('account')} | {a.get('status')} | {a.get('reward')}")
    print(f"------------------------------\n📊 成功 {ok_n}/{len(accounts_raw)}\n==============================")
    try:
        notify_and_format(
            f"{APP_NAME}签到专区",
            accounts,
            title=f"{APP_NAME}签到专区 {ok_n}/{len(accounts_raw)}",
            start_ts=started,
        )
    except Exception:
        print(format_report(f"{APP_NAME}签到专区", accounts, push_result="推送模块异常", cost_s=time.time() - started))
    return 0 if ok_n == len(accounts_raw) else 1


if __name__ == "__main__":
    sys.exit(main())
