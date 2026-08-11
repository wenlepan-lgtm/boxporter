# 搬运猴 BoxPorter

[English](README.en.md) | 中文

> 搬任务，不搬上下文。<br>
> Move tasks, not entire conversations.

BoxPorter 是一个轻量、可审计的编码 Agent 协作协议。它使用人能直接阅读的
Markdown“任务箱”，让执行 Agent 完成实现，让另一个 Agent 独立审核；只有审核
`PASS` 的任务才能进入完成箱。

它解决一个很朴素的问题：**不要为了确认“有没有新进展”，每隔几分钟重新调用一次
大模型并重新加载全部上下文。** 本地协调器先用零 Token 的确定性代码检查文件；只有
发生真正的交接、产生新提交或任务失去心跳时，才唤醒对应 Agent。

## 为什么是三个箱子

```text
pending/  ->  active/current.md  ->  passed/
                       |
                       +---------> blocked/
```

- `pending`：未开始的任务。
- `active/current.md`：唯一正在工作的任务，避免多人同时修改同一目标。
- `passed`：经过独立审核的不可变证据包（任务、结果、验证、双方报告和哈希清单）。
- `blocked`：需要凭据、设备、授权或外部状态变化的任务。

完整状态为：

```text
PENDING -> READY -> WORKING -> REVIEW_PENDING -> PASS
                       ^              |
                       +---- REVISE ---+
```

## 特点

- **人能看懂**：任务、结果、验证和审核都是 Markdown。
- **执行与审核分离**：执行者不能给自己的提交判 `PASS`。
- **内容寻址**：审核绑定 `result.md + verify.md` 的 SHA-256，提交后改证据会失效。
- **低 Token**：`tick` 没有状态变化时不会调用模型。
- **崩溃安全**：临时文件、`fsync` 和同文件系统原子移动避免半截任务。
- **供应商无关**：Claude Code、Codex、GLM、DeepSeek 或本地脚本都可作为执行者/审核者。
- **无运行依赖**：Python 标准库实现。

## 安装

```bash
git clone https://github.com/wenlepan-lgtm/boxporter.git
cd boxporter
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
```

## 五分钟体验

```bash
boxporter init
boxporter add \
  --id fix-login \
  --title "Fix login loop" \
  --body "Reproduce the redirect loop, fix the root cause, and add a regression test."
boxporter promote
boxporter status
```

执行 Agent 开始工作：

```bash
boxporter transition WORKING --handoff-to executor
```

执行完成后，把紧凑结论写入 `.boxporter/reports/result.md`，把真实命令及结果写入
`.boxporter/reports/verify.md`，然后封箱：

```bash
boxporter submit --author glm
```

独立审核者检查代码和证据后：

```bash
boxporter review \
  --result PASS \
  --author codex \
  --content "Root cause fixed and regression gate passed."
```

任务将原子移动到 `passed/`。如果审核不通过，使用 `--result REVISE`，任务会交回执行者。

完整的手工演示：

```bash
sh examples/manual-flow.sh
```

## 自动协调

`.boxporter/config.json` 可以配置两个命令数组：

```json
{
  "poll_seconds": 1200,
  "stale_seconds": 2400,
  "retry_seconds": 3600,
  "executor_command": [],
  "reviewer_command": []
}
```

命令数组为空时，BoxPorter 只报告需要人工交接，不调用任何模型。配置命令后，系统调度器
可以每 20 分钟执行一次：

```bash
boxporter tick
```

支持的参数占位符：`{root}`、`{workspace}`、`{task}`、`{executor_report}`、
`{reviewer_report}`。命令以参数数组启动，不经过 shell 拼接。

macOS 可用 `launchd`，Linux 可用 systemd timer 或 cron。20 分钟轮询是故障看门狗；
日常交接可以通过文件系统事件立即调用 `boxporter tick`。

## 设计边界

BoxPorter 不是 Agent 推理框架，也不是分布式消息队列。它面向一台机器或一个可信共享
文件系统上的小规模编码协作，重点是简单、低成本、能被人审计。

如果需要大量并行 Agent、跨机器投递、复杂权限或高吞吐，应使用正式消息队列、工作流
引擎或专门的 Agent orchestration 平台。

## 文档

- [协议与状态机](docs/PROTOCOL.md)
- [安全模型](docs/SECURITY.md)
- [示例任务](examples/demo-task.md)

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 项目来源

BoxPorter 起源于一次真实的 Claude/GLM 执行与 Codex 独立审核协作：用户提出用“未处理
箱、正在处理箱、已通过箱”代替反复复制长报告。实践证明，确定性文件协调可以显著减少
无变化轮询造成的上下文重复，同时保留人在环路中的可见性。

## License

MIT
