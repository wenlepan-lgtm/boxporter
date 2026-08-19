# ADR-010：Runner 固定版本与 Adapter 契约测试

- 状态：Accepted
- 日期：2026-08-14

## 上下文

OpenHands 与 DeepSeek Harness（官方标注 Developer Preview）等上游变化快。跟随 master/latest 升级会让生产配置随时被破坏。

## 决策

- 所有 Runner 固定 tag/版本/commit SHA，不跟随 latest。
- 升级流程：更新 Adapter → 跑 Adapter Contract Tests → 通过后才可切换生产配置。
- 契约测试覆盖：Session 创建/检查/停止/恢复、事件顺序与字段、Token 用量可读、工具调用与退出码可观察、Sandbox 权限符合 Profile、两 Session 不共享状态、Store 引用与哈希稳定、异常返回标准错误、浏览器断开不关 Session、Daemon 重启可重新关联或安全判恢复、fallback 不自动扩大权限。
- 生产配置与开发验证分离。

## 后果

- 破坏性上游变化只影响 Adapter，BoxPorter Core 测试无需修改。
- 自动升级只允许检查，不允许无人值守时无审核安装。
