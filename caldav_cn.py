#!/usr/bin/env python3
"""国内日历连接器（CalDAV）：QQ 邮箱等支持 CalDAV 的服务。

配置方式与 mail_cn 对齐：用户在连接器页填邮箱 + 客户端授权码，
本模块先走一遍标准 CalDAV 发现流程（current-user-principal ->
calendar-home-set -> 列日历）验证连通与授权，通过才落盘。
发现结果（日历 URL 列表）随配置一起保存，感官查询时直接用。

- QQ 邮箱：需在网页版设置里开启「POP3/IMAP/SMTP/Exchange/CardDAV」
  服务并生成授权码；DAV 入口 https://dav.qq.com（发现流程自动找路径）。
- 自定义：任何标准 CalDAV 服务，填服务根地址即可。
只读：只做 PROPFIND 与 calendar-query REPORT，不写日历。
"""
import base64
import datetime
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

PRIVATE_DIR = (os.environ.get("AGENT_PRIVATE_DIR")
               or os.environ.get("GOOGLE_PRIVATE_DIR")
               or "/opt/always-on-private")
CONFIG_FILE = os.path.join(PRIVATE_DIR, "calendar_cn.json")
LOCAL_TZ = ZoneInfo("Asia/Shanghai")

D = "DAV:"
C = "urn:ietf:params:xml:ns:caldav"

PROVIDERS = {
    "qq": {"label": "QQ 邮箱", "base": "https://dav.qq.com"},
}


def _private_dir():
    os.makedirs(PRIVATE_DIR, mode=0o700, exist_ok=True)
    return PRIVATE_DIR


def load_config():
    if not os.path.exists(CONFIG_FILE):
        return None
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)


def configured():
    return load_config() is not None


def delete_config():
    try:
        os.remove(CONFIG_FILE)
    except OSError:
        pass


def _save_config(cfg):
    _private_dir()
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONFIG_FILE)


# ---------- HTTP（手动跟随重定向：PROPFIND/REPORT 不能被降级成 GET） ----------
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _request(cfg, method, url, body=None, depth=None):
    auth = base64.b64encode(
        f"{cfg['address']}:{cfg['code']}".encode("utf-8")).decode("ascii")
    headers = {"Authorization": f"Basic {auth}",
               "Content-Type": "application/xml; charset=utf-8"}
    if depth is not None:
        headers["Depth"] = str(depth)
    data = body.encode("utf-8") if isinstance(body, str) else body
    for _ in range(4):
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method=method)
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(req, timeout=20) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                loc = e.headers.get("Location")
                if loc:
                    url = urllib.parse.urljoin(url, loc)
                    continue
            if e.code == 401:
                raise RuntimeError("认证失败：检查邮箱地址与授权码，"
                                   "并确认已开启 CardDAV/CalDAV 服务")
            raise RuntimeError(f"CalDAV 请求失败 HTTP {e.code}: "
                               f"{e.read()[:120]!r}")
    raise RuntimeError("CalDAV 重定向次数过多")


def _propfind(cfg, url, inner_xml, depth=0):
    body = ('<?xml version="1.0" encoding="utf-8"?>'
            f'<D:propfind xmlns:D="DAV:" xmlns:C="{C}">'
            f"<D:prop>{inner_xml}</D:prop></D:propfind>")
    status, raw = _request(cfg, "PROPFIND", url, body, depth=depth)
    if status not in (200, 207):
        raise RuntimeError(f"PROPFIND {url} 返回 HTTP {status}")
    return ET.fromstring(raw)


def _first_href(root, *tags):
    for el in root.iter():
        if el.tag in tags:
            href = el.find(f"{{{D}}}href")
            if href is not None and href.text:
                return href.text.strip()
    return None


# ---------- 发现流程 ----------
def discover(cfg):
    """返回日历集合 URL 列表。任何一步落空都抛 RuntimeError 说明原因。"""
    base = cfg["base_url"].rstrip("/")
    principal = None
    for cand in (base + "/.well-known/caldav", base + "/", base):
        try:
            root = _propfind(cfg, cand, "<D:current-user-principal/>")
        except Exception:
            continue
        href = _first_href(root, f"{{{D}}}current-user-principal")
        if href:
            principal = urllib.parse.urljoin(cand, href)
            break
    if not principal:
        raise RuntimeError("发现失败：服务器没有返回当前用户 principal"
                           "（可能未开启 CalDAV 服务）")
    root = _propfind(cfg, principal, "<C:calendar-home-set/>")
    home_href = _first_href(root, f"{{{C}}}calendar-home-set")
    if not home_href:
        raise RuntimeError("发现失败：服务器没有返回日历主目录")
    home = urllib.parse.urljoin(principal, home_href)
    root = _propfind(cfg, home, "<D:resourcetype/><D:displayname/>", depth=1)
    calendars = []
    for resp in root.iter(f"{{{D}}}response"):
        href_el = resp.find(f"{{{D}}}href")
        rt = resp.find(f".//{{{D}}}resourcetype")
        if href_el is None or rt is None:
            continue
        if rt.find(f"{{{C}}}calendar") is not None:
            calendars.append(urllib.parse.urljoin(home, href_el.text.strip()))
    if not calendars:
        # 主目录本身可能就是日历
        root0 = _propfind(cfg, home, "<D:resourcetype/>", depth=0)
        rt = root0.find(f".//{{{D}}}resourcetype")
        if rt is not None and rt.find(f"{{{C}}}calendar") is not None:
            calendars = [home]
    if not calendars:
        raise RuntimeError("发现失败：日历主目录下没有日历集合")
    return calendars


def configure(provider, address, code, base_url=None):
    """先真实发现一次，成功才保存。返回 (ok, 说明)。"""
    address = (address or "").strip()
    code = (code or "").strip()
    if provider == "qq":
        base = PROVIDERS["qq"]["base"]
    elif provider == "custom":
        base = (base_url or "").strip()
        if not base.startswith("https://"):
            return False, "自定义 CalDAV 地址要以 https:// 开头"
    else:
        return False, f"未知服务商: {provider}"
    if not address or not code:
        return False, "邮箱地址和授权码都要填"
    cfg = {"provider": provider, "address": address,
           "code": code, "base_url": base}
    try:
        calendars = discover(cfg)
    except Exception as e:
        return False, f"连接失败：{e}"
    cfg["calendar_urls"] = calendars
    _save_config(cfg)
    return True, f"连接成功，发现 {len(calendars)} 个日历：{address}"


# ---------- 查询未来日程 ----------
def _unfold(text):
    return re.sub(r"\r?\n[ \t]", "", text)


def _unescape(s):
    return (s.replace("\\n", "\n").replace("\\,", ",")
             .replace("\\;", ";").replace("\\\\", "\\"))


def _parse_dt(value, params):
    """返回 (aware datetime, 是否全天)。解析不了返回 (None, False)。"""
    value = value.strip()
    try:
        if re.fullmatch(r"\d{8}", value):
            d = datetime.datetime.strptime(value, "%Y%m%d")
            return d.replace(tzinfo=LOCAL_TZ), True
        if re.fullmatch(r"\d{8}T\d{6}Z", value):
            d = datetime.datetime.strptime(value, "%Y%m%dT%H%M%SZ")
            return d.replace(tzinfo=datetime.timezone.utc), False
        if re.fullmatch(r"\d{8}T\d{6}", value):
            d = datetime.datetime.strptime(value, "%Y%m%dT%H%M%S")
            tz = LOCAL_TZ
            tzid = params.get("TZID")
            if tzid:
                try:
                    tz = ZoneInfo(tzid)
                except Exception:
                    pass
            return d.replace(tzinfo=tz), False
    except Exception:
        pass
    return None, False


def _parse_vevents(ics_text):
    events = []
    current = None
    for line in _unfold(ics_text).splitlines():
        line = line.rstrip("\r")
        if line == "BEGIN:VEVENT":
            current = {}
            continue
        if line == "END:VEVENT":
            if current is not None:
                events.append(current)
            current = None
            continue
        if current is None or ":" not in line:
            continue
        name_params, _, value = line.partition(":")
        parts = name_params.split(";")
        name = parts[0].upper()
        params = {}
        for p in parts[1:]:
            if "=" in p:
                k, v = p.split("=", 1)
                params[k.upper()] = v
        if name in ("SUMMARY", "DTSTART", "DTEND", "UID"):
            current[name] = (value, params)
    out = []
    for ev in events:
        if "DTSTART" not in ev:
            continue
        start, all_day = _parse_dt(*ev["DTSTART"])
        if start is None:
            continue
        title = _unescape(ev.get("SUMMARY", ("日程", {}))[0]).strip() or "日程"
        out.append({"title": title, "start": start, "all_day": all_day})
    return out


def fetch_upcoming(days=7, limit=5):
    """未来 days 天内的日程，按开始时间升序，最多 limit 条。"""
    cfg = load_config()
    if not cfg:
        raise RuntimeError("calendar_cn 未配置")
    calendars = cfg.get("calendar_urls") or discover(cfg)
    now = datetime.datetime.now(LOCAL_TZ)
    end = now + datetime.timedelta(days=days)
    fmt = "%Y%m%dT%H%M%SZ"
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<C:calendar-query xmlns:D="DAV:" xmlns:C="{C}">'
        "<D:prop><D:getetag/><C:calendar-data/></D:prop>"
        '<C:filter><C:comp-filter name="VCALENDAR">'
        '<C:comp-filter name="VEVENT">'
        f'<C:time-range start="{now.astimezone(datetime.timezone.utc).strftime(fmt)}" '
        f'end="{end.astimezone(datetime.timezone.utc).strftime(fmt)}"/>'
        "</C:comp-filter></C:comp-filter></C:filter></C:calendar-query>")
    events, errors = [], []
    for cal_url in calendars:
        try:
            status, raw = _request(cfg, "REPORT", cal_url, body, depth=1)
            if status not in (200, 207):
                errors.append(f"HTTP {status}")
                continue
            root = ET.fromstring(raw)
            for el in root.iter(f"{{{C}}}calendar-data"):
                if el.text:
                    events.extend(_parse_vevents(el.text))
        except Exception as e:
            errors.append(str(e))
    if errors and not events:
        raise RuntimeError("日历查询失败：" + "；".join(errors))
    upcoming = [e for e in events if now <= e["start"] <= end]
    upcoming.sort(key=lambda e: e["start"])
    return [{"title": e["title"],
             "start": e["start"].isoformat(timespec="minutes"),
             "all_day": e["all_day"]} for e in upcoming[:limit]]
