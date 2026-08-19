# ADR-002：SQLite + Event Log 作为 V1 状态真相

- 状态：Accepted
- 日期：2026-08-14

## 上下文

V1 部署在单台 Mac mini。需要状态单一真相、崩溃可恢复、审计可追溯，但不需要跨主机分布式能力。

## 决策

- SQLite（WAL 模式、外键开启）保存业务状态；Event Log（append-only，序列号递增）记录所有状态变化与监督决策。
- 状态迁移、事件追加、Lease 更新在同一事务内完成。
- 文件产物（证据、日志）先写临时文件、fsync、算哈希，再原子 rename；SQLite 只存 URI 与哈希。
- 启动时执行 reconciliation：对比数据库、工作区、Lease、进程与 artifact。
- V1 不引入消息队列、PostgreSQL、Temporal。

## 后果

- 单机限制被接受为 V1 边界（见 ADR-014）；未来扩展时通过仓储接口迁移。
- 事件序列（seq）作为 Web 事件游标与断线补发的依据（见 ADR-013）。
- 所有写操作必须可重放、可审计。
