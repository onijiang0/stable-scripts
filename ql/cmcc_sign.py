#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# /*
# ------------------------------------------
# @Author: onijiang0
# @Date: 2026.09.15
# @Description: 中国移动10086签到 - 小程序 SSO 全链路
# cron: 30 14 * * *
# ------------------------------------------
# 变量名：cmcc
# 变量值：取码服务里的 openid，多账号 & 或换行分隔
#
# 依赖：
#   wx_server_url  必填，取码服务地址（Base）
#   wx_auth        必填 调用 API AUTH（请求头 auth）
#   cmcc_execute   可选 0=只验登录链不签到；默认 1 执行签到
#   cmcc_delay_min / cmcc_delay_max  可选 账号间隔随机秒，默认 8~25
#   cmcc_yx / cmcc_touch_id 可选
#
# 链路（源码已核实）：
#   1 POST {wx_server_url}/wx/code  appid=wx43aab19a93a3a6f2
#   2 GET  https://wx.online-cmcc.cn/wmhnewcenter/wechat86-applet/login
#        header X-WX-Code + Lrsbhbg8 -> sessionId（encryptData 需 AES 解密）
#   3 POST https://wx.online-cmcc.cn/wmhnewcenter/wechat86-applet/wmhsso?redirectSource=SSO_YQS
#        header X-WECHAT86-APPLET-JWT=sessionId -> bean.token = wmhToken
#   4 GET  https://wx.10086.cn/qwhdhub/qwhdmark/{id}?...&wmhToken=
#        -> Set-Cookie QWHD_SESSION_TOKEN
#   5 POST https://wx.10086.cn/qwhdhub/api/mark/do/mark
# ------------------------------------------
# */

from __future__ import annotations

import json
import logging
import os
import random
import re
import ssl
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

logging.basicConfig(
    level=logging.DEBUG if os.getenv("CMCC_DEBUG") else logging.WARNING,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
log = logging.getLogger("CMCC")

MP_APPID = os.getenv("cmcc_mp_appid", "wx43aab19a93a3a6f2").strip()
MP_BASE = "https://wx.online-cmcc.cn"
H5_BASE = "https://wx.10086.cn"
ACTIVITY_ID = "1021122301"
MARK_URL = f"{H5_BASE}/qwhdhub/api/mark/do/mark"
PRIZE_URL = f"{H5_BASE}/qwhdhub/api/mark/info/prizeInfo"
X_CFG = "ZS93dUFVa2kzaEpQSjM0SG55MUFDdz09"
JWT_HDR = "X-WECHAT86-APPLET-JWT"
UA = (
    "Mozilla/5.0 (Linux; Android 17; 2509FPN0BC Build/CP2A.260605.016; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
    "Chrome/150.0.7871.189 Mobile Safari/537.36 XWEB/1500117 "
    "MMWEBSDK/20260502 MMWEBID/9885 "
    "MicroMessenger/8.0.76.3141(0x28004C31) WeChat/arm64 Weixin "
    "NetType/WIFI Language/zh_CN ABI/arm64 miniProgram/wx43aab19a93a3a6f2"
)
ALREADY_RE = re.compile(
    r"已签|已经签|签到过|重复|already|TODAY_MARKED|ALL_MARKED|全部签完|已经全部签",
    re.I,
)
CTX = ssl._create_unverified_context()


def jitter(lo: float, hi: float) -> float:
    """随机延时，打散请求时间，降低风控。"""
    a, b = lo, hi
    if a > b:
        a, b = b, a
    sec = random.uniform(a, b)
    time.sleep(sec)
    return sec
# 小程序 encryptData 双层 base64 + AES-128-CBC（源码 AesDecryptNew）
AES_KEY = b"1234123412ABCDEF"
AES_IV = b"ABCDEF1234123412"


def decrypt_payload(text: str) -> Any:
    """登录/SSO 若返回 encryptData 则解密为 JSON。"""
    j = parse_json(text)
    if not isinstance(j, dict):
        return j
    enc = j.get("encryptData")
    if not enc or not isinstance(enc, str):
        return j
    import base64 as b64mod
    node_bin = os.environ.get("MIMO_NODE") or ("node.exe" if os.name == "nt" else "node")
    script = r"""
const crypto=require("crypto");const fs=require("fs");
const enc=fs.readFileSync(0,"utf8").trim();
function dec2(data,key,iv){
  const layer1=Buffer.from(data,"base64");
  let ct;
  try{ ct=Buffer.from(layer1.toString("utf8"),"base64"); }catch(e){ ct=layer1; }
  const d=crypto.createDecipheriv("aes-128-cbc",Buffer.from(key),Buffer.from(iv));
  return Buffer.concat([d.update(ct),d.final()]).toString("utf8");
}
const keys=["1234123412ABCDEF","123456zxyxfwzxyn","zxyxfwzxyn123456","ABCDEF1234123412"];
const ivs=["ABCDEF1234123412","zxyxfwzxyn123456","123456zxyxfwzxyn","1234123412ABCDEF"];
for(const k of keys)for(const iv of ivs){
  try{
    const out=dec2(enc,k,iv);
    if(out.includes("{")){ process.stdout.write(out); process.exit(0); }
  }catch(e){}
}
process.exit(2);
"""
    try:
        p = subprocess.run(
            [node_bin, "-e", script],
            input=enc.encode("utf-8"),
            capture_output=True,
            timeout=15,
        )
        raw = p.stdout.decode("utf-8", "replace").strip()
        if not raw:
            log.warning("decrypt 空输出 stderr=%s", p.stderr.decode("utf-8", "replace")[:160])
            return j
        log.debug("decrypt head=%s", raw[:160].replace("\n", " "))
        dec_j = parse_json(raw)
        return dec_j if dec_j is not None else raw
    except Exception as e:
        log.warning("encryptData 解密失败: %s", e)
        return j


def mask(o: str) -> str:
    o = str(o or "")
    return o[:8] + "***" if len(o) > 8 else o


def split_openids(raw: str) -> List[str]:
    return [
        p.strip()
        for p in raw.replace("&", "\n").replace(",", "\n").splitlines()
        if p.strip() and not p.strip().startswith("#")
    ]


def http_request(
    method: str,
    url: str,
    headers: Optional[Dict[str, str]] = None,
    body: Optional[dict] = None,
    timeout: int = 20,
    prefer_curl: bool = False,
) -> Tuple[int, Dict[str, List[str]], str]:
    """返回 (status, set_cookie_list, body_text)。TLS 失败时自动 curl 回退。"""
    h = {"User-Agent": UA, "Accept": "*/*"}
    if headers:
        h.update(headers)
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        h.setdefault("Content-Type", "application/json;charset=UTF-8")

    def via_curl() -> Tuple[int, Dict[str, List[str]], str]:
        cmd = ["curl.exe" if os.name == "nt" else "curl", "-sk", "-D", "-", "-o", "-", "-X", method, url,
               "--max-time", str(timeout)]
        for k, v in h.items():
            cmd += ["-H", f"{k}: {v}"]
        if data is not None:
            cmd += ["--data-binary", data.decode("utf-8")]
        try:
            p = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
            raw = p.stdout.decode("utf-8", "replace")
            if not raw:
                return 0, {}, p.stderr.decode("utf-8", "replace")[:300]
            parts = raw.split("\r\n\r\n", 1)
            if len(parts) == 1:
                parts = raw.split("\n\n", 1)
            head, body_s = (parts + [""])[:2]
            status = 0
            sc: List[str] = []
            for line in head.splitlines():
                if line.startswith("HTTP/"):
                    try:
                        status = int(line.split()[1])
                    except Exception:
                        pass
                if line.lower().startswith("set-cookie:"):
                    sc.append(line.split(":", 1)[1].strip())
            return status, {"Set-Cookie": sc}, body_s
        except Exception as e:
            return 0, {}, f"curl_error:{e}"

    if prefer_curl or os.getenv("CMCC_FORCE_CURL"):
        return via_curl()

    req = Request(url, data=data, headers=h, method=method)
    try:
        with urlopen(req, timeout=timeout, context=CTX) as resp:
            body_s = resp.read().decode("utf-8", "replace")
            sc = resp.headers.get_all("Set-Cookie") or []
            return resp.status, {"Set-Cookie": sc}, body_s
    except HTTPError as e:
        body_s = e.read().decode("utf-8", "replace") if e.fp else ""
        sc = e.headers.get_all("Set-Cookie") if e.headers else []
        return e.code, {"Set-Cookie": sc or []}, body_s
    except Exception as e:
        if "SSL" in type(e).__name__ or "handshake" in str(e).lower() or "timed out" in str(e).lower():
            log.debug("urllib 失败(%s)，改用 curl", e)
            return via_curl()
        return 0, {}, f"{type(e).__name__}:{e}"


def parse_json(text: str) -> Any:
    try:
        return json.loads(text)
    except Exception:
        return None


def dig_session(data: Any, key: str) -> str:
    if not isinstance(data, dict):
        return ""
    if data.get(key):
        return str(data[key])
    for k in ("data", "object", "result", "bean", "payload"):
        v = data.get(k)
        if isinstance(v, dict):
            got = dig_session(v, key)
            if got:
                return got
        elif isinstance(v, str) and key == "token" and v:
            return v
    return ""


class CodeService:
    def __init__(self, base: str, auth: str):
        self.base = base.rstrip("/")
        self.auth = auth

    def accounts(self) -> List[Dict[str, Any]]:
        st, _, body = http_request(
            "GET", f"{self.base}/api/accounts",
            headers={"auth": self.auth, "X-Client-Version": "1.1.5"},
        )
        j = parse_json(body) or {}
        if not j.get("status"):
            raise RuntimeError(f"accounts 失败: {j.get('message')} http={st}")
        return (j.get("data") or {}).get("items") or []

    def wx_code(self, openid: str, appid: str) -> str:
        st, _, body = http_request(
            "POST", f"{self.base}/wx/code",
            headers={"auth": self.auth, "X-Client-Version": "1.1.5"},
            body={"openid": openid, "appid": appid},
        )
        j = parse_json(body) or {}
        if not j.get("status"):
            raise RuntimeError(f"/wx/code 失败: {j.get('message')} http={st} {str(j)[:120]}")
        code = ((j.get("data") or {}).get("code") or "").strip()
        if not code:
            raise RuntimeError(f"/wx/code 无 code: {str(j)[:150]}")
        return code


def acquire_mark_session(base_url: str, openid: str, yx: str, touch_id: str) -> Dict[str, str]:
    """code -> login -> wmhsso -> user/info -> cookies"""
    sm_base = os.getenv("wx_server_url", "").strip()
    sm_auth = os.getenv("wx_auth", "").strip()
    if not sm_base or not sm_auth:
        raise RuntimeError("缺少 wx_server_url / wx_auth")
    sm = CodeService(sm_base, sm_auth)

    log.info("登录 openid=%s", mask(openid))
    code = sm.wx_code(openid, MP_APPID)
    log.debug("code len=%d", len(code))
    jitter(0.8, 2.5)

    st, sc, body = http_request(
        "GET", f"{MP_BASE}/wmhnewcenter/wechat86-applet/login",
        headers={"X-WX-Code": code, "Lrsbhbg8": X_CFG},
        prefer_curl=True,
    )
    j = decrypt_payload(body) if isinstance(body, str) else body
    if not isinstance(j, dict):
        j = parse_json(body) or {}
    session_id = dig_session(j, "sessionId")
    if not session_id and isinstance(j, dict) and isinstance(j.get("data"), dict):
        session_id = dig_session(j["data"], "sessionId")
    if not session_id:
        raise RuntimeError(f"login 失败 http={st}")
    jitter(0.8, 2.5)

    st2, _, body2 = http_request(
        "POST", f"{MP_BASE}/wmhnewcenter/wechat86-applet/wmhsso?redirectSource=SSO_YQS",
        headers={JWT_HDR: session_id, "Lrsbhbg8": X_CFG},
        body={},
        prefer_curl=True,
    )
    j2 = decrypt_payload(body2) if isinstance(body2, str) else body2
    if not isinstance(j2, dict):
        j2 = parse_json(body2) or {}
    wmh = dig_session(j2, "token")
    if not wmh and isinstance(j2, dict) and isinstance(j2.get("data"), dict):
        wmh = dig_session(j2["data"], "token")
    if not wmh:
        raise RuntimeError(f"wmhsso 失败 http={st2}")
    jitter(1.0, 3.0)

    referer = (
        f"{H5_BASE}/qwhdhub/qwhdmark/{ACTIVITY_ID}"
        f"?ys=&yx={yx}&touch_id={touch_id}&wmhToken={wmh}"
    )
    st_page, sc_page, body_page = http_request(
        "GET", referer,
        headers={
            "Host": "wx.10086.cn",
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Referer": f"{H5_BASE}/",
        },
        prefer_curl=True,
    )
    cookies: Dict[str, str] = {
        "qwhd_center_router": "hua",
        "yx": yx,
        "touch_id": touch_id,
    }
    blob_page = "\n".join(sc_page.get("Set-Cookie", [])) + "\n" + (body_page or "")
    for k in ("QWHD_SESSION_TOKEN", "d.sid", "shareToken"):
        m = re.search(rf"{re.escape(k)}=([^;\s\"']+)", blob_page)
        if m:
            cookies[k] = m.group(1)
    if "QWHD_SESSION_TOKEN" in cookies:
        log.info("会话 OK")
        return cookies

    st3, sc3, body3 = http_request(
        "POST", f"{H5_BASE}/qwhdhub/api/mark/user/info?_traceId={int(time.time()*1000)}_1",
        headers={
            "Host": "wx.10086.cn",
            "Content-Type": "application/json;charset=UTF-8",
            "Origin": H5_BASE,
            "Referer": referer,
            "x-requested-with": "XMLHttpRequest",
            "login-check": "1",
            "Cookie": cookie_header(cookies),
        },
        body={"appVersion": "", "miniVersion": ""},
        prefer_curl=True,
    )
    blob = "\n".join(sc3.get("Set-Cookie", [])) + "\n" + body3
    for k in ("QWHD_SESSION_TOKEN", "d.sid", "shareToken"):
        m = re.search(rf"{re.escape(k)}=([^;\s\"']+)", blob)
        if m:
            cookies[k] = m.group(1)
    if "QWHD_SESSION_TOKEN" not in cookies:
        raise RuntimeError("未获取到 QWHD_SESSION_TOKEN")
    log.info("会话 OK")
    return cookies


def cookie_header(cm: Dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cm.items())


PRIZE_KEYS = (
    "prizeName", "prize_name", "rewardName", "reward_name",
    "awardName", "award_name", "giftName", "gift_name",
    "prizeDesc", "prize_desc", "rewardDesc", "awardDesc",
    "todayPrize", "today_prize", "currentPrize", "markedPrize",
    "packageName", "package_name", "flowName", "dataName",
    "title", "name",
)


def _collect_prize_names(obj: Any, out: List[str], depth: int = 0) -> None:
    if depth > 5 or len(out) >= 6:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            kl = str(k)
            if kl.lower() in {x.lower() for x in PRIZE_KEYS} or kl in (
                "prizeName", "prizeList", "rewardList", "awardList",
                "todayPrize", "prizeInfo", "rewards", "prizes",
            ):
                if isinstance(v, str) and v.strip() and len(v) <= 80:
                    if v.strip() not in out:
                        out.append(v.strip())
                elif isinstance(v, (dict, list)):
                    _collect_prize_names(v, out, depth + 1)
            elif kl.lower() in ("prizelist", "rewardlist", "awardlist", "list", "items", "data", "prize"):
                _collect_prize_names(v, out, depth + 1)
    elif isinstance(obj, list):
        for it in obj[:8]:
            _collect_prize_names(it, out, depth + 1)


def extract_prize_summary(*payloads: Any) -> str:
    """从 prizeInfo / mark 返回中提取奖励文案，供日志打印。"""
    names: List[str] = []
    for p in payloads:
        if not p:
            continue
        _collect_prize_names(p, names)
    # 过滤明显不是奖品的通用词
    skip = {"成功", "ok", "OK", "null", "none", "未签到", "已签到"}
    names = [n for n in names if n and n not in skip]
    # 去重保序
    seen = set()
    uniq = []
    for n in names:
        if n not in seen:
            seen.add(n)
            uniq.append(n)
    return "、".join(uniq[:4])


def interpret_sign(raw: Dict[str, Any], prize_hint: str = "") -> Tuple[bool, str]:
    if not raw or not isinstance(raw, dict):
        return False, "未知返回"
    code = str(raw.get("code") or "")
    msg = str(raw.get("msg") or raw.get("message") or "").strip()
    success = raw.get("success") is True
    is_done = (
        bool(ALREADY_RE.search(msg))
        or code in ("TODAY_MARKED", "ALL_MARKED")
        or "全部签完" in msg
        or "已经全部" in msg
    )
    prize = extract_prize_summary(raw.get("data"), raw) or prize_hint
    if success or code in ("SUCCESS", "0") or is_done:
        if is_done:
            label = msg or "今日已签到/已签完"
        else:
            label = msg or "签到成功"
        if prize:
            label += f" | 奖励: {prize}"
        return True, label
    if prize:
        return False, (msg or f"code={code}") + f" | 奖励: {prize}"
    return False, msg or f"code={code}"


def do_mark(cm: Dict[str, str], yx: str, touch_id: str) -> Dict[str, Any]:
    referer = f"{H5_BASE}/qwhdhub/qwhdmark/{ACTIVITY_ID}?ys=&yx={yx}&touch_id={touch_id}#/"
    st, _, body = http_request(
        "POST", MARK_URL,
        headers={
            "Host": "wx.10086.cn",
            "Content-Type": "application/json;charset=UTF-8",
            "Origin": H5_BASE,
            "Referer": referer,
            "x-requested-with": "XMLHttpRequest",
            "login-check": "1",
            "Cookie": cookie_header(cm),
        },
        body={},
        prefer_curl=True,
    )
    log.debug("mark body=%s", body[:160])
    return parse_json(body) or {}


def prize_info(cm: Dict[str, str], yx: str, touch_id: str) -> Dict[str, Any]:
    referer = f"{H5_BASE}/qwhdhub/qwhdmark/{ACTIVITY_ID}?ys=&yx={yx}&touch_id={touch_id}#/"
    st, _, body = http_request(
        "POST", PRIZE_URL,
        headers={
            "Host": "wx.10086.cn",
            "Content-Type": "application/json;charset=UTF-8",
            "Origin": H5_BASE,
            "Referer": referer,
            "x-requested-with": "XMLHttpRequest",
            "login-check": "1",
            "Cookie": cookie_header(cm),
        },
        body={},
        prefer_curl=True,
    )
    return parse_json(body) or {}


def main() -> int:
    started = time.time()
    yx = os.getenv("cmcc_yx", "JHT042591F0005").strip()
    touch_id = os.getenv("cmcc_touch_id", "26-05-10005-2007-A01").strip()
    _ex = os.getenv("cmcc_execute")
    execute = True if _ex is None or _ex.strip() == "" else _ex.strip() not in ("0", "false", "no")
    openids_raw = os.getenv("cmcc", "").strip()
    base = os.getenv("wx_server_url", "").strip()
    auth = os.getenv("wx_auth", "").strip()
    if not base or not auth:
        log.error("缺少 wx_server_url / wx_auth")
        return 1

    sm = CodeService(base, auth)
    if openids_raw:
        openids = split_openids(openids_raw)
    else:
        try:
            openids = [
                a["openid"] for a in sm.accounts() if a.get("openid") and not a.get("disabled")
            ]
        except Exception as e:
            log.error("读账号失败: %s", e)
            return 1
    if not openids:
        log.error("无 openid")
        return 1

    delay_min = float(os.getenv("cmcc_delay_min", "8") or 8)
    delay_max = float(os.getenv("cmcc_delay_max", "25") or 25)

    mode = "签到" if execute else "DRY-RUN"
    print(f"中国移动10086 | {mode} | {len(openids)}账号")

    accounts: List[Dict[str, Any]] = []
    for idx, oid in enumerate(openids):
        if idx > 0:
            jitter(delay_min, delay_max)
        acc: Dict[str, Any] = {
            "account": mask(oid),
            "phone": "",
            "status": "-",
            "reward": "-",
            "month_days": None,
            "extra": [],
            "error": "",
            "success": False,
        }
        extras: List[str] = []
        try:
            cm = acquire_mark_session(base, oid, yx, touch_id)
            jitter(1.5, 5.0)
            prize_hint = ""
            try:
                info = prize_info(cm, yx, touch_id)
                bd = info.get("data") or {}
                prize_hint = extract_prize_summary(info)
                mt = bd.get("markedTimes")
                tt = bd.get("totalMarkTimes")
                tm = bd.get("todayMarked")
                if mt is not None:
                    try:
                        acc["month_days"] = int(mt)
                    except Exception:
                        pass
                if mt is not None or tm is not None:
                    extras.append(f"进度 {mt}/{tt} 今日已签={tm}")
            except Exception:
                pass

            if not execute:
                acc["status"] = "登录链 OK（未执行签到）✅"
                acc["success"] = True
                if prize_hint:
                    acc["reward"] = prize_hint
            else:
                raw = do_mark(cm, yx, touch_id)
                ok, msg = interpret_sign(raw, prize_hint=prize_hint)
                acc["status"] = (msg or "签到完成") + (" ✅" if ok else " ❌")
                acc["success"] = bool(ok)
                if prize_hint:
                    acc["reward"] = prize_hint
                if not ok:
                    acc["error"] = msg
        except Exception as e:
            acc["status"] = f"异常 ❌ ({str(e)[:60]})"
            acc["error"] = str(e)[:80]
        acc["extra"] = extras
        accounts.append(acc)

    ok_n = sum(1 for a in accounts if a.get("success"))
    try:
        from send_notify import notify_and_format

        notify_and_format(
            "中国移动10086",
            accounts,
            title=f"中国移动10086签到 {ok_n}/{len(accounts)}",
            start_ts=started,
        )
    except Exception as _ne:
        print("[notify] 使用旧格式:", _ne)
        _report = "\n".join(
            f"{'✅' if a.get('success') else '❌'} [{a.get('account')}] {a.get('status')}" for a in accounts
        )
        print(_report)
        try:
            from send_notify import send_notify

            send_notify(f"中国移动10086签到 {ok_n}/{len(accounts)}", _report)
        except Exception as _ne2:
            print("[notify] 跳过:", _ne2)
    return 0 if ok_n == len(accounts) else 1


if __name__ == "__main__":
    sys.exit(main())
