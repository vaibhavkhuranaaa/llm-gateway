"""Minimal HTTP adapters for simulator, OpenAI chat, and Anthropic messages."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from services.routing import Target


@dataclass(frozen=True)
class ProviderResult:
    body: dict[str, Any]
    cost_inputs: dict[str, int]


class ProviderFailure(Exception):
    def __init__(self, code: str, status: int, *, fallback_allowed: bool) -> None:
        super().__init__(code)
        self.code = code
        self.status = status
        self.fallback_allowed = fallback_allowed


def build_request(
    target: Target,
    request: Mapping[str, Any],
    scenario_id: str,
    api_key: str | None,
) -> tuple[str, dict[str, str], dict[str, Any]]:
    if target.provider == "simulator":
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        return "/simulate/chat/completions", headers, {"scenario_id": scenario_id}
    if not api_key:
        raise ProviderFailure("live_provider_disabled", 503, fallback_allowed=True)
    if target.provider == "openai":
        body = dict(request)
        body["model"] = target.model
        body["store"] = False
        return "/v1/chat/completions", {"Authorization": f"Bearer {api_key}"}, body
    if target.provider == "anthropic":
        body = _anthropic_request(request, target.model)
        return (
            "/v1/messages",
            {"X-Api-Key": api_key, "Anthropic-Version": "2023-06-01"},
            body,
        )
    raise ProviderFailure("provider_not_supported", 503, fallback_allowed=True)


def normalize_response(provider: str, payload: Any, requested_model: str) -> ProviderResult:
    if not isinstance(payload, dict):
        raise ProviderFailure("provider_protocol_error", 502, fallback_allowed=True)
    if provider in {"simulator", "openai"}:
        usage = payload.get("usage") or {}
        return ProviderResult(
            body=payload,
            cost_inputs={
                "input_tokens": int(usage.get("prompt_tokens", 0)),
                "output_tokens": int(usage.get("completion_tokens", 0)),
                "cached_input_tokens": int((usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)),
            },
        )
    if provider != "anthropic":
        raise ProviderFailure("provider_not_supported", 503, fallback_allowed=True)

    content = payload.get("content")
    usage = payload.get("usage") or {}
    if not isinstance(content, list) or not isinstance(usage, dict):
        raise ProviderFailure("provider_protocol_error", 502, fallback_allowed=True)
    text = "".join(block.get("text", "") for block in content if block.get("type") == "text")
    tool_calls = [
        {
            "id": block["id"],
            "type": "function",
            "function": {"name": block["name"], "arguments": json.dumps(block.get("input", {}), separators=(",", ":"))},
        }
        for block in content
        if block.get("type") == "tool_use"
    ]
    if not text and not tool_calls:
        raise ProviderFailure("provider_protocol_error", 502, fallback_allowed=True)
    message: dict[str, Any] = {"role": "assistant", "content": text or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    input_tokens = int(usage.get("input_tokens", 0))
    output_tokens = int(usage.get("output_tokens", 0))
    body = {
        "id": payload.get("id", "anthropic-message"),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": requested_model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": _anthropic_finish_reason(payload.get("stop_reason")),
            }
        ],
        "usage": {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
    }
    return ProviderResult(
        body=body,
        cost_inputs={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_input_tokens": int(usage.get("cache_read_input_tokens", 0)),
            "cache_creation_input_tokens": int(usage.get("cache_creation_input_tokens", 0)),
        },
    )


async def normalized_stream(
    provider: str,
    response: httpx.Response,
    requested_model: str,
) -> AsyncIterator[dict[str, Any] | None]:
    if provider in {"simulator", "openai"}:
        async for line in response.aiter_lines():
            if not line.startswith("data: "):
                continue
            data = line.removeprefix("data: ")
            if data == "[DONE]":
                yield None
                return
            try:
                payload = json.loads(data)
            except json.JSONDecodeError as error:
                raise ProviderFailure("provider_protocol_error", 502, fallback_allowed=True) from error
            if "error" in payload:
                raise ProviderFailure("provider_stream_error", 502, fallback_allowed=True)
            payload["model"] = requested_model
            yield payload
        raise ProviderFailure("provider_stream_ended", 502, fallback_allowed=True)

    if provider != "anthropic":
        raise ProviderFailure("provider_not_supported", 503, fallback_allowed=True)
    input_tokens = 0
    output_tokens = 0
    tool_indexes: dict[int, int] = {}
    async for line in response.aiter_lines():
        if not line.startswith("data: "):
            continue
        try:
            event = json.loads(line.removeprefix("data: "))
        except json.JSONDecodeError as error:
            raise ProviderFailure("provider_protocol_error", 502, fallback_allowed=True) from error
        event_type = event.get("type")
        if event_type == "error":
            raise ProviderFailure("provider_stream_error", 502, fallback_allowed=True)
        if event_type == "message_start":
            input_tokens = int((event.get("message", {}).get("usage") or {}).get("input_tokens", 0))
            yield _chunk(requested_model, {"role": "assistant"})
        elif event_type == "content_block_start" and event.get("content_block", {}).get("type") == "tool_use":
            block = event["content_block"]
            tool_index = len(tool_indexes)
            tool_indexes[int(event["index"])] = tool_index
            yield _chunk(
                requested_model,
                {
                    "tool_calls": [
                        {
                            "index": tool_index,
                            "id": block["id"],
                            "type": "function",
                            "function": {"name": block["name"], "arguments": ""},
                        }
                    ]
                },
            )
        elif event_type == "content_block_delta":
            delta = event.get("delta", {})
            if delta.get("type") == "text_delta":
                yield _chunk(requested_model, {"content": delta.get("text", "")})
            elif delta.get("type") == "input_json_delta":
                index = tool_indexes.get(int(event.get("index", -1)))
                if index is None:
                    raise ProviderFailure("provider_protocol_error", 502, fallback_allowed=True)
                yield _chunk(
                    requested_model,
                    {"tool_calls": [{"index": index, "function": {"arguments": delta.get("partial_json", "")}}]},
                )
        elif event_type == "message_delta":
            output_tokens = int((event.get("usage") or {}).get("output_tokens", output_tokens))
            yield _chunk(requested_model, {}, _anthropic_finish_reason((event.get("delta") or {}).get("stop_reason")))
            yield {
                "id": "provider-stream",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": requested_model,
                "choices": [],
                "usage": {
                    "prompt_tokens": input_tokens,
                    "completion_tokens": output_tokens,
                    "total_tokens": input_tokens + output_tokens,
                },
            }
        elif event_type == "message_stop":
            yield None
            return
    raise ProviderFailure("provider_stream_ended", 502, fallback_allowed=True)


def classify_status(status: int) -> ProviderFailure:
    if status == 429 or status >= 500:
        return ProviderFailure("provider_unavailable", 503, fallback_allowed=True)
    return ProviderFailure("provider_rejected_request", 502, fallback_allowed=False)


def _anthropic_request(request: Mapping[str, Any], model: str) -> dict[str, Any]:
    response_format = request.get("response_format") or {}
    if "seed" in request or "top_p" in request:
        raise ProviderFailure("unsupported_parameter", 400, fallback_allowed=True)
    if response_format.get("type") == "json_object":
        raise ProviderFailure("unsupported_parameter", 400, fallback_allowed=True)
    system = []
    messages = []
    for message in request.get("messages", []):
        role = message["role"]
        if role in {"system", "developer"}:
            if message.get("content"):
                system.append(message["content"])
            continue
        if role == "tool":
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": message["tool_call_id"],
                            "content": message.get("content") or "",
                        }
                    ],
                }
            )
            continue
        content: Any = message.get("content") or ""
        if role == "assistant" and message.get("tool_calls"):
            blocks = ([{"type": "text", "text": content}] if content else [])
            blocks.extend(
                {
                    "type": "tool_use",
                    "id": call["id"],
                    "name": call["function"]["name"],
                    "input": json.loads(call["function"]["arguments"]),
                }
                for call in message["tool_calls"]
            )
            content = blocks
        messages.append({"role": role, "content": content})

    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": request.get("max_completion_tokens") or request.get("max_tokens") or 1024,
        "stream": bool(request.get("stream")),
    }
    if system:
        body["system"] = "\n\n".join(system)
    tool_choice = request.get("tool_choice")
    if request.get("tools") and tool_choice != "none":
        body["tools"] = [
            {
                "name": tool["function"]["name"],
                "description": tool["function"].get("description", ""),
                "input_schema": tool["function"]["parameters"],
            }
            for tool in request["tools"]
        ]
    if tool_choice in {"auto", "required"}:
        body["tool_choice"] = {"type": "any" if tool_choice == "required" else "auto"}
    elif isinstance(tool_choice, dict):
        try:
            body["tool_choice"] = {"type": "tool", "name": tool_choice["function"]["name"]}
        except (KeyError, TypeError) as error:
            raise ProviderFailure("unsupported_parameter", 400, fallback_allowed=True) from error
    if response_format.get("type") == "json_schema":
        body["output_config"] = {
            "format": {
                "type": "json_schema",
                "schema": response_format["json_schema"]["schema"],
            }
        }
    for field in ("temperature", "stop"):
        if field not in request:
            continue
        mapped = "stop_sequences" if field == "stop" else field
        value = request[field]
        body[mapped] = [value] if field == "stop" and isinstance(value, str) else value
    return body


def _anthropic_finish_reason(reason: Any) -> str | None:
    return {
        "end_turn": "stop",
        "stop_sequence": "stop",
        "max_tokens": "length",
        "tool_use": "tool_calls",
        None: None,
    }.get(reason, "stop")


def _chunk(model: str, delta: dict[str, Any], finish_reason: str | None = None) -> dict[str, Any]:
    return {
        "id": "provider-stream",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
