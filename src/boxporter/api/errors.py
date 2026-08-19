"""Unified API error model (remediation §3.1.1): every error is
``{code, field, message, hint, trace_id}`` — machine-checkable, with a
repair suggestion and a trace id matching the audit event."""

from __future__ import annotations

from dataclasses import dataclass

from boxporter.core.errors import (
    BoxPorterError,
    ConcurrencyError,
    IllegalTransitionError,
    NotFoundError,
    ValidationError,
)

FIELD_PREFIXES: tuple[tuple[str, str], ...] = (
    ("task id", "task_id"),
    ("project id", "project_id"),
    ("goal id", "goal_id"),
    ("task title", "title"),
    ("task objective", "objective"),
    ("priority", "priority"),
    ("risk_level", "risk_level"),
    ("workspace", "workspace"),
    ("max_attempts", "max_attempts"),
    ("timeout_seconds", "timeout_seconds"),
    ("token_budget", "token_budget"),
    ("acceptance_criteria", "acceptance_criteria"),
    ("required_evidence", "required_evidence"),
    ("dependency task", "dependencies"),
)


@dataclass(frozen=True)
class ApiError:
    code: str
    field: str | None
    message: str
    hint: str | None
    trace_id: str

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "field": self.field,
            "message": self.message,
            "hint": self.hint,
            "trace_id": self.trace_id,
        }


_MESSAGE_HINTS: tuple[tuple[str, str, str | None, str | None], ...] = (
    ("task already exists", "CONFLICT", "task_id",
     "task_id 已存在：换一个 task_id，或先取消/归档旧任务"),
    ("dependencies not satisfied", "PRECONDITION", "dependencies",
     "依赖任务必须 PASSED 并封箱后才能 READY"),
    ("workspace does not exist", "PRECONDITION", "workspace",
     "请先在磁盘创建该工作目录，或修正 spec.workspace"),
    ("dependency task not found", "VALIDATION", "dependencies",
     "依赖引用了不存在的 task_id，请先创建依赖任务"),
    ("illegal task transition", "STATE_CONFLICT", "state",
     "当前状态下不允许该操作：刷新任务详情获取最新状态"),
    ("not allowed to run", "FORBIDDEN", None,
     "当前身份无权执行该操作"),
    ("max attempts", "PRECONDITION", "max_attempts",
     "已达最大尝试次数；如需继续请提高 max_attempts 或新建任务"),
    ("recovery budget", "PRECONDITION", None,
     "自动恢复预算已耗尽，任务已阻塞，需要人工处理"),
    ("budget", "BUDGET", None,
     "Token/费用预算耗尽；如需继续请提高预算或拆分任务"),
    ("secret scan", "SECURITY", None,
     "证据中发现疑似凭据，已阻断封箱；请清理后重试"),
)


def map_error(exc: BoxPorterError, trace_id: str) -> ApiError:
    message = str(exc)
    for needle, code, field, hint in _MESSAGE_HINTS:
        if needle in message:
            return ApiError(code=code, field=field, message=message, hint=hint,
                            trace_id=trace_id)
    if isinstance(exc, ValidationError):
        code, hint = "VALIDATION", "按 BOXPORTER_TASK_V2 规范修正字段后重试"
    elif isinstance(exc, IllegalTransitionError):
        code, hint = "STATE_CONFLICT", "当前状态下不允许该操作"
    elif isinstance(exc, ConcurrencyError):
        code, hint = "CONFLICT", "任务已被其他操作更新，请重试"
    elif isinstance(exc, NotFoundError):
        code, hint = "NOT_FOUND", "检查 id 是否正确"
    else:
        code, hint = "COMMAND_FAILED", "查看服务端事件流定位根因"
    mapped_field: str | None = None
    for prefix, name in FIELD_PREFIXES:
        if message.lower().startswith(prefix):
            mapped_field = name
            break
    return ApiError(code=code, field=mapped_field, message=message, hint=hint, trace_id=trace_id)
