# Runner 版本固定基线（Phase 0 记录，Phase 4 实施）

> 规则见 ADR-010：所有 Runner 固定 tag/版本/commit，不跟随 latest/master。
> 本文件每次升级需更新，并通过 Adapter Contract Tests 后才可切换生产配置。

## 主 Runner

| Runner | 固定版本 | 记录日期 | 说明 |
|---|---|---|---|
| OpenHands | `v1.13.0` | 2026-08-14 | 主执行器；Agent Server + SDK 模式 |
| Command Runner | BoxPorter 内置 | 2026-08-14 | 兼容 V0.2 `executor_command` / `reviewer_command` |

## 备用 Runner

| Runner | 固定版本 | 记录日期 | 说明 |
|---|---|---|---|
| DeepSeek Harness | commit `47f943859bef60e4160492346772ded9b24f765a` | 2026-08-14 | 官方无 release，标注 Developer Preview；实验性接入 |
| Codex CLI | 待定（Phase 4 前补齐） | — | 可选 Adapter |
| Claude Code | 待定（Phase 4 前补齐） | — | 可选 Adapter |
| GLM CLI | 待定（Phase 4 前补齐） | — | 可选 Adapter |

## 升级流程

1. 更新本表 + Adapter；
2. 跑 `tests/contract/` 全部契约测试；
3. 通过后在 `config/local.toml` 切换生产配置；
4. 记录升级 ADR 或变更日志。
