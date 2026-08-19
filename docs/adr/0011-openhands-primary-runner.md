# ADR-011：OpenHands 作为 V1 主 Runner

- 状态：Accepted
- 日期：2026-08-14

## 上下文

V1 需要一个面向软件工程的主执行运行时。DeepSeek Harness 官方仍标注 Developer Preview，存在破坏性兼容风险。

## 决策

- OpenHands Software Agent SDK / Agent Server 为 V1 主执行器（本地/容器工作区、HTTP API、WebSocket 事件、独立 Session）。
- DeepSeek Harness、Codex、Claude、GLM 通过同一 RunnerAdapter 契约作为可替换备用 Runner。
- BoxPorter Core 保持供应商无关；OpenHands 为主执行器不代表 Core 可依赖其私有业务对象。
- Adapter 优先级：OpenHands → Command（兼容 V0.2 executor_command/reviewer_command）→ DeepSeek Harness（实验）→ 其他。

## 后果

- 执行底座风险集中到一个成熟 Runtime，同时保留切换自由。
- 主 Runner 不可用时按任务允许的 fallback 链切换，不自动扩大权限。
