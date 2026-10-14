from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from .processor import ReportBuildError, build_report
from .runtime import assets_dir, static_dir


STATIC_DIR = static_dir()
DEFAULT_TEMPLATE_PATH = assets_dir() / "report_template.xlsx"

app = FastAPI(title="区域报表自动生成")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _engine_reportdb() -> bool:
    """MHERO_REPORT_ENGINE=reportdb：Excel 模板上传/处理入口下线（代码保留，默认 excel 引擎下仍可用）。"""
    return os.getenv("MHERO_REPORT_ENGINE", "excel").strip().lower() == "reportdb"


_RETIRED_HTML = (
    "<!doctype html><meta charset='utf-8'><title>已下线</title>"
    "<p>Excel 自动回填工具已下线：区域各指标情况一览现在由系统（reportdb）统一计算并导出，"
    "请使用 reportdb 网页的「日报导出」。</p>"
)


@app.get("/")
async def index():
    if _engine_reportdb():
        return HTMLResponse(_RETIRED_HTML, status_code=410)
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/generate")
async def generate_report(
    files: list[UploadFile] = File(default=[]),
    template: UploadFile | None = File(default=None),
) -> Response:
    if _engine_reportdb():
        raise HTTPException(status_code=410, detail="Excel 处理入口已下线：报表由系统（reportdb）统一计算并导出。")
    if not files and template is None:
        raise HTTPException(status_code=400, detail="请至少上传 7 份源报表。")

    uploaded_files: list[tuple[str, bytes]] = []
    for upload in files:
        content = await upload.read()
        uploaded_files.append((upload.filename or "未命名文件.xlsx", content))

    template_name: str | None = None
    if template is not None and template.filename:
        template_name = template.filename
        template_content = await template.read()
    else:
        if not DEFAULT_TEMPLATE_PATH.exists():
            raise HTTPException(status_code=500, detail="找不到内置模板文件。")
        template_content = DEFAULT_TEMPLATE_PATH.read_bytes()

    try:
        report_bytes, output_name = build_report(uploaded_files, template_content, template_name)
    except ReportBuildError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    encoded_name = quote(output_name)
    headers = {
        "Content-Disposition": (
            f'attachment; filename="district-report.xlsx"; '
            f"filename*=UTF-8''{encoded_name}"
        ),
    }
    return Response(
        content=report_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=headers,
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=False)
