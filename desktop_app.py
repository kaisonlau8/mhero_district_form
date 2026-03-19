from __future__ import annotations

from base64 import b64decode
import os
from pathlib import Path
import socket
import threading
import time
import urllib.request

import uvicorn
import webview

from app.main import app


APP_TITLE = "区域报表自动生成"
HOST = "127.0.0.1"
SERVER_START_TIMEOUT_SECONDS = 20


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((HOST, 0))
        return int(sock.getsockname()[1])


class DesktopBridge:
    def save_report(self, filename: str, base64_content: str) -> dict[str, str | bool]:
        window = webview.windows[0]
        save_targets = window.create_file_dialog(
            webview.SAVE_DIALOG,
            save_filename=filename,
            file_types=("Excel 文件 (*.xlsx)",),
        )
        if not save_targets:
            return {"ok": False, "message": "已取消保存。"}

        target_path = Path(save_targets[0])
        target_path.write_bytes(b64decode(base64_content))
        return {"ok": True, "path": str(target_path)}


class ServerThread(threading.Thread):
    def __init__(self, port: int) -> None:
        super().__init__(daemon=True)
        self.port = port
        self.server = uvicorn.Server(
            uvicorn.Config(
                app=app,
                host=HOST,
                port=port,
                log_level="warning",
            )
        )

    def run(self) -> None:
        self.server.run()

    def stop(self) -> None:
        self.server.should_exit = True


def wait_until_server_ready(port: int) -> None:
    url = f"http://{HOST}:{port}/api/health"
    deadline = time.time() + SERVER_START_TIMEOUT_SECONDS
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.2)

    raise RuntimeError("桌面应用启动失败：本地服务未能及时就绪。")


def maybe_schedule_autoclose() -> None:
    auto_close_seconds = os.getenv("MHF_AUTOCLOSE_SECONDS")
    if not auto_close_seconds:
        return

    delay = float(auto_close_seconds)

    def closer() -> None:
        time.sleep(delay)
        if webview.windows:
            webview.windows[0].destroy()

    threading.Thread(target=closer, daemon=True).start()


def main() -> None:
    port = find_free_port()
    server_thread = ServerThread(port)
    server_thread.start()
    wait_until_server_ready(port)

    webview.create_window(
        APP_TITLE,
        f"http://{HOST}:{port}/?desktop=1",
        js_api=DesktopBridge(),
        width=1280,
        height=860,
        min_size=(980, 720),
    )
    try:
        webview.start(maybe_schedule_autoclose)
    finally:
        server_thread.stop()
        server_thread.join(timeout=5)


if __name__ == "__main__":
    main()
