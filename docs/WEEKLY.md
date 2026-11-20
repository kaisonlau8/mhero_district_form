# 猛士周报导入

周报和每天 08:30 的 7 张源表不是同一套数。经营比较是季度环比和年度同比，更新频率仍是每周。每天 08:30 的调度没有改。

周五经营采集入口：

```bash
python scripts/weekly_operation_crawl.py --as-of 2026-11-20 --regions 华东
python scripts/weekly_operation_crawl.py --live
```

不加 `--live` 只打印计划，不连接 DMS。`--live` 才使用现场已经登录的共享浏览器：先拿共用导出锁（时刻表 id `district-form-weekly`），从报表菜单点「服务经营报表」，设成收款、日报、区域、空门店和起止日期，查询前后把筛选项读回来。成功只认这次点击查询之后，页面自己发出的那一对响应：请求里的筛选和本次一致，一条是明细，一条是汇总，汇总正文里有工单数、车辆数、总产值。旁边的成功响应、上一笔查询、页面上的旧数字，都不能放行。汇总响应没看到，或汇总请求失败，就明确失败并待人工复核，最多再试 2 次。数字和上次一样也可以导入，只要这次响应对得上。不会把旧 Excel 当成新导出，也不会把明细或分月的车辆数加起来。Excel 必须和这次汇总响应一致。已经成功的文件保留。某一份失败不把整个任务说成完成。生产机是 Ubuntu `dfms-tecent`，不是 Mac。仓库里原来的定时文件是 macOS launchd，网页进程在现场由 supervisor 的 `reportdb-web` 管。周五采集是一次命令，和每天 08:30 的采集一样用 cron，不做成常驻进程，也不改 08:30。

启动时间是每周五 14:45（Asia/Hong_Kong）。16:57 起不再开始新的地区或期间，把锁留给 17:00 事故任务的提前 3 分钟。已经开始的这一次会做完，然后释放锁，不会关掉浏览器。来不及做的范围记成缺口，不把整批说成完成。

请求必须是 JSON 或表单字段，字段名就是页面上的「统计类型、报表类型、区域、业务门店、开始日期、结束日期」。值只出现在别的文字里不算。空门店必须明确出现在请求里。真实 DMS 如果用别的字段名，脚本会失败关闭，需要现场联调后再改，不会去探测接口。

父部署时在生产机执行，本仓库这次没有执行：

```bash
cd /home/ubuntu/m-hero-prod/mhero_district_form
deploy/ubuntu-weekly-crawl.sh verify
deploy/ubuntu-weekly-crawl.sh install          # 只打印
deploy/ubuntu-weekly-crawl.sh install --apply  # 写入 crontab，并只追加时刻表这一条
deploy/ubuntu-weekly-crawl.sh verify
# 卸掉时：
deploy/ubuntu-weekly-crawl.sh uninstall --apply
```

`install --apply` 需要当时环境里已有 `DFMC_DMS_SESSION_HOME`，并且共享时刻表文件已经存在。它不新建一份时刻表，也不改 08:30 和 17:00 的条目。现有避让按每天的钟点计算，不辨星期，加上 14:45 之后每天 14:42 到约 15:30 都会避开强刷。

页面：登录后打开「报表 → 猛士周报」。管理员在「导入 Excel」上传。命令行：

```bash
python -m reportdb.weekly_import operation \
  --file 季度累计.xlsx --week 2026-11-16 --span quarter \
  --period-start 2026-10-01 --period-end 2026-11-20 \
  --exported-at "2026-11-20 14:25:35" --timezone +08:00

python -m reportdb.weekly_import operation \
  --file 上季同进度.xlsx --week 2026-11-16 --span quarter_base \
  --as-of 2026-11-20 --period-start 2026-07-01 --period-end 2026-08-20 \
  --exported-at "2026-11-20 14:25:35"

python -m reportdb.weekly_import operation \
  --file 年度累计.xlsx --week 2026-11-16 --span year \
  --period-start 2026-01-01 --period-end 2026-11-20 \
  --exported-at "2026-11-20 14:25:35"

python -m reportdb.weekly_import kpi \
  --file 服务管理主表.xlsx \
  --week 2026-11-16 \
  --as-of 2026-11-20 \
  --exported-at "2026-11-20 14:25:35"
```

经营文件按 DMS 的「收款 + 日报」导出。全国一份，每个区域再各导出一份，业务门店留空。Excel 里没有统计期间、收款类型和导出时间，所以上面这些参数要按当时的查询条件填写，不能从文件名猜测。

周次仍是周一，只用来归档「这一周更新的快照」。经营数字不是这一周对上一周。

- `quarter`：本季第一天到截止日。截止日必须落在所选周内。
- `quarter_base`：上一季度的对比期。未结束的季度默认按同进度（上季起日加上本季已经过的天数，上季更短就封顶到上季末日）。这是实现默认，不是业务另行指定。完整季度则对比上一季度整季。
- `year`：当年 1 月 1 日到截止日。
- `year_base`：去年 1 月 1 日到同月同日。2 月 29 日在去年不存在时取 2 月 28 日。完整年度对比完整上一年。

2026-11-01 至 2026-11-19，以及「周一到周内某天」，都不能导入成季度或年度。旧的周区间快照即使还在库里，角色为空，也不会拿来当季度环比或年度同比。

同一周、同一范围，文件和元数据都相同：再导入不会新增。内容不同：默认拒绝，旧快照保持原样。确认要换一版时加 `--revision` 或勾选「保留旧快照并写入新修订」。旧行仍在历史里。

服务考核指标不从 DMS 主表导入，也不为周报再导出一份。周报读取该周已经算好的 reportdb 总表（`calc.compute`，`report_results.table_key=summary`）：首保、二保、新保、续保、常备件备库率、招揽实施率。这是该快照日的整份明细汇总，不是再按季度起日至截止日切出来的另一套累计。没有该周快照时显示待计算。

收款工单数、总产值、车辆数只来自上面的服务经营报表收款导出，车辆数用汇总行去重值。这两块不能互换。总产值源数据单位为元（2026-11-20 确认），页面按千元展示并保留两位小数，涨跌仍按原始元计算。是否含税未注明。

督导原因在周报页面手填，开始是空的。

自动下载必须遵守 [共享 DMS 浏览器约定](https://github.com/kaisonlau8/m-hero/blob/main/docs/SHARED_DMS_BROWSER.md)：共用一把导出锁，和每天 08:30 的导出错开。服务指标仍用系统自己算出的总表，不从 DMS 指标主表导出。
