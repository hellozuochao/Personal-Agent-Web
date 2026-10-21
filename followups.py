#!/usr/bin/env python3
"""跟进事项：agent 给自己（替用户）记的"到时候要跟进"清单。

生命周期：open（未到期/到期窗口内）-> expired（到期超过 24 小时
还没处理，自动过期，不再打扰）。创建只能由用户批准的动作完成
（执行器 followup_create）；到期由 followup_sense 感知到，
decide() 负责开口提醒。存储在 followups.json，结构简单可读。
"""
import datetime
import json
import os

BASE = os.path.dirname(os.path.abspath(__file__))
FILE = os.path.join(BASE, "followups.json")
GRACE_HOURS = 24  # 到期后最多再提醒这么久，之后自动过期


def _now():
    return datetime.datetime.now()


def _load():
    if not os.path.exists(FILE):
        return []
    with open(FILE, encoding="utf-8") as f:
        return json.load(f)


def _save(items):
    tmp = FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    os.replace(tmp, FILE)


def create(text, due_in_hours, now=None):
    now = now or _now()
    items = _load()
    item = {
        "id": (max((i["id"] for i in items), default=0) + 1),
        "text": text,
        "created": now.isoformat(timespec="seconds"),
        "due": (now + datetime.timedelta(hours=due_in_hours)
                ).isoformat(timespec="seconds"),
        "status": "open",
    }
    items.append(item)
    _save(items)
    return item


def sweep(now=None):
    """把过期未处理的标 expired，返回全部事项。"""
    now = now or _now()
    items = _load()
    changed = False
    for it in items:
        if it["status"] != "open":
            continue
        due = datetime.datetime.fromisoformat(it["due"])
        if now > due + datetime.timedelta(hours=GRACE_HOURS):
            it["status"] = "expired"
            changed = True
    if changed:
        _save(items)
    return items


def open_items(now=None):
    return [i for i in sweep(now) if i["status"] == "open"]


def due_items(now=None):
    now = now or _now()
    out = []
    for it in open_items(now):
        due = datetime.datetime.fromisoformat(it["due"])
        if due <= now <= due + datetime.timedelta(hours=GRACE_HOURS):
            out.append(it)
    return out


def next_open(now=None):
    opens = open_items(now)
    if not opens:
        return None
    return min(opens, key=lambda i: i["due"])
