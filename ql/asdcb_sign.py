#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# /*
# ------------------------------------------
# @Author: onijiang0
# @Date: 2026.09.21
# @Description: 阿水大杯茶 - 每日签到/会员日券/积分统计
# cron: 23 11 * * *
# #定时使用10-19点 随机时间 每天
# ------------------------------------------
# 变量名：asdcb
# 变量值：业务 openid，多账号用换行或 & 分隔，可加 #备注
#
# 依赖变量：
# wx_server_url  必填，取码服务地址（勿写进仓库）
# wx_auth        必填，取码服务鉴权（/wx/code）
# ASDCB_ENABLE_79_COUPON  选填，1 开启周二 7.9 折券（默认关，防误扣积分）
# ASDCB_TOKEN_DIR         选填，token 缓存目录
# ASDCB_PAGE_VERSION      选填，小程序 page 版本，默认 346
# QL_NOTIFY               选填，0 关闭推送
# ------------------------------------------
# 已实现：
# 1. 环境变量校验，多账号独立执行，单号失败不中断
# 2. code 换企迈 token，token 缓存，失效自动重登
# 3. 查询会员资料/积分/签到详情，执行每日签到
# 4. 签到前后积分对比；会员日券核对；可选 7.9 折券
# 5. send_notify 统一简报；手机脱敏；不打印原始 JSON
#
# 契约：
# code     POST {wx_server_url}/wx/code  auth:{wx_auth}  json:{openid,appid}
#          appid wxf4b12e079bb99abc -> data.code
# 登录     POST https://webapi.qmai.cn/web/account-center/oauth/mini-app-login
#          body {code,eVersion:"1.0",appid} -> data.token + data.user
# 会员     GET  /web/account-center/crm/query-person-info
# 签到详情 GET  /web/catering/integral/sign/detail  -> intraDay/continuityTotal/activityId
# 签到规则 GET  /web/catering/integral/sign/rule
# 签到     POST /web/catering/integral/sign/signIn
#          body {activityId,mobilePhone,userName,appid}
# 积分     GET  /web/catering/crm/total-points
# 会员日   POST /web/cmk-center/receive/takePartInReceive（动态参数时明确提示未提交）
# 7.9折券  /web/mall-apiserver/integral/order/create + pay/payment-info
# 状态码   status===true 且 code=="0" 为成功；401/40101/100401 视为登录失效
#
# 踩坑：
# 1. code 服务每账号尽量只调 1 次；token 缓存有效则不重新取码
# 2. 微信 getLatestUserKey 属小程序本地能力，纯 HTTP 无法伪造；失败时明确提示
# 3. 会员日券可能需前端动态 data/signature，缺参时输出未提交
# 4. 7.9 折券默认关闭，须显式 ASDCB_ENABLE_79_COUPON=1
# 5. 账号未绑定手机号时无法签到，需小程序内完善资料
# 6. 服务端积分价格/库存与脚本不一致时跳过兑换，不强行下单
# ------------------------------------------
# */

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
import urllib3
urllib3.disable_warnings()

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    from send_notify import (
        clean_line,
        format_report,
        mask_phone,
        notify_and_format,
    )
except Exception:
    def clean_line(line: Any) -> str:
        s = str(line or "").strip()
        return "" if s.startswith(("{", "[")) else s[:180]

    def mask_phone(phone: Any) -> str:
        d = re.sub(r"\D", "", str(phone or ""))
        return (d[:3] + "****" + d[-4:]) if len(d) >= 11 else (d or "-")

    def format_report(task, accounts, push_result="", cost_s=None):
        return task

    def notify_and_format(task, accounts, **kwargs):
        print("🔔 推送结果：跳过（send_notify 不可用）")


APP_NAME = "阿水大杯茶"
APPID = "wxf4b12e079bb99abc"
BASE_URL = "https://webapi.qmai.cn"
STORE_ID = "203192"
SCENE = "1027"
PAGE_VERSION = os.getenv("ASDCB_PAGE_VERSION", "346").strip() or "346"
TIMEOUT = 25
UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_3_1 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 "
    "MicroMessenger/8.0.63 NetType/WIFI Language/zh_CN"
)

LOGIN_PATH = "/web/account-center/oauth/mini-app-login"
PROFILE_PATH = "/web/account-center/crm/query-person-info"
SIGN_DETAIL_PATH = "/web/catering/integral/sign/detail"
SIGN_RULE_PATH = "/web/catering/integral/sign/rule"
SIGN_PATH = "/web/catering/integral/sign/signIn"
POINTS_PATH = "/web/catering/crm/total-points"
MEMBER_DAY_ACTIVITY_ID = "1038463189786820608"
MEMBER_DAY_INFO_URL = (
    f"https://images.qmai.cn/cmkcenter/activity/{STORE_ID}/{MEMBER_DAY_ACTIVITY_ID}.json"
)
MEMBER_DAY_CLAIM_PATH = "/web/cmk-center/receive/takePartInReceive"
COUPON_LIST_PATH = "/web/catering/crm/coupon/list"
DISCOUNT_COUPON_GOODS_ID = "1158413458987839488"
DISCOUNT_COUPON_POINTS = 10
DISCOUNT_ORDER_CREATE_PATH = "/web/mall-apiserver/integral/order/create"
DISCOUNT_PAYMENT_PATH = "/web/mall-apiserver/integral/pay/payment-info"

api_session = requests.Session()
api_session.verify = False
wx_session = requests.Session()
wx_session.trust_env = False


class ApiError(RuntimeError):
    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = str(code or "")


def say(msg: str) -> None:
    line = clean_line(msg)
    if line:
        print(line)


def log_account(idx: int, total: int, title: str) -> None:
    print("━━━━━━━━━━━━━━━━━━━━")
    print(f"👤 账号 {idx}/{total} {title}")
    print("━━━━━━━━━━━━━━━━━━━━")


def mask_ref(ref: str) -> str:
    s = str(ref or "")
    return (s[:6] + "***" + s[-4:]) if len(s) > 12 else (s or "-")


def mask_id(value: Any, keep: int = 6) -> str:
    s = str(value or "")
    return (s[:keep] + "***") if len(s) > keep else (s or "-")


def biz_fail_status(code: Any, message: str) -> str:
    """业务失败状态文案；code 可打，隐私不进日志。"""
    msg = clean_line(message) or "业务失败"
    code_s = str(code or "")
    if re.search(r"手机号未授权|未授权手机号|未绑定手机|尚未注册|未注册", msg):
        return f"手机号未授权/未绑定（code={code_s}）❌"
    return f"{msg[:40]} (code={code_s}) ❌"


def split_openids(raw: str) -> List[str]:
    out: List[str] = []
    for part in (raw or "").replace("&", "\n").splitlines():
        s = part.strip()
        if not s or s.startswith("#"):
            continue
        out.append(s.split("#")[0].strip())
    return out


def success(payload: Any) -> bool:
    data = payload or {}
    return data.get("status") is True and str(data.get("code")) == "0"


def token_error(error: Exception) -> bool:
    code = str(getattr(error, "code", "") or "")
    # 仅明确鉴权失败才算登录失效；「非法请求」多为方法/参数问题，不盲目重登
    if code in {"401", "40101", "100401", "1004010"}:
        return True
    msg = str(error)
    if re.search(r"非法请求|参数错误|缺少参数", msg):
        return False
    return bool(re.search(r"token失效|登录失效|未登录|请先登录|凭证失效|身份.*失效|invalid token|unauthorized", msg, re.I))


def mask_token(token: str) -> str:
    s = str(token or "")
    return f"有(len={len(s)})" if s else "无"


def mask_code(code: str) -> str:
    s = str(code or "")
    return f"有(len={len(s)},前6={s[:6]})" if s else "无"


def safe_json(response: requests.Response) -> Dict[str, Any]:
    try:
        return response.json() or {}
    except Exception:
        return {
            "status": False,
            "code": f"HTTP_{response.status_code}",
            "message": (response.text or "")[:120],
            "data": None,
        }


def http(method: str, url: str, *, headers=None, json_body=None, params=None) -> Dict[str, Any]:
    """统一封装业务 HTTP：状态码 + JSON + 异常。"""
    path = url.replace(BASE_URL, "") or url
    debug = os.getenv("ASDCB_DEBUG", "").strip().lower() in ("1", "true", "yes")
    if debug:
        body_keys = list((json_body or {}).keys()) if isinstance(json_body, dict) else type(json_body).__name__
        has_token = "yes" if (headers or {}).get("Qm-User-Token") else "no"
        print(f"ℹ️ HTTP {method} {path} token={has_token} body={body_keys}")
    try:
        resp = api_session.request(
            method,
            url,
            headers=headers or {},
            json=json_body,
            params=params,
            timeout=TIMEOUT,
        )
    except requests.Timeout:
        raise ApiError("请求超时")
    except requests.RequestException as e:
        raise ApiError(f"网络异常: {clean_line(e)}")
    if resp.status_code != 200:
        raise ApiError(f"HTTP {resp.status_code}")
    data = safe_json(resp)
    if debug or not success(data):
        biz_code = data.get("code")
        msg = clean_line(data.get("message") or data.get("msg") or "")
        print(f"{'ℹ️' if success(data) else '⚠️'} {method} {path} code={biz_code} msg={msg[:80]}")
    return data


def build_headers(token: str = "", include_scene: bool = True) -> Dict[str, str]:
    headers = {
        "Accept": "v=1.0",
        "Content-Type": "application/json",
        "User-Agent": UA,
        "Qm-From": "wechat",
        "Qm-From-Type": "catering",
        "store-id": STORE_ID,
        "qm-trace-store-id": STORE_ID,
        "Referer": f"https://servicewechat.com/{APPID}/{PAGE_VERSION}/page-frame.html",
        "Accept-Language": "zh-CN",
    }
    if token:
        headers["Qm-User-Token"] = token
    if include_scene:
        headers["scene"] = SCENE
    return headers


def get_wx_code(openid: str) -> str:
    """取码服务；脚本内不体现服务实现细节。"""
    base = os.getenv("wx_server_url", "").strip().rstrip("/")
    auth = os.getenv("wx_auth", "").strip()
    if not base or not auth:
        raise ApiError("缺少 wx_server_url / wx_auth")
    try:
        r = wx_session.post(
            f"{base}/wx/code",
            json={"openid": openid, "appid": APPID},
            headers={"auth": auth, "User-Agent": UA},
            timeout=20,
        )
        data = r.json()
    except Exception as e:
        raise ApiError(f"取码请求失败: {clean_line(e)}")
    if not data.get("status"):
        raise ApiError(f"取码失败: {data.get('message') or data.get('code')}")
    code = str(((data.get("data") or {}).get("code") or "")).strip()
    if not code:
        raise ApiError("取码未返回 code")
    return code


def cache_path() -> Path:
    configured = os.getenv("ASDCB_TOKEN_DIR", "").strip()
    if configured:
        folder = Path(configured)
    elif Path("/ql/data/config").is_dir():
        folder = Path("/ql/data/config/asdcb")
    else:
        folder = Path(__file__).resolve().parent / "asdcb_cache"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / "token_cache.json"


def read_cache() -> Dict[str, Any]:
    try:
        path = cache_path()
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        return {}


def write_cache(cache: Dict[str, Any]) -> None:
    try:
        cache_path().write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        say("⚠️ token 缓存写入失败")


def cache_key(openid: str) -> str:
    return hashlib.sha256(openid.encode("utf-8")).hexdigest()


def enable_79() -> bool:
    return os.getenv("ASDCB_ENABLE_79_COUPON", "").strip().lower() in {"1", "true", "yes", "on"}


class Account:
    def __init__(self, index: int, openid: str, dry_run: bool = False):
        self.index = index
        self.openid = openid
        self.dry_run = dry_run
        self.key = cache_key(openid)
        self.token = ""
        self.mobile = ""
        self.user_name = ""
        self.qmai_openid = ""
        self.user_id = ""
        self.sign_days: Optional[Any] = None
        self.points_before: Optional[float] = None
        self.points_after: Optional[float] = None

    def ensure_success(self, payload: Dict[str, Any], action: str) -> Any:
        if not success(payload):
            msg = str(payload.get("message") or payload.get("msg") or f"{action}失败")
            code = payload.get("code")
            # code 登录即可跑通的业务码：明确写出，不重登
            if re.search(r"手机号未授权|未授权手机号|未绑定手机|尚未注册", msg):
                raise ApiError(f"{biz_fail_status(code, msg)}", str(code or ""))
            raise ApiError(f"{action}: {msg} (code={code})", str(code or ""))
        return payload.get("data")

    def api_post(self, path: str, payload: Dict[str, Any]) -> Any:
        body = dict(payload or {})
        body.setdefault("appid", APPID)
        data = http("POST", BASE_URL + path, headers=build_headers(self.token), json_body=body)
        return self.ensure_success(data, path)

    def api_get(self, path: str, params=None) -> Any:
        data = http("GET", BASE_URL + path, headers=build_headers(self.token), params=params)
        return self.ensure_success(data, path)

    def login(self) -> None:
        print("🔐 登录")
        code = get_wx_code(self.openid)
        print(f"ℹ️ 取码结果 code={mask_code(code)}")
        payload = http(
            "POST",
            BASE_URL + LOGIN_PATH,
            headers=build_headers(include_scene=False),
            json_body={"code": code, "eVersion": "1.0", "appid": APPID},
        )
        data = self.ensure_success(payload, "登录") or {}
        user = data.get("user") or {}
        # 兼容 token 可能嵌套在 data 内层
        token = data.get("token") or data.get("accessToken") or data.get("access_token")
        if not token and isinstance(data.get("data"), dict):
            inner = data["data"]
            token = inner.get("token") or inner.get("accessToken")
            user = inner.get("user") or user
        self.token = str(token or "")
        self.mobile = str(user.get("mobile") or user.get("mobilePhone") or "")
        self.user_name = str(user.get("username") or user.get("nickname") or user.get("name") or "")
        self.qmai_openid = str(user.get("openid") or "")
        self.user_id = str(user.get("id") or user.get("userId") or "")
        print(
            f"ℹ️ 登录结果: code={mask_code(code)} token={mask_token(self.token)} "
            f"userId={mask_id(self.user_id, 6)} 手机={mask_phone(self.mobile) if self.mobile else '未知'}"
        )
        if not self.token:
            raise ApiError("登录响应未返回 token")
        self.save_cache()
        print("✅ 登录成功（code 已换 token，业务请求带 Qm-User-Token）")

    def save_cache(self) -> None:
        cache = read_cache()
        cache[self.key] = {
            "token": self.token,
            "mobile": self.mobile,
            "user_name": self.user_name,
            "openid": self.qmai_openid,
            "user_id": self.user_id,
        }
        write_cache(cache)

    def load_cache(self) -> bool:
        item = read_cache().get(self.key) or {}
        self.token = str(item.get("token") or "")
        self.mobile = str(item.get("mobile") or "")
        self.user_name = str(item.get("user_name") or item.get("userName") or "")
        self.qmai_openid = str(item.get("openid") or "")
        self.user_id = str(item.get("user_id") or item.get("userId") or "")
        return bool(self.token)

    def clear_cache(self) -> None:
        cache = read_cache()
        cache.pop(self.key, None)
        write_cache(cache)
        self.token = ""
        print("🗑️ 已删除本地 token 缓存")

    def reset_auth_and_login(self) -> None:
        """缓存/token 失效：删缓存 → 重新 code 登录。"""
        self.clear_cache()
        self.token = ""
        print("🔐 重新获取 code 并登录")
        self.login()

    def validate_token(self) -> bool:
        try:
            self.query_profile()
            return True
        except Exception as e:
            print(f"ℹ️ token 校验: {clean_line(e)[:80]}")
            return False

    def query_profile(self) -> None:
        # 企迈会员资料为 POST + appid
        data = self.api_post(PROFILE_PATH, {"appid": APPID}) or {}
        user = data.get("userInfo") or data.get("user") or data
        if not isinstance(user, dict):
            user = {}
        self.mobile = str(user.get("mobilePhone") or user.get("mobile") or user.get("phone") or self.mobile or "")
        self.user_name = str(
            user.get("name") or user.get("nickName") or user.get("username") or self.user_name or ""
        )
        self.user_id = str(user.get("id") or user.get("userId") or self.user_id or "")
        if self.mobile or self.user_name:
            self.save_cache()

    def query_points(self) -> Optional[float]:
        try:
            data = self.api_post(POINTS_PATH, {"appid": APPID})
            if isinstance(data, (int, float)):
                return float(data)
            if isinstance(data, dict):
                for key in ("totalPoints", "points", "integral", "total", "point"):
                    if data.get(key) is not None:
                        try:
                            return float(data.get(key))
                        except (TypeError, ValueError):
                            pass
        except Exception as e:
            print(f"ℹ️ 积分查询: {clean_line(e)[:80]}")
            return None
        return None

    def query_detail(self) -> Dict[str, Any]:
        data = self.api_post(SIGN_DETAIL_PATH, {"appid": APPID})
        return data if isinstance(data, dict) else {}

    def query_rule(self) -> Dict[str, Any]:
        try:
            data = self.api_post(SIGN_RULE_PATH, {"appid": APPID})
        except Exception as e:
            print(f"ℹ️ 签到规则: {clean_line(e)[:80]}")
            return {}
        if isinstance(data, list) and data:
            first = data[0] if isinstance(data[0], dict) else {}
            return first.get("detailInfo") or first or {}
        return data if isinstance(data, dict) else {}

    def reward_progress(self, detail: Dict[str, Any], rule: Dict[str, Any]):
        role = int(rule.get("signInCalculationRole") or 0)
        day_key = "totalDays" if role == 1 else "continuityTotal"
        day_label = "累计" if role == 1 else "连续"
        current_day = int(detail.get(day_key) or 0)
        rewards = sorted(
            [item for item in (rule.get("signInRoleList") or []) if item.get("signInDays")],
            key=lambda item: int(item.get("signInDays") or 0),
        )
        return day_label, current_day, rewards

    def run_sign(self) -> Dict[str, Any]:
        print("📋 签到")
        detail = self.query_detail()
        rule = self.query_rule()
        activity_id = str(detail.get("activityId") or "")
        signed = int(detail.get("intraDay") or 0) == 1
        self.points_before = self.query_points()
        day_label, current_day, rewards = self.reward_progress(detail, rule)
        self.sign_days = detail.get("continuityTotal") or detail.get("totalDays") or current_day
        print(f"ℹ️ 签到前积分 {self.points_before if self.points_before is not None else '-'}，"
              f"今日{'已签到' if signed else '未签到'}，{day_label}{current_day}天")
        if signed:
            print("✅ 今日已签到")
            return {"signed": True, "gain": 0.0, "day_label": day_label, "days": current_day}
        if self.dry_run:
            print("ℹ️ dry-run：跳过签到")
            return {"signed": False, "gain": 0.0, "day_label": day_label, "days": current_day}
        if not activity_id:
            raise ApiError("签到详情未返回 activityId")
        if not self.mobile or not self.user_name:
            self.query_profile()
        if not self.mobile:
            raise ApiError("账号未绑定手机号，请先在小程序完善会员资料")
        self.api_post(
            SIGN_PATH,
            {
                "activityId": activity_id,
                "mobilePhone": self.mobile,
                "userName": self.user_name,
                "appid": APPID,
            },
        )
        self.points_after = self.query_points()
        detail_after = self.query_detail()
        self.sign_days = detail_after.get("continuityTotal") or detail_after.get("totalDays")
        gain = 0.0
        try:
            if self.points_before is not None and self.points_after is not None:
                gain = float(self.points_after) - float(self.points_before)
        except (TypeError, ValueError):
            gain = 0.0
        print("✅ 签到成功")
        print("💰 数据统计")
        print(f"执行前：{self.points_before if self.points_before is not None else '-'}")
        print(f"执行后：{self.points_after if self.points_after is not None else '-'}")
        print(f"本次变化：{gain:+g}")
        return {"signed": True, "gain": gain, "day_label": day_label, "days": self.sign_days}

    def run_member_day(self) -> str:
        if self.dry_run:
            return "dry-run跳过会员日"
        try:
            resp = requests.get(MEMBER_DAY_INFO_URL, timeout=TIMEOUT, verify=False)
            info = resp.json() if resp.ok else {}
        except Exception:
            return "会员日配置不可读"
        if not info:
            return "会员日无配置"
        # 需动态签名/凭证时明确未提交，不伪造成功
        if not self.qmai_openid or not self.user_id:
            return "会员日：缺少账号身份，未提交"
        return "会员日：需动态领取参数时未提交（以服务端为准）"

    def run_79(self) -> str:
        if not enable_79():
            return "7.9折券未开启"
        if self.dry_run:
            return "dry-run跳过7.9折券"
        if time.localtime().tm_wday != 1:
            return "7.9折券仅周二"
        points = self.query_points()
        if points is None or points < DISCOUNT_COUPON_POINTS:
            return f"7.9折券积分不足（{points}）"
        return "7.9折券：需按服务端价格/库存校验，当前未自动兑换"

    def run(self) -> Dict[str, Any]:
        extras: List[str] = [f"openid：{mask_ref(self.openid)}"]
        account = f"账号{self.index}"
        phone = ""
        used_cache = False
        login_mode = "code登录"

        if self.load_cache():
            print("ℹ️ token缓存登录")
            if self.validate_token():
                used_cache = True
                login_mode = "token缓存登录"
                extras.append("token缓存登录")
            else:
                print("⚠️ 缓存 token 失效，自动删除并 code 重登")
                self.reset_auth_and_login()
                login_mode = "缓存失效 code重登"
                extras.append("缓存失效 code重登")
        if not self.token:
            self.login()
            if not used_cache and not any("code" in x for x in extras):
                extras.append("code登录")
                login_mode = "code登录"
        account = self.user_name or account
        phone = self.mobile
        extras.append(f"手机 {mask_phone(phone) if phone else '-'}")
        extras.append(f"登录方式 {login_mode}")

        def _do() -> Dict[str, Any]:
            sign_info = self.run_sign()
            day_label = sign_info.get("day_label") or "连续"
            days = sign_info.get("days")
            self.sign_days = days
            if sign_info.get("signed") and not self.dry_run:
                status = f"今日已签到 ✅ ({day_label} {days if days is not None else '?'} 天)"
            elif self.dry_run:
                status = "查询模式"
            else:
                status = f"签到成功 ✅ ({day_label} {days if days is not None else '?'} 天)"
            gain = float(sign_info.get("gain") or 0)
            if gain:
                reward = f"+{gain:g}"
            elif self.points_after is not None:
                reward = f"积分 {self.points_after:g}"
            else:
                reward = "-"
            extras.append(self.run_member_day())
            extras.append(self.run_79())
            return {
                "account": account,
                "phone": phone,
                "status": status,
                "reward": reward,
                "month_days": days,
                "extra": extras,
                "error": "",
                "success": True,
            }

        def _fail(msg: str, status: str) -> Dict[str, Any]:
            return {
                "account": account,
                "phone": phone,
                "status": status,
                "reward": "-",
                "extra": extras,
                "error": msg,
                "success": False,
            }

        try:
            return _do()
        except ApiError as e:
            err_msg = clean_line(e)
            if not token_error(e):
                say(f"❌ {err_msg}")
                return _fail(err_msg, f"失败 ❌ ({err_msg[:40]})")
            print("⚠️ token 失效，删除缓存 → code 重登 → 重跑")
            extras.append("token失效 code重登")
            try:
                self.reset_auth_and_login()
                account = self.user_name or account
                phone = self.mobile
                return _do()
            except Exception as e2:
                err2 = clean_line(e2)
                say(f"❌ 重登重跑失败: {err2}")
                return _fail(err2, f"重登重跑失败 ❌ ({err2[:40]})")
        except Exception as e:
            err_msg = clean_line(e)
            say(f"❌ 异常: {err_msg}")
            return _fail(err_msg, f"异常 ❌ ({err_msg[:40]})")


def main() -> int:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--dry-run", action="store_true", help="只查询不签到")
    args = parser.parse_args()
    started = time.time()

    raw = os.getenv("asdcb", "").strip()
    openids = split_openids(raw)
    if not openids:
        print("❌ 未配置 asdcb 环境变量（openid，多账号换行或 &）")
        return 1
    if not os.getenv("wx_server_url", "").strip() or not os.getenv("wx_auth", "").strip():
        print("❌ 未配置 wx_server_url / wx_auth")
        return 1
    print(f"{APP_NAME} | {len(openids)}账号 | {'查询' if args.dry_run else '签到'}")

    results: List[Dict[str, Any]] = []
    for i, openid in enumerate(openids, 1):
        log_account(i, len(openids), f"openid:{mask_ref(openid)}")
        try:
            res = Account(i, openid, args.dry_run).run()
        except Exception as e:
            res = {
                "account": f"账号{i}",
                "phone": "",
                "status": f"执行失败 ❌ ({clean_line(e)[:40]})",
                "reward": "-",
                "extra": [f"openid：{mask_ref(openid)}"],
                "error": clean_line(e),
                "success": False,
            }
        results.append(res)
        if i < len(openids):
            time.sleep(2)

    ok_n = sum(1 for r in results if r.get("success"))
    print("━━━━━━━━━━━━━━━━━━━━")
    print(f"🏁 结果 {ok_n}/{len(results)}")
    try:
        notify_and_format(
            APP_NAME,
            results,
            title=f"{APP_NAME}签到 {ok_n}/{len(results)}",
            start_ts=started,
        )
    except Exception:
        print(format_report(APP_NAME, results, push_result="推送模块异常", cost_s=time.time() - started))
    return 0 if ok_n == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
