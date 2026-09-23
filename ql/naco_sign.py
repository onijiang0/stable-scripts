#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# /*
# ------------------------------------------
# @Author: onijiang0
# @Date: 2026.09.23
# @Description: NACO旗舰店（有赞微商城）每日签到
# cron: 45 17 * * *
# #定时使用10-19点 随机时间 每天
# ------------------------------------------
# 变量名：naco
# 变量值：wx_server 里的 openid，多账号换行或 & 分隔，可加 #备注
#
# 依赖变量：
# wx_server_url  必填，取码服务地址（使用者自备，勿写进仓库）
# wx_auth        必填，取码服务鉴权
# QL_NOTIFY      选填，0 关闭推送
# NACO_DRY_RUN   选填，1 只查状态不签到
# ------------------------------------------
# 已实现：
# 1. 多账号；缺变量报错；单号失败不中断
# 2. openid → /wx/code → uic.youzan.com auth.json 换 accessToken+sessionId
# 3. check-in-info.json 运行时发现 checkInId（活动/场次按月换，勿写死）
# 4. get_activity_by_yzuid_v2.json 查 isCheckin，已签则跳过提交（幂等）
# 5. checkinV2.json 签到；find_checkin_info_by_month.json 复核本月已签天数
# 6. send_notify 统一简报；openid/token/sid/uuid 脱敏
#
# 契约（appid wxf7eb51e2639162f9，有赞，kdt_id=41131244）：
# 取码     POST {wx_server_url}/wx/code   auth:{wx_auth} json:{openid,appid}
# 登录     POST https://uic.youzan.com/passport/general/auth.json?app_id=&kdt_id=
#          body {appId,code,platformName:weapp,signature:android,clientId,
#                grantType:yz_union,inWsc:true,kdtId,extraBizData}
#          头 extra-data / page-path / Referer
#          → {sessionId,accessToken,refreshToken,userId,mobile,openId,unionId}
# 发现     GET /wscump/checkin/check-in-info.json        -> data.checkInId
# 状态     GET /wscump/checkin/get_activity_by_yzuid_v2.json?checkinId=<id>
#          -> data.isCheckin / continuesDay / cycleTimes
# 签到     GET /wscump/checkin/checkinV2.json?checkinId=<id>   （大写 V2）
#          -> code=0 且 data.success=true 即成功
# 复核     GET /wscump/checkin/find_checkin_info_by_month.json?checkin_id=<id>&year&month
#          -> data.checkin_date 数组（毫秒时间戳）
# 业务请求公共：query app_id/kdt_id/access_token；
#          头 extra-data {is_weapp,sid,version,client,bizEnv,uuid,ftime}
# 业务码：HTTP 200 与 code=0 是两回事；"已签到"当成功（幂等）
#
# 踩坑：
# 1. signature 固定 "android"（平台标识，非哈希）；clientId=md5(uuid)[:18]
# 2. sessionId 即 extra-data.sid（KDTWEAPPSESSIONID）
# 3. access_token 走 URL query，sid 走 extra-data 头
# 4. 签到接口是 checkinV2.json 大写 V2
# 5. checkInId 会按月/活动换，务必运行时从 check-in-info.json 发现
# 6. 用 urllib/axios；Node fetch 会因自动附加头被拒（见逆向手册 3.2）
# 7. 别用 br 编码（无 brotli 依赖会乱码），accept-encoding 用 gzip, deflate
# ------------------------------------------
# */

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from send_notify import notify_and_format  # noqa: E402


APP_ID = "wxf7eb51e2639162f9"
KDT_ID = "41131244"
BASE = "https://h5.youzan.com"
AUTH_BASE = "https://uic.youzan.com"
VERSION = "2.250.7"
TIMEOUT = 25

CK_NAME = "naco"
DRY_RUN = os.getenv("NACO_DRY_RUN") == "1"
QL_NOTIFY_ON = os.getenv("QL_NOTIFY") != "0"

WX_SERVER_URL = (os.getenv("wx_server_url") or "").strip().rstrip("/")
WX_AUTH = (os.getenv("wx_auth") or "").strip()

# 有赞签到业务码：已签到视为成功
ALREADY_CODES = {"1000030071"}
ALREADY_MSG_RE = re.compile(r"已签|已经签|签到过|重复|already", re.I)

# 微信内置浏览器 UA（抓包实测）
UA = (
    "Mozilla/5.0 (Linux; Android 17; 2509FPN0BC Build/CP2A.260605.016; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/150.0.7871.189 "
    "Mobile Safari/537.36 XWEB/1500135 MMWEBSDK/20260502 MMWEBID/9885 "
    "MicroMessenger/8.0.76.3141(0x28004C31) WeChat/arm64 Weixin NetType/WIFI "
    "Language/zh_CN ABI/arm64 MiniProgramEnv/android"
)
REFERER = f"https://servicewechat.com/{APP_ID}/152/page-frame.html"


def mask(v: Any, keep: int = 4) -> str:
    s = str(v or "")
    if not s:
        return "(空)"
    if len(s) <= keep * 2:
        return s[:2] + "***"
    return s[:keep] + "***" + s[-keep:]


def split_accounts(value: str = "") -> List[str]:
    return [s.strip() for s in re.split(r"\n|&", value or "") if s.strip()]


def parse_account(raw: str) -> Dict[str, str]:
    text = raw.strip()
    parts = [p.strip() for p in text.split("#")]
    openid = parts[0] if parts else ""
    remark = parts[1] if len(parts) > 1 else ""
    return {"openid": openid, "remark": remark}


class NacoTask:
    def __init__(self, account: str, index: int):
        self.index = index
        a = parse_account(account)
        self.openid = a["openid"]
        self.remark = a["remark"] or f"账号{index}"

    # ---------- 取码 + 登录 ----------

    def fetch_code(self) -> str:
        if not WX_SERVER_URL:
            raise RuntimeError("缺少 wx_server_url")
        if not WX_AUTH:
            raise RuntimeError("缺少 wx_auth")
        body = json.dumps({"openid": self.openid, "appid": APP_ID}).encode()
        req = urllib.request.Request(
            WX_SERVER_URL + "/wx/code",
            data=body,
            method="POST",
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

    def login(self, code: str, uuid_: str) -> Dict[str, str]:
        url = f"{AUTH_BASE}/passport/general/auth.json?app_id={APP_ID}&kdt_id={KDT_ID}"
        client_id = hashlib.md5(uuid_.encode()).hexdigest()[:18]
        extra = json.dumps(
            {
                "sid": "",
                "version": VERSION,
                "clientType": "weapp-miniprogram",
                "client": "weapp",
                "bizEnv": "wsc",
                "uuid": uuid_,
                "ftime": int(time.time() * 1000),
            },
            separators=(",", ":"),
        )
        body = json.dumps(
            {
                "appId": APP_ID,
                "code": code,
                "platformName": "weapp",
                "signature": "android",
                "clientId": client_id,
                "grantType": "yz_union",
                "inWsc": True,
                "kdtId": int(KDT_ID),
                "extraBizData": {
                    "enterOptions": {
                        "extKdtId": int(KDT_ID),
                        "path": "pages/home/dashboard/index",
                        "query": {},
                        "scene": 1089,
                        "referrerInfo": {},
                        "sessionId": "host=&version=671108145&device=2",
                        "mode": "default",
                        "apiCategory": "default",
                    },
                    "guideBizDataMap": {"from_params": ""},
                    "sceneData": {},
                },
            },
            separators=(",", ":"),
        ).encode()
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "User-Agent": UA,
                "Content-Type": "application/json",
                "charset": "utf-8",
                "extra-data": extra,
                "page-path": "pages/home/dashboard/index",
                "Referer": REFERER,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                d = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"auth.json HTTP{e.code}: {e.read().decode()[:200]}")
        data = d.get("data") if isinstance(d.get("data"), dict) else d
        token = data.get("accessToken") or ""
        sid = data.get("sessionId") or ""
        if not token or not sid:
            raise RuntimeError(f"auth.json 缺字段: {json.dumps(d, ensure_ascii=False)[:250]}")
        return {
            "token": token,
            "sid": sid,
            "userId": str(data.get("userId") or ""),
            "mobile": data.get("mobile") or "",
            "openId": data.get("openId") or "",
        }

    # ---------- 业务请求 ----------

    def _headers(self, sid: str, uuid_: str) -> Dict[str, str]:
        extra = {
            "is_weapp": 1,
            "sid": sid,
            "version": VERSION,
            "client": "weapp",
            "bizEnv": "wsc",
            "uuid": uuid_,
            "ftime": int(time.time() * 1000),
        }
        return {
            "host": "h5.youzan.com",
            "extra-data": json.dumps(extra, separators=(",", ":")),
            "content-type": "application/json",
            "charset": "utf-8",
            "referer": REFERER,
            "user-agent": UA,
            # 不发 accept-encoding：urllib 默认 identity，避免 gzip 需手动解
        }

    def _get(self, path: str, token: str, sid: str, uuid_: str, **params: Any) -> Dict[str, Any]:
        q = {"app_id": APP_ID, "kdt_id": KDT_ID, "access_token": token}
        q.update(params)
        url = BASE + path + "?" + urllib.parse.urlencode(q)
        req = urllib.request.Request(url, method="GET", headers=self._headers(sid, uuid_))
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                raw = r.read()
                # 服务端可能仍回 gzip；urllib 只在它自己发 Accept-Encoding 时才解压
                try:
                    txt = raw.decode()
                except UnicodeDecodeError:
                    import gzip

                    txt = gzip.decompress(raw).decode()
                return {"status": r.status, "json": json.loads(txt)}
        except urllib.error.HTTPError as e:
            return {"status": e.code, "raw": e.read().decode()[:200]}

    def discover_checkin_id(self, token: str, sid: str, uuid_: str) -> Optional[str]:
        r = self._get("/wscump/checkin/check-in-info.json", token, sid, uuid_)
        j = r.get("json") or {}
        if j.get("code") != 0:
            raise RuntimeError(f"发现 checkInId 失败: code={j.get('code')} msg={j.get('msg')}")
        cid = (j.get("data") or {}).get("checkInId")
        return str(cid) if cid is not None else None

    def query_state(self, token: str, sid: str, uuid_: str, checkin_id: str) -> Dict[str, Any]:
        r = self._get(
            "/wscump/checkin/get_activity_by_yzuid_v2.json",
            token, sid, uuid_, checkinId=checkin_id,
        )
        j = r.get("json") or {}
        if j.get("code") != 0:
            raise RuntimeError(f"查询签到状态失败: code={j.get('code')} msg={j.get('msg')}")
        d = j.get("data") or {}
        return {
            "isCheckin": d.get("isCheckin"),
            "continuesDay": d.get("continuesDay"),
            "cycleTimes": d.get("cycleTimes"),
        }

    def do_checkin(self, token: str, sid: str, uuid_: str, checkin_id: str) -> Dict[str, Any]:
        r = self._get("/wscump/checkin/checkinV2.json", token, sid, uuid_, checkinId=checkin_id)
        j = r.get("json") or {}
        code = j.get("code")
        msg = j.get("msg") or ""
        d = j.get("data") or {}
        if code == 0:
            return {"ok": True, "reward": self._extract_reward(d), "raw": j}
        if str(code) in ALREADY_CODES or ALREADY_MSG_RE.search(msg):
            return {"ok": True, "already": True, "raw": j}
        return {"ok": False, "code": code, "msg": msg, "raw": j}

    @staticmethod
    def _extract_reward(d: Dict[str, Any]) -> str:
        lst = d.get("list") or []
        for item in lst:
            infos = item.get("infos") or {}
            title = infos.get("title") or ""
            if title:
                return title
        if d.get("success"):
            return "已发放"
        return ""

    def query_month(self, token: str, sid: str, uuid_: str, checkin_id: str) -> List[int]:
        t = time.localtime()
        r = self._get(
            "/wscump/checkin/find_checkin_info_by_month.json",
            token, sid, uuid_, checkin_id=checkin_id, year=t.tm_year, month=t.tm_mon,
        )
        j = r.get("json") or {}
        d = j.get("data") or {}
        return d.get("checkin_date") or []

    # ---------- 主流程 ----------

    def run(self) -> Dict[str, Any]:
        acc = {
            "account": self.remark,
            "phone": "",
            "status": "❌ 失败",
            "reward": "",
            "month_days": "?",
            "extra": [f"openid={mask(self.openid)}"],
        }
        try:
            if not self.openid:
                raise RuntimeError("openid 为空")

            # 登录态每次重取，不缓存（取码服务有限流才考虑缓存，这里无副作用）
            code = self.fetch_code()
            # uuid 是客户端随机设备标识，随机生成即可（clientId=md5(uuid)[:18]）
            uuid_ = hashlib.md5(str(time.time()).encode()).hexdigest()[:16] + str(int(time.time() * 1000))
            login = self.login(code, uuid_)
            token, sid = login["token"], login["sid"]
            acc["extra"].append(
                f"userId={mask(login['userId'])} mobile={mask(login['mobile'])}"
            )
            acc["phone"] = login["mobile"]

            cid = self.discover_checkin_id(token, sid, uuid_)
            if not cid:
                raise RuntimeError("未发现 checkInId")
            acc["extra"].append(f"checkInId={cid}")

            state = self.query_state(token, sid, uuid_, cid)
            if state.get("isCheckin"):
                days = self.query_month(token, sid, uuid_, cid)
                acc["status"] = "✅ 今日已签到（跳过提交）"
                acc["month_days"] = len(days)
                acc["extra"].append(f"连签 {state.get('continuesDay')} 天")
                return acc

            acc["extra"].append("今日未签到")

            if DRY_RUN:
                acc["status"] = "🧪 DRY-RUN（未提交）"
                return acc

            res = self.do_checkin(token, sid, uuid_, cid)
            if res.get("ok"):
                days = self.query_month(token, sid, uuid_, cid)
                acc["status"] = "✅ 签到成功" if not res.get("already") else "✅ 今日已签到"
                acc["reward"] = res.get("reward") or ""
                acc["month_days"] = len(days)
            else:
                acc["status"] = "⚠️ 未完成"
                acc["extra"].append(f"code={res.get('code')} msg={res.get('msg')}")
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
            notify_and_format("NACO旗舰店 签到", [], title="NACO 未配置账号")
        return 0

    if not WX_SERVER_URL or not WX_AUTH:
        print("缺少 wx_server_url 或 wx_auth")
        if QL_NOTIFY_ON:
            notify_and_format("NACO旗舰店 签到", [], title="NACO 未配置取码服务")
        return 0

    accounts = split_accounts(raw)
    print(f"共 {len(accounts)} 个账号")

    results: List[Dict[str, Any]] = []
    for i, a in enumerate(accounts, 1):
        results.append(NacoTask(a, i).run())
        if i < len(accounts):
            time.sleep(1.5)

    notify_and_format("NACO旗舰店 签到", results, start_ts=start)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"脚本异常: {e}")
        if QL_NOTIFY_ON:
            notify_and_format("NACO旗舰店 签到", [], title="NACO 脚本异常")
        sys.exit(1)
