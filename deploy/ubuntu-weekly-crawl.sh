#!/usr/bin/env bash
# 只给 Ubuntu 生产机 dfms-tecent 准备周五 14:45 周报采集。
# 默认只打印将要写入的内容。加上 --apply 才会改 crontab 和共享时刻表。
# 不改 08:30 流水线，不改 17:00 事故任务，不读取或打印密码。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="/home/ubuntu/m-hero-run/venvs/mhero_district_form/bin/python"
DB="/home/ubuntu/m-hero-run/data/reportdb/reportdb.sqlite3"
LOG_DIR="/home/ubuntu/m-hero-run/logs"
MARK_BEGIN="# mhero-weekly-crawl begin"
MARK_END="# mhero-weekly-crawl end"
ACTION="${1:-verify}"
APPLY=0
if [[ "${2:-}" == "--apply" || "${1:-}" == "--apply" ]]; then
  APPLY=1
fi

cron_block() {
  cat <<EOF
$MARK_BEGIN
CRON_TZ=Asia/Hong_Kong
45 14 * * 5 cd $ROOT && set -a && [ -f .env ] && . ./.env && set +a && $PYTHON $ROOT/scripts/weekly_operation_crawl.py --live --db $DB >> $LOG_DIR/weekly-operation-crawl.log 2>&1
$MARK_END
EOF
}

require_session_home() {
  if [[ -z "${DFMC_DMS_SESSION_HOME:-}" ]]; then
    echo "DFMC_DMS_SESSION_HOME 未设置。不猜测路径，也不写入时刻表。" >&2
    exit 2
  fi
}

schedule_file() {
  printf '%s/.runtime/crawl_schedule.json' "$DFMC_DMS_SESSION_HOME"
}

merge_schedule() {
  require_session_home
  "$PYTHON" - "$ROOT" "$(schedule_file)" <<'PY'
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from reportdb.weekly_crawl import merge_weekly_schedule
path = Path(sys.argv[2])
if not path.is_file():
    raise SystemExit(f"时刻表不存在，不新建：{path}")
data = json.loads(path.read_text(encoding="utf-8"))
merged = merge_weekly_schedule(data)
path.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(path)
PY
}

remove_schedule() {
  require_session_home
  "$PYTHON" - "$ROOT" "$(schedule_file)" <<'PY'
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from reportdb.weekly_crawl import SCHEDULE_ID
path = Path(sys.argv[2])
if not path.is_file():
    raise SystemExit(0)
data = json.loads(path.read_text(encoding="utf-8"))
entries = [item for item in data.get("entries", []) if item.get("id") != SCHEDULE_ID]
data["entries"] = entries
path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(path)
PY
}

current_cron() {
  crontab -l 2>/dev/null || true
}

install_cron() {
  local existing filtered
  existing="$(current_cron)"
  filtered="$(printf '%s\n' "$existing" | awk -v b="$MARK_BEGIN" -v e="$MARK_END" '
    $0==b {skip=1; next}
    $0==e {skip=0; next}
    skip {next}
    {print}
  ')"
  {
    printf '%s\n' "$filtered" | sed '/^$/d'
    cron_block
  } | crontab -
}

uninstall_cron() {
  current_cron | awk -v b="$MARK_BEGIN" -v e="$MARK_END" '
    $0==b {skip=1; next}
    $0==e {skip=0; next}
    skip {next}
    {print}
  ' | crontab -
}

verify() {
  echo "cron:"
  if current_cron | grep -F "$MARK_BEGIN" >/dev/null; then
    current_cron | awk -v b="$MARK_BEGIN" -v e="$MARK_END" '$0==b {p=1} p; $0==e {p=0}'
  else
    echo "未安装周五 14:45 cron"
  fi
  if [[ -n "${DFMC_DMS_SESSION_HOME:-}" && -f "$(schedule_file)" ]]; then
    echo "schedule:"
    "$PYTHON" - "$(schedule_file)" <<'PY'
import json, sys
data = json.loads(open(sys.argv[1], encoding="utf-8").read())
ids = [item.get("id") for item in data.get("entries", [])]
print("entries", ",".join(str(item) for item in ids))
print("weekly", "district-form-weekly" in ids)
print("daily", "district-form" in ids)
print("accident_evening", "accident-evening" in ids)
PY
  else
    echo "schedule: 未读取（DFMC_DMS_SESSION_HOME 未设置或文件不存在）"
  fi
}

case "$ACTION" in
  verify)
    verify
    ;;
  install)
    echo "将安装的 cron："
    cron_block
    if [[ "$APPLY" != 1 ]]; then
      echo "这是预览。父部署时执行: deploy/ubuntu-weekly-crawl.sh install --apply"
      exit 0
    fi
    mkdir -p "$LOG_DIR"
    install_cron
    merge_schedule
    verify
    ;;
  uninstall)
    if [[ "$APPLY" != 1 ]]; then
      echo "这是预览。父部署时执行: deploy/ubuntu-weekly-crawl.sh uninstall --apply"
      exit 0
    fi
    uninstall_cron
    remove_schedule
    verify
    ;;
  *)
    echo "用法: deploy/ubuntu-weekly-crawl.sh verify|install|uninstall [--apply]" >&2
    exit 2
    ;;
esac
