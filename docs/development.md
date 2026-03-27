# 开发文档

## 1. 文档目标

本文档面向后续维护这个项目的开发者，重点说明：

- 当前项目的技术栈与模块职责
- 从上传到导出的完整数据流
- 业务规则在代码中的落点
- 如何修改需求、模板、映射和输出规则
- 如何本地调试与验证

如果你只负责打包或发版，也请结合阅读：

- [打包与发版文档](release.md)

## 2. 项目目标

这个项目的本质不是“通用 Excel 平台”，而是“针对固定业务流程的自动化工具”。

它服务于一个明确的日常动作：

1. 上传 7 份日报 Excel
2. 把这些数据回填到固定模板中的多个工作表
3. 依赖模板现有公式，由 Excel 自行重算 `总表`
4. 输出新的结果文件

项目的设计原则是：

- 尽量复用现有 Excel 模板，不在代码里重写业务统计公式
- 只处理“导入、清空、重写、导出”这条链路
- 把复杂业务口径尽量收敛在 `app/processor.py`

## 3. 技术栈

### 3.1 后端

- Python 3.12
- FastAPI
- openpyxl

用途：

- 接收前端上传的文件
- 调用 Excel 处理逻辑
- 生成新的 `.xlsx` 并返回给前端

### 3.2 前端

- 原生 HTML
- 原生 CSS
- 原生 JavaScript

用途：

- 拖拽上传
- 文件识别状态展示
- 调用 `/api/generate`
- 网页版触发浏览器下载
- 桌面版调用 `pywebview` 的保存接口

### 3.3 桌面端

- pywebview
- uvicorn

用途：

- 启动本地 FastAPI 服务
- 打开桌面窗口承载网页
- 通过 Python bridge 执行系统保存对话框

### 3.4 打包

- PyInstaller
- GitHub Actions

用途：

- 本地构建 `.app`
- 构建 zip 安装包
- tag 推送后自动上传 GitHub Release

## 4. 架构总览

```mermaid
flowchart LR
    A["用户上传 7 份 Excel"] --> B["前端 app.js"]
    B --> C["POST /api/generate"]
    C --> D["FastAPI app.main"]
    D --> E["build_report in app.processor"]
    E --> F["openpyxl 读取模板与源文件"]
    F --> G["清空目标工作表并写入数据"]
    G --> H["保存新工作簿"]
    H --> I["返回 Excel 二进制"]
    I --> J["网页下载 或 桌面版保存对话框"]
```

## 5. 目录结构与职责

```text
app/
  assets/
    report_template.xlsx
  static/
    index.html
    app.js
    styles.css
  main.py
  processor.py
  runtime.py
desktop_app.py
build_macos_app.py
.github/workflows/release.yml
requirements.txt
start.command
```

### 5.1 `app/main.py`

FastAPI 主入口。

主要职责：

- 挂载静态资源目录
- 返回首页 `index.html`
- 暴露健康检查接口 `/api/health`
- 暴露核心接口 `/api/generate`

### 5.2 `app/processor.py`

项目最重要的业务文件。

主要职责：

- 识别上传文件类型
- 读取 Excel 数据
- 应用业务特殊规则
- 清空模板指定工作表
- 将源数据写入模板
- 输出最终 Excel 文件

如果以后业务映射、导出命名、列对齐规则有变化，优先改这里。

### 5.3 `app/runtime.py`

资源路径适配层。

主要职责：

- 区分“源码运行”与“PyInstaller 打包后运行”
- 正确定位 `app/static`
- 正确定位 `app/assets`

如果你发现桌面版能启动但找不到模板或静态文件，优先看这里。

### 5.4 `app/static/index.html`

页面骨架文件。

主要职责：

- 上传区域
- 模板替换区域
- 状态卡片容器
- 生成按钮

### 5.5 `app/static/app.js`

前端核心逻辑。

主要职责：

- 识别文件名关键字
- 维护 7 份文件的上传状态
- 调用后端接口
- 网页模式下载文件
- 桌面模式调用 `window.pywebview.api.save_report`

### 5.6 `desktop_app.py`

桌面入口。

主要职责：

- 启动本地 uvicorn 服务
- 打开 pywebview 窗口
- 暴露保存文件桥接 `DesktopBridge`

### 5.7 `build_macos_app.py`

本地打包脚本。

主要职责：

- 清理旧的 `build/`、`dist/`、`.spec`
- 调用 PyInstaller 打包 `.app`
- 调用 `ditto` 生成 release zip

### 5.8 `.github/workflows/release.yml`

GitHub Actions 自动发版脚本。

主要职责：

- 在 `v*` tag 推送时触发
- 在 macOS runner 上安装依赖
- 执行打包脚本
- 上传 zip 到 GitHub Release

## 6. 核心业务规则与代码落点

### 6.1 文件识别

落点：

- `app/processor.py`
- `SOURCE_SPECS`
- `detect_file_role`

当前识别逻辑：

- 以文件名去掉扩展名后的内容作为识别基础
- 去除空白字符
- 只要前缀或文件名中包含关键字即可识别

这使得文件名后缀中的日期时间戳可变而不影响系统识别。

### 6.2 模板识别

落点：

- `TEMPLATE_PREFIX = "区域各指标情况一览"`

作用：

- 识别用户额外上传的模板
- 如果上传了模板，则优先使用上传模板
- 否则回退到内置模板 `app/assets/report_template.xlsx`

### 6.3 目标工作表清空与重写

落点：

- `clear_sheet_data`
- `write_rows`
- `build_report`

策略：

- 保留第 1 行表头
- 删除第 2 行到最后一行的旧数据
- 逐行 append 新数据

这个策略的优点是：

- 模板结构稳定
- 表头不丢失
- 不需要重新创建工作表

### 6.4 续保双表拼接

落点：

- `normalize_backlog_rows`
- `build_report`

规则：

- `续保投保率` 直接使用原结构
- `去年同期交付未新保车辆` 在 `E/F` 之间补一列空白
- 两份数据拼接后统一写入 `续保`

### 6.5 备件库存 M 列转数字

落点：

- `parse_numeric_text`
- `normalize_stock_rows`

规则：

- 若 `M` 列是文本数字，如 `"53.00"`，则转为数值
- 若是整数，则尽量转成 `int`
- 若是小数，则转成 `float`
- 若不是数字字符串，则保持原值

### 6.6 输出文件名

落点：

- `choose_output_name`

规则：

- 按当前系统时间生成 `区域各指标情况一览MMDD.xlsx`

如果未来要改成别的命名方式，例如包含年份、时分、区域等，直接改该函数即可。

## 7. 本地开发环境搭建

### 7.1 创建虚拟环境

```bash
cd /Users/i/myCode/报表项目
python3 -m venv .venv
source .venv/bin/activate
```

### 7.2 安装依赖

```bash
pip install -r requirements.txt
```

如果要打桌面包，还需要：

```bash
pip install pyinstaller
```

### 7.3 启动网页模式

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

### 7.4 启动桌面调试模式

```bash
python desktop_app.py
```

### 7.5 自动打开网页模式

```bash
./start.command
```

## 8. 如何修改需求

### 8.1 增加新的源报表

需要同步改动的地方通常有两处：

1. `app/processor.py`
2. `app/static/app.js`

后端要改：

- 在 `SOURCE_SPECS` 中新增源文件定义
- 在 `build_report` 中定义写入目标工作表的逻辑

前端要改：

- 在 `REQUIRED_TYPES` 中新增上传项
- 页面状态卡片会自动按数组生成

如果还涉及模板新增工作表，请同时更新模板文件：

- `app/assets/report_template.xlsx`

### 8.2 修改文件识别关键字

改这里：

- `app/processor.py` 的 `SOURCE_SPECS`
- `app/static/app.js` 的 `REQUIRED_TYPES`

注意：

- 前后端的关键字最好保持一致
- 否则会出现前端识别成功、后端识别失败，或反过来的情况

### 8.3 修改模板默认文件

直接替换：

- `app/assets/report_template.xlsx`

注意事项：

- 工作表名称必须保持系统预期一致
- 至少应保留 `备件库存明细`、`招揽实施率`、`首保`、`二保`、`新保`、`续保`
- 如果模板公式依赖更多工作表，也应保留它们

### 8.4 修改导出文件名规则

改这里：

- `app/processor.py` 中的 `choose_output_name`

### 8.5 修改桌面版窗口行为

改这里：

- `desktop_app.py`

常见修改点：

- 窗口标题
- 窗口尺寸
- 最小尺寸
- 保存逻辑

### 8.6 修改打包文件名

改这里：

- `build_macos_app.py` 的 `ZIP_NAME`
- `.github/workflows/release.yml` 中上传的文件路径

## 9. 调试建议

### 9.1 先调网页模式，再调桌面模式

原因：

- 网页模式更容易看到接口报错
- 不受桌面桥接影响
- 能先确认业务处理逻辑是否正常

### 9.2 优先验证模板工作表名称

很多报错并不是代码问题，而是模板被改了。

如果出现类似问题：

- “模板缺少工作表：续保”
- “总表没有自动计算”

应优先检查模板结构是否与系统预期一致。

### 9.3 导出逻辑问题优先看 `processor.py`

如果问题表现为：

- 识别不到文件
- 写入位置错误
- 列数对不齐
- 输出文件名不对

优先检查：

- `detect_file_role`
- `normalize_backlog_rows`
- `normalize_stock_rows`
- `build_report`

### 9.4 桌面版保存问题优先看 `desktop_app.py`

如果问题表现为：

- 无法弹出保存框
- 保存路径异常
- 导出时报 `File exists: '/'`

优先检查：

- `DesktopBridge.save_report`

## 10. 验证清单

当前项目没有自动化测试套件，因此每次改动后至少做一轮人工验证。

建议验证以下项目：

### 10.1 网页模式验证

1. 启动 `uvicorn app.main:app`
2. 打开首页
3. 上传 7 份源报表
4. 确认状态卡片全部识别成功
5. 点击生成
6. 确认下载成功
7. 打开生成的 Excel，确认：
   - `备件库存明细` 已更新
   - `续保` 已拼接两张表
   - `总表` 打开后可自动重算

### 10.2 桌面模式验证

1. 运行 `python desktop_app.py`
2. 上传 7 份文件
3. 点击生成
4. 确认保存对话框弹出
5. 选择本地路径保存
6. 确认文件成功写入磁盘

### 10.3 打包验证

1. 运行 `python build_macos_app.py`
2. 确认产出：
   - `dist/区域报表自动生成.app`
   - `dist/mhero-district-form-macos-arm64.zip`
3. 启动 `.app`
4. 至少做一轮上传与保存测试

## 11. 已知限制

### 11.1 当前没有自动单元测试

因此每次修改都要靠人工 smoke test。

### 11.2 当前默认只打包 macOS arm64

如果后续要支持 Intel Mac 或 universal 包，需要调整：

- PyInstaller 构建环境
- Release 命名
- 发布流程说明

### 11.3 当前应用未签名、未公证

这会导致：

- 首次打开需要手动允许
- 更适合内部使用而非公开分发

### 11.4 公式重算依赖 Excel 客户端

代码不会自己替代 Excel 计算全部公式。

当前做法是：

- 保留模板公式
- 设置工作簿为打开时自动重算

## 12. 推荐维护方式

后续更新时建议遵循下面的顺序：

1. 先确认业务需求改动属于“前端展示”还是“Excel 数据规则”
2. 若涉及写表结构，优先修改 `app/processor.py`
3. 若涉及文件识别项，前后端配置一起改
4. 若涉及模板变化，先准备新版模板，再改代码
5. 网页模式验证通过后，再做桌面版验证
6. 最后再做打包与 release

这样能最大限度降低“已经打包了才发现业务规则没对上”的返工成本。
