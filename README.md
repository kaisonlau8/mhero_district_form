# 区域报表自动生成

一个用于处理 7 份日常 Excel 报表的本地工具。用户只需要上传或拖入源文件，系统就会自动把数据回填到“区域各指标情况一览xxxx”模板中，并导出结果文件。

项目同时提供两种运行方式：

- 本地网页模式：启动 FastAPI 服务，在浏览器中使用。
- macOS 桌面模式：通过 `pywebview` 将网页封装为桌面应用窗口。

## 文档导航

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
- 自动处理两条业务特殊规则：
- 自动处理三条业务特殊规则：
  - “去年同期交付未新保车辆”会在 `E/F` 之间补 1 列空白后再写入 `续保`
  - “门店备件库存导出”的 `M` 列会转成数字格式
  - “首保”工作表的 `H` 列不再直接使用源报表结果，而是按 `I` 列“实际首保日期”是否有值判断为“是/否”
- 导出文件名自动命名为 `区域各指标情况一览MMDD.xlsx`
- 支持内置模板，也支持用户临时上传新的模板文件

## 快速开始

### 方式一：本地网页模式

```bash
cd /Users/i/myCode/报表项目
chmod +x start.command
./start.command
```

启动后打开：

- [http://127.0.0.1:8000](http://127.0.0.1:8000)

### 方式二：开发环境手动启动

```bash
cd /Users/i/myCode/报表项目
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

### 方式三：构建 macOS 桌面应用

```bash
cd /Users/i/myCode/报表项目
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
