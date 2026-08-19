# BoxPorter V1.1 部署与运维（Mac mini）

> 对应规划书 Phase 7 与 §18。所有命令以 `/Users/Alamn/BoxPorter` 为例。

## 1. 安装

```bash
cd /Users/Alamn/BoxPorter
uv sync                # 基础依赖
uv sync --extra openhands  # （可选）OpenHands 主 Runner SDK
uv run pytest -q       # 全量测试
uv run mypy && uv run ruff check src tests
```

## 2. 初始化

```bash
mkdir -p data worktrees artifacts backups logs
uv run boxporter-v2 --data-dir data init \
  --project-id <project-id> --name <项目名> --workspace-root <绝对路径>
BOXPORTER_ADMIN_PASSWORD='<强密码>' \
  uv run boxporter-v2 --data-dir data web-set-password
```

## 3. 任务

```bash
uv run boxporter-v2 --data-dir data add-task --spec-file task.json   # BOXPORTER_TASK_V2
uv run boxporter-v2 --data-dir data ready <task-id>
uv run boxporter-v2 --data-dir data status --project-id <project-id>
```

## 4. 启动（launchd，24×7）

```bash
cp operations/launchd/com.boxporter.daemon.plist ~/Library/LaunchAgents/
cp operations/launchd/com.boxporter.web.plist ~/Library/LaunchAgents/
# 修改 plist 中的路径后：
launchctl load ~/Library/LaunchAgents/com.boxporter.daemon.plist
launchctl load ~/Library/LaunchAgents/com.boxporter.web.plist
```

- daemon：控制平面 + 调度 + WatchDog（KeepAlive 自动重启，异常退出 60s 内恢复）；
- web：仅监听 127.0.0.1:3088，远程访问走 Tailscale / 身份网关 / FRP（规划书 §18.4），**不要公网裸露**。

手工调试可用 `uv run boxporter-v2 --data-dir data daemon --policy AWAY`。

## 5. 备份与恢复演练（每月）

```bash
operations/scripts/backup.sh          # SQLite 一致性快照 + 证据包
operations/scripts/restore-drill.sh   # 校验 migration/哈希/PASSED 证据包
```

## 6. 常用运维命令

```bash
uv run boxporter-v2 --data-dir data tick --policy AWAY       # 单次确定性 tick（零模型）
uv run boxporter-v2 --data-dir data report --from <ISO> --to <ISO>
uv run boxporter-v2 --data-dir data events --after 0
uv run boxporter-v2 --data-dir data verify-package <PASSED 证据包目录>
```

## 7. 安全注意

- 登录密码、模型 API Key 只进 Keychain / 环境变量，不写入 Git、日志或证据。
- Web 高风险操作（审核 PASS、封箱、停 Run、改策略、撤销设备）需要重新认证。
- 所有远程写操作进入追加审计事件（Event Log）。

## 8. 已知边界（ADR-014）

单机可恢复高可靠，不是高可用集群：断电/断网期间控制台不可用，恢复后状态与证据无损（reconciliation 启动时执行）。
