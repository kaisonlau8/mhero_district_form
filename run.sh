#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$ROOT/.venv/bin/python"

export TZ="${TZ:-Asia/Shanghai}"

# shellcheck disable=SC1091
if [[ -f "$ROOT/.env" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$ROOT/.env"
  set +a
fi

if [[ ! -x "$PYTHON" ]]; then
  echo "虚拟环境不存在，先运行: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt playwright flask python-dotenv && .venv/bin/python -m playwright install chromium" >&2
  exit 1
fi

MODE="${1:-}"
shift || true

case "$MODE" in
  --console)
    exec "$PYTHON" "$ROOT/scripts/web_console.py" "$@"
    ;;
  --crawl)
    exec "$PYTHON" "$ROOT/scripts/crawl_district_reports.py" "$@"
    ;;
  --pipeline|--prod)
    exec "$PYTHON" "$ROOT/scripts/run_pipeline.py" "$@"
    ;;
  --help|-h|"")
    cat <<'EOF'
用法:
  ./run.sh --console              启动控制台 (默认 :9003)
  ./run.sh --crawl [--year Y --quarter Q]
  ./run.sh --pipeline [--year Y --quarter Q]
  ./run.sh --pipeline --skip-crawl
EOF
    exit 0
    ;;
  *)
    echo "未知参数: $MODE" >&2
    exit 1
    ;;
esac
