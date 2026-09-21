#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# /*
# ------------------------------------------
# @Author: onijiang0
# @Date: 2026.09.20
# @Description: 推送与简报封装（库文件，供 ql/ 下签到脚本 import，非独立定时任务）
# ------------------------------------------
# 用法：from send_notify import notify_and_format
#       notify_and_format(task, accounts, title=..., start_ts=...)
#
# 推送链路：优先执行 ../tools/sendNotify.js，失败回落 PushPlus HTTP。
# 环境变量：PUSHPLUS_TOKEN / PUSH_PLUS_TOKEN / PUSHPLUS_KEY（任一）；
#           PUSHPLUS_TOPIC 可选；QL_NOTIFY=0 关闭推送。
#
# 内置脱敏：mask_phone / mask_id；clean_line 丢弃原始 JSON 与超长串。
# ------------------------------------------
# */
from __future__ import annotations

import os
import re
import subprocess
import time
from typing import Any, Dict, List, Optional, Sequence, Union

SEP = "=" * 30
SUB = "-" * 30

# 原始/截断 JSON，禁止进入日志与推送
_RAW_JSON_RE = re.compile(
    r"(\{\s*\"month\"|\"result-status\"|\"encryptData\"|\"sessionKey\"|"
    r"\"accessToken\"|\"token\"\s*:|^\s*\[?\{\s*\"|^\s*\"\w+\"\s*:\s*\{)"
)


def mask_phone(phone: Any) -> str:
    s = str(phone or "").strip()
    digits = re.sub(r"\D", "", s)
    if len(digits) >= 11:
        return digits[:3] + "****" + digits[-4:]
    if len(s) >= 7:
        return s[:3] + "****" + s[-4:]
    return s or "-"


def mask_id(value: Any, keep: int = 8) -> str:
    s = str(value or "")
    return (s[:keep] + "***") if len(s) > keep else (s or "-")


def clean_line(line: Any) -> str:
    """丢弃原始 JSON / 超长截断串，只保留可读短句。"""
    s = str(line or "").strip()
    if not s:
        return ""
    if _RAW_JSON_RE.search(s) and len(s) > 60:
        return ""
    if s.startswith('{"') or s.startswith("[{") or s.startswith('{"month"'):
        return ""
    if len(s) > 180:
        s = s[:180] + "…"
    return s


def filter_lines(lines: Sequence[Any]) -> List[str]:
    out: List[str] = []
    for x in lines or []:
        s = clean_line(x)
        if s:
            out.append(s)
    return out


def _account_identity(account: Any, phone: Any) -> str:
    nick = str(account or "").strip() or "未知账号"
    ph = mask_phone(phone) if phone else ""
    if ph and ph != "-":
        return f"{nick} ({ph})"
    return nick


def format_account_block(
    *,
    account: Any = "微信用户",
    phone: Any = "",
    status: Any = "",
    reward: Any = "",
    month_days: Any = "",
    patch_card: Any = None,
    extra: Union[str, Sequence[str], None] = None,
    error: Any = "",
) -> List[str]:
    """单账号字段块（不含推送/耗时脚注）。"""
    lines: List[str] = [f"👤 账号信息：{_account_identity(account, phone)}"]
    st = clean_line(status) or "-"
    if error and "✅" not in st:
        err = clean_line(error)
        if err:
            st = f"{st} | {err}" if st != "-" else err
    mark = "✅" if ("✅" in st or "成功" in st or "已签" in st) else ("❌" if error or "失败" in st else "")
    if mark and mark not in st:
        st = f"{st} {mark}".strip()
    lines.append(f"📝 签到状态：{st}")

    rw = clean_line(reward)
    if rw and rw not in ("-", "0", "None"):
        if not re.search(r"[+\-]?\d", rw) and "积分" not in rw and "奖" not in rw:
            rw = rw
        lines.append(f"🎁 本次收益：{rw}")

    parts: List[str] = []
    if month_days not in (None, "", "-", "?"):
        parts.append(f"本月已签 {month_days} 天")
    if patch_card not in (None, "", "-"):
        parts.append(f"补签卡 {patch_card}")
    if parts:
        lines.append(f"📅 累计数据：{' | '.join(parts)}")

    if extra:
        if isinstance(extra, str):
            extra_list = [extra]
        else:
            extra_list = list(extra)
        extras = filter_lines(extra_list)
        if extras:
            # 全局累计与单账号一致时只保留一次
            seen = set()
            uniq = []
            for e in extras:
                key = re.sub(r"\s+", "", e)
                if key in seen:
                    continue
                seen.add(key)
                uniq.append(e)
            lines.append("📌 补充信息：")
            lines.extend(f"   {e}" for e in uniq[:8])
    return lines


def format_report(
    task: str,
    accounts: Sequence[Dict[str, Any]],
    *,
    push_result: str = "",
    cost_s: Optional[Union[int, float]] = None,
) -> str:
    """
    统一简报：
    ==============================
    🛒 任务名称：xxx
    👤 账号信息：...
    ------------------------------
    📝 签到状态：...
    🎁 本次收益：...
    📅 累计数据：...
    🔔 推送结果：企业微信推送成功
    ⏱️ 执行耗时：4 秒
    ==============================
    """
    lines: List[str] = [SEP, f"🛒 任务名称：{task}"]
    for acc in accounts or []:
        lines.extend(
            format_account_block(
                account=acc.get("account") or acc.get("nickname") or acc.get("name") or "微信用户",
                phone=acc.get("phone") or acc.get("mobile") or "",
                status=acc.get("status") or acc.get("sign") or "",
                reward=acc.get("reward") or acc.get("points") or "",
                month_days=acc.get("month_days") if acc.get("month_days") is not None else acc.get("days"),
                patch_card=acc.get("patch_card") if acc.get("patch_card") is not None else acc.get("supp"),
                extra=acc.get("extra"),
                error=acc.get("error") or "",
            )
        )
    lines.append(SUB)
    pr = clean_line(push_result) or "未推送"
    lines.append(f"🔔 推送结果：{pr}")
    if cost_s is not None:
        try:
            cs = int(round(float(cost_s)))
        except Exception:
            cs = cost_s
        lines.append(f"⏱️ 执行耗时：{cs} 秒")
    lines.append(SEP)
    return "\n".join(lines)


def _summarize_channel(text: str, returncode: int) -> str:
    low = (text or "").lower()
    if returncode not in (0, None) and "ok: true" not in low and "sendNotify.js ok" not in low:
        # 允许 stdout 已标记成功
        if "成功" not in (text or "") and "ok" not in low:
            return "推送失败"
    pairs = (
        (("企业微信", "qywx", "wework", "wechat work"), "企业微信推送成功"),
        (("钉钉", "dingtalk", "dingding"), "钉钉推送成功"),
        (("telegram", "tgbot"), "Telegram推送成功"),
        (("pushplus", "push+"), "PushPlus推送成功"),
        (("bark",), "Bark推送成功"),
        (("server酱", "serverchan", "sctapi"), "Server酱推送成功"),
        (("gotify",), "Gotify推送成功"),
        (("ntfy",), "Ntfy推送成功"),
        (("iGot", "igot"), "iGot推送成功"),
        (("wxpusher",), "WxPusher推送成功"),
    )
    for keys, label in pairs:
        for k in keys:
            if k.lower() in low:
                return label
    if "ok: true" in low or "sendNotify.js ok" in low or returncode == 0:
        return "推送成功"
    if "成功" in (text or ""):
        return "推送成功"
    return "推送失败"


def _safe_print_push(result: str) -> None:
    print(f"🔔 推送结果：{result}")


def send_notify(title: str, content: str) -> str:
    """
    调用 tools/sendNotify.js；失败时 PushPlus 兜底。
    只向控制台打印简短结果，不打印完整 Response。
    返回：如「企业微信推送成功」
    """
    if (os.getenv("QL_NOTIFY") or "").strip().lower() in ("0", "false", "no"):
        msg = "已关闭推送（QL_NOTIFY=0）"
        _safe_print_push(msg)
        return msg

    content = _sanitize_push_content(content)
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
            ".catch(e=>{console.error(e&&e.message?e.message:e);process.exit(1)});"
        )
        for node in (os.environ.get("MIMO_NODE") or os.environ.get("node") or "node", "nodejs", "node"):
            try:
                r = subprocess.run(
                    [node, "-e", runner, js, title, content],
                    capture_output=True, timeout=25, text=True,
                )
                blob = ((r.stdout or "") + "\n" + (r.stderr or "")).strip()
                if r.returncode == 0:
                    result = _summarize_channel(blob, r.returncode)
                    _safe_print_push(result)
                    return result
                # node 有输出但非 0：仍尝试识别是否某通道成功
                if "ok: true" in blob.lower() or "推送成功" in blob or "sendNotify.js ok" in blob:
                    result = _summarize_channel(blob, 0)
                    _safe_print_push(result)
                    return result
                # 不打印完整 Response
                brief = clean_line(blob.splitlines()[-1] if blob else "sendNotify.js 失败")
                # 继续走 PushPlus 兜底
                last_err = brief or "sendNotify.js 失败"
            except FileNotFoundError:
                continue
            except Exception as e:
                last_err = f"node 异常: {e}"
                continue
        else:
            last_err = "sendNotify.js 未成功"
    else:
        last_err = "未找到 tools/sendNotify.js"

    token = (
        os.getenv("PUSHPLUS_TOKEN")
        or os.getenv("PUSH_PLUS_TOKEN")
        or os.getenv("PUSHPLUS_KEY")
        or ""
    ).strip()
    if not token:
        result = f"推送失败（{last_err}）"
        _safe_print_push(result)
        return result
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
        text = resp.text or ""
        ok = resp.ok and ("200" in text[:20] or "成功" in text)
        result = "PushPlus推送成功" if ok else "PushPlus推送失败"
        _safe_print_push(result)
        return result
    except Exception as e:
        result = f"推送失败（{clean_line(e) or e}）"
        _safe_print_push(result)
        return result


def _sanitize_push_content(content: str) -> str:
    s = str(content or "")
    cleaned = []
    for line in s.splitlines():
        c = clean_line(line)
        if c or line.strip() in (SEP, SUB, "") or set(line.strip()) <= set("="):
            cleaned.append(c if c else line)
        elif line.strip().startswith(("🛒", "👤", "📝", "🎁", "📅", "🔔", "⏱️", "📌", "✅", "❌", "——")):
            cleaned.append(line)
        elif len(line.strip()) < 80 and not _RAW_JSON_RE.search(line):
            cleaned.append(line)
    return "\n".join(cleaned) if cleaned else s


def notify_and_format(
    task: str,
    accounts: Sequence[Dict[str, Any]],
    *,
    title: Optional[str] = None,
    start_ts: Optional[float] = None,
    echo: bool = True,
) -> str:
    """发送推送并返回完整简报（控制台会打印推送结果 + 完整报告）。"""
    if start_ts is None:
        cost: Optional[float] = None
    else:
        cost = max(0.0, time.time() - start_ts)

    body = format_report(task, accounts, push_result="", cost_s=None)
    # 推送正文不含「推送结果/耗时」——发送后才知道结果
    push_body = format_report(task, accounts, push_result="（发送中）", cost_s=None)
    # 简化：正文只要账号块
    push_body = "\n".join(
        [SEP, f"🛒 任务名称：{task}"]
        + [
            line
            for acc in accounts or []
            for line in format_account_block(
                account=acc.get("account") or acc.get("nickname") or acc.get("name") or "微信用户",
                phone=acc.get("phone") or acc.get("mobile") or "",
                status=acc.get("status") or acc.get("sign") or "",
                reward=acc.get("reward") or acc.get("points") or "",
                month_days=acc.get("month_days") if acc.get("month_days") is not None else acc.get("days"),
                patch_card=acc.get("patch_card") if acc.get("patch_card") is not None else acc.get("supp"),
                extra=acc.get("extra"),
                error=acc.get("error") or "",
            )
        ]
        + [SEP]
    )

    push_result = send_notify(title or task, push_body)
    report = format_report(task, accounts, push_result=push_result, cost_s=cost)
    if echo:
        print(report)
    return report


# 兼容旧调用名
send_ql_notify = send_notify
