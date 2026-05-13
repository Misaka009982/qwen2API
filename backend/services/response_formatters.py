from __future__ import annotations

import json
from typing import Any

from backend.runtime.execution import build_tool_directive
from backend.services.token_calc import calculate_execution_usage, to_anthropic_usage, to_gemini_usage_metadata, to_openai_usage


def build_openai_completion_payload(*, completion_id: str, created: int, model_name: str, prompt: str, execution, standard_request) -> dict[str, Any]:
    directive = build_tool_directive(standard_request, execution.state)
    usage = calculate_execution_usage(prompt, execution)
    if directive.stop_reason == "tool_use":
        oai_tool_calls = [
            {
                "id": block["id"],
                "type": "function",
                "function": {
                    "name": block["name"],
                    "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                },
            }
            for block in directive.tool_blocks
            if block.get("type") == "tool_use"
        ]
        msg: dict[str, Any] = {"role": "assistant", "content": None, "tool_calls": oai_tool_calls}
        finish_reason = "tool_calls"
    else:
        oai_tool_calls = []
        msg = {"role": "assistant", "content": execution.state.answer_text}
        finish_reason = "stop"

    log_payload = [
        {
            "id": call["id"],
            "name": call["function"]["name"],
            "arguments": call["function"]["arguments"],
        }
        for call in oai_tool_calls
    ]
    import logging
    logging.getLogger("qwen2api.chat").info(
        "[OAI] response finish_reason=%s tool_calls=%s text_preview=%r",
        finish_reason,
        log_payload,
        execution.state.answer_text[:300],
    )

    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model_name,
        "choices": [{"index": 0, "message": msg, "finish_reason": finish_reason}],
        "usage": to_openai_usage(usage),
    }


def build_anthropic_message_payload(*, msg_id: str, model_name: str, prompt: str, execution, standard_request) -> dict[str, Any]:
    directive = build_tool_directive(standard_request, execution.state)
    usage = calculate_execution_usage(prompt, execution)
    content_blocks: list[dict[str, Any]] = []
    if execution.state.reasoning_text:
        content_blocks.append({"type": "thinking", "thinking": execution.state.reasoning_text})
    content_blocks.extend(directive.tool_blocks)
    return {
        "id": msg_id,
        "type": "message",
        "role": "assistant",
        "model": model_name,
        "content": content_blocks,
        "stop_reason": directive.stop_reason,
        "stop_sequence": None,
        "usage": to_anthropic_usage(usage),
    }


def build_gemini_generate_payload(*, prompt: str, execution) -> dict[str, Any]:
    usage = calculate_execution_usage(prompt, execution)
    return {
        "candidates": [
            {
                "content": {
                    "parts": [{"text": execution.state.answer_text}],
                    "role": "model",
                }
            }
        ],
        "usageMetadata": to_gemini_usage_metadata(usage),
    }
