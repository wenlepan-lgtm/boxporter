#!/bin/sh
# BoxPorter 一键部署：安装并加载 launchd 服务（daemon + web）。
# 用法: install-launchd.sh [BoxPorter根目录]
# 幂等：重复执行会先卸载旧实例再加载新配置。
set -eu

ROOT="${1:-/Users/alamn/BoxPorter}"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"

for label in daemon web; do
  src="$ROOT/operations/launchd/com.boxporter.$label.plist"
  dst="$LAUNCH_AGENTS/com.boxporter.$label.plist"
  [ -f "$src" ] || { echo "missing: $src" >&2; exit 1; }
  plutil -lint "$src" >/dev/null
  launchctl unload "$dst" 2>/dev/null || true
  cp "$src" "$dst"
  launchctl load "$dst"
done

echo "installed: com.boxporter.daemon, com.boxporter.web"
launchctl list | grep com.boxporter || true
echo "web console: http://127.0.0.1:3088"
