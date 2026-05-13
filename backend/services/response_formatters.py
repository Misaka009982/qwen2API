from __future__ import annotations

import json
import uuid
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


def build_openai_response_payload(*, response_id: str, created: int, model_name: str, prompt: str, execution, standard_request) -> dict[str, Any]:
    directive = build_tool_directive(standard_request, execution.state)
    usage = calculate_execution_usage(prompt, execution)

    output: list[dict[str, Any]] = []
    output_text = execution.state.answer_text or ""

    if output_text:
        output.append({
            "id": f"msg_{uuid.uuid4().hex[:24]}",
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [{
                "type": "output_text",
                "text": output_text,
                "annotations": [],
            }],
        })

    tool_blocks = [block for block in directive.tool_blocks if block.get("type") == "tool_use"]
    for block in tool_blocks:
        output.append({
            "id": block["id"],
            "type": "function_call",
            "call_id": block["id"],
            "name": block["name"],
            "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
            "status": "completed",
        })

    if not output:
        output.append({
            "id": f"msg_{uuid.uuid4().hex[:24]}",
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [{
                "type": "output_text",
                "text": "",
                "annotations": [],
            }],
        })

    return {
        "id": response_id,
        "object": "response",
        "created_at": created,
        "status": "completed",
        "error": None,
        "incomplete_details": None,
        "model": model_name,
        "output": output,
        "output_text": output_text,
        "parallel_tool_calls": len(tool_blocks) > 1,
        "usage": {
            "input_tokens": usage["prompt_tokens"],
            "output_tokens": usage["completion_tokens"],
            "total_tokens": usage["total_tokens"],
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        },
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
