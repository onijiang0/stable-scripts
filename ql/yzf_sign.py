#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# /*
# ------------------------------------------
# @Description: 翼支付(电信 bestpay) - 小程序签到专区/绿色能量（smallcat 换 code）
# cron: 22 15 * * *
# ------------------------------------------
# 变量名：yzf
# 变量值：wx_server 里的 openid，多账号用 & 或换行分隔（可加 #备注）
#
# 依赖变量：
# wx_server_url    必填，wx_server 地址（勿写进仓库）
# wx_auth          必填，wx_server 鉴权值（/wx/code、/wx/getphonenumber 用）
# yzf_appid        可选，默认 wx1c4a70bbdfaa2029
# YZF_SECRET       必填，spanner 签名密钥（勿写进仓库）
# YZF_MGSSDK       可选，mgssdk.dec.js 绝对路径
# YZF_IPTID / YZF_IPRID  可选
# YZF_SESSION      可选，登录失败时仅查询的 sessionKey
# QL_NOTIFY        可选，设为 0 关闭推送
#
# 契约（appid wx1c4a70bbdfaa2029 / 网关 appid FC1902C211615）：
# smallcat  POST {wx_server_url}/wx/getphonenumber  auth:{wx_auth} json:{openid,appid}
#           -> data.code + data.raw{encryptedData,iv,data.mobile}
# smallcat  POST {wx_server_url}/wx/code  auth:{wx_auth} json:{openid,appid}
#           -> data.code（wx.login code）
# 登录      spanner MGS:
#           com.bestpay.mobile.service.account.prd.event.AppletAuthorizeEvent.appletAuthorizeLogin
#           com.bestpay.mobile.service.account.prd.event.AppletAuthorizeEvent.authorizeCodeAuth
#           （td 头：tntId=0108 arNo=8901011101110001 pdPath=appletAuthorize pdCd=01110110）
#           需手机号凭证 + 授权来源appId + 授权id + 业务渠道 + 授权来源
# 网关      POST https://spanner.bestpay.com.cn:10081
# 签名      md5(secretKey & Operation-Type=...&Request-Data=base64(JSON.stringify([data]))&Ts=...)
# 加密      encryptType=2，由 yzf_mgs_client.js + mgssdk WASM 完成
# 进页自动签：signNewSwitch + queryPageConfig + queryRedbagList 等
# ------------------------------------------
# */

from __future__ import annotations

import json
import logging
import os
import random
import subprocess
import sys
import time
from typing import Any, Dict, List

try:
    from send_notify import send_notify
except Exception:
    send_notify = None

logging.basicConfig(
    level=logging.DEBUG if os.getenv("yzf_debug") else logging.WARNING,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
log = logging.getLogger("YZF")

APP_NAME = "翼支付"
APPID = (os.getenv("yzf_appid") or "wx1c4a70bbdfaa2029").strip()
UA = (
    "Mozilla/5.0 (Linux; Android 17; 2509FPN0BC Build/CP2A.260605.016) "
    "AppleWebKit/537.36 MicroMessenger/8.0.76 miniProgram/wx1c4a70bbdfaa2029"
)


def _env_lines(name: str) -> list[str]:
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


def smallcat_material(openid: str) -> dict:
    """每 openid 每次运行只调 1 次 /wx/code；getphonenumber 按需。"""
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
        mobile = raw.get("mobile") or data_ph.get("mobile") or ""
    time.sleep(1)
    cr = sc_post(base, auth, "/wx/code", {"openid": openid, "appid": APPID})
    code = ((cr.get("data") or {}).get("code") or "").strip()
    phone_code = (data_ph.get("code") or "").strip()
    if not code and not phone_code:
        raise RuntimeError(f"/wx/code 失败: {cr.get('message') or '无 code'}")
    return {
        "openid": openid,
        "appid": APPID,
        "wxCode": code or phone_code,
        "phoneCode": phone_code,
        "encryptedData": raw.get("encryptedData") or "",
        "iv": raw.get("iv") or "",
        "mobile": mobile,
        "cloudId": raw.get("cloud_id") or "",
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
    # login mode when smallcat material present
    if payload.get("wxCode") or payload.get("phoneCode"):
        env["YZF_LOGIN"] = "1"
    if payload.get("sessionKey"):
        env["yzf"] = payload["sessionKey"]
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
    return {
        "ok": proc.returncode == 0 and (data is not None or any("✅" in x for x in lines)),
        "code": proc.returncode,
        "lines": lines,
        "data": data or {},
        "raw": out[-2500:],
    }


def main() -> int:
    started = time.time()
    accounts_raw = _env_lines("yzf")
    if not accounts_raw:
        print("未配置 yzf（wx_server openid）")
        return 1
    accounts: List[Dict[str, Any]] = []
    ok_n = 0
    for i, openid in enumerate(accounts_raw):
        name = f"账号{i + 1}"
        acc: Dict[str, Any] = {
            "account": name,
            "phone": "",
            "status": "-",
            "reward": "-",
            "extra": [f"openid {openid[:12]}…"],
            "error": "",
            "success": False,
        }
        try:
            material = smallcat_material(openid)
            mobile = material.get("mobile") or ""
            acc["phone"] = mobile
            acc["extra"].append(
                f"wxCode={'有' if material.get('wxCode') else '无'} "
                f"phoneCode={'有' if material.get('phoneCode') else '无'}"
            )
        except Exception as e:
            acc["status"] = f"smallcat 失败 ❌ ({str(e)[:50]})"
            acc["error"] = str(e)[:80]
            accounts.append(acc)
            continue
        res = run_node(material)
        session_fallback = os.getenv("YZF_SESSION", "").strip()
        if (not res.get("ok")) and session_fallback:
            acc["extra"].append("登录未成功，回退 YZF_SESSION 查询")
            res = run_node({"sessionKey": session_fallback, "productNo": os.getenv("yzf_phone", "")})
        if res.get("ok"):
            ok_n += 1
            acc["success"] = True
            acc["status"] = "登录/查询完成 ✅"
            data = res.get("data") or {}
            if data.get("sessionKey"):
                acc["extra"].append("已获 session")
            body_lines = [x for x in (res.get("lines") or []) if x and not str(x).startswith("{") and "session" not in x.lower()]
            acc["extra"].extend(body_lines[:6])
        else:
            err = res.get("err") or "\n".join((res.get("lines") or [])[:3]) or "未知错误"
            acc["status"] = f"失败 ❌ ({str(err)[:50]})"
            acc["error"] = str(err)[:80]
        accounts.append(acc)
        if i < len(accounts_raw) - 1:
            time.sleep(random.randint(8, 20))

    try:
        from send_notify import notify_and_format

        notify_and_format(
            "翼支付签到专区",
            accounts,
            title=f"翼支付签到专区 {ok_n}/{len(accounts_raw)}",
            start_ts=started,
        )
    except Exception:
        try:
            from send_notify import format_report, send_notify

            body = format_report(
                "翼支付签到专区",
                accounts,
                push_result="",
                cost_s=time.time() - started,
            )
            pr = send_notify(f"翼支付签到专区 {ok_n}/{len(accounts_raw)}", body)
            print(format_report("翼支付签到专区", accounts, push_result=pr, cost_s=time.time() - started))
        except Exception as ne:
            print("[notify] 跳过:", ne)
            for a in accounts:
                print(f"{'✅' if a.get('success') else '❌'} [{a.get('account')}] {a.get('status')}")
    return 0 if ok_n == len(accounts_raw) else 1


if __name__ == "__main__":
    sys.exit(main())
