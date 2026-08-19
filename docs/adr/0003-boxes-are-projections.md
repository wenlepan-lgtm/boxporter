# ADR-003：四箱是投影，细粒度状态属于状态机

- 状态：Accepted
- 日期：2026-08-14

## 上下文

V0.2 用物理目录（pending/active/blocked/passed）表达任务阶段，导致 `REVIEW_PENDING`、`REVISE` 等状态无法表达，或被迫加新目录。规划书明确：`REVIEW_PENDING` 与 `REVISE` 是 ACTIVE 内部状态，不应创建第五、第六个箱子。

## 决策

- 细粒度状态（PENDING/READY/WORKING/REVIEW_PENDING/REVISE/BLOCKED/FAILED/PASS/DONE/CANCELED）由状态机与数据库维护。
- 四箱（PENDING/ACTIVE/BLOCKED/PASSED）是面向人的**投影**：
  - PENDING ← PENDING、READY
  - ACTIVE ← WORKING、REVIEW_PENDING、REVISE、FAILED（可恢复）
  - BLOCKED ← BLOCKED
  - PASSED ← PASS、DONE
  - CANCELED → 归档
- UI 拖动、目录移动都不能绕过状态机；所有迁移走合法迁移表。
- 兼容 V0.2：`boxes/` 目录投影在 V1 迁移期内保留读取兼容，写入以 SQLite 为真相。

## 后果

- 状态与展示解耦；新增细粒度状态不产生新箱子。
- 箱子数量恒定，便于 UI 与报告稳定。
