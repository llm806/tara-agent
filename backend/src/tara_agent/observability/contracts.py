"""Trace 节点的稳定类型和安全数据边界。"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum, StrEnum
from itertools import islice
from typing import Any
from uuid import UUID


class ObservationKind(StrEnum):
    """统一执行树允许记录的节点类型。"""

    WORKFLOW = "workflow"
    NODE = "node"
    LLM = "llm"
    TOOL = "tool"
    SERVICE = "service"
    DATA = "data"


class ObservationStatus(StrEnum):
    """Trace 节点的完整生命周期状态。"""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ObservationUpdate:
    """节点开始或结束时可以补充的结构化信息。"""

    input_data: dict[str, Any] | None = None
    output_data: dict[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None
    error_data: dict[str, Any] | None = None
    model_provider: str | None = None
    model_name: str | None = None
    model_parameters: dict[str, Any] | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    context_length: int | None = None
    tool_name: str | None = None
    filters: dict[str, Any] | None = None
    marker: str | None = None
    sample_count: int | None = None
    sample_ids: list[str] | None = None
    data_sources: list[dict[str, Any]] | None = None
    artifact_refs: list[dict[str, Any]] | None = None
    attributes: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class TraceDataLimits:
    """单个节点原始输入输出的持久化上限。"""

    max_depth: int = 8
    max_collection_items: int = 100
    max_string_characters: int = 20_000
    max_total_values: int = 2_000
    max_total_characters: int = 100_000


DEFAULT_TRACE_DATA_LIMITS = TraceDataLimits()
REDACTED_VALUE = "[REDACTED]"

_SENSITIVE_KEYS = {
    "accesstoken",
    "apikey",
    "authorization",
    "clientsecret",
    "connectionstring",
    "cookie",
    "databaseurl",
    "idtoken",
    "password",
    "passwd",
    "proxyauthorization",
    "refreshtoken",
    "secret",
    "setcookie",
    "token",
}
_BEARER_PATTERN = re.compile(r"(?i)\bbearer\s+[^\s,;]+")
_CREDENTIAL_URL_PATTERN = re.compile(
    r"(?i)([a-z][a-z0-9+.-]*://)([^\s/:@]+):([^\s/@]+)@"
)
_SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b(api[-_ ]?key|authorization|client[-_ ]?secret|password|passwd|"
    r"access[-_ ]?token|refresh[-_ ]?token|secret)\b(\s*[:=]\s*)([^\s,;&]+)"
)


@dataclass(slots=True)
class _SanitizeState:
    remaining_values: int
    remaining_characters: int


def sanitize_trace_mapping(
    value: Mapping[str, Any] | None,
    *,
    limits: TraceDataLimits = DEFAULT_TRACE_DATA_LIMITS,
) -> dict[str, Any] | None:
    """清理将写入 Trace JSON 字段的数据，并保持顶层对象结构。"""

    if value is None:
        return None
    state = _SanitizeState(
        remaining_values=limits.max_total_values,
        remaining_characters=limits.max_total_characters,
    )
    sanitized = _sanitize_value(value, depth=0, state=state, limits=limits)
    if isinstance(sanitized, dict):
        return sanitized
    return {"value": sanitized}


def sanitize_trace_text(value: str, *, max_characters: int = 4_000) -> str:
    """清理错误等自由文本中的常见凭据，并限制持久化长度。"""

    redacted = _redact_text(value)
    if len(redacted) <= max_characters:
        return redacted
    omitted = len(redacted) - max_characters
    return f"{redacted[:max_characters]}\n[TRUNCATED {omitted} CHARACTERS]"


def _sanitize_value(
    value: Any,
    *,
    depth: int,
    state: _SanitizeState,
    limits: TraceDataLimits,
) -> Any:
    if state.remaining_values <= 0:
        return "[TRUNCATED VALUE LIMIT]"
    state.remaining_values -= 1

    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, str):
        return _bounded_text(value, state=state, limits=limits)
    if isinstance(value, bytes):
        return f"[BINARY DATA OMITTED {len(value)} BYTES]"
    if isinstance(value, UUID | date | datetime):
        return str(value)
    if isinstance(value, Enum):
        return _sanitize_value(value.value, depth=depth, state=state, limits=limits)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _sanitize_value(
            model_dump(mode="json"),
            depth=depth,
            state=state,
            limits=limits,
        )
    if depth >= limits.max_depth:
        return "[TRUNCATED DEPTH LIMIT]"
    if isinstance(value, Mapping):
        return _sanitize_mapping(value, depth=depth, state=state, limits=limits)
    if isinstance(value, Sequence):
        return _sanitize_sequence(value, depth=depth, state=state, limits=limits)
    return _bounded_text(str(value), state=state, limits=limits)


def _sanitize_mapping(
    value: Mapping[Any, Any],
    *,
    depth: int,
    state: _SanitizeState,
    limits: TraceDataLimits,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for raw_key, item in islice(value.items(), limits.max_collection_items):
        key = str(raw_key)[:256]
        if _is_sensitive_key(key):
            result[key] = REDACTED_VALUE
            continue
        result[key] = _sanitize_value(
            item,
            depth=depth + 1,
            state=state,
            limits=limits,
        )
    omitted = len(value) - limits.max_collection_items
    if omitted > 0:
        result[_notice_key(result)] = {"omitted_items": omitted}
    return result


def _sanitize_sequence(
    value: Sequence[Any],
    *,
    depth: int,
    state: _SanitizeState,
    limits: TraceDataLimits,
) -> list[Any]:
    result = [
        _sanitize_value(item, depth=depth + 1, state=state, limits=limits)
        for item in value[: limits.max_collection_items]
    ]
    omitted = len(value) - limits.max_collection_items
    if omitted > 0:
        result.append({"_trace_truncated": {"omitted_items": omitted}})
    return result


def _bounded_text(
    value: str,
    *,
    state: _SanitizeState,
    limits: TraceDataLimits,
) -> str:
    redacted = _redact_text(value)
    allowed = min(limits.max_string_characters, max(0, state.remaining_characters))
    state.remaining_characters -= min(len(redacted), allowed)
    if len(redacted) <= allowed:
        return redacted
    omitted = len(redacted) - allowed
    return f"{redacted[:allowed]}\n[TRUNCATED {omitted} CHARACTERS]"


def _redact_text(value: str) -> str:
    value = _BEARER_PATTERN.sub("Bearer [REDACTED]", value)
    value = _CREDENTIAL_URL_PATTERN.sub(r"\1[REDACTED]@", value)
    return _SECRET_ASSIGNMENT_PATTERN.sub(r"\1\2[REDACTED]", value)


def _is_sensitive_key(value: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", value.lower())
    return normalized in _SENSITIVE_KEYS or normalized.endswith(
        ("apikey", "password", "secret", "token", "cookie")
    )


def _notice_key(value: Mapping[str, Any]) -> str:
    key = "_trace_truncated"
    while key in value:
        key = f"_{key}"
    return key
