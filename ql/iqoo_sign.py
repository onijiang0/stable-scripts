#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# /*
# ------------------------------------------
# @Description: iQOO社区 - 签到/浏览/点赞/分享/评论/发帖(聊游戏)/抽奖/积分
# cron: 10 14 * * *
# ------------------------------------------
# 变量名：iqoo
# 变量值：openid，多账号用换行分隔
# 示例：owNAX6j2...DOLtPc
#
# 依赖变量：
# wx_auth           必填，smallcat 调用 API AUTH
# wx_server_url     必填，wx_server 地址（勿写进仓库）
# iqoo_appid        默认 wxcf4266fbc9463132
# iqoo_browse       脚本内默认 0（已关闭浏览任务）；环境变量可覆盖，但默认不浏览
# iqoo_like         默认 4（单日硬顶）；任务前读今日进度，已满则不再点赞
# iqoo_share        默认 4，分享次数
# iqoo_comment      默认 1，评论次数（内容来自一言，去掉来源后缀）
# iqoo_draw         默认 1=只抽每日免费第一抽（当天已中奖则跳过）；0=不抽
# iqoo_post         默认 1，发帖次数（发到「聊游戏」categoryId=21）
#
# ========== 已实现 ==========
# 1. 登录：smallcat getphonenumber + code -> v3/users/vivo/mini 拿 accessToken
# 2. 签到：POST v3/sign（已签则识别「已经签到」）
# 3. 点赞：先读今日进度，满 4 停；不跳过已赞帖
# 4. 分享：先读今日进度，满 4 停
# 5. 评论/发帖：共用今日 post 配额，先读进度，满则停
# 6. 抽奖：先查中奖记录/抽奖池，已抽则跳过
# 7. 浏览：默认关闭；开启时也先读 view 进度
# 8. 任务前统一打印今日进度预读
# 9. 多账号：openid 换行分隔，账号间 sleep 3s
#
# ========== 踩坑 / 限制 ==========
# 1. 签名：SIGN 头 = IQOO-HMAC-SHA256 + appid=1002,timestamp=,signature=
#    raw = METHOD&/api/path&sortedQs&jsonBody&appid=1002&ts
#    HMAC-SHA256(appKey=2618194b0ebb620055e19cf9811d3c13) 必须 base64，不是 hex
#    GET 用排序 encodeURIComponent 查询串；POST 用 JSON.stringify(body)
# 2. 必带头：X-Visitor(任意uuid/murmur)、X-Platform=mini、Authorization Bearer
#    头名是 SIGN 不是 X-Sign；path 必须带 /api/ 前缀
# 3. 发帖 content 必须是 dict {text,indexes:[]}，纯字符串会 -5003 请输入帖子内容
#    「聊游戏」是分类不是话题：categoryId=21（父级19游戏圈）
#    无发帖权限会 -4002；发太快 -10002
# 4. 阅读任务 viewCount：thread.detail 能读到帖，但服务端基本不把 API 请求
#    计入「浏览帖子」（反自动化）。号2 偶发 1/2 多半来自真机。无独立 view 上报接口。
# 5. 抽奖：同日重复调 luck.draw 仍会成功并消耗任务次数 -> 必须用中奖记录防重
#    today.draw.count 是任务送的次数，免费第一抽不依赖它（count=0 也能抽）
# 6. smallcat：/wx/code 约 8次/90s；getphonenumber 会占用该 openid 会话
#    不是每个 openid 都能取手机号（有的返回 js-login code empty）
# 7. thread.list 的 pageData 字段是 threadId；推荐列表字段是 id
#    v4/categories/{id}/threads 的 Data.data[] 用 id
# 8. 同日重跑：已签/已抽/已发帖(限1) 会跳过；点赞不因已赞跳过
#
# 契约（bbs-api.iqoo.com + smallcat）：
# 登录  getphonenumber + code -> v3/users/vivo/mini -> accessToken
# 签到  POST v3/sign
# 浏览  GET  v4/categories/{id}/threads + GET v3/thread.detail
# 点赞  POST v3/posts.update {id:threadId,postId,data:{attributes:{isLiked:true}}}
# 分享  POST v3/thread.share {threadId}
# 评论  POST v3/posts.create {id:threadId,type:0,content,source}
# 发帖  POST v3/thread.create {title,content:{text,indexes:[]},categoryId:21}
# 抽奖  GET  v3/user.winning.list 防重；POST v3/luck.draw
# 进度  GET  v5/users/tasks/today-progress
# 积分  GET  v3/user?userId= -> Data.score
# 一言  GET  https://v1.hitokoto.cn/?encode=json -> hitokoto
# 响应  {Code,Message,Data}；Code==0 成功
# ------------------------------------------
# */

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Dict, List, Optional, Tuple

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("IQOO")

APP_KEY = "2618194b0ebb620055e19cf9811d3c13"
API_BASE = "https://bbs-api.iqoo.com/api/"
UA = (
    "Mozilla/5.0 (Linux; Android 17) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Version/4.0 Chrome/150.0.0 Mobile Safari/537.36 "
    "MicroMessenger/8.0.76 MiniProgramEnv/android"
)
ALREADY = ("已签", "已经签", "重复", "今日已签", "already")


def enc_qs(s: Any) -> str:
    return (
        urllib.parse.quote(str(s), safe="")
        .replace("!", "%21")
        .replace("'", "%27")
        .replace("(", "%28")
        .replace(")", "%29")
        .replace("*", "%2A")
    )


class IqooApi:
    def __init__(self, appid: str, token: str = ""):
        self.appid = appid
        self.token = token
        self.visitor = uuid.uuid4().hex
        self.uid: Optional[int] = None

    def _sign(self, method: str, path: str, params: Optional[dict], body: Optional[dict]) -> str:
        ts = int(time.time())
        qs = ""
        if method == "GET" and params:
            qs = "&".join(f"{enc_qs(k)}={enc_qs(params[k])}" for k in sorted(params.keys()))
        body_s = ""
        if method != "GET" and body is not None:
            body_s = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
        raw = f"{method}&{path}&{qs}&{body_s}&appid=1002&timestamp={ts}"
        dig = hmac.new(APP_KEY.encode(), raw.encode("utf-8"), hashlib.sha256).digest()
        sig = base64.b64encode(dig).decode()
        return f"IQOO-HMAC-SHA256 appid=1002,timestamp={ts},signature={sig}"

    def call(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        url = API_BASE + endpoint
        headers = {
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "content-type": "application/json",
            "Referer": f"https://servicewechat.com/{self.appid}/16/page-frame.html",
            "X-Visitor": self.visitor,
            "X-Platform": "mini",
            "SIGN": self._sign(method, "/api/" + endpoint, params if method == "GET" else None, body),
            "Content-Nonce": "",
            "Authorization": ("Bearer " + self.token) if self.token else "Bearer ",
        }
        data = None
        if method == "GET":
            if params:
                url += "?" + urllib.parse.urlencode(params)
        else:
            data = json.dumps(body if body is not None else {}, separators=(",", ":"), ensure_ascii=False).encode()
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                return json.loads(raw)
            except Exception:
                return {"Code": e.code, "Message": raw[:200]}
        except Exception as e:
            return {"Code": -1, "Message": str(e)}

    def sc_post(self, sc_base: str, auth: str, path: str, body: dict) -> dict:
        req = urllib.request.Request(
            sc_base.rstrip("/") + path,
            data=json.dumps(body).encode(),
            method="POST",
            headers={"auth": auth, "Content-Type": "application/json", "User-Agent": UA},
        )
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                return json.loads(raw)
            except Exception:
                return {"status": False, "message": raw[:200]}
        except Exception as e:
            return {"status": False, "message": str(e)}

    def login(self, sc_base: str, auth: str, openid: str) -> Tuple[bool, str]:
        ph = self.sc_post(sc_base, auth, "/wx/getphonenumber", {"openid": openid, "appid": self.appid})
        raw = (ph.get("data") or {}).get("raw") or {}
        if not raw.get("encryptedData"):
            return False, f"getphonenumber失败: {ph.get('message') or ph}"
        time.sleep(1)
        cr = self.sc_post(sc_base, auth, "/wx/code", {"openid": openid, "appid": self.appid})
        code = (cr.get("data") or {}).get("code")
        if not code:
            return False, f"获取code失败: {cr.get('message') or cr}"
        d = self.call(
            "POST",
            "v3/users/vivo/mini",
            body={"code": code, "encryptedData": raw.get("encryptedData") or "", "iv": raw.get("iv") or "", "from": 46},
        )
        data = d.get("Data") if isinstance(d, dict) else None
        if not isinstance(data, dict) or not data.get("accessToken"):
            return False, f"登录失败 Code={d.get('Code')} {d.get('Message')}"
        self.token = data.get("accessToken") or ""
        self.uid = data.get("userId")
        return True, f"userId={self.uid}"

    def score(self) -> int:
        if not self.uid:
            return 0
        d = self.call("GET", "v3/user", params={"userId": self.uid})
        try:
            return int((d.get("Data") or {}).get("score") or 0)
        except Exception:
            return 0

    def sign(self) -> str:
        d = self.call("POST", "v3/sign", body={})
        if d.get("Code") == 0:
            data = d.get("Data") or {}
            tips = ""
            for t in (d.get("Meta") or {}).get("tips") or []:
                tips = t.get("message") or tips
            return f"签到成功 连签{data.get('serialDays')}天 +{data.get('score')} {tips}".strip()
        msg = str(d.get("Message") or "")
        if any(x in msg for x in ALREADY):
            return msg or "今日已签到"
        return f"签到失败 Code={d.get('Code')} {msg}"

    def progress_raw(self) -> dict:
        """今日任务进度原始数值，用于点赞/分享防超做。"""
        d = self.call("GET", "v5/users/tasks/today-progress")
        data = d.get("Data") or {}
        return {
            "likeCount": int(data.get("likeCount") or 0),
            "likeUpperLimit": int(data.get("likeUpperLimit") or 4),
            "shareCount": int(data.get("shareCount") or 0),
            "shareUpperLimit": int(data.get("shareUpperLimit") or 4),
            "viewCount": int(data.get("viewCount") or 0),
            "viewUpperLimit": int(data.get("viewUpperLimit") or 0),
            "postCount": int(data.get("postCount") or 0),
            "createPostUpperLimit": int(data.get("createPostUpperLimit") or 0),
            "dailyScore": data.get("dailyScore"),
        }

    def progress(self) -> dict:
        data = self.progress_raw()
        return {
            "view": f"{data['viewCount']}/{data['viewUpperLimit']}",
            "like": f"{data['likeCount']}/{data['likeUpperLimit']}",
            "share": f"{data['shareCount']}/{data['shareUpperLimit']}",
            "post": f"{data['postCount']}/{data['createPostUpperLimit']}",
            "dailyScore": data.get("dailyScore"),
        }

    def browse_pool(self, n: int) -> List[dict]:
        """优先分类页帖子（更可能是本日未读），避免总刷同一批推荐帖。"""
        seen: set = set()
        out: List[dict] = []

        def add(lst):
            for t in lst or []:
                if not isinstance(t, dict):
                    continue
                tid = t.get("id") or t.get("threadId")
                if not tid or tid in seen:
                    continue
                seen.add(tid)
                if "id" not in t and t.get("threadId"):
                    t = dict(t)
                    t["id"] = t["threadId"]
                out.append(t)

        for cid in (21, 26, 27, 28, 45, 16, 9, 10):
            for page in (1, 2, 3):
                d = self.call("GET", f"v4/categories/{cid}/threads", params={"page": page, "perPage": 10})
                add((d.get("Data") or {}).get("data") or (d.get("Data") or {}).get("pageData"))
                if len(out) >= max(n, 6):
                    return out[: max(n, 6)]
        d = self.call("GET", "v5/recommend/thread/list", params={"page": 2, "perPage": 10})
        add((d.get("Data") or {}).get("data"))
        return out[: max(n, 6)]

    def list_threads(self, n: int) -> List[dict]:
        """点赞/分享/评论用：多接口回退，失败写日志。"""
        seen: set = set()
        out: List[dict] = []
        errs: List[str] = []

        def add(lst):
            for t in lst or []:
                if not isinstance(t, dict):
                    continue
                tid = t.get("id") or t.get("threadId")
                if not tid or tid in seen:
                    continue
                seen.add(tid)
                if "id" not in t and t.get("threadId"):
                    t = dict(t)
                    t["id"] = t["threadId"]
                out.append(t)

        sources = [
            ("v5/recommend/thread/list", {"page": 1, "perPage": 20}, ("data", "pageData")),
            ("v3/thread.list", {"page": 1, "perPage": 10}, ("pageData", "data")),
        ]
        for cid in (21, 26, 27, 28, 45, 16, 9, 10):
            sources.append(
                (f"v4/categories/{cid}/threads", {"page": 1, "perPage": 10}, ("data", "pageData"))
            )

        for ep, params, keys in sources:
            if len(out) >= max(n, 8):
                break
            d = self.call("GET", ep, params=params)
            code = d.get("Code")
            if code not in (0, None, "0"):
                errs.append(f"{ep}:{code}")
                continue
            data = d.get("Data") or {}
            lst = None
            for k in keys:
                if data.get(k):
                    lst = data.get(k)
                    break
            add(lst)

        if not out:
            print(f"[iqoo] 获取帖子列表失败: {errs or '空列表'}")
        elif errs:
            print(f"[iqoo] 帖子列表部分失败: {errs}")
        return out[: max(n, 8)]

    def _remain_quota(self, count_key: str, upper_key: str, hard_cap: int, label: str):
        """读今日进度，返回 (日志列表, done_today, upper, remain)。"""
        lines: List[str] = []
        try:
            prog = self.progress_raw()
        except Exception as e:
            prog = {}
            lines.append(f"读取今日{label}进度失败: {e}")
        done = int(prog.get(count_key) or 0)
        upper_raw = int(prog.get(upper_key) or hard_cap)
        upper = min(upper_raw, hard_cap) if hard_cap > 0 else upper_raw
        if upper <= 0:
            upper = hard_cap
        remain = max(0, upper - done)
        lines.append(f"今日{label} {done}/{upper}（脚本硬顶{hard_cap if hard_cap>0 else upper}）")
        return lines, done, upper, remain

    def browse(self, threads: List[dict], n: int) -> List[str]:
        """浏览：先读 view 进度，满则跳过，避免重复刷。"""
        if n <= 0:
            return ["浏览关闭"]
        lines, done, upper, remain = self._remain_quota(
            "viewCount", "viewUpperLimit", max(n, 8), "浏览"
        )
        if remain <= 0:
            lines.append("今日浏览已满，自动停止")
            return lines
        want = min(n, remain, len(threads) if threads else 0)
        lines.append(f"本轮最多再浏览 {want} 篇")
        ok_n = 0
        for t in threads[:want]:
            if ok_n >= remain:
                break
            tid = t.get("id") or t.get("threadId")
            r = self.call("GET", "v3/thread.detail", params={"threadId": tid})
            ok = r.get("Code") == 0
            lines.append(f"浏览#{tid} {'OK' if ok else r.get('Message')}")
            if ok:
                ok_n += 1
            time.sleep(2)
        try:
            p = self.progress_raw()
            lines.append(
                f"浏览后进度 {p['viewCount']}/{p['viewUpperLimit']} 本进程成功{ok_n}篇"
            )
        except Exception:
            lines.append(f"浏览完成{ok_n}篇")
        return lines

    def like(self, threads: List[dict], n: int) -> List[str]:
        """点赞：先读今日进度；达到上限(默认4)自动停止。不跳过已赞帖。"""
        if n <= 0:
            return ["点赞关闭"]
        HARD_CAP = 4
        lines, done_today, upper, remain = self._remain_quota(
            "likeCount", "likeUpperLimit", HARD_CAP, "点赞"
        )
        if remain <= 0:
            lines.append("今日点赞已满，自动停止")
            return lines
        want = min(n, remain)
        lines.append(f"本轮最多再赞 {want} 次")
        ok_n = 0
        for t in threads[:want]:
            if ok_n >= remain:
                lines.append(f"已达上限 {upper}，停止点赞")
                break
            tid = t.get("id") or t.get("threadId")
            pid = t.get("postId") or t.get("pid")
            body: Dict[str, Any] = {"id": tid, "data": {"attributes": {"isLiked": True}}}
            if pid:
                body["postId"] = pid
            r = self.call("POST", "v3/posts.update", body=body)
            ok = r.get("Code") == 0
            extra = "(原本已赞)" if t.get("isLiked") else ""
            msg = str(r.get("Message") or r.get("Code") or "")[:40]
            lines.append(f"点赞#{tid} {'OK' if ok else msg}{extra}")
            if ok:
                ok_n += 1
                if ok_n >= remain:
                    lines.append(f"已赞满 {done_today + ok_n}/{upper}，自动停止")
                    break
            time.sleep(0.8)
        try:
            after = self.progress_raw()
            lines.append(
                f"点赞后进度 {after['likeCount']}/{after['likeUpperLimit']} 本进程成功{ok_n}次"
            )
        except Exception:
            lines.append(f"点赞完成{ok_n}/{want}次")
        return lines

    def share(self, threads: List[dict], n: int) -> List[str]:
        """分享：先读今日进度，满则停，硬顶4。"""
        if n <= 0:
            return ["分享关闭"]
        HARD_CAP = 4
        lines, done, upper, remain = self._remain_quota(
            "shareCount", "shareUpperLimit", HARD_CAP, "分享"
        )
        if remain <= 0:
            lines.append("今日分享已满，自动停止")
            return lines
        want = min(n, remain)
        lines.append(f"本轮最多再分享 {want} 次")
        ok_n = 0
        for t in threads[:want]:
            if ok_n >= remain:
                lines.append(f"已达上限 {upper}，停止分享")
                break
            tid = t.get("id") or t.get("threadId")
            r = self.call("POST", "v3/thread.share", body={"threadId": tid})
            ok = r.get("Code") == 0
            msg = str(r.get("Message") or r.get("Code") or "")[:40]
            lines.append(f"分享#{tid} {'OK' if ok else msg}")
            if ok:
                ok_n += 1
                if ok_n >= remain:
                    lines.append(f"已分享满 {done + ok_n}/{upper}，自动停止")
                    break
            time.sleep(0.8)
        try:
            after = self.progress_raw()
            lines.append(
                f"分享后进度 {after['shareCount']}/{after['shareUpperLimit']} 本进程成功{ok_n}次"
            )
        except Exception:
            lines.append(f"分享完成{ok_n}/{want}次")
        return lines

    def hitokoto(self) -> str:
        req = urllib.request.Request("https://v1.hitokoto.cn/?encode=json", headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                d = json.loads(r.read().decode())
            return (d.get("hitokoto") or "").strip()
        except Exception:
            return "今天也要加油鸭"

    def _post_remain(self, lines: List[str], label: str) -> Tuple[int, int, int]:
        """评论/发帖共用：读 postCount / createPostUpperLimit。"""
        try:
            prog = self.progress_raw()
        except Exception as e:
            prog = {}
            lines.append(f"读取今日发帖/评论进度失败: {e}")
        done = int(prog.get("postCount") or 0)
        upper = int(prog.get("createPostUpperLimit") or 1)
        if upper <= 0:
            upper = 1
        remain = max(0, upper - done)
        lines.append(f"今日{label}进度 {done}/{upper}")
        return done, upper, remain

    def comment(self, threads: List[dict], n: int) -> List[str]:
        """评论：与发帖共用今日 post 配额，先读进度，满则停。"""
        if n <= 0:
            return ["评论关闭"]
        lines: List[str] = []
        done, upper, remain = self._post_remain(lines, "评论/发帖")
        if remain <= 0:
            lines.append("今日评论/发帖已满，自动停止")
            return lines
        want = min(n, remain)
        lines.append(f"本轮最多再评论 {want} 次")
        ok_n = 0
        for t in threads[:want]:
            if ok_n >= remain:
                break
            tid = t.get("id") or t.get("threadId")
            content = self.hitokoto()
            r = self.call(
                "POST",
                "v3/posts.create",
                body={"id": tid, "type": 0, "content": content, "source": "iQOO 13"},
            )
            ok = r.get("Code") == 0
            msg = str(r.get("Message") or r.get("Code") or "")[:40]
            lines.append(f"评论#{tid} {'OK' if ok else msg} 「{content[:20]}」")
            if ok:
                ok_n += 1
                if ok_n >= remain:
                    lines.append(f"今日评论/发帖已满 {done + ok_n}/{upper}，停止")
                    break
            time.sleep(1)
        try:
            after = self.progress_raw()
            lines.append(
                f"评论后进度 post={after['postCount']}/{after['createPostUpperLimit']} 本进程成功{ok_n}次"
            )
        except Exception:
            lines.append(f"评论完成{ok_n}/{want}次")
        return lines

    def create_thread(self, n: int) -> List[str]:
        """发帖：先读今日 post 进度，满则停（与评论共用配额）。"""
        if n <= 0:
            return ["发帖关闭"]
        lines: List[str] = []
        done, upper, remain = self._post_remain(lines, "发帖/评论")
        if remain <= 0:
            lines.append("今日发帖/评论已满，自动停止")
            return lines
        want = min(n, remain)
        lines.append(f"本轮最多再发帖 {want} 次")
        ok_n = 0
        for i in range(want):
            if ok_n >= remain:
                break
            text = self.hitokoto()
            title = text[:30]
            r = self.call(
                "POST",
                "v3/thread.create",
                body={
                    "title": title,
                    "content": {"text": text, "indexes": []},
                    "categoryId": 21,
                },
            )
            data = r.get("Data") or {}
            ok = r.get("Code") == 0
            tid = data.get("threadId") if ok else None
            msg = str(r.get("Message") or r.get("Code") or "")[:40]
            lines.append(
                f"发帖{i + 1} {'OK #'+str(tid)+' '+str(data.get('categoryName')) if ok else msg} 「{title[:16]}」"
            )
            if not ok:
                break
            ok_n += 1
            if ok_n >= remain:
                lines.append(f"今日发帖/评论已满 {done + ok_n}/{upper}，停止")
                break
            time.sleep(2)
        try:
            after = self.progress_raw()
            lines.append(
                f"发帖后进度 post={after['postCount']}/{after['createPostUpperLimit']} 本进程成功{ok_n}次"
            )
        except Exception:
            lines.append(f"发帖完成{ok_n}次")
        return lines

    def draw(self, max_times: int) -> List[str]:
        """抽奖：先查今日中奖记录 + 抽奖池，已有或次数不足则跳过。"""
        lines = []
        if max_times <= 0:
            return ["抽奖跳过"]
        from datetime import datetime, timedelta, timezone

        today = datetime.now(timezone(timedelta(hours=8))).strftime("%m-%d")
        d = self.call("GET", "v3/user.winning.list", params={"page": 1})
        wins = (d.get("Data") or {}).get("list") or []
        for w in wins:
            created = str(w.get("created_at") or "")
            if created.startswith(today):
                lines.append(f"今日已抽过（{created} {w.get('prize_name')}），跳过")
                return lines
        d = self.call("GET", "v3/today.draw.count")
        cnt = int((d.get("Data") or {}).get("count") or 0)
        lines.append(f"抽奖池剩余={cnt}（只抽免费1次）")
        # 免费抽不依赖池次数；仍记录日志便于观察
        r = self.call("POST", "v3/luck.draw", body={})
        if r.get("Code") == 0:
            data = r.get("Data") or {}
            lines.append(f"免费抽奖: {data.get('prize_name') or data.get('prize_id')}")
        else:
            msg = str(r.get("Message") or "")
            lines.append(f"免费抽奖: {r.get('Code')} {msg[:60]}")
            if any(x in msg for x in ("已抽", "次数", "上限", "already", "limit")):
                lines.append("抽奖已达上限/已抽过，不再重试")
        return lines


def parse_openids(raw: str) -> List[str]:
    out: List[str] = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("o"):
            out.append(line.split("#")[0].strip())
    return out


def run_account(
    idx: int,
    openid: str,
    sc: str,
    auth: str,
    appid: str,
    browse_n: int,
    like_n: int,
    share_n: int,
    comment_n: int,
    draw_n: int,
    post_n: int,
) -> Dict[str, Any]:
    extras: List[str] = [f"openid {openid[:12]}…"]
    acc: Dict[str, Any] = {
        "account": f"账号{idx}",
        "phone": "",
        "status": "-",
        "reward": "-",
        "extra": extras,
        "error": "",
        "success": False,
    }
    api = IqooApi(appid)
    ok, msg = api.login(sc, auth, openid)
    if not ok:
        acc["status"] = f"登录失败 ❌ ({str(msg)[:50]})"
        acc["error"] = str(msg)[:80]
        return acc
    extras.append(f"登录 {msg}" if msg else "登录 OK")

    score0 = api.score()
    extras.append(f"初始酷币={score0}")

    try:
        p0 = api.progress_raw()
        extras.append(
            f"进度预读 浏览{p0['viewCount']}/{p0['viewUpperLimit']} "
            f"点赞{p0['likeCount']}/{p0['likeUpperLimit']} "
            f"分享{p0['shareCount']}/{p0['shareUpperLimit']} "
            f"发帖{p0['postCount']}/{p0['createPostUpperLimit']}"
        )
    except Exception as e:
        extras.append(f"进度预读失败: {e}")

    sign_line = str(api.sign() or "")
    if any(x in sign_line for x in ("已签", "成功")):
        acc["status"] = f"{sign_line[:40]} ✅"
        acc["success"] = True
    elif "失败" in sign_line or "❌" in sign_line:
        acc["status"] = f"{sign_line[:40]} ❌"
        acc["error"] = sign_line[:80]
    else:
        acc["status"] = sign_line[:40] or "签到结果未知"
        acc["success"] = True
    extras.append(sign_line[:80])

    need = max(like_n, share_n, comment_n, 2)
    threads = api.list_threads(need)
    extras.append(f"候选帖{len(threads)}篇")

    if browse_n > 0:
        try:
            browse_src = api.browse_pool(max(browse_n, 4))
            extras.extend([str(x)[:60] for x in api.browse(browse_src, browse_n)][:3])
        except Exception as e:
            extras.append(f"浏览异常 {e}")
    else:
        extras.append("浏览任务关闭")

    if like_n > 0:
        try:
            extras.extend([str(x)[:60] for x in api.like(threads, like_n)][:3])
        except Exception as e:
            extras.append(f"点赞异常 {e}")
    try:
        extras.extend([str(x)[:60] for x in api.share(threads, share_n)][:3])
    except Exception as e:
        extras.append(f"分享异常 {e}")
    try:
        if comment_n > 0:
            extras.extend([str(x)[:60] for x in api.comment(threads, comment_n)][:3])
    except Exception as e:
        extras.append(f"评论异常 {e}")
    try:
        if post_n > 0:
            extras.extend([str(x)[:60] for x in api.create_thread(post_n)][:2])
    except Exception as e:
        extras.append(f"发帖异常 {e}")
    try:
        extras.extend([str(x)[:60] for x in api.draw(draw_n)][:2])
    except Exception as e:
        extras.append(f"抽奖异常 {e}")

    try:
        p = api.progress()
        extras.append(
            f"今日进度 浏览{p['view']} 点赞{p['like']} 分享{p['share']} 评论{p['post']} 今日分{p['dailyScore']}"
        )
    except Exception:
        pass
    score1 = api.score()
    delta = score1 - score0
    sign = "+" if delta >= 0 else ""
    acc["reward"] = f"{sign}{delta}"
    extras.append(f"酷币 {score0} → {score1}")
    return acc


def main() -> int:
    started = time.time()
    raw = os.getenv("iqoo", "").strip()
    auth = os.getenv("wx_auth", "").strip()
    sc = os.getenv("wx_server_url", "").strip()
    appid = os.getenv("iqoo_appid", "wxcf4266fbc9463132").strip()
    browse_n = int(os.getenv("iqoo_browse", "0") or "0")
    like_n = int(os.getenv("iqoo_like", "4") or "4")
    share_n = int(os.getenv("iqoo_share", "4") or "4")
    comment_n = int(os.getenv("iqoo_comment", "1") or "1")
    draw_n = int(os.getenv("iqoo_draw", "1") or "1")
    post_n = int(os.getenv("iqoo_post", "1") or "1")

    if not raw:
        log.error("缺少 iqoo（openid，多账号换行分隔）")
        return 1
    if not auth:
        log.error("缺少 wx_auth")
        return 1
    if not sc:
        log.error("缺少 wx_server_url（wx_server 地址，勿写进仓库）")
        return 1

    openids = parse_openids(raw)
    if not openids:
        log.error("iqoo 未解析出 openid")
        return 1

    accounts: List[Dict[str, Any]] = []
    for i, oid in enumerate(openids, 1):
        accounts.append(
            run_account(i, oid, sc, auth, appid, browse_n, like_n, share_n, comment_n, draw_n, post_n)
        )
        if i < len(openids):
            time.sleep(3)

    ok_n = sum(1 for a in accounts if a.get("success"))
    try:
        from send_notify import notify_and_format

        notify_and_format(
            "iQOO社区",
            accounts,
            title=f"iQOO社区任务 {ok_n}/{len(accounts)}",
            start_ts=started,
        )
    except Exception as _ne:
        print("[notify] 使用旧格式:", _ne)
        _report = "\n".join(
            f"{'✅' if a.get('success') else '❌'} [{a.get('account')}] {a.get('status')} "
            f"收益={a.get('reward')}"
            for a in accounts
        )
        print(_report)
        try:
            from send_notify import send_notify

            send_notify("iQOO社区任务简报", _report)
        except Exception as _ne2:
            print("[notify] 跳过:", _ne2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
