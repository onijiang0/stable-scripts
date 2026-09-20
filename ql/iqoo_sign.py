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
# 3. 点赞：POST v3/posts.update，**不跳过** isLiked=true（由服务端判定）
# 4. 分享：POST v3/thread.share
# 5. 评论：POST v3/posts.create，内容一言 hitokoto（只取正文，去掉「——出处」）
# 6. 发帖：POST v3/thread.create 到「聊游戏」categoryId=21
# 7. 抽奖：只抽每日免费第一抽；查 v3/user.winning.list 当天已有 created_at 则跳过
# 8. 积分：起止各取一次，日志打印「酷币 a -> b（+delta）」
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

    def browse(self, threads: List[dict], n: int) -> List[str]:
        """尽量用未读帖；服务端 viewCount 可能不立刻涨，日志如实报。"""
        lines = []
        ok_n = 0
        want = max(n, 4)
        for t in threads[:want]:
            tid = t.get("id") or t.get("threadId")
            r = self.call("GET", "v3/thread.detail", params={"threadId": tid})
            ok = r.get("Code") == 0
            lines.append(f"浏览#{tid} {'OK' if ok else r.get('Message')}")
            if ok:
                ok_n += 1
            time.sleep(2)
        p = self.progress()
        lines.append(f"浏览完成{ok_n}篇 任务进度 view={p['view']}")
        return lines

    def like(self, threads: List[dict], n: int) -> List[str]:
        """点赞：任务前先读今日进度；达到上限(默认4)自动停止，防重复执行超赞。

        - 不跳过已赞帖，统一发起 posts.update
        - 以服务端 likeCount/likeUpperLimit 为准，剩余多少赞多少
        - 本进程成功满 4 次（或上限）即停
        """
        lines = []
        if n <= 0:
            return ["点赞关闭"]

        HARD_CAP = 4  # 单日硬顶，防止多开脚本继续加赞
        try:
            prog = self.progress_raw()
        except Exception as e:
            prog = {"likeCount": 0, "likeUpperLimit": HARD_CAP}
            lines.append(f"读取今日进度失败: {e}")

        done_today = prog.get("likeCount", 0)
        upper = int(prog.get("likeUpperLimit") or HARD_CAP)
        upper = min(upper, HARD_CAP)
        remain = max(0, upper - done_today)
        lines.append(f"今日已赞 {done_today}/{upper}（脚本硬顶{HARD_CAP}）")
        if remain <= 0:
            lines.append("今日点赞已满，自动停止")
            return lines

        want = min(n, remain)
        lines.append(f"本轮最多再赞 {want} 次")
        ok_n = 0
        for t in threads[:want]:
            if ok_n >= remain or ok_n >= HARD_CAP:
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

        # 再核一次服务端进度
        try:
            after = self.progress_raw()
            lines.append(
                f"点赞后进度 {after['likeCount']}/{after['likeUpperLimit']} 本进程成功{ok_n}次"
            )
        except Exception:
            lines.append(f"点赞完成{ok_n}/{want}次")
        return lines

    def share(self, threads: List[dict], n: int) -> List[str]:
        lines = []
        ok_n = 0
        for t in threads[:n]:
            tid = t.get("id") or t.get("threadId")
            r = self.call("POST", "v3/thread.share", body={"threadId": tid})
            ok = r.get("Code") == 0
            lines.append(f"分享#{tid} {'OK' if ok else str(r.get('Message'))[:40]}")
            if ok:
                ok_n += 1
            time.sleep(0.8)
        lines.append(f"分享完成{ok_n}次")
        return lines

    def hitokoto(self) -> str:
        req = urllib.request.Request("https://v1.hitokoto.cn/?encode=json", headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                d = json.loads(r.read().decode())
            # 只要正文，去掉「——出处」后缀
            return (d.get("hitokoto") or "").strip()
        except Exception:
            return "今天也要加油鸭"

    def comment(self, threads: List[dict], n: int) -> List[str]:
        lines = []
        ok_n = 0
        for t in threads[:n]:
            tid = t.get("id") or t.get("threadId")
            content = self.hitokoto()
            r = self.call(
                "POST",
                "v3/posts.create",
                body={"id": tid, "type": 0, "content": content, "source": "iQOO 13"},
            )
            ok = r.get("Code") == 0
            lines.append(f"评论#{tid} {'OK' if ok else str(r.get('Message'))[:40]} 「{content[:20]}」")
            if ok:
                ok_n += 1
            time.sleep(1)
        lines.append(f"评论完成{ok_n}次")
        return lines

    def create_thread(self, n: int) -> List[str]:
        lines = []
        for i in range(max(n, 0)):
            text = self.hitokoto()
            title = text[:30]
            r = self.call(
                "POST",
                "v3/thread.create",
                body={
                    "title": title,
                    "content": {"text": text, "indexes": []},
                    "categoryId": 21,  # 聊游戏
                },
            )
            data = r.get("Data") or {}
            ok = r.get("Code") == 0
            tid = data.get("threadId") if ok else None
            lines.append(
                f"发帖{i + 1} {'OK #'+str(tid)+' '+str(data.get('categoryName')) if ok else str(r.get('Code'))+' '+str(r.get('Message'))[:40]} 「{title[:16]}」"
            )
            if not ok:
                break
            time.sleep(2)
        return lines

    def draw(self, max_times: int) -> List[str]:
        """只抽每日免费第一抽；当天已有中奖记录则跳过，防同日重复跑。"""
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
        r = self.call("POST", "v3/luck.draw", body={})
        if r.get("Code") == 0:
            data = r.get("Data") or {}
            lines.append(f"免费抽奖: {data.get('prize_name') or data.get('prize_id')}")
        else:
            lines.append(f"免费抽奖失败: {r.get('Code')} {r.get('Message')}")
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
) -> List[str]:
    lines = [f"—— 账号{idx} {openid[:12]}... ——"]
    api = IqooApi(appid)
    ok, msg = api.login(sc, auth, openid)
    lines.append(("登录OK " + msg) if ok else ("登录失败 " + msg))
    if not ok:
        return lines

    score0 = api.score()
    lines.append(f"初始酷币={score0}")

    lines.append(api.sign())

    need = max(like_n, share_n, comment_n, 2)
    threads = api.list_threads(need)
    lines.append(f"候选帖{len(threads)}篇")

    # 浏览任务默认在脚本内关闭（browse_n=0）；仅当明确 >0 时执行
    if browse_n > 0:
        browse_src = api.browse_pool(max(browse_n, 4))
        lines.append(f"浏览池{len(browse_src)}篇")
        try:
            lines.extend(api.browse(browse_src, browse_n))
        except Exception as e:
            lines.append(f"浏览异常 {e}")
    else:
        lines.append("浏览任务已关闭(脚本内 iqoo_browse=0)")

    if like_n > 0:
        try:
            lines.extend(api.like(threads, like_n))
        except Exception as e:
            lines.append(f"点赞异常 {e}")
    else:
        lines.append("点赞关闭")
    try:
        lines.extend(api.share(threads, share_n))
    except Exception as e:
        lines.append(f"分享异常 {e}")
    try:
        if comment_n > 0:
            lines.extend(api.comment(threads, comment_n))
    except Exception as e:
        lines.append(f"评论异常 {e}")
    try:
        if post_n > 0:
            lines.extend(api.create_thread(post_n))
    except Exception as e:
        lines.append(f"发帖异常 {e}")

    try:
        lines.extend(api.draw(draw_n))
    except Exception as e:
        lines.append(f"抽奖异常 {e}")

    p = api.progress()
    lines.append(
        f"今日进度 浏览{p['view']} 点赞{p['like']} 分享{p['share']} 评论{p['post']} 今日分{p['dailyScore']}"
    )
    score1 = api.score()
    delta = score1 - score0
    sign = "+" if delta >= 0 else ""
    lines.append(f"酷币 {score0} -> {score1}（{sign}{delta}）")
    return lines


def main() -> int:
    raw = os.getenv("iqoo", "").strip()
    auth = os.getenv("wx_auth", "").strip()
    sc = os.getenv("wx_server_url", "").strip()
    appid = os.getenv("iqoo_appid", "wxcf4266fbc9463132").strip()
    # 脚本内默认关闭浏览；如需开启请设 iqoo_browse>0
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

    all_lines: List[str] = []
    for i, oid in enumerate(openids, 1):
        all_lines.extend(
            run_account(i, oid, sc, auth, appid, browse_n, like_n, share_n, comment_n, draw_n, post_n)
        )
        if i < len(openids):
            time.sleep(3)

    print("\n" + "=" * 36)
    print("      iQOO社区任务简报")
    print("=" * 36)
    _report = "\n".join(all_lines)
    print(_report)
    try:
        from notify_report import send_ql_notify

        send_ql_notify("iQOO社区任务简报", _report)
    except Exception as _ne:
        print("[notify] 跳过:", _ne)
    return 0


if __name__ == "__main__":
    sys.exit(main())
