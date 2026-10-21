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


EXECUTORS = {
    "mail_mark_read": _mail_mark_read,
    "mail_flag": _mail_flag,
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
