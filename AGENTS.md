#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
stable-scripts 给任何 coding agent 用的项目说明。
密钥/账号在面板 env 与工作区 _AGENT_HANDOFF.md（勿写进本文件）。
"""

# 目标
# 维护青龙签到脚本：GitHub onijiang0/stable-scripts → ql/
# 青龙目录 wxapp/；只上传面板没有的业务脚本。
# 本地路径：C:\Users\理塘王\.workbuddy\workspace\stable-scripts

# 硬性规范
# 1. 顶部注释：@Author onijiang0 / @Date / @Description / cron / 变量名 / 变量值
#    / 依赖变量 / 已实现 / 契约 / 踩坑
# 2. 平台业务参数（appid、storeId、secretKey、ipRId 等）写进脚本默认值
# 3. 账号相关只进环境变量：openid、token、sessionKey、手机号、
#    wx_server_url / wx_auth 真值
# 4. 默认不缓存 token；每次 code 登录
# 5. 日志脱敏 openid/token；业务结果写清积分/奖品
# 6. 推送：from send_notify import notify_and_format
#    notify_and_format(task, accounts, title=..., start_ts=started)
# 7. cron 10-19 点随机，避开整点/半点
# 8. 代码/注释不出现 wx_server 字样
# 9. Git 提交用：git -c user.name=mimo -c user.email=bot@local
# 10. 未要求不要 push；「铺设」=青龙上传 + cron

# 环境变量（使用人/面板配置）
# wx_server_url   取码服务地址
# wx_auth         取码鉴权
# tebu            特步 openid（微盟 wx40915...）
# yzf             翼支付 openid（wx1c4a70bbdfaa2029）
# YZF_SESSION     翼支付 sessionKey（会过期）
# yzf_phone       翼支付手机号，查积分用
# QL_NOTIFY       0 关闭推送

# 平台契约摘要
# 特步：xapi.weimob.com + loginX + onecrm sign/signMainInfo
# 翼支付：spanner.bestpay.com.cn:10081 + authTokenLogin + SignInService.signIn
#         浏览任务 sendTaskMessAge + receiveTaskAward
# 详见工作区 yzf-sign-notes.md 与 _AGENT_HANDOFF.md

# 跑通前检查
# python -c "import ast;ast.parse(open('ql/xxx_sign.py',encoding='utf-8').read())"
# node -e "new Function(require('fs').readFileSync('ql/yzf_mgs_client.js','utf8'))"
# 青龙手动：task wxapp/xxx_sign.py
