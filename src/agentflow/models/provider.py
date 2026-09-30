"""Provider-specific validation without a Chat/Responses translation bridge."""
from __future__ import annotations

import copy
import json
from typing import Any

from agentflow.common import DomainError

from .profiles import AttemptContext, ModelProfile

CHAT_FIELDS = {
    "model", "messages", "stream", "stream_options", "max_tokens", "max_completion_tokens",
    "temperature", "top_p", "top_k", "tools", "tool_choice", "response_format", "thinking",
    "parallel_tool_calls", "stop", "user", "seed", "n", "frequency_penalty", "presence_penalty",
    "logprobs", "top_logprobs", "reasoning_effort", "prompt_cache_key", "prompt_cache_retention",
}
RESPONSE_FIELDS = {
    "model", "input", "instructions", "stream", "max_output_tokens", "tools", "tool_choice",
    "reasoning", "text", "temperature", "top_p", "parallel_tool_calls", "max_tool_calls",
    "previous_response_id", "conversation", "store", "background", "user", "metadata", "include",
    "service_tier", "prompt_cache_key", "prompt_cache_retention", "safety_identifier", "client_metadata",
}


class ModelProvider:
    def normalize_request(
        self, profile: ModelProfile, context: AttemptContext, protocol: str, payload: dict[str, Any]
    ) -> tuple[dict[str, Any], int]:
        profile.assert_accepted(protocol)
        context.assert_current(protocol)
        if not isinstance(payload, dict):
            raise DomainError("invalid_request", "Model request must be a JSON object", 422)
        fields = CHAT_FIELDS if protocol == "chat_completions" else RESPONSE_FIELDS
        unexpected = set(payload) - fields
        if unexpected:
            raise DomainError("unsupported_parameter", f"Unsupported request fields: {sorted(unexpected)}", 422)
        try:
            encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
        except (ValueError, TypeError) as exc:
            raise DomainError("invalid_request", "Request is not finite JSON", 422) from exc
        if len(encoded) > profile.max_request_bytes:
            raise DomainError("request_too_large", "Model request exceeds configured bound", 413)
        body = copy.deepcopy(payload)
        # Codex 0.154 sends local client telemetry. It is not a provider request
        # semantic and must never be forwarded to the configured upstream.
        body.pop("client_metadata", None)
        if body.get("model") != profile.accepted_api_model:
            raise DomainError("model_mismatch", "Request model differs from accepted task profile", 403)
        if "stream" in body and not isinstance(body["stream"], bool):
            raise DomainError("invalid_request", "stream must be boolean", 422)
        self._validate_tools(body.get("tools", []), protocol, profile, context)
        self._validate_images(body)
        if protocol == "chat_completions":
            messages = body.get("messages")
            if not isinstance(messages, list) or not messages:
                raise DomainError("invalid_request", "Chat request requires messages", 422)
            if "max_tokens" in body and "max_completion_tokens" in body:
                raise DomainError("invalid_request", "Specify one output limit", 422)
            key = "max_completion_tokens" if "max_completion_tokens" in body else "max_tokens"
            output = self._output_limit(body.get(key), profile, context)
            if profile.provider == "deepseek":
                # Explicit same-protocol provider parameter normalization.
                body.pop("max_completion_tokens", None)
                key = "max_tokens"
            body[key] = output
            if body.get("n", 1) != 1:
                raise DomainError("unsupported_semantics", "Only one completion per reservation is supported", 422)
            if body.get("stream"):
                options = body.get("stream_options", {})
                if not isinstance(options, dict) or set(options) - {"include_usage"}:
                    raise DomainError("unsupported_parameter", "Unsupported stream_options", 422)
                body["stream_options"] = {"include_usage": True}
        else:
            if not body.get("input") and not body.get("instructions"):
                raise DomainError("invalid_request", "Responses request requires input or instructions", 422)
            self._reasoning_policy(body, profile, context)
            output = self._output_limit(body.get("max_output_tokens"), profile, context)
            body["max_output_tokens"] = output
            if profile.provider == "deepseek":
                for key in ["previous_response_id", "conversation"]:
                    if body.get(key) is not None:
                        raise DomainError("unsupported_semantics", f"DeepSeek does not support {key}", 422)
                if any(body.get(key) is not None and body.get(key) is not False for key in ("store", "background")):
                    raise DomainError("unsupported_semantics", "Remote store/background is not supported", 422)
                body["store"] = False
                body["background"] = False
            limit = body.get("max_tool_calls")
            if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 0):
                raise DomainError("invalid_request", "Invalid max_tool_calls", 422)
            if limit is not None and limit > context.max_tool_calls:
                raise DomainError("tool_limit_exceeded", "Request cannot expand the task tool allowance", 403)
            # DeepSeek ignores max_tool_calls. Execution-time ToolBroker or verified
            # backend hooks enforce it; proxy events alone are not shell interception.
            from .coding_completion import add_completion_hint
            add_completion_hint(body, max_request_bytes=profile.max_request_bytes)
        return body, output

    @staticmethod
    def _reasoning_policy(body, profile, context):
        if profile.reasoning_effort != context.reasoning_effort:
            raise DomainError('reasoning_policy_mismatch', 'Frozen task reasoning policy differs from its model profile', 403)
        effort = context.reasoning_effort
        if effort is None:
            return
        reasoning = body.get('reasoning')
        if reasoning is None:
            reasoning = {}
        if not isinstance(reasoning, dict):
            raise DomainError('invalid_request', 'Responses reasoning must be an object', 422)
        requested = reasoning.get('effort')
        if requested is not None and not isinstance(requested, str):
            raise DomainError('invalid_request', 'Responses reasoning effort must be a string', 422)
        if requested is not None and requested != effort:
            raise DomainError('reasoning_policy_conflict', 'Request reasoning effort differs from the frozen owner policy', 403)
        body['reasoning'] = {**reasoning, 'effort': effort}

    @staticmethod
    def _output_limit(value: Any, profile: ModelProfile, context: AttemptContext) -> int:
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
            raise DomainError("invalid_request", "Output token limit must be a positive integer", 422)
        # SDKs can send their own smaller default. Internal clients do not own
        # this policy: use the server profile within the frozen attempt's
        # authority, including when an older attempt still has a lower ceiling.
        return min(profile.max_output_tokens, context.max_output_tokens)

    @staticmethod
    def _validate_tools(tools: Any, protocol: str, profile: ModelProfile, context: AttemptContext) -> None:
        if not isinstance(tools, list) or len(tools) > 128:
            raise DomainError("invalid_tools", "Invalid tool list", 422)
        for tool in tools:
            if not isinstance(tool, dict):
                raise DomainError("invalid_tools", "Tool must be an object", 422)
            kind = tool.get("type")
            if kind == "function":
                definition = tool.get("function") if protocol == "chat_completions" else tool
                if not isinstance(definition, dict):
                    raise DomainError("invalid_tools", "Function tool definition is missing", 422)
                name = definition.get("name")
            elif protocol == "responses" and kind == "custom" and tool.get("name") == "apply_patch":
                name = "apply_patch"
            else:
                raise DomainError("unsupported_tool", "Only registered function/apply_patch tools are supported", 422)
            if not isinstance(name, str) or not name:
                raise DomainError("invalid_tools", "Tool name is required", 422)
            for allowlist in [profile.allowed_tool_names, context.allowed_tool_names]:
                if allowlist is not None and name not in allowlist:
                    raise DomainError("forbidden_tool", f"Tool is outside the task allowlist: {name}", 403)

    @classmethod
    def _validate_images(cls, value: Any) -> None:
        if isinstance(value, dict):
            if value.get("type") in {"input_image", "image_url"}:
                image = value.get("image_url")
                url = image.get("url") if isinstance(image, dict) else image
                if not isinstance(url, str) or not url.startswith(("data:image/png;base64,", "data:image/jpeg;base64,", "data:image/webp;base64,")):
                    raise DomainError("unsupported_image_source", "Only inline task images are accepted", 422)
            for item in value.values():
                cls._validate_images(item)
        elif isinstance(value, list):
            for item in value:
                cls._validate_images(item)


class ResponseTracker:
    """SSE framing and terminal/usage evidence independent from transport EOF."""

    def __init__(self, protocol: str, max_event_bytes: int = 2 * 1024 * 1024, *, output_limit: int | None = None):
        import codecs

        self.protocol = protocol
        self.decoder = codecs.getincrementaldecoder("utf-8")("strict")
        self.buffer = ""
        self.max_event_bytes = max_event_bytes
        self.terminal = False
        self.terminal_kind: str | None = None
        self.output_seen = False
        self.input_tokens: int | None = None
        self.output_tokens: int | None = None
        self.response_model: str | None = None
        self.error_seen = False
        self.output_limit = output_limit
        self.failure_code: str | None = None
        self.finish_reasons: set[str] = set()
        self.incomplete_reason: str | None = None
        self.reasoning_tokens: int | None = None
        self.non_reasoning_output_seen = False

    def feed(self, data: bytes) -> None:
        self.buffer += self.decoder.decode(data)
        self.buffer = self.buffer.replace("\r\n", "\n")
        while "\n\n" in self.buffer:
            event, self.buffer = self.buffer.split("\n\n", 1)
            if len(event.encode()) > self.max_event_bytes:
                raise DomainError("invalid_stream", "SSE event exceeds limit", 502)
            lines = [line[5:].lstrip(" ") for line in event.split("\n") if line.startswith("data:")]
            if not lines:
                continue
            content = "\n".join(lines)
            if self.terminal:
                raise DomainError('invalid_stream', 'Data arrived after the protocol terminal event', 502)
            if content == "[DONE]":
                if self.protocol != "chat_completions" or not self.output_seen:
                    raise DomainError("invalid_stream", "Unexpected terminal marker", 502)
                self.terminal = True
                self.terminal_kind = 'failed' if self.error_seen else 'incomplete' if self.failure_code else 'completed'
                continue
            try:
                payload = json.loads(content)
            except json.JSONDecodeError as exc:
                raise DomainError("invalid_stream", "Invalid JSON SSE event", 502) from exc
            self.observe(payload, streaming=True)
        if len(self.buffer.encode()) > self.max_event_bytes:
            raise DomainError("invalid_stream", "SSE event exceeds limit", 502)

    def finish(self) -> None:
        self.buffer += self.decoder.decode(b"", final=True)
        if self.buffer.strip():
            raise DomainError("invalid_stream", "Truncated SSE event", 502)
        if not self.terminal:
            raise DomainError("incomplete_stream", "Transport EOF without protocol terminal event", 502)

    def observe(self, payload: Any, *, streaming: bool = False) -> None:
        if not isinstance(payload, dict):
            raise DomainError("invalid_response", "Model response is not an object", 502)
        if payload.get("error"):
            self.error_seen = True
            self.failure_code = self.failure_code or 'model_request_failed'
        response = payload.get("response", payload)
        if not isinstance(response, dict):
            raise DomainError("invalid_response", "Invalid response envelope", 502)
        if response.get('error'):
            self.error_seen = True
            self.failure_code = self.failure_code or 'model_request_failed'
        if isinstance(response.get("model"), str):
            self.response_model = response["model"]
        usage = response.get("usage")
        if isinstance(usage, dict):
            first = usage.get("input_tokens", usage.get("prompt_tokens"))
            second = usage.get("output_tokens", usage.get("completion_tokens"))
            if all(isinstance(x, int) and not isinstance(x, bool) and x >= 0 for x in [first, second]):
                self.input_tokens = max(self.input_tokens or 0, first)
                self.output_tokens = max(self.output_tokens or 0, second)
            elif first is not None or second is not None:
                raise DomainError("invalid_usage", "Provider usage is malformed", 502)
            details = usage.get('output_tokens_details' if self.protocol == 'responses' else 'completion_tokens_details')
            reasoning = details.get('reasoning_tokens') if isinstance(details, dict) else None
            if type(reasoning) is int and reasoning >= 0 and type(second) is int and reasoning <= second:
                self.reasoning_tokens = max(self.reasoning_tokens or 0, reasoning)
        if self.protocol == "chat_completions":
            choices = response.get("choices")
            if isinstance(choices, list) and choices:
                if len(choices) != 1 or not isinstance(choices[0], dict):
                    raise DomainError('invalid_response', 'Chat response must contain the one authorized choice', 502)
                choice = choices[0]
                if choice.get('index') is not None and (type(choice['index']) is not int or choice['index'] != 0):
                    raise DomainError('invalid_response', 'Chat response choice index differs from the request', 502)
                finish = choice.get('finish_reason')
                if finish is not None:
                    if not isinstance(finish, str):
                        raise DomainError('invalid_response', 'Chat finish reason must be a string', 502)
                    self.finish_reasons.add(finish if finish in {'stop', 'tool_calls', 'function_call', 'length', 'content_filter'} else 'unknown')
                    if len(self.finish_reasons) > 1:
                        raise DomainError('invalid_response', 'Chat response has conflicting finish reasons', 502)
                    if finish == 'length':
                        self.failure_code, self.incomplete_reason = 'model_output_limit', 'max_output_tokens'
                    elif finish not in {'stop', 'tool_calls', 'function_call'}:
                        self.failure_code, self.incomplete_reason = 'invalid_model_output', 'content_filter' if finish == 'content_filter' else 'unknown'
                message = choice.get('delta' if streaming else 'message')
                if isinstance(message, dict) and any(message.get(field) for field in ('content', 'tool_calls', 'function_call')):
                    self.non_reasoning_output_seen = True
                self.output_seen = True
            if not streaming:
                if not self.output_seen and not self.error_seen:
                    raise DomainError("invalid_response", "Chat response has no choices", 502)
                self.terminal = True
                self.terminal_kind = 'failed' if self.error_seen else 'incomplete' if self.failure_code else 'completed'
        else:
            kind = payload.get("type")
            if streaming and kind in {"response.completed", "response.failed", "response.incomplete", "response.cancelled"}:
                if response.get('status') != kind.removeprefix('response.'):
                    raise DomainError('invalid_response', 'Responses terminal event and status disagree', 502)
                self.terminal = True
                self.terminal_kind = kind.removeprefix("response.")
            if not streaming:
                status = response.get("status")
                if status not in {"completed", "failed", "incomplete", "cancelled"}:
                    raise DomainError("invalid_response", "Responses object is not terminal", 502)
                self.terminal = True
                self.terminal_kind = status
            if self.terminal_kind == 'completed' and (self.error_seen or response.get('incomplete_details') is not None):
                raise DomainError('invalid_response', 'Completed response carries conflicting failure details', 502)
            if self.terminal_kind == 'incomplete':
                detail = response.get('incomplete_details')
                reason = detail.get('reason') if isinstance(detail, dict) else None
                self.incomplete_reason = reason if isinstance(reason, str) and reason in {'max_output_tokens', 'content_filter'} else 'unknown'
                self.failure_code = 'model_output_limit' if reason == 'max_output_tokens' else 'invalid_model_output'
            output = response.get('output')
            if isinstance(output, list) and any(isinstance(item, dict) and item.get('type') != 'reasoning' for item in output):
                self.non_reasoning_output_seen = True
            if kind in {'response.output_text.delta', 'response.function_call_arguments.delta', 'response.custom_tool_call_input.delta'} and payload.get('delta'):
                self.non_reasoning_output_seen = True

    def completion_metadata(self) -> dict:
        """Accounting totals include reasoning; no fixed visible-token reservation is inferred."""
        return {'output_status': self.terminal_kind or 'unknown', 'failure_code': self.failure_code,
            'finish_reasons': sorted(self.finish_reasons), 'incomplete_reason': self.incomplete_reason,
            'output_limit': self.output_limit, 'output_tokens': self.output_tokens,
            'reasoning_tokens': self.reasoning_tokens, 'non_reasoning_output_seen': self.non_reasoning_output_seen}
