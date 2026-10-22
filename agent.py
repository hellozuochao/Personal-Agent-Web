#!/usr/bin/env python3
"""always-on agent 最小闭环：定时醒来 -> 感知 -> 决策 -> 行动 -> 安静输出。

设计铁律（来自 Grok Bot / Muse 拆解）：
1. 状态与机器分离：状态只活在 state.json，进程随时可杀可重建。
2. 能闭嘴：只有"实质变化"或"需要你拍板"才说话，否则只写日志。
3. 审批前置：高风险动作先进 approvals.json 队列，等人点头才执行。
4. 工具按成本升序调用。
"""
import argparse
import datetime
import json
import os
import time

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE, "state.json")
APPROVALS_FILE = os.path.join(BASE, "approvals.json")
LOG_FILE = os.path.join(BASE, "agent.log")
TICK_SECONDS = int(os.environ.get("AGENT_TICK_SECONDS", "300"))

from tools import TOOLS, refresh as _refresh_tools  # noqa: E402
from executors import EXECUTORS, execute as _execute_item  # noqa: E402
import memory as agent_memory  # noqa: E402


# ---------- 状态 ----------
DEFAULT_STATE = {
    "tick_count": 0,
    "last_tick": None,
    "inbox_waterline": None,
    "inbox_alerts": 0,
    "approval_queue": [],
    "outbox": [],
    "phone_data": {},
}


def load_state():
    if not os.path.exists(STATE_FILE):
        return dict(DEFAULT_STATE)  # 全新部署：从默认状态起步
    with open(STATE_FILE, encoding="utf-8") as f:
        state = json.load(f)
    for k, v in DEFAULT_STATE.items():
        state.setdefault(k, v)  # 老状态缺字段时补齐
    return state


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)  # 原子写：崩了也不丢状态


def log(msg):
    line = f"{datetime.datetime.now().isoformat(timespec='seconds')} {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ---------- 输出纪律：没实质变化就不说话 ----------
SPEAK_DEDUP_HOURS = 24  # 同一句话 24 小时内只说一次，防止同一问题每轮重复播报


def maybe_speak(state, text, reason):
    """reason 只能是 'material_change'（实质变化）或 'needs_decision'（需要拍板）。
    其他情况一律只记日志——安静是功能，不是 bug。
    同一文本在 SPEAK_DEDUP_HOURS 内已播报过 -> 只记日志，不再进 outbox。"""
    assert reason in ("material_change", "needs_decision"), f"非法输出理由: {reason}"
    now = datetime.datetime.now()
    history = [h for h in state.get("speak_log", [])
               if (now - datetime.datetime.fromisoformat(h["ts"]))
               < datetime.timedelta(hours=SPEAK_DEDUP_HOURS * 2)]
    if any(h["text"] == text
           and (now - datetime.datetime.fromisoformat(h["ts"]))
           < datetime.timedelta(hours=SPEAK_DEDUP_HOURS)
           for h in history):
        state["speak_log"] = history
        log(f"[播报去重] 24 小时内已报过，跳过: {text[:40]}")
        return
    history.append({"ts": now.isoformat(timespec="seconds"), "text": text})
    state["speak_log"] = history
    entry = {"ts": now.isoformat(timespec="seconds"),
             "reason": reason, "text": text, "delivered": False}
    state.setdefault("outbox", []).append(entry)
    log(f"[SPEAK:{reason}] {text}")


# ---------- 审批链 ----------
def request_approval(state, action, reason, exec_payload=None):
    """高风险动作不直接执行，先进队列等人点头。
    同一动作已在 pending 中 -> 不重复入队、不重复播报（模型每轮都提也只留一条）。
    exec_payload 是机器可读的执行载荷（{"type": ..., "params": {...}}），
    有它、且类型在执行器注册表里，批准后才真的干活；没有就只是文本提案。"""
    queue = state.setdefault("approval_queue", [])
    if any(item.get("action") == action and item.get("status") == "pending"
           for item in queue):
        log(f"[审批去重] 已在待审批中，跳过: {action[:40]}")
        return
    item = {"ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "action": action, "reason": reason, "status": "pending"}
    if exec_payload:
        item["exec"] = exec_payload
    queue.append(item)
    save_approvals(queue)
    maybe_speak(state, f"有个动作等你拍板：{action}（{reason}）", "needs_decision")


def save_approvals(queue):
    with open(APPROVALS_FILE, "w", encoding="utf-8") as f:
        json.dump(queue, f, ensure_ascii=False, indent=2)


# ---------- 决策 ----------
# 有 OPENROUTER_API_KEY 就走模型，没有就用规则版；模型调用失败也回退规则版。
# key 落位前全部行为与原来一致。
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "openrouter/auto")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

DECIDE_SYSTEM = """你是 always-on 个人智能体的决策模块。输入是当前状态摘要和本轮工具观察结果，
输出下一步计划。只输出 JSON，不要解释。

格式：
{"speak": {"text": "...", "reason": "material_change|needs_decision"} | null,
 "actions": [{"do": "动作描述", "needs_approval": true, "reason": "..."}]}

规则：
- speak 只在两种情况下非空：实质变化（material_change，如新增重要提醒、工具异常），
  或需要用户拍板（needs_decision）。无事时 speak 为 null——安静是功能，不是 bug。
- 高风险动作（花钱、发消息、删数据、改配置）必须 needs_approval=true。
- 无事可做时 actions 为空数组。
- 输入里带了 pending_approvals（待审批数）和 last_spoken（上一条播报）：
  同一问题已在待审批中、或刚播报过，就不要重复提出、不要重复 speak。
  持续存在的状态（如某文件一直缺失）只报第一次，后续轮次视为无事。
- 可执行动作：mail_sense 观察里的邮件带 uid、from_addr、message_id，
  另有 folders 列出账号里真实存在的文件夹名。可用 exec 类型：
  mail_mark_read / mail_flag：params {"uids": [uid, ...]}；
  mail_archive：把邮件移到文件夹，params {"uids": [...], "folder": "文件夹名"}，
  folder 只能从 folders 里选、不要编造，明显是通知/账单堆积时才提议归档；
  mail_draft_reply：为某封邮件起草回复（只存草稿、绝不发送），
  params {"to": "对方邮箱（用 from_addr）", "subject": "Re: 原主题",
  "body": "完整可直接发送的回复正文", "in_reply_to": "原邮件 message_id"}，
  仅当新邮件明显需要用户回复时才提议，正文要写完整、语气得体；
  uid 必须来自本轮观察数据、不许编造；带 exec 的动作一律 needs_approval=true。
  其他动作不要带 exec。
- 跟进事项：followup_sense 观察的 due 非空 = 有跟进事项到期，
  用 speak 提醒用户（material_change），一次说清哪件事。
  当一件事需要在未来某个时间跟进（等回复、等结果、到期处理）时，
  提动作并附带 exec：{"type": "followup_create",
  "params": {"text": "要跟进的事", "due_in_hours": 距今小时数}}。
- 日历：calendar 观察里有 2 小时内开始的日程时，用 speak 提醒
  （material_change）；没有就不要提日历。"""


def _decide_rules(state, observations):
    """最小规则版：够跑通闭环，也是模型的回退方案。"""
    for ob in observations:
        if ob["tool"] == "heartbeat" and not ob["ok"]:
            return {"speak": (f"心跳工具异常：{ob['result']}", "material_change"),
                    "actions": []}
        if (ob["tool"] == "inbox_waterline" and ob["ok"]
                and ob["result"].get("new_alerts", 0) > 0):
            n = ob["result"]["new_alerts"]
            return {"speak": (f"收件箱分诊新增 {n} 封提醒", "material_change"),
                    "actions": []}
    return {"speak": None, "actions": []}


def _decide_model(state, observations):
    """调 OpenRouter 做决策。输出做防御性校验：reason 非法就丢弃 speak。"""
    import urllib.request
    summary = {k: state.get(k) for k in ("tick_count", "last_tick")}
    # 收件箱工具没在本机注册时（数据源不在这台机器），摘要里就不出现收件箱字段，
    # 免得模型盯着一个 null 水位反复臆测故障、重复提审批（2026-10-21 实测教训）。
    if any(t.name == "inbox_waterline" for t in TOOLS):
        summary["inbox_waterline"] = state.get("inbox_waterline")
        summary["inbox_alerts"] = state.get("inbox_alerts")
    summary["pending_approvals"] = len(
        [a for a in state.get("approval_queue", [])
         if a.get("status") == "pending"])
    mem = agent_memory.summary_for_decide()
    if mem:
        summary["memory"] = mem  # 长期记忆块：决策时能看见用户偏好和未收尾事项
    outbox = state.get("outbox", [])
    summary["last_spoken"] = outbox[-1].get("text") if outbox else None
    body = json.dumps({
        "model": OPENROUTER_MODEL,
        "messages": [
            {"role": "system", "content": DECIDE_SYSTEM},
            {"role": "user", "content": json.dumps(
                {"state": summary, "observations": observations},
                ensure_ascii=False)},
        ],
        "temperature": 0.2,
        "max_tokens": 500,
    }).encode("utf-8")
    req = urllib.request.Request(
        OPENROUTER_URL, data=body,
        headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)
    text = data["choices"][0]["message"]["content"]
    plan = json.loads(text[text.index("{"): text.rindex("}") + 1])
    speak = plan.get("speak")
    if speak and speak.get("reason") not in ("material_change", "needs_decision"):
        speak = None  # 非法理由直接丢弃，不说话
    elif speak:
        speak = (speak["text"], speak["reason"])
    actions = []
    for a in plan.get("actions", []):
        if not (isinstance(a, dict) and a.get("do")):
            continue
        ex = a.get("exec")
        if ex is not None and not (
                isinstance(ex, dict) and ex.get("type") in EXECUTORS
                and isinstance(ex.get("params"), dict)):
            # exec 载荷不合法（类型不在注册表/参数缺失）-> 剥掉载荷，
            # 动作降级为纯文本提案，批准了也不会被误执行。
            a = {k: v for k, v in a.items() if k != "exec"}
        actions.append(a)
    return {"speak": speak, "actions": actions}


def decide(state, observations):
    """observations: [{"tool": name, "ok": bool, "result": ...}]
    返回: {"speak": (text, reason) | None, "actions": [action, ...]}"""
    if OPENROUTER_API_KEY:
        try:
            return _decide_model(state, observations)
        except Exception as e:
            log(f"[决策] 模型调用失败，回退规则版: {e}")
    return _decide_rules(state, observations)


# ---------- 主循环 ----------
def tick(state):
    state["tick_count"] = state.get("tick_count", 0) + 1
    state["last_tick"] = datetime.datetime.now().isoformat(timespec="seconds")
    log(f"--- tick #{state['tick_count']} 醒来 ---")
    _refresh_tools()  # 配置变了（网页上连接/断开连接器）这一轮就生效

    # 1. 感知：按成本升序跑工具
    observations = []
    for tool in sorted(TOOLS, key=lambda t: t.cost):
        try:
            result = tool.run(state)
            observations.append({"tool": tool.name, "ok": True, "result": result})
        except Exception as e:  # 工具崩了不崩主循环
            observations.append({"tool": tool.name, "ok": False, "result": str(e)})
            log(f"[工具异常] {tool.name}: {e}")

    # 2. 决策
    plan = decide(state, observations)

    # 3. 行动：高风险先进审批，低风险直接跑。
    # 带 exec 载荷的动作一律先进审批——动用户邮箱必须人点头，模型说了不算。
    for action in plan["actions"]:
        if action.get("needs_approval") or action.get("exec"):
            request_approval(state, action["do"], action.get("reason", ""),
                             action.get("exec"))
        else:
            log(f"[执行] {action['do']}")

    # 3b. 执行已批准的动作（网页/CLI 里点过批准的）：
    # 有 exec 载荷 -> 走执行器真干活，结果写回队列项并播报一次；
    # 执行失败累计 3 次 -> 标 failed 并播报，不无限重试。
    # 纯文本动作没有执行器 -> 只关账并注明，不再冒充"已执行"。
    executed_notes = []
    for item in state.get("approval_queue", []):
        if item.get("status") != "approved" or item.get("executed"):
            continue
        if not item.get("exec"):
            item["executed"] = True
            item["status"] = "closed_noop"
            item["exec_result"] = "纯文本动作，无可执行载荷，仅记录批准"
            log(f"[执行关账] {item['action']}（无执行器）")
            continue
        ok, detail = _execute_item(item)
        item["exec_attempts"] = item.get("exec_attempts", 0) + 1
        item["exec_result"] = detail
        if ok:
            item["executed"] = True
            item["status"] = "executed"
            executed_notes.append(f"{item['action']} -> {detail}")
            log(f"[执行成功] {item['action']} -> {detail}")
            maybe_speak(state, f"已执行：{item['action']}（{detail}）",
                        "material_change")
        else:
            log(f"[执行失败] {item['action']}: {detail}"
                f"（第 {item['exec_attempts']} 次）")
            if item["exec_attempts"] >= 3:
                item["executed"] = True
                item["status"] = "failed"
                maybe_speak(
                    state,
                    f"执行失败（已重试 3 次）：{item['action']} —— {detail}",
                    "material_change")
    save_approvals(state.get("approval_queue", []))

    # 3c. 记忆蒸馏（影子模式）：观察攒够了就产出记忆编辑提案，
    # 做成一条带 memory_apply 载荷的审批——用户批准才落库。
    # 队列里已有待批的记忆提案时不叠加新提案。
    try:
        queue = state.get("approval_queue", [])
        has_pending_memory = any(
            i.get("status") == "pending"
            and (i.get("exec") or {}).get("type") == "memory_apply"
            for i in queue)
        if not has_pending_memory:
            out = agent_memory.distill_propose(state["tick_count"])
            if out:
                proposal, consumed = out
                state["memory_last_distill"] = {
                    "ts": datetime.datetime.now().isoformat(timespec="seconds"),
                    "consumed": consumed,
                    "edits_proposed": len(proposal["edits"]),
                    "reflection": (proposal["reflection"] or "")[:120]}
                if proposal["edits"]:
                    labels = "、".join(sorted(
                        {e.get("label", "?") for e in proposal["edits"]}))
                    request_approval(
                        state,
                        f"记忆蒸馏：建议改动 {len(proposal['edits'])} 处（{labels}）",
                        proposal["reflection"] or "定期蒸馏",
                        {"type": "memory_apply",
                         "params": {"edits": proposal["edits"]}})
                else:
                    log(f"[记忆蒸馏] 消费 {consumed} 条观察，无值得落库的改动")
    except Exception as e:
        state["memory_last_distill"] = {
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "error": str(e)[:200]}
        log(f"[记忆蒸馏] 异常（已忽略）: {e}")

    # 3d) 每日自动备份：状态/记忆/观察打包快照（防损坏与坏部署）
    try:
        import backup as agent_backup
        snap = agent_backup.maybe_daily_snapshot(state)
        if snap:
            state["last_backup"] = snap
            log(f"[备份] 今日快照完成：{snap['files']} 个文件")
    except Exception as e:
        log(f"[备份] 异常（已忽略）: {e}")

    # 4. 输出：只有实质变化或需要拍板才说话
    if plan["speak"]:
        text, reason = plan["speak"]
        maybe_speak(state, text, reason)
    else:
        log("无实质变化，保持安静。")

    # 5. 记一笔观察（记忆的观察层）：本轮的新邮件/播报/提案/执行结果
    try:
        rec = {"ts": state["last_tick"], "tick": state["tick_count"]}
        for ob in observations:
            if (ob["tool"] == "mail_sense" and ob["ok"]
                    and ob["result"].get("new_count")):
                rec["new_mail"] = [
                    {"from": m.get("from"), "subject": m.get("subject")}
                    for m in ob["result"].get("new", [])][:5]
            if (ob["tool"] == "followup_sense" and ob["ok"]
                    and ob["result"].get("due")):
                rec["followup_due"] = [d.get("text")
                                       for d in ob["result"]["due"]]
            if (ob["tool"] == "calendar_cn_sense" and ob["ok"]
                    and ob["result"].get("next")):
                rec["calendar_next"] = ob["result"]["next"].get("title")
        if plan["speak"]:
            rec["spoke"] = plan["speak"][0]
        proposed = [a.get("do") for a in plan["actions"] if a.get("do")]
        if proposed:
            rec["proposed"] = proposed
        if executed_notes:
            rec["executed"] = executed_notes
        agent_memory.obs_append(rec)
    except Exception as e:
        log(f"[记忆观察] 写入异常（已忽略）: {e}")

    save_state(state)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="只跑一轮就退出")
    args = ap.parse_args()

    state = load_state()
    if args.once:
        tick(state)
        return
    log(f"常驻启动，每 {TICK_SECONDS} 秒一轮。Ctrl+C 停。")
    while True:
        tick(load_state())  # 每轮重读：状态是唯一真相源
        time.sleep(TICK_SECONDS)


if __name__ == "__main__":
    main()
