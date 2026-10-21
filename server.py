#!/usr/bin/env python3
"""手机端 HTTP API + 网页：状态查询、消息、审批操作。标准库实现，零依赖。

这是混合架构里"云端大脑"露给手机端的接口：手机只做感官和手，
通过这几个接口看状态、上报本地数据、点批准/拒绝。
GET / 直接返回配套网页 page.html（与本文件同目录），手机浏览器打开
填一次 token 即可看实时状态/消息/审批，数据全部走下面的 token 接口。

启动: AGENT_API_TOKEN=xxx python3 server.py [--host 0.0.0.0] [--port 8000]
  - 本机调试: 默认只绑 127.0.0.1，可不设 token
  - 手机直连（2026-10-20 用户定版）: --host 0.0.0.0 公网可达，
    AGENT_API_TOKEN 是唯一的门，token 泄露 = 接口全开，务必保密

已知取舍: 和 agent.py 主循环共用 state.json，并发写可能互相覆盖；
 scaffold 阶段可接受，后续换 SQLite 或加文件锁。
"""
import argparse
import json
import os
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import agent  # 复用 load_state / save_state / save_approvals / log

TOKEN = os.environ.get("AGENT_API_TOKEN", "")

# 对话接口直连 OpenRouter（与 agent.decide 同一把 key、同一个模型口径）。
# key 由 systemd 注入本进程环境；没有 key 时 /chat 返回 501。
OR_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OR_MODEL = os.environ.get("OPENROUTER_MODEL", "openrouter/auto")
OR_URL = "https://openrouter.ai/api/v1/chat/completions"

CHAT_SYSTEM = """你是运行在用户云服务器上的 always-on 个人智能体，每 5 分钟醒来
检查一次收件箱等事项，平时通过网页和用户交流。用户现在通过网页直接和你说话。

要求：
- 用中文，简短直接，先给结论，不寒暄。
- 你能汇报自己的实时状态（运行轮次、收件箱水位、待审批、未读消息），数据在下方上下文里。
- 你目前只能对话和汇报，不能执行动作；用户让你做事时，说明这类动作要走审批流程，现在先记下需求。
- 不知道的事就说不知道，不要编造。"""

_PAGE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "page.html")
try:
    with open(_PAGE_PATH, encoding="utf-8") as _f:
        PAGE_HTML = _f.read()
except OSError:
    PAGE_HTML = "<h1>page.html 缺失</h1><p>网页文件不在 server.py 同目录。</p>"


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html):
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth(self):
        if not TOKEN:
            return True  # 没设 token = 本机调试模式
        return self.headers.get("Authorization") == f"Bearer {TOKEN}"

    def _pending_approvals(self, state):
        return [a for a in state.get("approval_queue", [])
                if a.get("status") == "pending"]

    def _code_version(self):
        # 自动更新脚本每次拉取后把 commit 号写入同目录 .deployed_commit。
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            ".deployed_commit")
        try:
            with open(path, encoding="utf-8") as f:
                return f.read().strip()[:12] or None
        except OSError:
            return None

    def _chat(self, state, data):
        """POST /chat：把用户消息 + 精简历史 + 实时状态喂给模型，返回回复文本。"""
        if not OR_KEY:
            return 501, {"error": "model_not_connected",
                         "note": "本进程没有 OPENROUTER_API_KEY"}
        msg = data.get("message")
        if not isinstance(msg, str) or not msg.strip():
            return 400, {"error": "empty message"}
        msg = msg.strip()[:4000]
        history = []
        raw_hist = data.get("history")
        if isinstance(raw_hist, list):
            for h in raw_hist[-10:]:
                if (isinstance(h, dict) and h.get("role") in ("user", "assistant")
                        and isinstance(h.get("content"), str)):
                    history.append({"role": h["role"],
                                    "content": h["content"][:2000]})
        pending = self._pending_approvals(state)
        recent = [e.get("text", "")[:120] for e in state.get("outbox", [])[-3:]]
        context = {
            "tick_count": state.get("tick_count", 0),
            "code_version": self._code_version(),
            "last_tick": state.get("last_tick"),
            "inbox_waterline": state.get("inbox_waterline"),
            "inbox_alerts": state.get("inbox_alerts"),
            "pending_approvals": len(pending),
            "undelivered": len([e for e in state.get("outbox", [])
                                if not e.get("delivered")]),
            "recent_outbox": recent,
        }
        system = (CHAT_SYSTEM + "\n\n当前状态："
                  + json.dumps(context, ensure_ascii=False))
        body = json.dumps({
            "model": OR_MODEL,
            "messages": ([{"role": "system", "content": system}]
                         + history
                         + [{"role": "user", "content": msg}]),
            "temperature": 0.5,
            "max_tokens": 800,
        }).encode("utf-8")
        req = urllib.request.Request(
            OR_URL, data=body,
            headers={"Authorization": f"Bearer {OR_KEY}",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                result = json.load(resp)
        except urllib.error.HTTPError as e:
            agent.log(f"[对话] 模型接口 HTTP {e.code}")
            return 502, {"error": "model_http_error", "code": e.code}
        except Exception as e:
            agent.log(f"[对话] 模型调用失败: {e}")
            return 502, {"error": "model_call_failed"}
        try:
            reply = result["choices"][0]["message"]["content"].strip()
        except Exception:
            return 502, {"error": "bad model response"}
        agent.log(f"[对话] 用户: {msg[:60]} -> 回复 {len(reply)} 字")
        return 200, {"reply": reply}

    # ---------- 读 ----------
    def do_GET(self):
        path = urlparse(self.path).path  # 去掉 ?t= 等查询参数再路由
        if path in ("/", "/index.html"):
            # 网页本体只是空壳，不含任何数据；数据走下面 token 接口。
            return self._send_html(PAGE_HTML)
        if path == "/health":
            return self._send(200, {"ok": True})
        if not self._auth():
            return self._send(401, {"error": "unauthorized"})
        state = agent.load_state()
        if path == "/status":
            return self._send(200, {
                "tick_count": state.get("tick_count", 0),
                "code_version": self._code_version(),
                "last_tick": state.get("last_tick"),
                "inbox_waterline": state.get("inbox_waterline"),
                "inbox_alerts": state.get("inbox_alerts"),
                "pending_approvals": len(self._pending_approvals(state)),
                "undelivered": len([e for e in state.get("outbox", [])
                                    if not e.get("delivered")]),
                "phone_data": state.get("phone_data", {}),
            })
        if path == "/outbox":
            items = [e for e in state.get("outbox", []) if not e.get("delivered")]
            return self._send(200, {"items": items})
        if path == "/approvals":
            return self._send(200, {"items": self._pending_approvals(state)})
        return self._send(404, {"error": "not found"})

    # ---------- 写 ----------
    def do_POST(self):
        path = urlparse(self.path).path
        if not self._auth():
            return self._send(401, {"error": "unauthorized"})
        length = int(self.headers.get("Content-Length", 0))
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return self._send(400, {"error": "bad json"})
        state = agent.load_state()

        if path == "/report":
            # 手机端上报本地数据（电量/位置/日历/通知等），原样存入
            # state["phone_data"]，后续可做成新的感知工具。
            state["phone_data"] = data if isinstance(data, dict) else {}
            agent.save_state(state)
            agent.log(f"[手机上报] {list(state['phone_data'].keys())}")
            return self._send(200, {"ok": True})

        if path == "/chat":
            # 对话接口：带实时状态上下文直连模型（实现在 _chat）。
            code, obj = self._chat(state, data)
            return self._send(code, obj)

        if path in ("/approve", "/deny"):
            pending = self._pending_approvals(state)
            idx = data.get("index")
            if not isinstance(idx, int) or not 0 <= idx < len(pending):
                return self._send(400, {"error": "bad index"})
            pending[idx]["status"] = ("approved" if path == "/approve"
                                      else "denied")
            agent.save_state(state)
            agent.save_approvals(state.get("approval_queue", []))
            agent.log(f"[审批] {pending[idx]['action']} -> {pending[idx]['status']}")
            return self._send(200, {"ok": True, "status": pending[idx]["status"]})

        return self._send(404, {"error": "not found"})

    def log_message(self, *args):
        pass  # 不刷屏，关键事件走 agent.log


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    if args.host != "127.0.0.1" and not TOKEN:
        print("警告: 监听非本机地址但未设置 AGENT_API_TOKEN，审批接口无保护!")
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    agent.log(f"API 启动: http://{args.host}:{args.port}")
    print(f"API 启动: http://{args.host}:{args.port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
