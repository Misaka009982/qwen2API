import json
import logging
from typing import Any

import tiktoken

log = logging.getLogger("qwen2api.token")

_ENCODING_CANDIDATES = ("o200k_base", "cl100k_base")
encoder = None
encoder_name = None

for candidate in _ENCODING_CANDIDATES:
    try:
        encoder = tiktoken.get_encoding(candidate)
        encoder_name = candidate
        break
    except Exception as exc:
        log.debug("Failed to load tiktoken encoding %s: %s", candidate, exc)

if encoder is None:
    log.warning("Failed to load tiktoken encodings: %s", ", ".join(_ENCODING_CANDIDATES))
else:
    log.info("Token calculator initialized with encoding=%s", encoder_name)


def count_tokens(text: str) -> int:
    """计算文本 token 数，优先使用更适合中英混合文本的编码。"""
    if not text:
        return 0
    if encoder:
        try:
            return len(encoder.encode(text))
        except Exception:
            pass
    return max(1, len(text.encode("utf-8")) // 2)


def _tool_calls_to_text(tool_calls: list[dict[str, Any]] | None) -> str:
    if not tool_calls:
        return ""
    lines: list[str] = []
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        payload = {
            "name": call.get("name", ""),
            "input": call.get("input", {}),
        }
        lines.append(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return "\n".join(line for line in lines if line)


def completion_to_token_text(
    completion: str,
    *,
    reasoning_text: str = "",
    tool_calls: list[dict[str, Any]] | None = None,
) -> str:
    parts = [reasoning_text, completion, _tool_calls_to_text(tool_calls)]
    return "\n".join(part for part in parts if part)


def calculate_usage(
    prompt: str,
    completion: str,
    *,
    reasoning_text: str = "",
    tool_calls: list[dict[str, Any]] | None = None,
) -> dict[str, int]:
    """统一计算 prompt/completion token，并覆盖 reasoning/tool_calls 场景。"""
    prompt_tokens = count_tokens(prompt)
    completion_tokens = count_tokens(
        completion_to_token_text(
            completion,
            reasoning_text=reasoning_text,
            tool_calls=tool_calls,
        )
    )
    total_tokens = prompt_tokens + completion_tokens
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }


def calculate_execution_usage(prompt: str, execution) -> dict[str, int]:
    return calculate_usage(
        prompt,
        execution.state.answer_text,
        reasoning_text=getattr(execution.state, "reasoning_text", ""),
        tool_calls=getattr(execution.state, "tool_calls", None),
    )


def to_openai_usage(usage: dict[str, int]) -> dict[str, int]:
    return {
        "prompt_tokens": usage["prompt_tokens"],
        "completion_tokens": usage["completion_tokens"],
        "total_tokens": usage["total_tokens"],
    }


def to_anthropic_usage(usage: dict[str, int]) -> dict[str, int]:
    return {
        "input_tokens": usage["prompt_tokens"],
        "output_tokens": usage["completion_tokens"],
    }


def to_gemini_usage_metadata(usage: dict[str, int]) -> dict[str, int]:
    return {
        "promptTokenCount": usage["prompt_tokens"],
        "candidatesTokenCount": usage["completion_tokens"],
        "totalTokenCount": usage["total_tokens"],
    }
