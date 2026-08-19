# ADR-007：完整 Session 不进入 PASSED

- 状态：Accepted
- 日期：2026-08-14

## 上下文

把完整对话复制给审核者或证据包会重复消耗 Token、锚定审核思路、扩散机密与无关日志。规划书原则：“搬任务，不搬完整上下文”。

## 决策

- 完整消息、工具调用与 Runtime 事件留在各 Runtime 的 Session Store。
- PASSED 证据包中的 `trajectory.ref.json` 只保存：session_id、runner/provider/model、trajectory hash、存储位置、保留期限、脱敏状态与审计引用。
- 交接（Context Pack）只含完成下一阶段所需信息：目标、约束、输入引用、快照、验收标准、证据与摘要。

## 后果

- 审核者独立而不被锚定；证据包体积与泄露面大幅缩小。
- Session 保留期与脱敏由策略配置（见 ADR-008）。
