# ADR-005：Git Worktree 与 Submission Manifest

- 状态：Accepted
- 日期：2026-08-14

## 上下文

V0.2 的提交只绑定 result.md + verify.md 的哈希，无法证明代码未变化。审核必须是“对冻结快照的独立验证”。

## 决策

- 每个执行 Run 在独立 Git Worktree（或确认的安全工作区）中工作；审核 Run 使用独立的只读 Worktree。
- 提交时冻结 `BOXPORTER_SUBMISSION_V2` Manifest：绑定 base_commit、head_commit、git_tree_sha、git_diff_sha256、task/result/verify/artifact-manifest 各文件哈希、executor_run_id 与 session 引用。
- `submission_sha256 = SHA256(canonical_json(manifest_without_sha))`，canonical JSON 规则固定。
- 审核只针对冻结清单；审核期间受保护内容变化 → 原审核失效（INVALID_SUBMISSION）。
- PASSED 证据包可脱离 BoxPorter 离线重算全部哈希。

## 后果

- 代码变化无法静默逃过审核。
- Git 提供 Tree/Diff 内容身份，是审核最可靠的工程边界。
