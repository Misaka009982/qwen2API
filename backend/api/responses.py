from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
import json
import logging
import time
import uuid
from typing import Any

from backend.core.request_logging import new_request_id, request_context, update_request_context
from backend.services.attachment_preprocessor import preprocess_attachments
from backend.services.auth_quota import resolve_auth_context
from backend.services.completion_bridge import run_retryable_completion_bridge
from backend.services.context_attachment_manager import derive_session_key, prepare_context_attachments
from backend.services.openai_stream_translator import OpenAIStreamTranslator
from backend.services.prompt_builder import CLAUDE_CODE_OPENAI_PROFILE, OPENCLAW_OPENAI_PROFILE
from backend.services.response_formatters import build_openai_response_payload
from backend.services.standard_request_builder import build_chat_standard_request
from backend.services.task_session import (
    build_openai_assistant_history_message,
    clear_invalidated_session_chat,
    log_session_plan_reuse_cancelled,
    persist_session_turn,
    plan_persistent_session_turn,
)
from backend.runtime.execution import RuntimeAttemptState, build_tool_directive, build_usage_delta_factory, request_max_attempts
from backend.services.qwen_client import QwenClient

log = logging.getLogger("qwen2api.responses")
router = APIRouter()


def _detect_openai_client_profile(request: Request, req_data: dict) -> str:
    del req_data
    if request.headers.get("x-anthropic-billing-header"):
        return CLAUDE_CODE_OPENAI_PROFILE
    return OPENCLAW_OPENAI_PROFILE


def _normalize_responses_input(input_value: Any) -> list[dict[str, Any]]:
    if isinstance(input_value, str):
        return [{"role": "user", "content": input_value}]

    if isinstance(input_value, list):
        messages: list[dict[str, Any]] = []
        for item in input_value:
            if isinstance(item, str):
                messages.append({"role": "user", "content": item})
                continue
            if not isinstance(item, dict):
                continue

            role = item.get("role") or item.get("type") or "user"
            content = item.get("content")
            if content is None and isinstance(item.get("text"), str):
                content = item.get("text")

            if isinstance(content, list):
                normalized_parts: list[dict[str, Any]] = []
                for part in content:
                    if isinstance(part, str):
                        normalized_parts.append({"type": "text", "text": part})
                        continue
                    if not isinstance(part, dict):
                        continue
                    part_type = part.get("type")
                    if part_type in {"input_text", "output_text", "text"}:
                        normalized_parts.append({"type": "text", "text": part.get("text", "")})
                    elif part_type in {"input_image", "image_url"}:
                        if part_type == "image_url":
                            normalized_parts.append({"type": "image_url", "image_url": part.get("image_url", part)})
                        else:
                            normalized_parts.append(part)
                    elif part_type in {"input_file", "file"}:
                        normalized_parts.append(part)
                messages.append({"role": role, "content": normalized_parts or ""})
                continue

            messages.append({"role": role, "content": content if content is not None else ""})

        return messages or [{"role": "user", "content": ""}]

    if isinstance(input_value, dict):
        role = input_value.get("role", "user")
        content = input_value.get("content")
        if content is None and isinstance(input_value.get("text"), str):
            content = input_value["text"]
        return [{"role": role, "content": content if content is not None else ""}]

    return [{"role": "user", "content": ""}]


def _normalize_responses_tools(tools: Any) -> list[dict[str, Any]]:
    if not isinstance(tools, list):
        return []

    normalized = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") == "function":
            normalized.append(tool)
            continue
        if tool.get("name"):
            normalized.append({
                "type": "function",
                "function": {
                    "name": tool.get("name", ""),
                    "description": tool.get("description", ""),
                    "parameters": tool.get("parameters") or tool.get("input_schema") or {},
                },
            })
    return normalized


def _responses_to_chat_request(req_data: dict[str, Any]) -> dict[str, Any]:
    messages = []
    instructions = req_data.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        messages.append({"role": "system", "content": instructions})
    messages.extend(_normalize_responses_input(req_data.get("input", "")))

    chat_req = {
        "model": req_data.get("model", "gpt-4o-mini"),
        "messages": messages,
        "tools": _normalize_responses_tools(req_data.get("tools", [])),
        "stream": bool(req_data.get("stream", False)),
    }

    if isinstance(req_data.get("metadata"), dict):
        chat_req["metadata"] = req_data["metadata"]
    if req_data.get("conversation_id"):
        chat_req["conversation_id"] = req_data.get("conversation_id")
    if req_data.get("session_key"):
        chat_req["session_key"] = req_data.get("session_key")
    if isinstance(req_data.get("stream_options"), dict):
        chat_req["stream_options"] = req_data["stream_options"]
    if req_data.get("max_output_tokens") is not None:
        chat_req["max_tokens"] = req_data.get("max_output_tokens")

    return chat_req


@router.post("/v1/responses")
async def responses_create(request: Request):
    app = request.app
    users_db = app.state.users_db
    client: QwenClient = app.state.qwen_client

    auth = await resolve_auth_context(request, users_db)
    token = auth.token

    try:
        req_data = await request.json()
    except Exception:
        raise HTTPException(400, {"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}})

    chat_req_data = _responses_to_chat_request(req_data)
    client_profile = _detect_openai_client_profile(request, chat_req_data)
    session_key = derive_session_key("openai", token, chat_req_data)
    original_history_messages = chat_req_data.get("messages", [])

    file_store = getattr(app.state, "file_store", None)
    preprocessed = None
    if file_store is not None:
        preprocessed = await preprocess_attachments(chat_req_data, file_store, owner_token=token)
        chat_req_data = preprocessed.payload

    context_prepared = await prepare_context_attachments(
        app=app,
        payload=chat_req_data,
        surface="openai",
        auth_token=token,
        client_profile=client_profile,
        existing_attachments=(preprocessed.attachments if preprocessed is not None else None),
    )
    chat_req_data = context_prepared["payload"]

    standard_request = build_chat_standard_request(
        chat_req_data,
        default_model="gpt-4o-mini",
        surface="openai",
        client_profile=client_profile,
    )
    if preprocessed is not None:
        standard_request.attachments = preprocessed.attachments
        standard_request.uploaded_file_ids = preprocessed.uploaded_file_ids
    standard_request.upstream_files = context_prepared["upstream_files"]
    standard_request.session_key = context_prepared["session_key"]
    standard_request.context_mode = context_prepared["context_mode"]
    standard_request.bound_account_email = context_prepared["bound_account_email"]
    standard_request.bound_account = context_prepared["bound_account"]

    session_plan = await plan_persistent_session_turn(app=app, request=standard_request, payload=chat_req_data, surface="openai")
    if session_plan.enabled:
        standard_request.persistent_session = True
        standard_request.full_prompt = session_plan.full_prompt
        standard_request.prompt = session_plan.prompt
        standard_request.session_message_hashes = session_plan.current_hashes
        standard_request.upstream_chat_id = session_plan.existing_chat_id if session_plan.reuse_chat else None
        if standard_request.bound_account is None and session_plan.account_email:
            standard_request.bound_account = await app.state.account_pool.acquire_wait_preferred(session_plan.account_email, timeout=60)
            if standard_request.bound_account is not None:
                standard_request.bound_account_email = standard_request.bound_account.email
        elif standard_request.bound_account is not None and not standard_request.bound_account_email:
            standard_request.bound_account_email = standard_request.bound_account.email
        if standard_request.upstream_chat_id and standard_request.bound_account is None:
            log_session_plan_reuse_cancelled(
                request=standard_request,
                planned_chat_id=session_plan.existing_chat_id,
                reason="missing_bound_account",
            )
            standard_request.upstream_chat_id = None
            standard_request.prompt = standard_request.full_prompt or standard_request.prompt

    model_name = standard_request.response_model
    qwen_model = standard_request.resolved_model
    prompt = standard_request.prompt
    history_messages = original_history_messages
    stream_options = chat_req_data.get("stream_options") if isinstance(chat_req_data.get("stream_options"), dict) else {}
    include_usage = bool(stream_options.get("include_usage"))
    response_id = f"resp_{uuid.uuid4().hex}"
    created = int(time.time())

    with request_context(req_id=new_request_id(), surface="openai_responses", requested_model=model_name, resolved_model=qwen_model):
        log.info(
            "[RESP] model=%s stream=%s tool_enabled=%s profile=%s tools=%s prompt_len=%s prompt_tail=%r",
            qwen_model,
            standard_request.stream,
            standard_request.tool_enabled,
            standard_request.client_profile,
            [t.get('name') for t in standard_request.tools],
            len(prompt),
            prompt[-500:],
        )

        if standard_request.stream:
            async def generate():
                async with app.state.session_locks.hold(session_key):
                    try:
                        update_request_context(stream_attempt=1)
                        translator = OpenAIStreamTranslator(
                            completion_id=response_id,
                            created=created,
                            model_name=model_name,
                            client_profile=standard_request.client_profile,
                            build_final_directive=lambda answer_text: build_tool_directive(
                                standard_request,
                                RuntimeAttemptState(answer_text=answer_text),
                            ),
                            allowed_tool_names=standard_request.tool_names,
                            include_usage=include_usage,
                        )

                        async def on_delta(evt: dict[str, Any], text_chunk: str | None, tool_calls: list[dict[str, Any]] | None) -> None:
                            translator.on_delta(evt, text_chunk, tool_calls)

                        result = await run_retryable_completion_bridge(
                            client=client,
                            standard_request=standard_request,
                            prompt=prompt,
                            users_db=users_db,
                            token=token,
                            history_messages=history_messages,
                            max_attempts=request_max_attempts(standard_request),
                            usage_delta_factory=build_usage_delta_factory(prompt),
                            allow_after_visible_output=True,
                            capture_events=False,
                            on_delta=on_delta,
                        )
                        execution = result.execution
                        directive = result.directive or build_tool_directive(standard_request, execution.state)
                        assistant_message = build_openai_assistant_history_message(
                            execution=execution,
                            request=standard_request,
                            directive=directive,
                        )
                        await persist_session_turn(
                            app=app,
                            request=standard_request,
                            surface="openai",
                            execution=execution,
                            assistant_message=assistant_message,
                        )
                        final_finish_reason = "tool_calls" if directive.stop_reason == "tool_use" else execution.state.finish_reason
                        for chunk in translator.finalize(final_finish_reason):
                            yield chunk
                        return
                    except HTTPException as he:
                        await clear_invalidated_session_chat(app=app, request=standard_request)
                        yield f"data: {json.dumps({'error': he.detail})}\n\n"
                        return
                    except Exception as e:
                        await clear_invalidated_session_chat(app=app, request=standard_request)
                        yield f"data: {json.dumps({'error': str(e)})}\n\n"
                        return

            return StreamingResponse(
                generate(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        try:
            async with app.state.session_locks.hold(session_key):
                update_request_context(stream_attempt=1)
                result = await run_retryable_completion_bridge(
                    client=client,
                    standard_request=standard_request,
                    prompt=prompt,
                    users_db=users_db,
                    token=token,
                    history_messages=history_messages,
                    max_attempts=request_max_attempts(standard_request),
                    usage_delta_factory=build_usage_delta_factory(prompt),
                    allow_after_visible_output=True,
                )
                execution = result.execution
                directive = result.directive or build_tool_directive(standard_request, execution.state)
                assistant_message = build_openai_assistant_history_message(
                    execution=execution,
                    request=standard_request,
                    directive=directive,
                )
                await persist_session_turn(
                    app=app,
                    request=standard_request,
                    surface="openai",
                    execution=execution,
                    assistant_message=assistant_message,
                )

                return JSONResponse(build_openai_response_payload(
                    response_id=response_id,
                    created=created,
                    model_name=model_name,
                    prompt=result.prompt,
                    execution=execution,
                    standard_request=standard_request,
                ))
        except Exception as e:
            await clear_invalidated_session_chat(app=app, request=standard_request)
            raise HTTPException(status_code=500, detail=str(e))
