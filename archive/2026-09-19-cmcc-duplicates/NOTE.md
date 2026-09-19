# 归档：10086 签到重复实现

- **归档日期**：2026-09-19
- **归档原因**：10086 签到长期存在三份并行实现，变量名同为 `cmcc`。
  同一账号若在不同设备/面板分别启用，会重复签到、互相干扰，且三者维护成本重复。
  现统一为 `ql/cmcc_sign.py` 一份。
- **被谁取代**：`ql/cmcc_sign.py`（小程序 SSO 全链路，持续维护中）

## 三份实现对照

| 文件 | 状态 | 走的链路 | 变量名 | cron | 最后改动 |
|---|---|---|---|---|---|
| `../../ql/cmcc_sign.py` | **保留** | 小程序 SSO 全链路：`/wx/code` → `wmhnewcenter/wechat86-applet/login` → `wmhsso` → `qwhdmark/{id}` → `mark/do/mark` | `cmcc` | `30 14 * * *` | 2026-09-19 |
| `cmcc_sign.js` | 归档 | `/qwhdhub/api/mark/do/mark` 直连（qwhdmark 接口，需自行拿 `QWHD_SESSION_TOKEN`） | `cmcc` | `30 8 * * *` | 2026-09-14 |
| `cmcc_sign_oauth.js` | 归档 | smallcat `/wx/oauth` + 完整跳转链（Node 版，尝试提取 `QWHD_SESSION_TOKEN`） | `cmcc` | `30 8 * * *` | 2026-09-15 |

后两份都是 `cmcc_sign.py` 打通 SSO 链路之前的**过渡试错产物**：`cmcc_sign.js` 是直连接口版，
`cmcc_sign_oauth.js` 是公众号 OAuth 探路版，都在 SSO 链路稳定后失去价值。

## 最后可用版本

| 归档文件 | 原路径 | 最后修改提交 | blob |
|---|---|---|---|
| `cmcc_sign.js` | `ql/cmcc_sign.js` | `aa86bf2`（2026-09-14） | `ab72f7ba20dc9d8719a4c3a764141683b36b0834` |
| `cmcc_sign_oauth.js` | `ql/cmcc_sign_oauth.js` | `38e0744`（2026-09-15） | `4aa681c83545b2de1f5d181f6e2d99d7bc14ee1b` |

## 恢复方法

整个文件移回 `ql/`（`git mv` 会保留历史）：

```bash
git mv archive/2026-09-19-cmcc-duplicates/cmcc_sign.js ql/cmcc_sign.js
```

只想临时看一眼内容，不必恢复：

```bash
git show ab72f7ba20dc9d8719a4c3a764141683b36b0834
```

> ⚠️ 恢复到 `ql/` 之前，**先停用青龙面板里当前启用的那条 10086 任务**，
> 否则 `cmcc_sign.py` 与恢复的脚本会同时签到同一账号。
