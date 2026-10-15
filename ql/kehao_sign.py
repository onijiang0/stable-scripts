#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# /*
# ------------------------------------------
# @Author: onijiang0
# @Date: 2026.10.14
# @Description: 可好便利店(达达 Dmall) 每日签到 - wx.login code 静默登录 + 签到
# cron: 25 17 * * *
# #定时使用10-19点 随机时间 每天
# ------------------------------------------
# 变量名：kehao
# 变量值：wx_server 里的 openid，多账号换行或 & 分隔，可加 #备注
#
# 依赖变量：
# wx_server_url  必填，取码服务地址（使用者自备，勿写进仓库）
# wx_auth        必填，取码服务鉴权
# QL_NOTIFY      选填，0 关闭推送
# KEHAO_DRY_RUN  选填，1 只查状态不签到
# KEHAO_TASK     选填，0 关闭每日浏览任务（默认开）
# ------------------------------------------
# 已实现：
# 1. 多账号；缺变量报错；单号失败不中断
# 2. /wx/code 拿 code -> loginInSilenceWithoutPhone 静默登录（免手机号）
# 3. memberInfoNew 取 userId/openId/积分；checkIn 签到
# 4. **每日浏览任务**：page 取 tasks[] -> acceptTask -> 等 browseSeconds -> finishTask
#    -> receiveReward（实测 2 个任务，各「浏览15S得5积分」）
# 5. bzKey 自生成（设备指纹混淆串，已逆验精确复现）
# 6. send_notify 统一简报；openid/token 脱敏
#
# 契约（appid wx74ccada2e896dd46，达达 Dmall 体系，uniquecode=kehao_wx）：
# 取码   POST {wx_server_url}/wx/code  json:{openid, appid} -> data.code
# 登录   POST https://passport.dmall-os.cn/wechatApplet/loginInSilenceWithoutPhone
#        form: code & appletType=17 & deviceId & dmallStoreId= & eventCode=wxmini_click
#              & params_json=<track json> & utmSource=
#        头 dmtenantid:8 + Referer + UA
#        -> data.token / data.ticketName / data.ticketLoginId
# 会员   POST https://weixinapp.dmall-os.cn/member/memberInfoNew
#        form: param=<json>（**无需 p 参数**，实测通过）
#        -> data.userInfo.{userId, openId, phone} / data.points
# 签到   GET  https://sign-in.dmall-os.cn/v2/checkIn?param=<json>&d_track_data=<json>
#        -> code=0000 成功（data.todayRewards/consecutiveDays）；1007=今日已签到
# 任务页 GET  .../v2/page?param=<json>&d_track_data=<json>
#        param 需多带 "signMonth":""；data 是布局块数组（layoutType 2/3/4/5），
#        layoutType=4 的 tasks[] 给 signTaskId / title / browseSeconds / rewardDesc
# 任务   GET  .../v2/acceptTask    ?param=<json+sigTaskConfigId>&d_track_data=
#        GET  .../v2/finishTask    ?param=<json+sigTaskConfigId>&d_track_data=
#        GET  .../v2/receiveReward ?param=<json+sigTaskConfigId>&d_track_data=
#        sigTaskConfigId = page 里 tasks[].signTaskId（不是 signConfigId/taskId）
#        -> code=0000 即成功
# 业务头 ticketname / token / uniquecode:kehao_wx / channel:miniprograms /
#        platform:43 / applettype:17 / scene:vip / v:v6.5.0 / appversion:6.5.0 /
#        venderid:37121 / storeid:570081 / graystoreid / grayvenderid /
#        storegroupkey / bzkey / dmtenantid:8 / clienttype:1 / mixflag:1 /
#        networktype:0 / userid / openid / businesscode:1 / originbusinessformat:1
# 业务码 0000=成功；1007=今日已签到（幂等，当成功）
#
# 踩坑：
# 1. bzKey 是「设备指纹 JSON → base64 → 反转 → 右旋 20」的混淆串，头与 param 两版
#    （param 版多一个 deviceId 字段）；已离线逆验，两版都能精确复现
# 2. 签到 GET **不需要 p 参数**；p 只在部分 POST（如 memberInfoNew）出现，实测可省
# 3. 取码请求必须带微信 UA，否则取码服务返回 403
# 4. 取码服务有短时风控，失败**不要重试**，等下一轮
# ------------------------------------------
# */

from __future__ import annotations

import base64
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from send_notify import notify_and_format  # noqa: E402


APP_ID = "wx74ccada2e896dd46"
UNIQUECODE = "kehao_wx"
VENDER_ID = 37121
STORE_ID = "570081"
STORE_GROUP_KEY = "3ff845961f96af2a96ea37bd61e2063c@MS01NzAwODEtMzcxMjE"
VERSION = "6.5.0"
TIMEOUT = 25

CK_NAME = "kehao"
DRY_RUN = os.getenv("KEHAO_DRY_RUN") == "1"
QL_NOTIFY_ON = os.getenv("QL_NOTIFY") != "0"

WX_SERVER_URL = (os.getenv("wx_server_url") or "").strip().rstrip("/")
WX_AUTH = (os.getenv("wx_auth") or "").strip()

LOGIN_URL = "https://passport.dmall-os.cn/wechatApplet/loginInSilenceWithoutPhone"
MEMBER_URL = "https://weixinapp.dmall-os.cn/member/memberInfoNew"
CHECKIN_URL = "https://sign-in.dmall-os.cn/v2/checkIn"
POINT_URL = "https://sign-in.dmall-os.cn/v2/queryUserPoint"
PAGE_URL = "https://sign-in.dmall-os.cn/v2/page"
TASK_URL_TPL = "https://sign-in.dmall-os.cn/v2/%s"  # acceptTask / finishTask / receiveReward

# 浏览任务开关（默认开；设 KEHAO_TASK=0 关闭）
TASK_ON = os.getenv("KEHAO_TASK") != "0"

ALREADY_CODES = {"1007"}
ALREADY_MSG_RE = re.compile(r"已签|已经签|签到过|重复|勿重复", re.I)

UA = (
    "Mozilla/5.0 (Linux; Android 17; 2509FPN0BC Build/CP2A.260605.016; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/150.0.7871.189 "
    "Mobile Safari/537.36 XWEB/1500145 MMWEBSDK/20260502 MMWEBID/9885 "
    "MicroMessenger/8.0.76.3141(0x28004C31) WeChat/arm64 Weixin NetType/WIFI "
    "Language/zh_CN ABI/arm64 MiniProgramEnv/android"
)
REFERER = f"https://servicewechat.com/{APP_ID}/58/page-frame.html"


def mask(v: Any, keep: int = 4) -> str:
    s = str(v or "")
    if not s:
        return "(空)"
    if len(s) <= keep * 2:
        return s[:2] + "***"
    return s[:keep] + "***" + s[-keep:]


def mask_phone(phone: Any) -> str:
    d = re.sub(r"\D", "", str(phone or ""))
    if len(d) >= 11:
        return d[:3] + "****" + d[-4:]
    return mask(phone)


def split_accounts(value: str = "") -> List[str]:
    return [s.strip() for s in re.split(r"\n|&", value or "") if s.strip()]


def parse_account(raw: str) -> Dict[str, str]:
    parts = [p.strip() for p in raw.strip().split("#")]
    return {
        "openid": parts[0] if parts else "",
        "remark": parts[1] if len(parts) > 1 else "",
    }


def make_device_id() -> str:
    """设备指纹：CBD88BA1 + 30 位数字（沿用抓包同一形态）。"""
    return "CBD88BA1" + str(int(time.time() * 1000)) + "0" * 17


def enc_bzkey(obj: dict) -> str:
    """bzKey 混淆：json -> base64 -> 反转 -> 右旋 20。"""
    s = base64.b64encode(json.dumps(obj, separators=(",", ":")).encode()).decode()
    rev = s[::-1]
    k = 20
    return rev[-k:] + rev[:-k]


def bzkey_header(device_id: str) -> str:
    return enc_bzkey({
        "uuid": device_id, "lat": "", "lng": "", "latitude": "", "longitude": "",
        "deliveryLat": "", "deliveryLng": "", "areaId": "",
    })


def bzkey_param(device_id: str) -> str:
    return enc_bzkey({
        "uuid": device_id, "deviceId": device_id, "lat": "", "lng": "",
        "latitude": "", "longitude": "", "deliveryLat": "", "deliveryLng": "",
        "areaId": "",
    })


def http(method: str, url: str, headers: Dict[str, str],
         form: Optional[List[tuple]] = None) -> Dict[str, Any]:
    data = None
    h = dict(headers)
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        h["content-type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            txt = r.read().decode("utf-8", "replace")
            status = r.status
    except urllib.error.HTTPError as e:
        txt = e.read().decode("utf-8", "replace")
        status = e.code
    try:
        return {"status": status, "json": json.loads(txt)}
    except Exception:
        return {"status": status, "raw": txt[:300]}


class KehaoTask:
    def __init__(self, account: str, index: int):
        self.index = index
        a = parse_account(account)
        self.openid = a["openid"]
        self.remark = a["remark"] or f"账号{index}"
        self.device_id = make_device_id()

    def fetch_code(self) -> str:
        body = json.dumps({"openid": self.openid, "appid": APP_ID}).encode()
        req = urllib.request.Request(
            WX_SERVER_URL + "/wx/code", data=body, method="POST",
            headers={"auth": WX_AUTH, "Content-Type": "application/json", "User-Agent": UA},
        )
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                d = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"/wx/code HTTP{e.code}: {e.read().decode()[:150]}")
        if not d.get("status"):
            raise RuntimeError(f"/wx/code 失败: {d.get('message')}")
        code = (d.get("data") or {}).get("code") or d.get("code")
        if not code:
            raise RuntimeError("取码响应缺少 code")
        return code

    def _track(self) -> str:
        return json.dumps({
            "session_id": "CBDA10B20E30000237825806182B175E1" + str(int(time.time())),
            "project": "kehao_mini", "tdc": "", "tpc": "",
            "uuid": self.device_id, "env": "minip",
        }, separators=(",", ":"))

    def login(self, code: str) -> Dict[str, str]:
        r = http("POST", LOGIN_URL, {
            "charset": "utf-8", "dmtenantid": "8",
            "Referer": REFERER, "User-Agent": UA,
        }, form=[
            ("code", code), ("appletType", "17"), ("deviceId", self.device_id),
            ("dmallStoreId", ""), ("eventCode", "wxmini_click"),
            ("params_json", json.dumps({"d_track_data": json.loads(self._track())},
                                       separators=(",", ":"))),
            ("utmSource", ""),
        ])
        j = r.get("json") or {}
        data = j.get("data") or {}
        if not data.get("token") or not data.get("ticketName"):
            raise RuntimeError(f"登录失败: {json.dumps(j, ensure_ascii=False)[:200]}")
        return {
            "token": data["token"],
            "ticket": data["ticketName"],
            "loginId": data.get("ticketLoginId") or "",
        }

    def _headers(self, login: Dict[str, str], extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        h = {
            "charset": "utf-8", "ticketname": login["ticket"], "clienttype": "1",
            "uniquecode": UNIQUECODE, "channel": "miniprograms", "User-Agent": UA,
            "appversion": VERSION, "networktype": "0", "areacode": "", "platform": "43",
            "token": login["token"], "applettype": "17", "scene": "vip", "areaid": "",
            "v": "v" + VERSION, "mixflag": "1", "bzkey": bzkey_header(self.device_id),
            "dmtenantid": "8", "Referer": REFERER,
        }
        if extra:
            h.update(extra)
        return h

    def _param(self, login: Dict[str, str], extra: Optional[dict] = None) -> str:
        p = {
            "terminal": "devtools", "platform": "43", "channel": "miniprograms",
            "loginId": login["loginId"], "deviceId": self.device_id,
            "v": "v" + VERSION, "appVersion": VERSION,
            "venderId": VENDER_ID, "vendorId": VENDER_ID, "source": "43",
            "bzKey": bzkey_param(self.device_id),
        }
        if extra:
            p.update(extra)
        return json.dumps(p, separators=(",", ":"))

    def member_info(self, login: Dict[str, str]) -> Dict[str, Any]:
        r = http("POST", MEMBER_URL, self._headers(login),
                 form=[("param", self._param(login, {"clientType": 1, "appletType": "17"}))])
        j = r.get("json") or {}
        ui = (j.get("data") or {}).get("userInfo") or {}
        return {
            "userId": str(ui.get("userId") or ""),
            "openId": ui.get("openId") or "",
            "phone": ui.get("phone") or "",
            "points": (j.get("data") or {}).get("points"),
            "raw": j,
        }

    def checkin(self, login: Dict[str, str], user: Dict[str, Any]) -> Dict[str, Any]:
        h = self._headers(login, {
            "businesscode": "1", "storeid": STORE_ID, "graystoreid": STORE_ID,
            "grayvenderid": str(VENDER_ID), "storegroupkey": STORE_GROUP_KEY,
            "originbusinessformat": "1", "openid": user.get("openId") or "",
            "venderid": str(VENDER_ID),
        })
        if user.get("userId"):
            h["userid"] = user["userId"]
        q = urllib.parse.urlencode({
            "param": self._param(login),
            "d_track_data": self._track(),
        })
        r = http("GET", CHECKIN_URL + "?" + q, h)
        j = r.get("json") or {}
        code = str(j.get("code") or "")
        msg = j.get("msg") or ""
        if code == "0000":
            d = j.get("data") or {}
            rewards = d.get("todayRewards") or []
            reward = ""
            for it in rewards:
                nm = it.get("name") or it.get("typeName") or ""
                cnt = it.get("count")
                if nm:
                    reward = f"{nm}x{cnt}" if cnt else nm
                    break
            return {"ok": True, "reward": reward,
                    "days": d.get("consecutiveDays"), "raw": j}
        if code in ALREADY_CODES or ALREADY_MSG_RE.search(msg):
            return {"ok": True, "already": True, "msg": msg, "raw": j}
        return {"ok": False, "code": code, "msg": msg, "raw": j}

    def query_points(self, login: Dict[str, str], user: Dict[str, Any]) -> Optional[int]:
        h = self._headers(login, {"businesscode": "1", "openid": user.get("openId") or "",
                                  "venderid": str(VENDER_ID)})
        if user.get("userId"):
            h["userid"] = user["userId"]
        q = urllib.parse.urlencode({"param": self._param(login),
                                    "d_track_data": self._track()})
        r = http("GET", POINT_URL + "?" + q, h)
        j = r.get("json") or {}
        d = j.get("data")
        if isinstance(d, dict):
            for k in ("point", "points", "totalPoint", "availablePoint"):
                if d.get(k) is not None:
                    return d[k]
            return None
        if isinstance(d, (int, str)) and str(d).isdigit():
            return int(d)
        return None

    # ---------- 浏览任务 ----------

    def _biz_headers(self, login: Dict[str, str], user: Dict[str, Any]) -> Dict[str, str]:
        h = self._headers(login, {
            "businesscode": "1", "openid": user.get("openId") or "",
            "venderid": str(VENDER_ID), "storeid": STORE_ID, "graystoreid": STORE_ID,
            "grayvenderid": str(VENDER_ID), "storegroupkey": STORE_GROUP_KEY,
        })
        if user.get("userId"):
            h["userid"] = user["userId"]
        return h

    def page_tasks(self, login: Dict[str, str], user: Dict[str, Any]) -> List[Dict[str, Any]]:
        """取签到页任务列表（layoutType=4 块里的 tasks[]）。"""
        q = urllib.parse.urlencode({
            "param": self._param(login, {"signMonth": ""}),
            "d_track_data": self._track(),
        })
        r = http("GET", PAGE_URL + "?" + q, self._biz_headers(login, user))
        j = r.get("json") or {}
        if str(j.get("code")) != "0000":
            raise RuntimeError(f"page 失败: code={j.get('code')} msg={j.get('msg')}")
        tasks: List[Dict[str, Any]] = []
        for blk in j.get("data") or []:
            if isinstance(blk, dict) and blk.get("tasks"):
                tasks.extend([t for t in blk["tasks"] if isinstance(t, dict)])
        return tasks

    def _task_call(self, ep: str, login: Dict[str, str], user: Dict[str, Any],
                   task_id: Any) -> bool:
        q = urllib.parse.urlencode({
            "param": self._param(login, {"sigTaskConfigId": task_id}),
            "d_track_data": self._track(),
        })
        r = http("GET", (TASK_URL_TPL % ep) + "?" + q, self._biz_headers(login, user))
        j = r.get("json") or {}
        return str(j.get("code")) == "0000"

    def do_tasks(self, login: Dict[str, str], user: Dict[str, Any]) -> Dict[str, Any]:
        """逐个任务：接任务 -> 等 browseSeconds 秒 -> 完成 -> 领奖。"""
        tasks = self.page_tasks(login, user)
        done, points, lines = 0, 0, []
        for t in tasks:
            tid = t.get("signTaskId")
            title = t.get("title") or f"任务{tid}"
            if tid is None:
                continue
            rd = t.get("rewardDesc") or {}
            m = re.search(r"\d+", str(rd.get("value") or ""))
            task_pts = int(m.group()) if m else 0
            wait = t.get("browseSeconds")
            try:
                wait = int(wait) if wait is not None else 15
            except Exception:
                wait = 15
            if wait < 0 or wait > 120:
                wait = 15
            try:
                # acceptTask 失败 = 今日已做过 / 不可重复，直接跳过（省掉浏览等待）
                if not self._task_call("acceptTask", login, user, tid):
                    lines.append(f"{title} 今日已做")
                    continue
                time.sleep(wait)  # 浏览时长要求
                fin_ok = self._task_call("finishTask", login, user, tid)
                rec_ok = self._task_call("receiveReward", login, user, tid)
                if fin_ok and rec_ok:
                    done += 1
                    points += task_pts
                    lines.append(f"{title} ✅" + (f"+{task_pts}分" if task_pts else ""))
                else:
                    lines.append(f"{title} ⚠️(完成{int(fin_ok)} 领奖{int(rec_ok)})")
            except Exception as e:
                lines.append(f"{title} ❌ {e}")
        return {"total": len(tasks), "done": done, "points": points, "lines": lines}

    def run(self) -> Dict[str, Any]:
        acc = {
            "account": self.remark, "phone": "", "status": "❌ 失败",
            "reward": "", "month_days": "",
            "extra": [f"openid={mask(self.openid)}"],
        }
        try:
            if not self.openid:
                raise RuntimeError("openid 为空")

            code = self.fetch_code()
            login = self.login(code)
            user = self.member_info(login)
            acc["phone"] = user.get("phone") or ""
            acc["extra"].append(f"userId={mask(user.get('userId'))}")

            if DRY_RUN:
                tasks = self.page_tasks(login, user)
                acc["status"] = "🧪 DRY-RUN（未签到 / 未做任务）"
                acc["extra"].append(f"待做任务 {len(tasks)} 个")
                return acc

            state = self.checkin(login, user)
            if state.get("ok"):
                if state.get("already"):
                    acc["status"] = "✅ 今日已签到"
                    acc["extra"].append(f"msg={state.get('msg')}")
                else:
                    acc["status"] = "✅ 签到成功"
                    acc["reward"] = state.get("reward") or ""
                    if state.get("days"):
                        acc["extra"].append(f"连签 {state.get('days')} 天")
            else:
                acc["status"] = "⚠️ 未完成"
                acc["extra"].append(f"签到 code={state.get('code')} msg={state.get('msg')}")

            # 每日浏览任务
            if TASK_ON:
                try:
                    tk = self.do_tasks(login, user)
                    pts_s = f" +{tk['points']}分" if tk.get("points") else ""
                    acc["extra"].append(f"浏览任务 {tk['done']}/{tk['total']} 完成{pts_s}")
                    for ln in tk["lines"]:
                        acc["extra"].append(f"  {ln}")
                    if tk["done"] and not acc["reward"]:
                        acc["reward"] = f"任务{tk['points']}分" if tk.get("points") else "任务完成"
                except Exception as e:
                    acc["extra"].append(f"浏览任务异常: {e}")

            # 任务后再查一次积分，反映最新值
            pts = self.query_points(login, user)
            if pts is None:
                pts = user.get("points")
            if pts is not None:
                acc["extra"].append(f"当前积分 {pts}")
        except Exception as e:
            acc["status"] = "❌ 失败"
            acc["extra"].append(f"异常: {e}")
        return acc


def main() -> int:
    start = time.time()
    raw = os.getenv(CK_NAME, "")
    if not raw.strip():
        print(f"未找到变量 {CK_NAME}")
        if QL_NOTIFY_ON:
            notify_and_format("可好便利店 签到", [], title="kehao 未配置账号")
        return 0
    if not WX_SERVER_URL or not WX_AUTH:
        print("缺少 wx_server_url 或 wx_auth")
        if QL_NOTIFY_ON:
            notify_and_format("可好便利店 签到", [], title="kehao 未配置取码服务")
        return 0

    accounts = split_accounts(raw)
    print(f"共 {len(accounts)} 个账号")

    results: List[Dict[str, Any]] = []
    for i, a in enumerate(accounts, 1):
        results.append(KehaoTask(a, i).run())
        if i < len(accounts):
            time.sleep(1.5)

    notify_and_format("可好便利店 签到", results, start_ts=start)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"脚本异常: {e}")
        if QL_NOTIFY_ON:
            notify_and_format("可好便利店 签到", [], title="kehao 脚本异常")
        sys.exit(1)
