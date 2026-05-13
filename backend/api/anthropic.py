from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
import asyncio
import json
import logging
import uuid

from backend.adapter.standard_request import StandardRequest
from backend.adapter.cli_proxy import CLIProxy
from backend.core.config import resolve_model, settings
from backend.core.request_logging import new_request_id, request_context, update_request_context
from backend.runtime import stream_presenter
from backend.runtime.execution import (
    build_tool_directive,
    cleanup_runtime_resources,
    collect_completion_run,
    collect_completion_run_with_recovery,
    evaluate_retry_directive,
    request_max_attempts,
)
from backend.services.auth_quota import resolve_auth_context
from backend.services.context_attachment_manager import prepare_context_attachments, derive_session_key
from backend.services.attachment_preprocessor import preprocess_attachments
from backend.services.prompt_builder import CLAUDE_CODE_OPENAI_PROFILE, messages_to_prompt
from backend.services.qwen_client import QwenClient
from backend.services.task_session import (
    build_anthropic_assistant_history_message,
    build_retry_rebase_prompt,
    clear_invalidated_session_chat,
    log_session_plan_reuse_cancelled,
    persist_session_turn,
    plan_persistent_session_turn,
)
from backend.services.token_calc import calculate_execution_usage, count_tokens, to_anthropic_usage
from backend.toolcall.normalize import build_tool_name_registry

log = logging.getLogger("qwen2api.anthropic")
router = APIRouter()


class _AnthropicStreamState:
    def __init__(self, *, msg_id: str, model_name: str, prompt: str):
        self.msg_id = msg_id
        self.model_name = model_name
        self.prompt = prompt
        self.pending_chunks: list[str] = []
        self.answer_text_buffer: list[tuple[int, str]] = []
        self.block_index = 0
        self.current_block: dict[str, object] = {"type": None, "index": None, "tool_call_id": None}
        self.opened_tool_calls: set[str] = set()

    def ensure_message_start(self) -> None:
        if not self.pending_chunks:
            self.pending_chunks.append(_message_start_event(self.msg_id, self.model_name, self.prompt, ""))

    def close_current_block(self) -> None:
        index = self.current_block.get("index")
        if index is None:
            return
        self.pending_chunks.append(stream_presenter.anthropic_content_block_stop(index))
        self.current_block = {"type": None, "index": None, "tool_call_id": None}

    def open_textual_block(self, block_type: str) -> int:
        current_type = self.current_block.get("type")
        current_index = self.current_block.get("index")
        if current_type == block_type and isinstance(current_index, int):
            return current_index
        self.close_current_block()
        index = self.block_index
        self.block_index += 1
        if block_type == "thinking":
            content_block = {"type": "thinking", "thinking": ""}
        else:
            content_block = {"type": "text", "text": ""}
        self.pending_chunks.append(stream_presenter.anthropic_content_block_start(index, content_block))
        self.current_block = {"type": block_type, "index": index, "tool_call_id": None}
        return index

    def open_tool_block(self, tool_call_id: str, tool_name: str) -> int:
        current_index = self.current_block.get("index")
        if (
            self.current_block.get("type") == "tool_use"
            and self.current_block.get("tool_call_id") == tool_call_id
            and isinstance(current_index, int)
        ):
            return current_index
        self.close_current_block()
        index = self.block_index
        self.block_index += 1
        self.pending_chunks.append(
            f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': index, 'content_block': {'type': 'tool_use', 'id': tool_call_id, 'name': tool_name, 'input': {}}}, ensure_ascii=False)}\n\n"
        )
        self.current_block = {"type": "tool_use", "index": index, "tool_call_id": tool_call_id}
        self.opened_tool_calls.add(tool_call_id)
        return index

    def append_thinking_delta(self, text_chunk: str) -> None:
        index = self.open_textual_block("thinking")
        self.pending_chunks.append(
            stream_presenter.anthropic_content_block_delta(index, {"type": "thinking_delta", "thinking": text_chunk})
        )

    def buffer_answer_text(self, text_chunk: str) -> None:
        index = self.open_textual_block("text")
        self.answer_text_buffer.append((index, text_chunk))

    def append_tool_delta(self, *, tool_call_id: str, tool_name: str, partial_json: str) -> None:
        index = self.open_tool_block(tool_call_id, tool_name)
        if partial_json:
            self.pending_chunks.append(
                stream_presenter.anthropic_content_block_delta(index, {"type": "input_json_delta", "partial_json": partial_json})
            )

    def flush_answer_text(self) -> None:
        if not self.answer_text_buffer:
            return
        for index, text_chunk in self.answer_text_buffer:
            self.pending_chunks.append(
                stream_presenter.anthropic_content_block_delta(index, {"type": "text_delta", "text": text_chunk})
            )
        self.answer_text_buffer = []

    def clear_answer_text(self) -> None:
        self.answer_text_buffer = []


def _build_standard_request(req_data: dict) -> StandardRequest:
    """使用 CLIProxy 进行协议转换"""
    standard_request = CLIProxy.from_anthropic(req_data, client_profile=CLAUDE_CODE_OPENAI_PROFILE)
    CLIProxy.log_conversion("anthropic", standard_request.response_model, len(standard_request.prompt), len(standard_request.tools))
    return standard_request


def _anthropic_usage(prompt: str, answer_text: str) -> dict[str, int]:
    usage = calculate_execution_usage(
        prompt,
        type("Execution", (), {"state": type("State", (), {"answer_text": answer_text, "reasoning_text": "", "tool_calls": []})()})(),
    )
    return to_anthropic_usage(usage)


def _message_start_event(msg_id: str, model_name: str, prompt: str, answer_text: str) -> str:
    return stream_presenter.anthropic_message_start(msg_id, model_name, _anthropic_usage(prompt, answer_text))


async def _run_anthropic_attempt(
    *,
    client: QwenClient,
    standard_request: StandardRequest,
    current_prompt: str,
    history_messages: list[dict],
    stream_attempt: int,
    max_attempts: int,
):
    update_request_context(stream_attempt=stream_attempt + 1)
    execution = await collect_completion_run(client, standard_request, current_prompt)
    retry = evaluate_retry_directive(
        request=standard_request,
        current_prompt=current_prompt,
        history_messages=history_messages,
        attempt_index=stream_attempt,
        max_attempts=max_attempts,
        state=execution.state,
        allow_after_visible_output=True,
    )
    return execution, retry


def _visible_answer_text_length(*, directive, execution, stream_state: _AnthropicStreamState | None = None) -> int:
    if directive.stop_reason == "tool_use":
        return 0
    if stream_state is not None:
        return sum(len(text_chunk) for _, text_chunk in stream_state.answer_text_buffer)
    return len(execution.state.answer_text)


async def _add_used_tokens_for_prompt(*, users_db, token: str, total_tokens: int) -> None:
    users = await users_db.get()
    for user in users:
        if user["id"] == token:
            user["used_tokens"] += total_tokens
            break
    await users_db.save(users)


async def _reacquire_bound_account_if_needed(*, client: QwenClient, standard_request: StandardRequest) -> None:
    preferred_email = getattr(standard_request, "bound_account_email", None)
    if preferred_email:
        standard_request.bound_account = await client.account_pool.acquire_wait_preferred(preferred_email, timeout=60)
    else:
        standard_request.bound_account = None


@router.post("/messages/count_tokens")
@router.post("/v1/messages/count_tokens")
@router.post("/anthropic/v1/messages/count_tokens")
async def anthropic_count_tokens(request: Request):
    try:
        req_data = await request.json()
    except Exception:
