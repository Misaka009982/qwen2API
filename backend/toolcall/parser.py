from __future__ import annotations

import json
import re

from backend.toolcall.fallback_textkv import parse_textkv_format
from backend.toolcall.formats_json import parse_json_format
from backend.toolcall.formats_xml import parse_xml_format


def _mask_chars(text: str) -> str:
    return "".join("\n" if ch == "\n" else " " for ch in text)


def mask_ignored_tool_syntax_regions(text: str) -> str:
    """Mask markdown code fences / inline code so examples are never executed as tool calls.

    The returned string preserves original length and newlines so regex match positions remain usable
    against the original source text.
    """
    if not text:
        return text

    spans: list[tuple[int, int]] = []
    lines = text.splitlines(keepends=True)
    offset = 0
    fence_char = ""
    fence_len = 0
    fence_start = -1

    for line in lines:
        stripped = line.lstrip(" \t")
        indent = len(line) - len(stripped)
        if not fence_char:
            if stripped.startswith("```") or stripped.startswith("~~~"):
                char = stripped[0]
                run = 0
                while run < len(stripped) and stripped[run] == char:
                    run += 1
                if run >= 3:
                    fence_char = char
                    fence_len = run
                    fence_start = offset + indent
        else:
            if stripped.startswith(fence_char * fence_len):
                spans.append((fence_start, offset + len(line)))
                fence_char = ""
                fence_len = 0
                fence_start = -1
        offset += len(line)

    if fence_char and fence_start >= 0:
        spans.append((fence_start, len(text)))

    masked = text
    if spans:
        chars = list(masked)
        for start, end in spans:
            chars[start:end] = list(_mask_chars(masked[start:end]))
        masked = "".join(chars)

    chars = list(masked)
    i = 0
    while i < len(masked):
        if masked[i] != "`":
            i += 1
            continue
        run = 1
        while i + run < len(masked) and masked[i + run] == "`":
            run += 1
        close = masked.find("`" * run, i + run)
        if close == -1:
            i += run
            continue
        segment = masked[i:close + run]
        chars[i:close + run] = list(_mask_chars(segment))
        i = close + run
    return "".join(chars)


def _has_top_level_json_tool_syntax(text: str) -> bool:
    stripped = mask_ignored_tool_syntax_regions(text).strip()
    if stripped.startswith("```"):
        stripped = stripped.removeprefix("```json").removeprefix("```").strip()
        if stripped.endswith("```"):
            stripped = stripped[:-3].strip()

    if not stripped.startswith("{"):
        return False

    repaired = stripped.replace('"name="', '"name": "')
    if '"name=' in repaired:
        return True

    try:
        payload = json.loads(repaired)
    except (json.JSONDecodeError, TypeError, ValueError):
        return False

    if not isinstance(payload, dict):
        return False

    if isinstance(payload.get("tool_calls"), list):
        return True

    has_name = isinstance(payload.get("name"), str) and bool(payload.get("name"))
    has_args = any(key in payload for key in ("input", "arguments", "args", "parameters"))
    return has_name and has_args


def _has_xml_like_tool_syntax(text: str) -> bool:
    lowered = mask_ignored_tool_syntax_regions(text).lower()
    return any(marker in lowered for marker in ("<invoke", "<tool_call", "</tool_call>"))


def parse_tool_calls_detailed(text: str, allowed_names: set[str]) -> dict[str, object]:
    parse_text = mask_ignored_tool_syntax_regions(text)
    candidates = [
        ("json", parse_json_format(parse_text, allowed_names)),
        ("xml", parse_xml_format(parse_text, allowed_names)),
        ("textkv", parse_textkv_format(parse_text, allowed_names)),
    ]

    for source, calls in candidates:
        if calls:
            return {
                "calls": calls,
                "source": source,
                "saw_tool_syntax": True,
            }

    return {
        "calls": [],
        "source": None,
        "saw_tool_syntax": (
            _has_top_level_json_tool_syntax(text)
            or _has_xml_like_tool_syntax(text)
            or any(
                marker in mask_ignored_tool_syntax_regions(text)
                for marker in ("function.name:", "function.arguments:", '"name="')
            )
        ),
    }
