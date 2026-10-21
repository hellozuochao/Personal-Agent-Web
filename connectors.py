#!/usr/bin/env python3
"""连接器注册表（v1）：每个连接器声明它是谁、怎么授权、连上给 agent
什么能力。状态一律现场计算，不落库；令牌只存各自的私有位置。

v1 收编两个连接器，正好覆盖两种授权原型：
- google（oauth2_device）：用户自助授权——网页点"连接"，用户在 Google
  页面输码同意，令牌存私有目录 google_token.json。断开 = 删令牌，
  客户端配置保留，可重新连接。
- openrouter（managed_key）：部署注入的密钥，网页只看状态，不能断开。

新增连接器 = 在 CONNECTORS 里加一条 + 在 tools.py 注册它提供的工具，
网页的连接器页签会自动渲染，不用改页面结构。
"""
import os

import google_auth


def _google_status():
    if not google_auth.client_configured():
        return {"state": "unconfigured", "detail": "服务器未放置客户端配置"}
    if google_auth.connected():
        return {"state": "connected",
                "detail": "已授权：" + (google_auth.token_scope() or "")}
    return {"state": "ready", "detail": "客户端已配置，尚未授权"}


def _openrouter_status():
    if os.environ.get("OPENROUTER_API_KEY"):
        return {"state": "connected", "detail": "密钥已由部署配置注入"}
    return {"state": "unconfigured", "detail": "未注入 OPENROUTER_API_KEY"}


CONNECTORS = [
    {
        "id": "google",
        "name": "Google（Gmail + 日历）",
        "auth": "oauth2_device",
        "description": "只读访问 Gmail 邮件元数据与 Google 日历，给 agent 装上邮件和日程感官。",
        "provides": ["gmail_sense 邮件感官", "calendar_sense 日历感官"],
        "status": _google_status,
        "disconnectable": True,
    },
    {
        "id": "openrouter",
        "name": "OpenRouter（模型通道）",
        "auth": "managed_key",
        "description": "agent 的大脑通道：每轮决策与网页对话都经它调用大模型。",
        "provides": ["decide 模型决策", "chat 网页对话"],
        "status": _openrouter_status,
        "disconnectable": False,
    },
]


def list_status():
    out = []
    for c in CONNECTORS:
        s = c["status"]()
        out.append({
            "id": c["id"], "name": c["name"], "auth": c["auth"],
            "description": c["description"], "provides": c["provides"],
            "disconnectable": c["disconnectable"],
            "state": s["state"], "detail": s["detail"],
        })
    return out


def get(connector_id):
    for c in CONNECTORS:
        if c["id"] == connector_id:
            return c
    return None


def disconnect(connector_id):
    """断开连接器。返回 True 表示已断开（或本就无可断）。"""
    if connector_id == "google":
        try:
            os.remove(google_auth.TOKEN_FILE)
        except OSError:
            pass
        return True
    return False
