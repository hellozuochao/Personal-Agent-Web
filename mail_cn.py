#!/usr/bin/env python3
"""国内邮箱连接器（IMAP + 客户端授权码）：网易 163/126、QQ 邮箱等。

用户在网页连接器页自助配置：邮箱地址 + 客户端授权码（不是登录密码，
在邮箱网页版"设置 → POP3/SMTP/IMAP"里开启服务后生成，可单独吊销）。
配置存私有目录 mail_cn.json（目录 700 / 文件 600），授权码不进代码、
不进仓库。感官只读邮件头（发件人/主题/时间）+ 未读数，不读正文、
不标已读（SELECT 只读 + BODY.PEEK）。
"""
import email
import imaplib
import json
import os
import re
from email.header import decode_header

PRIVATE_DIR = os.environ.get("AGENT_PRIVATE_DIR") or os.environ.get(
    "GOOGLE_PRIVATE_DIR", "/opt/always-on-private")
CONFIG_FILE = os.path.join(PRIVATE_DIR, "mail_cn.json")

PROVIDERS = {
    "wangyi": {"name": "网易邮箱（163/126/yeah）", "host": "imap.163.com", "port": 993},
    "qq": {"name": "QQ 邮箱", "host": "imap.qq.com", "port": 993},
    "foxmail": {"name": "Foxmail 邮箱", "host": "imap.foxmail.com", "port": 993},
}


def guess_provider(address):
    domain = (address or "").rsplit("@", 1)[-1].lower()
    if domain in ("163.com", "126.com", "yeah.net", "188.com", "163.net"):
        return "wangyi"
    if domain == "qq.com":
        return "qq"
    if domain == "foxmail.com":
        return "foxmail"
    return None


def load_config():
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
        if cfg.get("email") and cfg.get("code"):
            return cfg
    except (OSError, ValueError):
        pass
    return None


def configured():
    return load_config() is not None


def save_config(cfg):
    os.makedirs(PRIVATE_DIR, mode=0o700, exist_ok=True)
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False)
    os.replace(tmp, CONFIG_FILE)
    os.chmod(CONFIG_FILE, 0o600)


def delete_config():
    try:
        os.remove(CONFIG_FILE)
    except OSError:
        pass


def _decode(value):
    if not value:
        return ""
    parts = []
    for text, enc in decode_header(value):
        if isinstance(text, bytes):
            try:
                parts.append(text.decode(enc or "utf-8", "replace"))
            except (LookupError, UnicodeDecodeError):
                parts.append(text.decode("utf-8", "replace"))
        else:
            parts.append(text)
    return "".join(parts).strip()


def _connect(cfg):
    host = cfg.get("host") or PROVIDERS[cfg["provider"]]["host"]
    port = int(cfg.get("port") or 993)
    conn = imaplib.IMAP4_SSL(host, port, timeout=20)
    conn.login(cfg["email"], cfg["code"])
    # 网易要求客户端表明身份（RFC 2971 ID 命令），不发可能被判 Unsafe Login
    try:
        conn.xatom('ID ("name" "always-on-agent" "version" "1.0" '
                   '"vendor" "personal-agent")')
    except Exception:
        pass
    return conn


def test_login(provider, address, code):
    """配置前先验：能登录并打开收件箱才保存。返回 (ok, 说明)。"""
    cfg = {"provider": provider, "email": address, "code": code}
    try:
        conn = _connect(cfg)
        try:
            typ, data = conn.select("INBOX", readonly=True)
            if typ != "OK":
                return False, "登录成功但打不开收件箱"
            total = int(data[0]) if data and data[0] else 0
            return True, f"连接成功，收件箱共 {total} 封"
        finally:
            try:
                conn.logout()
            except Exception:
                pass
    except imaplib.IMAP4.error as e:
        return False, f"登录被拒：{e}（多半是授权码不对，或 IMAP 服务未开启）"
    except Exception as e:
        return False, f"连接失败：{e}"


def configure(provider, address, code):
    """网页配置入口：校验 → 试登录 → 成功才落盘。返回 (ok, 说明)。"""
    address = (address or "").strip()
    code = (code or "").strip()
    if not address or "@" not in address:
        return False, "邮箱地址不完整"
    if not code:
        return False, "授权码为空"
    if not provider:
        provider = guess_provider(address)
    if provider not in PROVIDERS:
        return False, "暂不支持这个邮箱服务商"
    ok, detail = test_login(provider, address, code)
    if not ok:
        return False, detail
    save_config({"provider": provider, "email": address, "code": code})
    return True, detail


def fetch_recent(limit=5):
    """感官读取：未读数 + 最新 limit 封的邮件头。失败抛异常给调用方。"""
    cfg = load_config()
    if not cfg:
        raise RuntimeError("mail_cn 未配置")
    conn = _connect(cfg)
    try:
        typ, data = conn.select("INBOX", readonly=True)
        if typ != "OK":
            raise RuntimeError("打不开收件箱")
        total = int(data[0]) if data and data[0] else 0
        typ, unseen_data = conn.search(None, "UNSEEN")
        unseen = (len(unseen_data[0].split())
                  if typ == "OK" and unseen_data and unseen_data[0] else 0)
        latest = []
        if total:
            start = max(1, total - limit + 1)
            typ, msg_data = conn.fetch(
                f"{start}:{total}",
                "(UID BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            if typ == "OK":
                for item in msg_data:
                    if isinstance(item, tuple):
                        m = re.search(rb"UID (\d+)", item[0])
                        msg = email.message_from_bytes(item[1])
                        latest.append({
                            "uid": int(m.group(1)) if m else None,
                            "from": _decode(msg.get("From")),
                            "subject": _decode(msg.get("Subject")),
                            "date": msg.get("Date", "")})
                latest.reverse()
        return {"total": total, "unseen": unseen, "latest": latest}
    finally:
        try:
            conn.logout()
        except Exception:
            pass


# ---------- 写操作（执行层专用，只经审批通过后调用） ----------

def _uid_store(uids, flag):
    cfg = load_config()
    if not cfg:
        raise RuntimeError("mail_cn 未配置")
    conn = _connect(cfg)
    try:
        typ, _ = conn.select("INBOX")  # 写操作：不能只读打开
        if typ != "OK":
            raise RuntimeError("打不开收件箱")
        uidset = ",".join(str(int(u)) for u in uids)
        typ, _ = conn.uid("STORE", uidset, "+FLAGS", f"({flag})")
        if typ != "OK":
            raise RuntimeError("标记操作被服务器拒绝")
        return len(uids)
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def mark_read(uids):
    """把指定 uid 的邮件标记为已读。返回处理封数。"""
    return _uid_store(uids, r"\Seen")


def set_flag(uids):
    """给指定 uid 的邮件加星标（重要标记）。返回处理封数。"""
    return _uid_store(uids, r"\Flagged")
