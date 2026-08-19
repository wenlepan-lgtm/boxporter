#!/bin/sh
# BoxPorter 在线备份：由 launchd 定时或手动执行。
# 用法: backup.sh [数据目录] [备份目录] [证据目录]
set -eu

DATA_DIR="${1:-/Users/Alamn/BoxPorter/data}"
BACKUP_ROOT="${2:-/Users/Alamn/BoxPorter/backups}"
EVIDENCE_ROOT="${3:-/Users/Alamn/BoxPorter/artifacts}"

exec /Users/Alamn/BoxPorter/.venv/bin/boxporter-v2 \
  --data-dir "$DATA_DIR" backup \
  --backup-root "$BACKUP_ROOT" \
  --evidence-root "$EVIDENCE_ROOT"
