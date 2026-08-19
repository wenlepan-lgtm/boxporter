# ADR-006：Executor / Reviewer 身份、Session 与权限隔离

- 状态：Accepted
- 日期：2026-08-14

## 上下文

同一 Agent 修改代码后自判通过，不构成独立验收。审核必须由不同身份、不同 Session 对冻结提交独立验证。

## 决策

强制执行以下隔离，违反即拒绝进入 PASS：

```text
executor_run_id        != reviewer_run_id
executor_session_id    != reviewer_session_id
executor_identity      != reviewer_identity
executor_worktree      != reviewer_worktree
```

- Reviewer 对被审核代码只读，可在隔离环境运行测试，只允许写审核报告与新增审核证据。
- 不允许审核者修改被审核代码后 PASS。
- Reviewer 默认看不到 Executor 完整聊天（仅引用与摘要）。
- 执行者没有 PASS 权限，只能声明 SUBMITTED 或 BLOCKED_REQUESTED。

## 后果

- 串通或污染上下文无法通过协议层完成审核。
- 每个 Runner 的 Profile 必须映射到相同角色权限语义（见 ADR-001）。
