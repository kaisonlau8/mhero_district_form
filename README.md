# 区域报表自动生成

本地网页工具：拖入 7 份日报，自动写入“区域各指标情况一览”模板并下载结果。

## 启动

```bash
cd /Users/i/myCode/报表项目
chmod +x start.command
./start.command
```

如果只想手动启动服务：

```bash
cd /Users/i/myCode/报表项目
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

打开 [http://127.0.0.1:8000](http://127.0.0.1:8000)。

## 规则

- 按文件名前缀自动识别 7 份源报表，忽略每天变化的导出后缀。
- “去年同期交付未新保车辆”会在 E、F 列之间补 1 个空白列，再追加到“续保”sheet。
- “门店备件库存导出”的 M 列会写成数字格式，保证模板公式可以参与计算。
- 默认使用内置模板 [app/assets/report_template.xlsx](/Users/i/myCode/报表项目/app/assets/report_template.xlsx)，也可以在页面上额外上传新的“区域各指标情况一览xxxx”。
