# ADR-015：Runner 恢复语义与能力声明

- 状态：Accepted
- 日期：2026-08-14

## 上下文

修复指引指出：OpenHands Adapter（固定 SDK 1.42.1）声明 `supports_checkpoint=False`、`supports_resume=False`，但实施报告与运行时能力页表述不一致；`reconcile` 曾为无法观察的会话伪造 handle，导致假阳性“存活”。

## 决策

1. **恢复语义以 Runner 能力声明为准**：
   - `supports_resume=False` 的 Runner（OpenHands、Command）不参与同 Run 恢复编排；失败后的恢复一律走新 Run / 新 Attempt（`BeginNextAttempt`），这本身就是支持的路径。
   - 未来若某个 Runner 支持恢复：`checkpoint()` 必须落盘并写入 `runs.checkpoint_ref`；`reconcile` 仅在存在有效 checkpoint 且 `supports_resume=True` 时尝试重挂。
2. **reconcile 不伪造 handle**：
   - 仅有 pid 的 Runner（Command）可在进程存活期内按 pid 重挂观察。
   - 无 pid 会话（如 OpenHands SDK 线程）在 Daemon 重启后一律判 `CRASHED`，stop_reason=`reconciliation: runner session cannot be re-attached`，并写 `RECONCILE_NO_REATTACH` 事件；不得出现“无 context 但被当作可观察”的 handle。
3. **能力页一致**：Daemon 启动时把 Registry 的能力写入 settings（`runner_capabilities`），Web `/api/system/runners` 与 CLI `boxporter-v2 runners` 展示同一来源；报告/文档不得承诺未声明的能力。
4. **tick 观察闭环**：终态观察驱动状态推进（SUCCEEDED executor → 冻结提交；SUCCEEDED reviewer → 记录结论；CRASHED/TIMED_OUT → FailRun），handle 在终态后清理；观察不到的 run 不进闭环。

## 后果

- 重启后 OpenHands 活跃 run 会被安全判失败并走预算内恢复（新 Attempt），而不是假装存活。
- 报告、ADN 与运行时能力页单一来源，不再过度承诺。
- 新增回归测试：无 handle 不得伪造恢复；终态观察推进状态机。
