from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from backend.adapter.standard_request import CLAUDE_CODE_OPENAI_PROFILE, StandardRequest
from backend.core.config import settings
from backend.core.request_logging import update_request_context
from backend.runtime.stream_metrics import StreamMetrics
from backend.services import tool_parser
from backend.services.token_calc import calculate_execution_usage
from backend.toolcall.normalize import normalize_tool_name
from backend.toolcall.parser import mask_ignored_tool_syntax_regions
from backend.toolcall.stream_state import StreamingToolCallState


# Qwen 偶尔生成的毒性"工具不存在"或"无法继续"幻觉。
# 在流式收到前 20 字时识别，触发早期拦截 + retry 而不是流给客户端。
_TOXIC_REFUSAL_RE = re.compile(
    # 英文：工具不存在/不可用
    r"Tool\s+\S+\s+(?:does\s+not\s+exists?|is\s+not\s+(?:available|registered))"
    r"|I\s+cannot\s+execute\s+this\s+tool"
    # 英文：任务放弃/拒绝继续
    r"|I[''\u2019]?\s*m\s+sorry[,. ]"
    r"|I\s+cannot\s+(?:help|assist|proceed|continue|support|perform)"
    r"|I[''\u2019]?m\s+not\s+(?:able|designed)\s+to"
    r"|unable\s+to\s+(?:proceed|continue|perform|complete)"
    # 中文：工具/操作不存在或无法继续
    r"|该工具.{0,8}?不存在|工具.{0,12}?不存在"
    r"|我(?:无法|不能|不可以)(?:继续|进行|支持|完成|操作|执行)"
    r"|无法(?:进行|支持|完成|执行).{0,10}?操作"
    r"|抱歉.{0,20}?(?:无法|不能|不支持)",
    re.IGNORECASE,
)


log = logging.getLogger("qwen2api.runtime")


@dataclass(slots=True)
class RuntimeAttemptState:
    answer_text: str = ""
    reasoning_text: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    blocked_tool_names: list[str] = field(default_factory=list)
    finish_reason: str = "stop"
    raw_events: list[dict[str, Any]] = field(default_factory=list)
    emitted_visible_output: bool = False
    stage_metrics: dict[str, float] = field(default_factory=dict)


@dataclass(slots=True)
class RuntimeExecutionResult:
    state: RuntimeAttemptState
    chat_id: str | None
    acc: Any | None


@dataclass(slots=True)
class RuntimeToolDirective:
    tool_blocks: list[dict[str, Any]] = field(default_factory=list)
    stop_reason: str = "end_turn"


@dataclass(slots=True)
class RuntimeRetryDirective:
    retry: bool
    next_prompt: str
    reason: str | None = None


@dataclass(slots=True)
class RuntimeRetryContinuation:
    should_continue: bool
    next_prompt: str


@dataclass(slots=True)
class RuntimeRetryLoop:
    prompt: str
    max_attempts: int


@dataclass(slots=True)
class RuntimeAttemptPlan:
    loop: RuntimeRetryLoop
    prompt: str


@dataclass(slots=True)
class AnthropicStreamCompletionResult:
    chunks: list[str]


@dataclass(slots=True)
class AnthropicStreamSuccessResult:
    chunks: list[str]
    usage_delta: int


@dataclass(slots=True)
class RuntimeAttemptOutcome:
    execution: RuntimeExecutionResult
    continuation: RuntimeRetryContinuation


@dataclass(slots=True)
class RuntimeAttemptCursor:
    index: int
    number: int


TRAILING_IDLE_AFTER_TOOL_SECONDS = 2.0


__all__ = [
    "RuntimeAttemptState",
    "RuntimeExecutionResult",
    "RuntimeToolDirective",
    "RuntimeRetryDirective",
    "RuntimeRetryContinuation",
    "RuntimeRetryLoop",
    "RuntimeAttemptPlan",
    "AnthropicStreamCompletionResult",
    "AnthropicStreamSuccessResult",
    "RuntimeAttemptOutcome",
    "RuntimeAttemptCursor",
    "anthropic_stream_stop_reason",
    "anthropic_stream_usage_delta",
    "build_retry_loop",
    "build_tool_directive",
    "build_usage_delta_factory",
    "begin_runtime_attempt",
    "cleanup_runtime_resources",
    "collect_completion_run",
    "collect_completion_run_with_recovery",
    "continue_after_retry_directive",
    "evaluate_retry_directive",
    "extract_blocked_tool_names",
    "finalize_anthropic_stream_success",
    "complete_anthropic_stream_success",
    "has_recent_search_no_results",
    "has_recent_unchanged_read_result",
    "inject_assistant_message",
    "native_tool_calls_to_markup",
    "parse_tool_directive_once",
