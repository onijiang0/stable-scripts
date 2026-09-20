#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wxapp 脚本共用推送：放到青龙 wxapp/notify_report.py

优先调用 tools/sendNotify.js 或面板根 sendNotify.js，
失败则走 PushPlus HTTP。
环境变量：PUSHPLUS_TOKEN / PUSH_PLUS_TOKEN / PUSHPLUS_KEY
可选：QL_NOTIFY=0 关闭推送
"""
from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request


def _token() -> str:
    for k in ("PUSHPLUS_TOKEN", "PUSH_PLUS_TOKEN", "PUSHPLUS_KEY"):
        v = (os.getenv(k) or "").strip()
        if v:
            return v
    return ""


def _node_candidates() -> list[str]:
    here = os.path.dirname(os.path.abspath(__file__))
    return [
        os.path.join(here, "..", "tools", "sendNotify.js"),
        "/ql/data/scripts/tools/sendNotify.js",
        "/ql/data/scripts/sendNotify.js",
        "/ql/data/scripts/smallfawn_QLScriptPublic_main/sendNotify.js",
    ]


def _pushplus_http(title: str, content: str) -> bool:
    token = _token()
    if not token:
        print("[notify] 未配置 PUSHPLUS_* token")
        return False
    body = json.dumps(
        {
            "token": token,
            "title": title,
            "content": content,
            "topic": os.getenv("PUSHPLUS_TOPIC") or "",
            "template": "txt",
        },
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://www.pushplus.plus/send",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", "replace")
        print("[notify] PushPlus:", raw[:160])
        return '"code":200' in raw or '"status":200' in raw
    except Exception as e:
        print("[notify] PushPlus 失败:", e)
        return False


def send_ql_notify(title: str, content: str) -> None:
    if (os.getenv("QL_NOTIFY") or "").strip().lower() in ("0", "false", "no"):
        print("[notify] QL_NOTIFY=0，跳过推送")
        return
    node = os.getenv("MIMO_NODE") or "node"
    # Windows 本机可能是 node.exe；青龙容器内是 node
    for cand in ("node", "nodejs"):
        node_bin = node if os.name != "nt" else node
        js = None
        for p in _node_candidates():
            if os.path.isfile(p):
                js = os.path.normpath(p)
                break
        if not js:
            break
        script = (
            "const m=require(process.argv[1]);"
            "const fn=m.sendNotify||m.default||m;"
            "Promise.resolve(fn(process.argv[2],process.argv[3]))"
            ".then(()=>console.log('[notify] node ok'))"
            ".catch(e=>{console.error(e);process.exit(1)});"
        )
        try:
            r = subprocess.run(
                [cand, "-e", script, js, title, content],
                capture_output=True,
                timeout=20,
                text=True,
            )
            print((r.stdout or "")[-300:])
            if r.returncode == 0:
                return
            print((r.stderr or "")[-200:])
        except FileNotFoundError:
            continue
        except Exception as e:
            print("[notify] node 调用异常:", e)
    _pushplus_http(title, content)


if __name__ == "__main__":
    send_ql_notify("notify_report 自测", "推送链路自测内容")
