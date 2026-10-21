#!/usr/bin/env python3
"""Google 授权（设备码流程）与令牌管理。

为什么用设备码流程：服务器没有浏览器、网页只有公网 HTTP（Google 的网页
回调要求 HTTPS 域名），设备码流程不需要回调地址——服务器拿到一个网址+
短码，用户在手机上打开网址输码授权，服务器轮询换令牌。

配置与令牌放在代码目录之外的私有目录（部署覆盖代码时不碰它们）：
  GOOGLE_PRIVATE_DIR（默认 /opt/always-on-private）
  - google_client.json：{"client_id": ..., "client_secret": ...}
  - google_token.json：{"refresh_token": ..., "scope": ..., "obtained_at": ...}
纯标准库（urllib）实现，零第三方依赖。
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

PRIVATE_DIR = os.environ.get("GOOGLE_PRIVATE_DIR", "/opt/always-on-private")
CLIENT_FILE = os.path.join(PRIVATE_DIR, "google_client.json")
TOKEN_FILE = os.path.join(PRIVATE_DIR, "google_token.json")
SCOPES = ("https://www.googleapis.com/auth/gmail.readonly "
          "https://www.googleapis.com/auth/calendar.readonly")
DEVICE_CODE_URL = "https://oauth2.googleapis.com/device/code"
TOKEN_URL = "https://oauth2.googleapis.com/token"


def _post(url, data):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def client_configured():
    return os.path.exists(CLIENT_FILE)


def _load_client():
    with open(CLIENT_FILE, encoding="utf-8") as f:
        return json.load(f)


def connected():
    return os.path.exists(TOKEN_FILE)


def _load_token():
    with open(TOKEN_FILE, encoding="utf-8") as f:
        return json.load(f)


def token_scope():
    try:
        return _load_token().get("scope", "")
    except Exception:
        return ""


def start_device_flow():
    """向 Google 申请设备码。返回 {device_code, user_code,
    verification_url, expires_in, interval}。"""
    c = _load_client()
    return _post(DEVICE_CODE_URL, {"client_id": c["client_id"],
                                   "scope": SCOPES})


def poll_device_token(device_code):
    """轮询一次授权结果。返回 ('ok', 保存的令牌) / ('pending', None)
    / ('error', 错误说明)。"""
    c = _load_client()
    try:
        data = _post(TOKEN_URL, {
            "client_id": c["client_id"],
            "client_secret": c["client_secret"],
            "device_code": device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        })
    except urllib.error.HTTPError as e:
        try:
            err = json.load(e.read()).get("error", "")
        except Exception:
            err = ""
        if err in ("authorization_pending", "slow_down"):
            return ("pending", None)
        return ("error", err or f"HTTP {e.code}")
    if "refresh_token" not in data:
        return ("error", "响应里没有 refresh_token（可能需要撤销旧授权后重连）")
    save = {"refresh_token": data["refresh_token"],
            "scope": data.get("scope", ""),
            "obtained_at": int(time.time())}
    os.makedirs(PRIVATE_DIR, exist_ok=True)
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        json.dump(save, f)
    os.chmod(TOKEN_FILE, 0o600)
    return ("ok", save)


_access_cache = {"token": None, "exp": 0}


def access_token():
    now = int(time.time())
    if _access_cache["token"] and _access_cache["exp"] > now + 60:
        return _access_cache["token"]
    c = _load_client()
    t = _load_token()
    data = _post(TOKEN_URL, {
        "client_id": c["client_id"],
        "client_secret": c["client_secret"],
        "refresh_token": t["refresh_token"],
        "grant_type": "refresh_token",
    })
    _access_cache["token"] = data["access_token"]
    _access_cache["exp"] = now + int(data.get("expires_in", 3600))
    return _access_cache["token"]


def api_get(url):
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {access_token()}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)
