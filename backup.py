"""自动备份：每天把运行时文件快照一份，并支持打包导出。

- 服务器本机快照（backups/<日期>/，保留最近 7 天）：防文件损坏、坏部署。
- /admin/backup 导出 zip：由 yoyo 侧定时拉走存异地副本，防整机丢失。
只备数据文件，不含代码（代码在 GitHub 仓库）和私钥目录（含授权码，
丢了重新在连接器页填，不进备份避免扩散）。
"""

import datetime
import io
import os
import shutil
import zipfile

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BACKUP_DIR = os.path.join(BASE_DIR, "backups")
FILES = ["state.json", "approvals.json", "memory_blocks.json",
         "memory_state.json", "observations.jsonl", "reflections.jsonl",
         "followups.json", "agent.log", "VERSION"]
KEEP = 7


def maybe_daily_snapshot(state):
    """今天还没备过就备一次。返回 {"date","files"} 或 None。"""
    today = datetime.date.today().isoformat()
    if state.get("last_backup_date") == today:
        return None
    dest = os.path.join(BACKUP_DIR, today)
    os.makedirs(dest, exist_ok=True)
    copied = 0
    for name in FILES:
        src = os.path.join(BASE_DIR, name)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(dest, name))
            copied += 1
    state["last_backup_date"] = today
    # 轮转：只保留最近 KEEP 天
    try:
        dirs = sorted(d for d in os.listdir(BACKUP_DIR)
                      if os.path.isdir(os.path.join(BACKUP_DIR, d)))
        for old in dirs[:-KEEP]:
            shutil.rmtree(os.path.join(BACKUP_DIR, old), ignore_errors=True)
    except Exception:
        pass
    return {"date": today, "files": copied}


def build_zip_bytes():
    """把当前运行时文件打成一个 zip（异地拉取用）。"""
    buf = io.BytesIO()
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in FILES:
            path = os.path.join(BASE_DIR, name)
            if os.path.exists(path):
                z.write(path, arcname=name)
        z.writestr("BACKUP_INFO.txt",
                   f"always-on agent backup\ncreated: {stamp}\n"
                   "restore: put files next to agent.py, restart services.\n")
    return buf.getvalue()
