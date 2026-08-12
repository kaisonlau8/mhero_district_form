# 区域报表自动生成

处理 7 份日常 Excel 源表，回填「区域各指标情况一览」模板并导出结果。支持：

- **自动化流水线**：共享 DMS 浏览器爬取源表 → 每天 08:30 出报表 → 飞书群推送（控制台 `:9003`）
- **本地网页模式**：FastAPI 上传生成（`:8000`）
- **macOS 桌面模式**：`pywebview` 封装

> 工具集总览 / 文档地图 / 依赖关系：[m-hero](https://github.com/kaisonlau8/m-hero)

| 项 | 值 |
|----|-----|
| 流水线控制台 | `http://127.0.0.1:9003` / http://127.0.0.1:9003 |
| 黄页 | http://127.0.0.1:9004 |
| 共享会话 | 与事故车、VIP 共用 DMS Chromium |

## 文档导航

- 工具集：[m-hero](https://github.com/kaisonlau8/m-hero) · [共享浏览器](https://github.com/kaisonlau8/m-hero/blob/main/docs/SHARED_DMS_BROWSER.md)
- [使用文档](docs/usage.md)
- [开发文档](docs/development.md)
- [打包与发版文档](docs/release.md)

如果只是第一次使用，建议先看“使用文档”。

如果要改需求、换模板、加规则、重新打包，建议先看“开发文档”和“打包与发版文档”。

## 项目能力

- 自动识别 7 份源报表文件，忽略每天变化的导出时间后缀。
- 自动清空并重写模板中的以下工作表：
  - `备件库存明细`
  - `招揽实施率`
  - `首保`
  - `二保`
  - `新保`
  - `续保`
- 模板包含 11 个工作表，其中公式驱动的工作表会自动重算：
  - `直营店指标` — 直营门店各项指标汇总
  - `区域指标排序` — 各区域指标排名
  - `直营店指标排序` — 直营门店指标排名
  - `总表` — 区域级指标汇总
  - `常备件备库率` — 经销商备库率明细（123 家经销商）
- 自动处理三条业务特殊规则：
  - “去年同期交付未新保车辆”会在 `E/F` 之间补 1 列空白后再写入 `续保`
  - “门店备件库存导出”的 `M` 列会转成数字格式
  - “首保”工作表的 `H` 列不再直接使用源报表结果，而是按 `I` 列“实际首保日期”是否有值判断为“是/否”
- 导出文件名自动命名为 `区域各指标情况一览MMDD.xlsx`
- 支持内置模板，也支持用户临时上传新的模板文件

## 快速开始

### 方式一：本地网页模式

```bash
cd /Users/i/myCode/m-hero/mhero_district_form
chmod +x start.command
./start.command
```

启动后打开：

- [http://127.0.0.1:8000](http://127.0.0.1:8000)

### 方式二：开发环境手动启动

```bash
cd /Users/i/myCode/m-hero/mhero_district_form
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

### 方式三：构建 macOS 桌面应用

```bash
cd /Users/i/myCode/m-hero/mhero_district_form
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install pyinstaller
python build_macos_app.py
```

构建结果：

- `dist/区域报表自动生成.app`
- `dist/mhero-district-form-macos-arm64.zip`

## 仓库结构

```text
app/
  assets/
    report_template.xlsx      默认模板
  static/
    index.html                页面结构
    app.js                    前端交互与上传/下载逻辑
    styles.css                页面样式
  main.py                     FastAPI 入口
  processor.py                Excel 处理核心逻辑
  runtime.py                  资源路径定位
desktop_app.py                macOS 桌面入口
build_macos_app.py            本地打包脚本
.github/workflows/release.yml GitHub Actions 打包与 Release 上传
```

## DMS 自动爬取 + 每天 08:30 出报表

依赖共享 Chromium 会话（与事故车/VIP 相同）：`DFMC_DMS_SESSION_HOME=/Users/i/dms-shared-session`。浏览器需已登录 DMS。开跑前 3 分钟至爬取登记完成期间保活不会强刷。

| 项 | 值 |
|----|-----|
| 本地控制台 | `http://127.0.0.1:9003` |
| 公网 | http://127.0.0.1:9003 |
| Tunnel | `m-hero-district-form` → `:9003` |
| 导出 | 完整公式版 Excel（`/api/report/latest`） |

```bash
# 控制台：配置年份/季度、上传默认模板、手动触发流水线
./run.sh --console
# http://127.0.0.1:9003 或 http://127.0.0.1:9003

# 手动跑完整流水线（爬 7 份源表 → 生成区域报表）
./run.sh --pipeline --year 2026 --quarter 3

# 仅用已下载文件生成报表
./run.sh --pipeline --skip-crawl
```

定时任务（launchd）：

- `com.mhero-district-form.web` — 控制台常驻 `:9003`
- `com.mhero-district-form.pipeline` — 每天 **08:30** 跑 `run_pipeline.py`（读取 `config/crawl_settings.json` 的年/季度）
- `com.cloudflare.cloudflared.m-hero-district-form` — Cloudflare Tunnel 公网入口

报表生成后（成功或失败）会通过飞书群机器人 Webhook 推送卡片；成功时可点「下载报表」：
`http://127.0.0.1:9003/api/report/latest`（配置见 `.env` 的 `FEISHU_WEBHOOK_URL`）。

产物目录：`download/`（源表）、`output/`（`区域各指标情况一览MMDD.xlsx`）。

## 当前发布说明

- GitHub Release 中已经提供可下载的 macOS 安装包
- 当前产物是未签名应用
- 当前默认面向 Apple Silicon `arm64`

如果 macOS 首次打开时提示来源未知，可在 Finder 中对应用执行“右键 -> 打开”。

## 后续维护建议

后续如果需求变化，通常只需要优先关注以下文件：

- 业务映射或输出规则：`app/processor.py`
- 页面交互：`app/static/app.js`
- 桌面保存逻辑：`desktop_app.py`
- 打包产物命名：`build_macos_app.py`
- 自动发版：`.github/workflows/release.yml`

详细说明见：

- [开发文档](docs/development.md)
- [打包与发版文档](docs/release.md)
