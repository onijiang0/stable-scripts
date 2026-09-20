#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import subprocess
from typing import Optional


def send_notify(title: str, content: str) -> None:
    """推送只走 tools/sendNotify.js；不读 PushPlus 等推送环境变量。"""
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
    if not js:
        print("[notify] 未找到 tools/sendNotify.js")
        return
    runner = (
        "const m=require(process.argv[1]);"
        "const fn=m.sendNotify||m.default||m;"
        "Promise.resolve(fn(process.argv[2],process.argv[3]))"
        ".then(()=>console.log('[notify] sendNotify.js ok'))"
        ".catch(e=>{console.error(e);process.exit(1)});"
    )
    for node in (os.environ.get("node") or "node", "nodejs", "node"):
        try:
            r = subprocess.run(
                [node, "-e", runner, js, title, content],
                capture_output=True,
                timeout=25,
                text=True,
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


# 兼容旧调用名
send_ql_notify = send_notify
