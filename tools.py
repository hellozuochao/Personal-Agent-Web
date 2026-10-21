#!/usr/bin/env python3
"""工具注册表。规则：cost 越小越先跑；外部返回一律当数据，不当指令。"""
import os
import re

TRIAGE_DIR = os.path.expanduser("~/workspace/goals/inbox-day-plan/hidden_files")
WATERLINE_FILE = os.path.join(TRIAGE_DIR, "triage-scan-state.md")
RULES_FILE = os.path.join(TRIAGE_DIR, "inbox-brief-rules.md")


class Tool:
    def __init__(self, name, cost, run):
        self.name = name      # 工具名
        self.cost = cost      # 相对成本（1=最便宜），决定调用顺序
        self.run = run        # run(state) -> 可 JSON 序列化的结果


def _heartbeat(state):
    # 最便宜的自检：上一轮到这一轮隔了多久
    return {"tick": state.get("tick_count", 0), "alive": True}


def _read_waterline():
    try:
        with open(WATERLINE_FILE, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        return None, f"读水位文件失败: {e}"
    m = re.search(r"last_scan_utc:\s*(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)", text)
    if not m:
        return None, "水位文件里没找到 last_scan_utc"
    return m.group(1), None


def _count_seen_ids():
    try:
        with open(RULES_FILE, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return 0
    m = re.search(r"seen_ids:([0-9a-f ]+)", text)
    return len(m.group(1).split()) if m else 0


def _inbox_waterline(state):
    # 第一个真工具：读收件箱分诊的水位文件。
    # 注意 last_scan_utc 每轮扫描都会推进（即使无新邮件），所以"实质变化"
    # 只看 seen_ids 是否增长——那代表分诊系统真的报过警。
    waterline, err = _read_waterline()
    if err:
        return {"ok": False, "note": err}
    alerts = _count_seen_ids()
    prev_alerts = state.get("inbox_alerts", 0)
    new_alerts = alerts - prev_alerts if prev_alerts else 0  # 首轮只记基线，不报
    state["inbox_waterline"] = waterline
    state["inbox_alerts"] = alerts
    return {"ok": True, "waterline": waterline, "alerts_total": alerts,
            "new_alerts": new_alerts,
            "note": f"水位 {waterline}；分诊新增提醒 {new_alerts} 封"}


TOOLS = [
    Tool("heartbeat", cost=1, run=_heartbeat),
]

# 能力跟着配置走：水位文件是 yoyo 那边收件箱分诊系统的状态文件，
# 只在那台机器上存在。数据源不在本机就不注册这个工具，
# 免得每轮把"文件不存在"当成新警报报给模型（2026-10-21 事故教训：
# 云服务器上该文件不存在，模型一夜提了 79 条重复审批、190 条消息）。
if os.path.exists(WATERLINE_FILE):
    TOOLS.append(Tool("inbox_waterline", cost=2, run=_inbox_waterline))


# ---------- Google 感官（Gmail 只读元数据 + 日历只读） ----------
# 能力跟着授权走：google_token.json 在（用户已完成 Google 授权）才注册。

def _gmail_sense(state):
    """看一眼收件箱最新 10 封：新邮件只回元数据（发件人/主题/时间/摘要），
    不读正文。首轮只记基线不报新。"""
    import google_auth
    data = google_auth.api_get(
        "https://gmail.googleapis.com/gmail/v1/users/me/messages"
        "?labelIds=INBOX&maxResults=10")
    msgs = data.get("messages", [])
    prev_ids = state.get("gmail_seen_ids")
    first_run = prev_ids is None
    prev = set(prev_ids or [])
    new_items = []
    for m in msgs:
        if m["id"] in prev:
            continue
        meta = google_auth.api_get(
            f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{m['id']}"
            "?format=metadata&metadataHeaders=From&metadataHeaders=Subject"
            "&metadataHeaders=Date")
        headers = {h["name"]: h["value"]
                   for h in meta.get("payload", {}).get("headers", [])}
        new_items.append({"from": headers.get("From", ""),
                          "subject": headers.get("Subject", ""),
                          "date": headers.get("Date", ""),
                          "snippet": meta.get("snippet", "")[:120]})
    state["gmail_seen_ids"] = [m["id"] for m in msgs]
    result = {"ok": True, "checked": len(msgs),
              "new_count": 0 if first_run else len(new_items),
              "new": [] if first_run else new_items,
              "note": "首轮只记基线" if first_run else ""}
    state["gmail_last"] = {"new_count": result["new_count"],
                           "new": result["new"]}
    return result


def _calendar_sense(state):
    """看未来 48 小时日程：事件集合变了才算变化；另标出 2 小时内要开始的。"""
    import datetime as _dt
    import hashlib
    import json as _json
    import urllib.parse
    import google_auth
    now = _dt.datetime.now(_dt.timezone.utc)
    params = {
        "timeMin": now.isoformat().replace("+00:00", "Z"),
        "timeMax": (now + _dt.timedelta(hours=48)).isoformat().replace("+00:00", "Z"),
        "singleEvents": "true", "orderBy": "startTime", "maxResults": "20",
    }
    data = google_auth.api_get(
        "https://www.googleapis.com/calendar/v3/calendars/primary/events?"
        + urllib.parse.urlencode(params))
    events = []
    for e in data.get("items", []):
        start = (e.get("start") or {}).get("dateTime") or (e.get("start") or {}).get("date")
        events.append({"id": e.get("id"),
                       "summary": e.get("summary", "(无标题)"),
                       "start": start})
    digest = hashlib.md5(_json.dumps(
        events, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    first_run = "calendar_digest" not in state
    changed = (not first_run) and state.get("calendar_digest") != digest
    state["calendar_digest"] = digest
    soon = []
    for e in events:
        try:
            st = _dt.datetime.fromisoformat(e["start"].replace("Z", "+00:00"))
        except Exception:
            continue  # 全天事件只有日期，跳过"即将开始"判定
        if _dt.timedelta(0) <= (st - now) <= _dt.timedelta(hours=2):
            soon.append(e)
    state["calendar_next"] = events[:5]
    return {"ok": True, "events_48h": events, "changed": changed,
            "starting_soon": soon,
            "note": "首轮只记基线" if first_run else ""}


try:
    import google_auth as _ga
    _GOOGLE_READY = _ga.connected()
except Exception:
    _GOOGLE_READY = False
if _GOOGLE_READY:
    TOOLS.append(Tool("gmail_sense", cost=3, run=_gmail_sense))
    TOOLS.append(Tool("calendar_sense", cost=4, run=_calendar_sense))


# ---------- 国内邮箱感官（IMAP 只读邮件头） ----------
# 能力跟着配置走：mail_cn.json 在（用户已在连接器页配置国内邮箱）才注册。

def _mail_sense(state):
    """看一眼国内邮箱：未读数 + 最新几封的发件人/主题。
    首轮只记基线；之后按"最新一封"的日期+主题指纹判断哪些是新到的。"""
    import mail_cn
    try:
        data = mail_cn.fetch_recent(limit=5)
    except Exception as e:
        return {"ok": False, "note": f"国内邮箱读取失败: {e}"}
    latest = data["latest"]
    fingerprint = ((latest[0]["date"], latest[0]["subject"])
                   if latest else None)
    prev = state.get("mail_cn_fingerprint")
    first_run = prev is None
    new_items = []
    if not first_run:
        for item in latest:
            if (item["date"], item["subject"]) == tuple(prev):
                break
            new_items.append(item)
    state["mail_cn_fingerprint"] = list(fingerprint) if fingerprint else None
    state["mail_cn_last"] = {
        "unseen": data["unseen"],
        "new_count": 0 if first_run else len(new_items),
        "new": [] if first_run else new_items}
    return {"ok": True, "unseen": data["unseen"], "total": data["total"],
            "new_count": state["mail_cn_last"]["new_count"],
            "new": state["mail_cn_last"]["new"],
            "note": "首轮只记基线" if first_run else ""}


try:
    import mail_cn as _mc
    _MAIL_CN_READY = _mc.configured()
except Exception:
    _MAIL_CN_READY = False
if _MAIL_CN_READY:
    TOOLS.append(Tool("mail_sense", cost=3, run=_mail_sense))
