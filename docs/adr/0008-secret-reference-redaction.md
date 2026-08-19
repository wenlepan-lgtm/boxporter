# ADR-008：Secret Reference 与脱敏策略

- 状态：Accepted
- 日期：2026-08-14

## 上下文

长 Session、终端输出与证据包可能泄露密码、API Key、Cookie、客户数据。Secret 不得进入 Markdown 任务、Git、日志或证据。

## 决策

- Secret 一律使用引用（`secret://boxporter/macmini/login`、`secret://project/deepseek/api-key`），由 Keychain/Secret Manager 解析；Agent 只获得当前 Run 所需的短期 Secret。
- 产物与 Session 进入保留库前执行脱敏扫描（密码、Key、Token、私钥、Cookie、Session Secret、个人信息、环境变量泄露）；发现即阻止封箱、撤销 Secret、生成安全事件。
- Context Pack 与证据包只允许 Secret Reference 与哈希。

## 后果

- 泄密风险从“依赖 Agent 自觉”变为结构性检查。
- 需要维护 Secret Reference 解析器与脱敏规则库。
