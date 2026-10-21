#!/usr/bin/env python3
"""审批队列 CLI：agent 只管把高风险动作推进队列，拍板永远是人来做。"""
import argparse
import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
APPROVALS_FILE = os.path.join(BASE, "approvals.json")
STATE_FILE = os.path.join(BASE, "state.json")


def load():
    with open(APPROVALS_FILE, encoding="utf-8") as f:
        return json.load(f)


def save(queue):
    with open(STATE_FILE + ".tmp", "w", encoding="utf-8") as f:  # 复用原子写习惯
        pass
    with open(APPROVALS_FILE, "w", encoding="utf-8") as f:
        json.dump(queue, f, ensure_ascii=False, indent=2)
    # 同步回 state.json，保持"状态是唯一真相源"
    with open(STATE_FILE, encoding="utf-8") as f:
        state = json.load(f)
    state["approval_queue"] = queue
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["list", "approve", "deny"])
    ap.add_argument("index", nargs="?", type=int)
    args = ap.parse_args()

    queue = load()
    if args.cmd == "list":
        pending = [(i, q) for i, q in enumerate(queue) if q["status"] == "pending"]
        if not pending:
            print("没有待审批的动作。")
            return
        for i, q in pending:
            print(f"[{i}] {q['ts']} {q['action']} —— {q['reason']}")
        return

    if args.index is None:
        print("用法: approvals.py approve|deny <序号>", file=sys.stderr)
        sys.exit(2)
    item = queue[args.index]
    if item["status"] != "pending":
        print(f"第 {args.index} 个已是 {item['status']}，不动。")
        return
    item["status"] = "approved" if args.cmd == "approve" else "denied"
    save(queue)
    print(f"已{item['status']}：{item['action']}")


if __name__ == "__main__":
    main()
