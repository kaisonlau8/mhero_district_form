"""HeroClaw 私聊：解析手机号、发卡片、发 Excel。"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = SCRIPT_DIR.parent
load_dotenv(PLUGIN_ROOT / ".env")

BASE_URL = "https://open.feishu.cn/open-apis"
_token_cache: dict[str, Any] = {"token": "", "expires_at": 0.0}
_phone_cache_path = PLUGIN_ROOT / "config" / "phone_open_ids.json"


def _json_request(method: str, url: str, *, headers: dict | None = None, payload: dict | None = None, timeout: float = 30) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req_headers = {"Content-Type": "application/json; charset=utf-8"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    parsed = json.loads(body) if body else {}
    if int(parsed.get("code") or 0) != 0:
        raise RuntimeError(parsed.get("msg") or str(parsed))
    return parsed


def get_tenant_token() -> str:
    if _token_cache["token"] and time.time() < float(_token_cache["expires_at"]):
        return str(_token_cache["token"])
    app_id = os.getenv("APP_ID", "").strip()
    app_secret = os.getenv("APP_SECRET", "").strip()
    if not app_id or not app_secret:
        raise RuntimeError("缺少 APP_ID 或 APP_SECRET")
    data = _json_request(
        "POST",
        f"{BASE_URL}/auth/v3/tenant_access_token/internal",
        payload={"app_id": app_id, "app_secret": app_secret},
        timeout=15,
    )
    _token_cache["token"] = data["tenant_access_token"]
    _token_cache["expires_at"] = time.time() + int(data.get("expire") or 7200) - 300
    return str(_token_cache["token"])


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {get_tenant_token()}"}


def _load_phone_cache() -> dict[str, str]:
    if not _phone_cache_path.exists():
        return {}
    try:
        data = json.loads(_phone_cache_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    raw = data.get("by_phone") if isinstance(data, dict) else {}
    return raw if isinstance(raw, dict) else {}


def _save_phone_cache(cache: dict[str, str]) -> None:
    _phone_cache_path.parent.mkdir(parents=True, exist_ok=True)
    _phone_cache_path.write_text(
        json.dumps({"by_phone": cache}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def normalize_phone(phone: str) -> str:
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    if digits.startswith("86") and len(digits) == 13:
        digits = digits[2:]
    return digits


def cached_open_id(phone: str) -> str:
    return str(_load_phone_cache().get(normalize_phone(phone)) or "")


def resolve_phone_to_open_id(phone: str) -> str:
    phone = normalize_phone(phone)
    if len(phone) != 11:
        return ""
    cache = _load_phone_cache()
    if cache.get(phone):
        return cache[phone]
    data = _json_request(
        "POST",
        f"{BASE_URL}/contact/v3/users/batch_get_id?user_id_type=open_id",
        headers=_auth_headers(),
        payload={"mobiles": [phone], "emails": [], "include_resigned": False},
        timeout=15,
    )
    user_list = (data.get("data") or {}).get("user_list") or []
    open_id = ""
    if user_list:
        open_id = str(user_list[0].get("user_id") or user_list[0].get("open_id") or "")
    if open_id:
        cache[phone] = open_id
        _save_phone_cache(cache)
    return open_id


def send_card_message(open_id: str, card: dict) -> dict:
    data = _json_request(
        "POST",
        f"{BASE_URL}/im/v1/messages?receive_id_type=open_id",
        headers=_auth_headers(),
        payload={
            "receive_id": open_id,
            "msg_type": "interactive",
            "content": json.dumps(card, ensure_ascii=False),
        },
    )
    return data.get("data") or {}


def send_file_message(open_id: str, file_path: str) -> dict:
    path = Path(file_path)
    boundary = f"----mhero{uuid.uuid4().hex}"
    file_bytes = path.read_bytes()
    chunks = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file_type\"\r\n\r\nxlsx\r\n".encode(),
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file_name\"\r\n\r\n{path.name}\r\n".encode(),
        (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{path.name}\"\r\n"
            "Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n"
        ).encode()
        + file_bytes
        + b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ]
    body = b"".join(chunks)
    req = urllib.request.Request(
        f"{BASE_URL}/im/v1/files",
        data=body,
        headers={
            **_auth_headers(),
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        uploaded = json.loads(resp.read().decode("utf-8", errors="replace"))
    if int(uploaded.get("code") or 0) != 0:
        raise RuntimeError(uploaded.get("msg") or "上传 Excel 失败")
    file_key = (uploaded.get("data") or {}).get("file_key") or ""
    if not file_key:
        raise RuntimeError("上传 Excel 未返回 file_key")
    data = _json_request(
        "POST",
        f"{BASE_URL}/im/v1/messages?receive_id_type=open_id",
        headers=_auth_headers(),
        payload={
            "receive_id": open_id,
            "msg_type": "file",
            "content": json.dumps({"file_key": file_key}),
        },
    )
    return data.get("data") or {}
