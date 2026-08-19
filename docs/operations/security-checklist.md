# BoxPorter 生产安全清单（规划书 §19 / §21.3 门槛）

上线前逐项确认；任何一项未满足都不得宣称“个人生产可用”。

## 认证与入口

- [ ] Web 仅监听 127.0.0.1（launchd plist `serve --host 127.0.0.1`）
- [ ] 未将 3088 端口直接公网裸露（检查路由器端口转发）
- [ ] 登录密码已设置且未写入 Git/日志/证据（`boxporter-v2 web-set-password`）
- [ ] 远程访问走 Tailscale/WireGuard 或 SSH 隧道（规划书 §18.4）
- [ ] 高风险操作（审核/封箱/停 Run/改策略/撤销会话）使用重认证

## Secret 与数据

- [ ] 模型 API Key 仅存 Keychain 或环境变量，使用 Secret Reference
- [ ] `data/.admin-password` 权限 600 且不入 Git
- [ ] `.gitignore` 覆盖 data/*.sqlite、artifacts/、logs/
- [ ] 备份目录加密（FileVault 开启或备份脚本后接加密）

## 无人值守边界

- [ ] Away Mode 只允许 low/medium 风险任务（policy 默认值）
- [ ] 生产写入/推送/外发/付费动作无审批不可执行（默认禁止清单）
- [ ] 任务 Token 预算与每日 Token 上限已按实际费用设定
- [ ] 无同任务同角色重复租约（Lease 唯一约束 + 测试覆盖）

## 恢复与运维

- [ ] launchd 两个服务已加载且 KeepAlive 生效
- [ ] 断电自动开机 + UPS 已配置（系统设置 → 节能 → 断电后自动启动）
- [ ] `operations/scripts/backup.sh` 已接入定时任务（建议 launchd StartCalendarInterval 每日）
- [ ] `operations/scripts/restore-drill.sh` 每月演练并记录报告
- [ ] newsyslog 轮转已安装（operations/newsyslog/com.boxporter.conf）
- [ ] 外部健康探针（如 Uptime Kuma / cron curl）监控 Web 可用性

## 验收测试

- [ ] `uv run pytest -q` 全绿（138+ 用例）
- [ ] `uv run mypy && uv run ruff check src tests` 全绿
- [ ] 真实 Runner（OpenHands 等）固定版本 + 契约测试通过后才切换生产
- [ ] 24–72 小时无人值守长稳测试无 P0/P1 问题
