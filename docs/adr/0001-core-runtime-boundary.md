# ADR-001：BoxPorter Core 与 Runner Runtime 的职责边界

- 状态：Accepted
- 日期：2026-08-14

## 上下文

OpenHands、DeepSeek Harness、Codex、Claude、GLM 等 Runtime 已提供模型调用、工具、Agent Loop、Session 与 Sandbox。BoxPorter 若重写这些能力，将把有限资源消耗在通用地基上，且上游仍在快速变化。

## 决策

- BoxPorter Core（Porter 控制平面）只负责：任务协议、状态机、调度、Lease、预算、恢复、提交、审核、证据与四箱 UI。
- Runner Runtime 负责：模型、工具、Agent Loop、Session、Sandbox/Workspace、原始事件与轨迹。
- 一切 Runner 接入必须通过 `RunnerAdapter` 接口（见规划书 §4.2），Adapter 返回机器可读状态，不得只返回终端文本。
- BoxPorter 不 Fork 任何上游项目，不依赖任何 Runtime 的私有业务对象。

## 后果

- 上游 API 变化只影响 Adapter；Core 测试不因 Runner 升级而修改。
- Adapter 契约测试（`tests/contract/`）成为 Runner 升级的门禁。
- 能力不足的 Runner 必须显式返回“不支持”，不得静默降低隔离要求。
