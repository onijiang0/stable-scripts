#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import subprocess
from typing import Optional


def send_notify(title: str, content: str) -> None:
    if (os.getenv("QL_NOTIFY") or "").strip().lower() in ("0", "false", "no"):
        print("[notify] QL_NOTIFY=0，跳过推送")
        return
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, "..", "tools", "sendNotify.js"),
        "/ql/data/scripts/tools/sendNotify.js",
        "/ql/data/scripts/sendNotify.js",
    ]
    js: Optional[str] = None
    for p in candidates:
        if os.path.isfile(p):
            js = os.path.normpath(p)
            break
    if js:
        runner = (
            "const m=require(process.argv[1]);"
            "const fn=m.sendNotify||m.default||m;"
            "Promise.resolve(fn(process.argv[2],process.argv[3]))"
            ".then(()=>console.log('[notify] sendNotify.js ok'))"
            ".catch(e=>{console.error(e);process.exit(1)});"
        )
        for node in (os.environ.get("MIMO_NODE") or "node", "nodejs", "node"):
            try:
                r = subprocess.run(
                    [node, "-e", runner, js, title, content],
                    capture_output=True, timeout=25, text=True,
                )
                if r.stdout:
                    print(r.stdout.strip()[-200:])
                if r.returncode == 0:
                    return
                if r.stderr:
                    print(r.stderr.strip()[-200:])
            except FileNotFoundError:
                continue
            except Exception as e:
                print("[notify] node 异常:", e)
    token = (
        os.getenv("PUSHPLUS_TOKEN")
        or os.getenv("PUSH_PLUS_TOKEN")
        or os.getenv("PUSHPLUS_KEY")
        or ""
    ).strip()
    if not token:
        print("[notify] 无 PUSHPLUS token，且 sendNotify.js 未成功")
        return
    try:
        import requests

        resp = requests.post(
            "https://www.pushplus.plus/send",
            json={
                "token": token,
                "title": title,
                "content": content,
                "topic": os.getenv("PUSHPLUS_TOPIC") or "",
                "template": "txt",
            },
            timeout=15,
        )
        print("[notify] PushPlus", resp.text[:120])
    except Exception as e:
        print("[notify] PushPlus 失败:", e)


# 兼容旧调用名
send_ql_notify = send_notify
