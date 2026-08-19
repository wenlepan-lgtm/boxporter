# ADR-004：Lease、Heartbeat 与 fencing token

- 状态：Accepted
- 日期：2026-08-14

## 上下文

V0.2 以任务文件 mtime 判断“还在工作”，无法区分进程崩溃与仍在执行，也无法阻止过期旧进程继续写入。规划书要求：同任务同角色同时最多一个有效 Lease；fencing token 阻止过期旧进程写入。

## 决策

- Lease 由数据库事务创建（task_id + role 唯一约束生效），包含 run_id、owner_instance、pid、heartbeat_at、expires_at、fencing_token。
- 心跳来自 Runner 事件或 Porter 采样；不接受 touch 任务文件伪造心跳。
- 任何携带过期 fencing_token 的写状态请求被拒绝并记安全事件。
- Lease 过期只触发诊断（采集进程/事件/Git/预算），不直接启动第二个执行者。
- 默认值：心跳间隔 30s、Lease 有效期 5min、可疑停滞诊断 20min（可配置）。

## 后果

- 重复执行写同一任务被结构性阻止，而非依赖运行时自觉。
- 恢复决策与“进程是否还在”解耦，先诊断后动作。
