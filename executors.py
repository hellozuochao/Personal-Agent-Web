#!/usr/bin/env python3
"""执行器注册表：审批通过后真正干活的地方。

只有注册表里的类型能被执行。动作的 exec 载荷形如
{"type": "mail_mark_read", "params": {"uids": [123, 456]}}，
由决策层在提出动作时附带（uid 必须来自感官观察数据）。
每个执行器返回 (ok, 结果说明)；异常一律转成 (False, 说明)，
不许把主循环带崩。未知类型一律拒绝执行——宁可失败，不冒充成功。
"""


def _uids(params):
    out = []
    for u in (params or {}).get("uids", []):
        try:
            out.append(int(u))
        except (TypeError, ValueError):
            pass
    return out[:50]


def _mail_mark_read(params):
    import mail_cn
    uids = _uids(params)
    if not uids:
        return False, "没有有效的邮件 uid"
    n = mail_cn.mark_read(uids)
    return True, f"已把 {n} 封邮件标记为已读"


def _mail_flag(params):
    import mail_cn
    uids = _uids(params)
    if not uids:
        return False, "没有有效的邮件 uid"
    n = mail_cn.set_flag(uids)
    return True, f"已给 {n} 封邮件加星标"


def _memory_apply(params):
    import datetime
    import memory as agent_memory
    edits = (params or {}).get("edits") or []
    if not edits:
        return False, "没有编辑指令"
    ts = datetime.datetime.now().isoformat(timespec="seconds")
    applied, rejected = agent_memory.apply_edits(edits, ts)
    if not applied:
        return False, "记忆编辑全部被拒：" + "；".join(rejected)
    detail = f"已应用 {len(applied)} 处记忆改动（{'、'.join(applied)}）"
    if rejected:
        detail += f"；拒绝 {len(rejected)} 处：{'；'.join(rejected)}"
    return True, detail


EXECUTORS = {
    "mail_mark_read": _mail_mark_read,
    "mail_flag": _mail_flag,
    "memory_apply": _memory_apply,
}


def execute(item):
    """执行一条已批准的队列项。返回 (ok, 说明)。"""
    payload = item.get("exec") or {}
    fn = EXECUTORS.get(payload.get("type"))
    if fn is None:
        return False, f"未知执行类型: {payload.get('type')}"
    try:
        return fn(payload.get("params") or {})
    except Exception as e:
        return False, f"执行器异常: {e}"
