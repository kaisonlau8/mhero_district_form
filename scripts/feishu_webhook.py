#!/usr/bin/env python3
"""Send Feishu custom-bot webhook notifications for district-form pipeline."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = SCRIPT_DIR.parent
load_dotenv(PLUGIN_ROOT / ".env")

DEFAULT_WEBHOOK = (
    "https://open.feishu.cn/open-apis/bot/v2/hook/"
    "00329119-6957-4300-b9e8-81a260512bc1"
)
PUBLIC_BASE = os.getenv("PUBLIC_BASE_URL", "http://127.0.0.1:9003").rstrip("/")


def webhook_url() -> str:
    return (os.getenv("FEISHU_WEBHOOK_URL") or DEFAULT_WEBHOOK).strip()


def post_webhook(payload: dict[str, Any], *, timeout: float = 20) -> dict[str, Any]:
    url = webhook_url()
    if not url:
        return {"ok": False, "error": "FEISHU_WEBHOOK_URL empty"}
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(body) if body else {}
            except json.JSONDecodeError:
                parsed = {"raw": body}
            ok = int(parsed.get("code", 0) or 0) == 0 or parsed.get("StatusCode") == 0
            return {"ok": ok, "status": resp.status, "response": parsed}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        return {"ok": False, "error": f"HTTP {exc.code}: {detail}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def build_pipeline_card(result: dict[str, Any]) -> dict[str, Any]:
    status = str(result.get("status") or "unknown")
    year = result.get("year")
    quarter = result.get("quarter")
    report = str(result.get("report") or "")
    report_name = Path(report).name if report else ""
    started = result.get("startedAt") or ""
    finished = result.get("finishedAt") or ""
    error = str(result.get("error") or "")
    crawl = result.get("crawl") or {}
    file_count = crawl.get("fileCount")
    crawl_status = crawl.get("status") or "-"

    ok = status == "ok"
    title = "区域报表已生成" if ok else f"区域报表失败（{status}）"
    template = "green" if ok else "red"
    download_url = f"{PUBLIC_BASE}/api/report/latest"

    lines = [
        f"**周期**：{year} Q{quarter}",
        f"**状态**：{status}",
        f"**爬取**：{crawl_status}" + (f"（{file_count} 份）" if file_count is not None else ""),
        f"**开始**：{started}",
        f"**结束**：{finished}",
    ]
    if report_name:
        lines.append(f"**文件**：{report_name}")
    if error:
        lines.append(f"**错误**：{error[:400]}")
    elements: list[dict[str, Any]] = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "\n".join(lines),
            },
        }
    ]
    if ok:
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "下载报表"},
                        "type": "primary",
                        "url": download_url,
                    }
                ],
            }
        )

    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "template": template,
                "title": {"tag": "plain_text", "content": title},
            },
            "elements": elements,
        },
    }


def notify_pipeline_result(result: dict[str, Any]) -> dict[str, Any]:
    """Notify Feishu group after pipeline finishes (success or failure)."""
    if result.get("status") == "dry_run":
        return {"ok": True, "skipped": "dry_run"}
    payload = build_pipeline_card(result)
    resp = post_webhook(payload)
    if resp.get("ok"):
        print(f"[feishu] webhook ok: {resp.get('response')}")
    else:
        print(f"[feishu] webhook failed: {resp}")
    return resp


if __name__ == "__main__":
    demo = {
        "status": "ok",
        "year": 2026,
        "quarter": 3,
        "startedAt": "demo",
        "finishedAt": "demo",
        "report": str(PLUGIN_ROOT / "output" / "区域各指标情况一览0812.xlsx"),
        "crawl": {"status": "ok", "fileCount": 7},
        "error": "",
    }
    print(json.dumps(notify_pipeline_result(demo), ensure_ascii=False, indent=2))
