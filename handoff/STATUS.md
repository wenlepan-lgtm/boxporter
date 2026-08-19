# BoxPorter V1.1 发布状态

更新：2026-08-19

## 结论

BoxPorter V1.1 的核心控制面、Runner 接口、Web 管理面和单机运维基线已实现，准备作为
`v1.1.0` 发布。V0.2 的公开 API 保留在 `src/boxporter/v0.py`，新控制面通过
`boxporter-v2` 命令提供。

## 已实现

- 四箱状态投影与 Task/Attempt/Run/Lease/Event 生命周期。
- Executor 与 Reviewer 身份、会话和 Worktree 隔离。
- Submission Manifest、验收门、秘密扫描和证据封箱。
- 事件驱动调度、重试退避、预算、进度检测、熔断、审批和恢复。
- Command、DeepSeek Harness、OpenHands Runner 适配层。
- FastAPI、项目域 Web 控制台、Run 管理、健康面板和 SSE 游标重放。
- SQLite 迁移、备份恢复、launchd 与日志轮转运维基线。

## 发布验证

- `uv run ruff check .`
- `uv run mypy .`
- `uv run pytest`
- `git diff --check`
- 发布差异秘密扫描；运行凭据、SQLite/WAL/SHM、日志和缓存不进入仓库。

## 已知边界

- 定位是一台机器或可信共享文件系统上的小规模编码 Agent 协作，不是分布式消息队列。
- 真实模型凭据、远程访问、UPS/FileVault、定时备份与 24–72 小时长稳仍需部署方按环境验收。
- 管理员密码仅在部署时本地生成并保存在被 `.gitignore` 排除的运行目录中。
