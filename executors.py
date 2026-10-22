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


def _mail_archive(params):
    import mail_cn
    uids = _uids(params)
    if not uids:
        return False, "没有有效的邮件 uid"
    folder = str(params.get("folder") or "").strip()
    if not folder:
        return False, "缺少目标文件夹 folder"
    n = mail_cn.move_messages(uids, folder)
    return True, f"已把 {n} 封邮件移到「{folder}」"


def _mail_draft_reply(params):
    import mail_cn
    to = str(params.get("to") or "").strip()
    subject = str(params.get("subject") or "").strip()
    body = str(params.get("body") or "").strip()
    if not to or "@" not in to:
        return False, "收件人 to 不合法"
    if not subject:
        return False, "缺少主题 subject"
    if not body:
        return False, "缺少正文 body"
    if len(body) > 4000:
        return False, "正文超过 4000 字，请缩短"
    drafts = mail_cn.create_draft(
        to, subject, body, str(params.get("in_reply_to") or "").strip())
    return True, f"回复草稿已存入「{drafts}」：{subject}（发给 {to}，未发送）"


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


def _followup_create(params):
    import followups
    text = ((params or {}).get("text") or "").strip()
    try:
        hours = float((params or {}).get("due_in_hours"))
    except (TypeError, ValueError):
        return False, "due_in_hours 不是数字"
    if not text:
        return False, "跟进事项内容为空"
    if not (0 < hours <= 24 * 30):
        return False, "due_in_hours 超出范围（1 小时 ~ 30 天）"
    item = followups.create(text, hours)
    return True, f"已建跟进事项 #{item['id']}：{text}（{item['due']} 到期）"


EXECUTORS = {
    "mail_mark_read": _mail_mark_read,
    "mail_flag": _mail_flag,
    "mail_archive": _mail_archive,
    "mail_draft_reply": _mail_draft_reply,
    "memory_apply": _memory_apply,
    "followup_create": _followup_create,
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
