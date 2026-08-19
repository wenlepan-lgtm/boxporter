#!/bin/sh
# 每月恢复演练（ADR-014）：
# 1. 在临时目录校验最新备份（数据库、migration、文件哈希）；
# 2. 重算全部 PASSED 证据包哈希；
# 3. 输出演练报告。
set -eu

BOXPORTER_BIN="${BOXPORTER_BIN:-/Users/Alamn/BoxPorter/.venv/bin/boxporter-v2}"
BACKUP_ROOT="${BACKUP_ROOT:-/Users/Alamn/BoxPorter/backups}"
DATA_DIR="${DATA_DIR:-/Users/Alamn/BoxPorter/data}"

latest="$(ls -1t "$BACKUP_ROOT" | grep '^backup-' | head -n1)"
if [ -z "$latest" ]; then
  echo "drill: no backups found in $BACKUP_ROOT" >&2
  exit 1
fi

echo "drill: verifying $BACKUP_ROOT/$latest"
"$BOXPORTER_BIN" --data-dir "$DATA_DIR" backup-verify "$BACKUP_ROOT/$latest"
echo "drill: PASSED"
