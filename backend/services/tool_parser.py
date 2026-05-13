import json
import logging
import re
import uuid
from typing import Any, cast

from backend.adapter.standard_request import CLAUDE_CODE_OPENAI_PROFILE, OPENCLAW_OPENAI_PROFILE
from backend.core.request_logging import get_request_context
from backend.services.tool_arg_fixer import fix_tool_call_arguments
from backend.services.tool_name_obfuscation import from_qwen_name
from backend.toolcall.normalize import build_tool_name_registry, normalize_tool_name
from backend.toolcall.parser import mask_ignored_tool_syntax_regions, parse_tool_calls_detailed

__all__ = ["parse_tool_calls", "parse_tool_calls_detailed", "inject_format_reminder", "parse_tool_calls_silent", "ToolSieve"]

log = logging.getLogger("qwen2api.tool_parser")


CASE_SENSITIVE_TOOL_NAMES = {"Bash", "Edit", "Write", "Read", "Grep", "Glob", "WebFetch", "WebSearch"}


def _normalize_tool_name_case(name: str, tool_names: set[str]) -> str:
    if not isinstance(name, str) or not name:
        return name
    if name in tool_names:
        return name
    lowered = name.lower()
    for candidate in tool_names:
        if candidate.lower() == lowered:
            if candidate in CASE_SENSITIVE_TOOL_NAMES:
                return candidate
            return candidate
    return name


def _find_tool_use_json(text: str, tool_names: set[str]):
    masked = mask_ignored_tool_syntax_regions(text)
    i = 0
    while i < len(masked):
        pos = masked.find('{', i)
        if pos == -1:
            break
        depth = 0
        for j in range(pos, len(masked)):
            if masked[j] == '{':
                depth += 1
            elif masked[j] == '}':
                depth -= 1
                if depth == 0:
                    candidate = text[pos:j + 1]
                    try:
                        obj = json.loads(candidate)
                        if isinstance(obj, dict) and obj.get("type") == "tool_use" and obj.get("name"):
                            normalized_name = normalize_tool_name(obj.get("name", ""), tool_names)
                            if normalized_name in tool_names:
                                obj = dict(obj)
                                obj["name"] = normalized_name
                                return pos, obj

                    except (json.JSONDecodeError, ValueError):
                        pass
                    break
        i = pos + 1

    return None


def _extract_first_xml_tool_call(text: str) -> str | None:
    masked = mask_ignored_tool_syntax_regions(text)
    wrapped_match = re.search(r"<tool_calls>\s*(<tool_call>[\s\S]*?</tool_call>)\s*</tool_calls>", masked, re.IGNORECASE)
    if wrapped_match:
        start, end = wrapped_match.span(1)
        return text[start:end]

    tool_call_match = re.search(r"<tool_call>\s*(\{[\s\S]*?\}|[\s\S]*?)\s*</tool_call>", masked, re.IGNORECASE)
    if tool_call_match:
        start, end = tool_call_match.span(0)
        return text[start:end]
    return None


def _extract_first_json_tool_call(text: str) -> str | None:
    normalized = text.strip()
    masked_normalized = mask_ignored_tool_syntax_regions(normalized)

    # 优先查找完整的 JSON 对象
    # markers 按优先级：Qwen 官方 tool_calls 外层包装 > 单对象 > 松散片段
    markers = [
        '<tool_call>{"name"',
        '<tool_calls><tool_call>{"name"',
        '{"tool_calls"',
        '{"name"',
        '"name":',
        '"name="',
        'function.name:',
    ]
    start_positions = [masked_normalized.find(marker) for marker in markers if masked_normalized.find(marker) != -1]
    if not start_positions:
        return None
    start = min(start_positions)
    candidate = normalized[start:]
    candidate_masked = masked_normalized[start:]

    wrapped_match = re.search(r"<tool_calls>\s*(<tool_call>[\s\S]*?</tool_call>)\s*</tool_calls>", candidate_masked, re.IGNORECASE)
    if wrapped_match:
        inner_start, inner_end = wrapped_match.span(1)
        return candidate[inner_start:inner_end]

    tool_call_match = re.search(r"<tool_call>\s*(\{[\s\S]*?\}|[\s\S]*?)\s*</tool_call>", candidate_masked, re.IGNORECASE)
    if tool_call_match:
        inner_start, inner_end = tool_call_match.span(0)
        return candidate[inner_start:inner_end]

    json_start = candidate_masked.find("{")
    if json_start == -1:
        return None
    depth = 0
    for idx in range(json_start, len(candidate)):
        ch = candidate[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                json_str = candidate[json_start:idx + 1]
                # 验证是否是有效的工具调用 JSON
                try:
                    obj = json.loads(json_str)
                    if isinstance(obj, dict) and "name" in obj:
                        return json_str
                except (json.JSONDecodeError, ValueError):
                    pass
                return json_str
    return candidate[json_start:]


def _normalize_fragmented_tool_call(answer: str) -> str:
    text = answer.strip()
    masked_text = mask_ignored_tool_syntax_regions(text)
    if "##TOOL_CALL##" in masked_text and "##END_CALL##" in masked_text:
        return text

    extracted_tool_call = _extract_first_xml_tool_call(text) or _extract_first_json_tool_call(text)
    if extracted_tool_call:
        return extracted_tool_call

    text = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"</?think>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"Tool\s+[A-Za-z0-9_.:-]*\s*does not exists?\\.?", "", text, flags=re.IGNORECASE)
