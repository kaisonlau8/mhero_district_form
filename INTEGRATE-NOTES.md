# PR22 移动端整合

基底为 PR22 `5167ba0`，移动端参考为 `f2f06ad`。

## 冲突处理

唯一冲突文件是 `reportdb/templates/weekly.html`。以 PR22 为准，保留服务指标 `kpi-sheet` 大表、全国行、低于全国值的红标和原因列，也保留经营竖柱、千元单位、原命名及文案。没有恢复旧版 `metric-card` 图表，也没有加入旧版的说明文案。

移植移动版布局：切换按钮可换行，860px 及以下将经营摘要排为单列、各区竖柱排为两列，并调整日期、编辑表单及截图视图的导航。将 `metric-scroll` 用在 PR22 大表上，保留表格语义，避免卡片化；窄屏可在表内横滚，打印不受最小表宽限制。历史表增加外层容器，缺数占位只在竖柱内绝对定位。

## 文件范围

- `reportdb/templates/weekly.html`：解决冲突，整合移动布局。
- `reportdb/templates/base.html`、`_home_table.html`、`data.html`、`weekly_import.html`：纳入已预拷入的非冲突移动模板，涵盖导航、总表横滚、明细卡片和导入表单布局。
- `requirements-dev.txt`：纳入 Playwright 测试依赖。
- `tests/test_reportdb_mobile.py`：纳入 Chromium 回归，并将周报断言改为覆盖经营竖柱、摘要布局以及 PR22 指标大表和局部横滚。
- `INTEGRATE-NOTES.md`：记录整合范围。

未改计算、导入后端、`weekly_crawl` 或业务口径。

## 验证命令

```sh
python -m pytest -q tests/test_reportdb_weekly.py tests/test_reportdb_mobile.py
```

本地设置 `MHERO_WEEKLY_SAMPLE_DIR=/workspace/dms-weekly-feasibility-20261120` 后运行，结果为 **31 passed**，包含真实样本回归和 Chromium 的 360、390、430、1280px 四档布局验证。
