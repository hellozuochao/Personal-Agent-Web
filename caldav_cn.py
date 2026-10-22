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
        if name in ("SUMMARY", "DTSTART", "DTEND", "UID", "RRULE"):
            current[name] = (value, params)
        elif name == "EXDATE":
            current.setdefault("EXDATE", []).append((value, params))
    out = []
    for ev in events:
        if "DTSTART" not in ev:
            continue
        start, all_day = _parse_dt(*ev["DTSTART"])
        if start is None:
            continue
        title = _unescape(ev.get("SUMMARY", ("日程", {}))[0]).strip() or "日程"
        exdates = []
        for value, params in ev.get("EXDATE", []):
            for piece in value.split(","):
                piece = piece.strip()
                if piece:
                    dt, _ = _parse_dt(piece, params)
                    if dt is not None:
                        exdates.append(dt)
        out.append({"title": title, "start": start, "all_day": all_day,
                    "rrule": ev.get("RRULE", (None, {}))[0],
                    "exdates": exdates})
    return out


_WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


def _parse_rrule(text):
    rule = {}
    for part in text.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            rule[k.strip().upper()] = v.strip()
    return rule


def _until_value(raw, start):
    raw = raw.strip()
    try:
        if raw.endswith("Z"):
            return datetime.datetime.strptime(
                raw, "%Y%m%dT%H%M%SZ").replace(
                    tzinfo=datetime.timezone.utc)
        if "T" in raw:
            return datetime.datetime.strptime(
                raw, "%Y%m%dT%H%M%S").replace(tzinfo=start.tzinfo)
        d = datetime.datetime.strptime(raw, "%Y%m%d").date()
        return datetime.datetime(d.year, d.month, d.day, 23, 59, 59,
                                 tzinfo=start.tzinfo)
    except Exception:
        return None


def _month_day(year, month, rule, start):
    """按规则算某月的目标日期：BYDAY 序数（如 2TU/-1FR）优先，
    其次 BYMONTHDAY，否则用起始日的日号。无效返回 None。"""
    import calendar as _cal
    last = _cal.monthrange(year, month)[1]
    byday = rule.get("BYDAY", "")
    if byday:
        token = byday.split(",")[0].strip()
        wd = _WEEKDAYS.get(token[-2:])
        if wd is None:
            return None
        prefix = token[:-2]
        if prefix:  # 带序数
            try:
                ordinal = int(prefix)
            except Exception:
                ordinal = 0
            days = [d for d in range(1, last + 1)
                    if datetime.date(year, month, d).weekday() == wd]
            if ordinal > 0 and ordinal <= len(days):
                return days[ordinal - 1]
            if ordinal < 0 and -ordinal <= len(days):
                return days[ordinal]
            return None
    if rule.get("BYMONTHDAY"):
        try:
            day = int(rule["BYMONTHDAY"].split(",")[0])
        except Exception:
            day = start.day
    else:
        day = start.day
    return day if 1 <= day <= last else None


def _expand_event(ev, win_start, win_end):
    """把一个（可能带 RRULE 的）日程展开成窗口内的实例列表。
    支持 DAILY/WEEKLY/MONTHLY/YEARLY + INTERVAL/COUNT/UNTIL/BYDAY/EXDATE，
    覆盖绝大多数真实日历的写法；展开上限防死循环。"""
    start = ev["start"]
    base = {"title": ev["title"], "all_day": ev["all_day"]}
    if not ev.get("rrule"):
        return ([dict(base, start=start)]
                if win_start <= start <= win_end else [])
    rule = _parse_rrule(ev["rrule"])
    freq = rule.get("FREQ", "").upper()
    try:
        interval = max(1, int(rule.get("INTERVAL", "1")))
    except Exception:
        interval = 1
    count_limit = None
    if rule.get("COUNT"):
        try:
            count_limit = int(rule["COUNT"])
        except Exception:
            count_limit = None
    until = _until_value(rule["UNTIL"], start) if rule.get("UNTIL") else None
    exdates = ev.get("exdates") or []

    def excluded(dt):
        for ex in exdates:
            if ev["all_day"]:
                if ex.date() == dt.date():
                    return True
            elif ex == dt:
                return True
        return False

    instances = []
    produced = [0]

    def emit(cand):
        """返回 False 表示序列应终止（超出 COUNT/UNTIL/窗口）。"""
        produced[0] += 1
        if count_limit is not None and produced[0] > count_limit:
            return False
        if until is not None and cand > until:
            return False
        if cand > win_end:
            return False
        if cand >= win_start and not excluded(cand):
            instances.append(dict(base, start=cand))
        return True

    def at(day):
        return datetime.datetime.combine(
            day, datetime.time(start.hour, start.minute, start.second),
            tzinfo=start.tzinfo)

    if freq == "DAILY":
        d = start.date()
        for _ in range(2000):
            if not emit(at(d)):
                break
            d += datetime.timedelta(days=interval)
    elif freq == "WEEKLY":
        byday = sorted({_WEEKDAYS[t.strip()[-2:]]
                        for t in rule.get("BYDAY", "").split(",")
                        if t.strip()[-2:] in _WEEKDAYS})
        if not byday:
            byday = [start.weekday()]
        monday = start.date() - datetime.timedelta(days=start.weekday())
        stop = False
        for w in range(0, 520, interval):
            if stop:
                break
            base_d = monday + datetime.timedelta(weeks=w)
            for wd in byday:
                cand_d = base_d + datetime.timedelta(days=wd)
                if cand_d < start.date():
                    continue  # 首周里起始日之前的星期几不算实例
                if not emit(at(cand_d)):
                    stop = True
                    break
    elif freq in ("MONTHLY", "YEARLY"):
        step_months = interval if freq == "MONTHLY" else interval * 12
        y, m = start.year, start.month
        for _ in range(600):
            day = _month_day(y, m, rule, start)
            if day is not None:
                cand = at(datetime.date(y, m, day))
                if cand >= start and not emit(cand):
                    break
            m += step_months
            y, m = y + (m - 1) // 12, (m - 1) % 12 + 1
    else:  # 不认识的 FREQ：当单次处理
        return ([dict(base, start=start)]
                if win_start <= start <= win_end else [])
    return instances


def _report_events(cfg, cal_url, body, depth):
    status, raw = _request(cfg, "REPORT", cal_url, body, depth=depth)
    if status not in (200, 207):
        raise RuntimeError(f"REPORT 返回 HTTP {status}")
    root = ET.fromstring(raw)
    events = []
    for el in root.iter(f"{{{C}}}calendar-data"):
        if el.text:
            events.extend(_parse_vevents(el.text))
    return events


def _get_events(cfg, cal_url):
    """兜底路径：列出日历里的 .ics 资源逐个 GET 回来本地解析。
    有些 CalDAV 实现（尤其邮箱厂商的）REPORT 支持不完整，
    但 PROPFIND+GET 是 WebDAV 基础能力，几乎都能走通。"""
    root = _propfind(cfg, cal_url, "<D:getetag/><D:resourcetype/>", depth=1)
    events = []
    count = 0
    for resp in root.iter(f"{{{D}}}response"):
        href_el = resp.find(f"{{{D}}}href")
        rt = resp.find(f".//{{{D}}}resourcetype")
        if href_el is None:
            continue
        if rt is not None and rt.find(f"{{{D}}}collection") is not None:
            continue  # 集合本身/子集合不是日程资源
        if count >= 60:
            break
        url = urllib.parse.urljoin(cal_url, href_el.text.strip())
        status, raw = _request(cfg, "GET", url)
        if status == 200:
            events.extend(_parse_vevents(raw.decode("utf-8", "replace")))
            count += 1
    return events


def fetch_upcoming(days=7, limit=5):
    """未来 days 天内的日程，按开始时间升序，最多 limit 条。
    每个日历依次尝试：REPORT(Depth 1) -> REPORT(Depth 0) -> PROPFIND+GET。"""
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
        got, last_err = None, None
        for attempt in (lambda: _report_events(cfg, cal_url, body, 1),
                        lambda: _report_events(cfg, cal_url, body, 0),
                        lambda: _get_events(cfg, cal_url)):
            try:
                got = attempt()
                break
            except Exception as e:
                last_err = e
        if got is None:
            errors.append(str(last_err))
        else:
            events.extend(got)
    if errors and not events:
        raise RuntimeError("日历查询失败：" + "；".join(errors))
    upcoming = []
    for e in events:
        upcoming.extend(_expand_event(e, now, end))
    upcoming.sort(key=lambda e: e["start"])
    return [{"title": e["title"],
             "start": e["start"].isoformat(timespec="minutes"),
             "all_day": e["all_day"]} for e in upcoming[:limit]]
