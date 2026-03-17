"""
mitmproxy content view addon for AI/LLM/MCP traffic.

Prettifies request and response bodies for:
- Anthropic Messages API (streaming SSE and non-streaming JSON)
- MCP Protocol (JSON-RPC 2.0 over HTTP via mcp-proxy.anthropic.com)
- OpenAI Chat Completions API (and compatible providers)
- Google Gemini API (stub)
- Claude Code telemetry/event logging
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import textwrap
import threading
from typing import Any

from mitmproxy import contentviews, http

try:
    import anthropic as _anthropic_sdk
except ImportError:
    _anthropic_sdk = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pricing per million tokens (USD) — used for cost estimates
# ---------------------------------------------------------------------------
MODEL_PRICING: dict[str, dict[str, float]] = {
    "claude-opus-4": {"input": 15.0, "output": 75.0, "cache_read": 1.5, "cache_write": 18.75},
    "claude-sonnet-4": {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75},
    "claude-haiku-4": {"input": 0.80, "output": 4.0, "cache_read": 0.08, "cache_write": 1.0},
    "gpt-4o": {"input": 2.5, "output": 10.0},
    "gpt-4.1": {"input": 2.0, "output": 8.0},
    "gpt-4.1-mini": {"input": 0.4, "output": 1.6},
    "gpt-4.1-nano": {"input": 0.1, "output": 0.4},
    "o3": {"input": 2.0, "output": 8.0},
    "o4-mini": {"input": 1.1, "output": 4.4},
}

AI_HOSTS = {
    "api.anthropic.com",
    "mcp-proxy.anthropic.com",
    "api.openai.com",
    "generativelanguage.googleapis.com",
}

AI_PATH_PREFIXES = (
    "/v1/messages",
    "/v1/mcp/",
    "/api/event_logging/",
    "/v1/chat/completions",
    "/v1beta/models/",
)

MAX_TEXT = 300
MAX_SYSTEM = 500


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _trunc(text: str, limit: int = MAX_TEXT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def _indent(text: str, prefix: str = "  ") -> str:
    return textwrap.indent(text, prefix)


def _resolve_pricing(model: str) -> dict[str, float] | None:
    for key, prices in MODEL_PRICING.items():
        if key in model:
            return prices
    return None


def _estimate_cost(model: str, usage: dict[str, Any]) -> str | None:
    prices = _resolve_pricing(model)
    if not prices:
        return None
    inp = usage.get("input_tokens", 0)
    out = usage.get("output_tokens", 0)
    cache_read = usage.get("cache_read_input_tokens", 0)
    cache_create = usage.get("cache_creation_input_tokens", 0)
    cost = (
        (inp - cache_read) * prices.get("input", 0)
        + out * prices.get("output", 0)
        + cache_read * prices.get("cache_read", 0)
        + cache_create * prices.get("cache_write", prices.get("input", 0))
    ) / 1_000_000
    return f"${cost:.6f}"


def _strip_chunked_encoding(text: str) -> str:
    """Remove HTTP chunked transfer-encoding length markers from raw body text."""
    return re.sub(r"^[0-9a-fA-F]+\r?\n", "", text, flags=re.MULTILINE)


def _format_usage(usage: dict[str, Any]) -> list[str]:
    lines = []
    for key in ("input_tokens", "output_tokens", "cache_creation_input_tokens",
                "cache_read_input_tokens", "service_tier", "inference_geo"):
        if key in usage and usage[key]:
            lines.append(f"  {key}: {usage[key]}")
    cache_detail = usage.get("cache_creation", {})
    for ck, cv in cache_detail.items():
        if cv:
            lines.append(f"  cache_creation.{ck}: {cv}")
    return lines


# ---------------------------------------------------------------------------
# Anthropic Messages API — Request
# ---------------------------------------------------------------------------

def _format_anthropic_request(body: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(f"model: {body.get('model', '?')}")
    lines.append(f"max_tokens: {body.get('max_tokens', '?')}")
    lines.append(f"stream: {body.get('stream', False)}")

    if thinking := body.get("thinking"):
        lines.append(f"thinking: {json.dumps(thinking)}")
    if effort := (body.get("output_config") or {}).get("effort"):
        lines.append(f"effort: {effort}")
    if metadata := body.get("metadata"):
        uid = metadata.get("user_id", "")
        if uid:
            lines.append(f"user_id: {_trunc(uid, 60)}")

    # System prompt
    system = body.get("system")
    if system:
        lines.append("")
        if isinstance(system, list):
            total_len = sum(len(b.get("text", "")) for b in system if isinstance(b, dict))
            lines.append(f"system_prompt: ({len(system)} blocks, ~{total_len} chars)")
            for i, block in enumerate(system):
                if not isinstance(block, dict):
                    continue
                txt = block.get("text", "")
                cache = block.get("cache_control")
                cache_str = f"  [cache: {cache}]" if cache else ""
                lines.append(f"  [{i}]{cache_str} {_trunc(txt, MAX_SYSTEM)}")
        else:
            lines.append(f"system_prompt: {_trunc(str(system), MAX_SYSTEM)}")

    # Tools
    tools = body.get("tools", [])
    if tools:
        lines.append("")
        lines.append(f"tools: ({len(tools)} defined)")
        for t in tools:
            name = t.get("name", "?")
            lines.append(f"  - {name}")

    # Messages
    messages = body.get("messages", [])
    lines.append("")
    lines.append(f"messages: ({len(messages)} turns)")
    for msg in messages:
        role = msg.get("role", "?")
        content = msg.get("content", "")
        if isinstance(content, str):
            lines.append(f"  [{role}] {_trunc(content)}")
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    lines.append(f"  [{role}] {_trunc(str(block))}")
                    continue
                btype = block.get("type", "?")
                if btype == "text":
                    lines.append(f"  [{role}/text] {_trunc(block.get('text', ''))}")
                elif btype == "tool_use":
                    inp_keys = list(block.get("input", {}).keys()) if isinstance(block.get("input"), dict) else []
                    lines.append(f"  [{role}/tool_use] {block.get('name', '?')}({', '.join(inp_keys)})")
                elif btype == "tool_result":
                    tid = block.get("tool_use_id", "?")
                    rc = block.get("content", "")
                    if isinstance(rc, list):
                        summary = ", ".join(b.get("type", "?") for b in rc if isinstance(b, dict))
                        lines.append(f"  [{role}/tool_result] id={_trunc(tid, 30)} -> [{summary}]")
                    else:
                        lines.append(f"  [{role}/tool_result] id={_trunc(tid, 30)} -> {_trunc(str(rc), 100)}")
                elif btype == "thinking":
                    lines.append(f"  [{role}/thinking] (redacted, signature present)")
                else:
                    lines.append(f"  [{role}/{btype}] {_trunc(str(block), 150)}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Anthropic Messages API — Streaming SSE Response
# ---------------------------------------------------------------------------

def _format_anthropic_sse(text: str) -> str:
    clean = _strip_chunked_encoding(text)
    lines_out: list[str] = []
    model = ""
    msg_id = ""
    usage_start: dict[str, Any] = {}
    usage_final: dict[str, Any] = {}
    stop_reason = ""
    thinking_count = 0
    tool_calls: list[dict[str, str]] = []
    current_tool: dict[str, str] | None = None
    text_parts: list[str] = []
    context_mgmt: dict | None = None

    for line in clean.split("\n"):
        line = line.strip()
        if not line.startswith("data: "):
            continue
        payload = line[6:].strip()
        if payload == "[DONE]":
            continue
        try:
            evt = json.loads(payload)
        except json.JSONDecodeError:
            continue

        etype = evt.get("type", "")

        if etype == "message_start":
            msg = evt.get("message", {})
            model = msg.get("model", "")
            msg_id = msg.get("id", "")
            usage_start = msg.get("usage", {})

        elif etype == "content_block_start":
            cb = evt.get("content_block", {})
            if cb.get("type") == "thinking":
                thinking_count += 1
            elif cb.get("type") == "tool_use":
                current_tool = {"name": cb.get("name", "?"), "id": cb.get("id", ""), "input": ""}

        elif etype == "content_block_delta":
            delta = evt.get("delta", {})
            dt = delta.get("type", "")
            if dt == "text_delta":
                text_parts.append(delta.get("text", ""))
            elif dt == "input_json_delta" and current_tool is not None:
                current_tool["input"] += delta.get("partial_json", "")

        elif etype == "content_block_stop":
            if current_tool is not None:
                tool_input = current_tool["input"]
                try:
                    parsed = json.loads(tool_input)
                    tool_input = json.dumps(parsed, indent=2)
                except json.JSONDecodeError:
                    pass
                tool_calls.append({"name": current_tool["name"], "id": current_tool["id"], "input": tool_input})
                current_tool = None

        elif etype == "message_delta":
            stop_reason = evt.get("delta", {}).get("stop_reason", "")
            usage_final = evt.get("usage", {})
            context_mgmt = evt.get("context_management")

        elif etype == "message_stop":
            pass

    # Build output
    if model:
        lines_out.append(f"model: {model}")
    if msg_id:
        lines_out.append(f"message_id: {msg_id}")
    if stop_reason:
        lines_out.append(f"stop_reason: {stop_reason}")

    # Merge usage from start + final
    merged_usage = {**usage_start, **{k: v for k, v in usage_final.items() if v}}
    if merged_usage:
        lines_out.append("")
        lines_out.append("usage:")
        lines_out.extend(_format_usage(merged_usage))
        if model:
            cost = _estimate_cost(model, merged_usage)
            if cost:
                lines_out.append(f"  estimated_cost: {cost}")

    if thinking_count:
        lines_out.append("")
        lines_out.append(f"thinking_blocks: {thinking_count} (content redacted)")

    if tool_calls:
        lines_out.append("")
        lines_out.append(f"tool_calls: ({len(tool_calls)})")
        for tc in tool_calls:
            lines_out.append(f"  - {tc['name']} (id: {_trunc(tc['id'], 30)})")
            lines_out.append(_indent(tc["input"], "    "))

    full_text = "".join(text_parts)
    if full_text:
        lines_out.append("")
        lines_out.append("assistant_response:")
        lines_out.append(_indent(_trunc(full_text, 2000)))

    if context_mgmt:
        edits = context_mgmt.get("applied_edits", [])
        if edits:
            lines_out.append("")
            lines_out.append(f"context_management: {len(edits)} edits applied")

    return "\n".join(lines_out) if lines_out else text


# ---------------------------------------------------------------------------
# Anthropic Messages API — Non-streaming JSON Response
# ---------------------------------------------------------------------------

def _format_anthropic_json_response(body: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(f"model: {body.get('model', '?')}")
    lines.append(f"message_id: {body.get('id', '?')}")
    lines.append(f"stop_reason: {body.get('stop_reason', '?')}")

    usage = body.get("usage", {})
    if usage:
        lines.append("")
        lines.append("usage:")
        lines.extend(_format_usage(usage))
        model = body.get("model", "")
        if model:
            cost = _estimate_cost(model, usage)
            if cost:
                lines.append(f"  estimated_cost: {cost}")

    content = body.get("content", [])
    if content:
        lines.append("")
        lines.append(f"content: ({len(content)} blocks)")
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type", "?")
            if btype == "text":
                lines.append(f"  [text] {_trunc(block.get('text', ''))}")
            elif btype == "tool_use":
                lines.append(f"  [tool_use] {block.get('name', '?')}")
            elif btype == "thinking":
                lines.append(f"  [thinking] (redacted)")
            else:
                lines.append(f"  [{btype}] {_trunc(str(block), 150)}")

    ctx = body.get("context_management", {})
    if ctx and ctx.get("applied_edits"):
        lines.append(f"context_management: {len(ctx['applied_edits'])} edits applied")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# MCP Protocol (JSON-RPC 2.0)
# ---------------------------------------------------------------------------

def _format_mcp(body: dict[str, Any], path: str = "") -> str:
    lines: list[str] = []

    server_id_match = re.search(r"/v1/mcp/(\S+)", path)
    if server_id_match:
        lines.append(f"mcp_server_id: {server_id_match.group(1)}")

    if "jsonrpc" in body:
        lines.append(f"jsonrpc: {body.get('jsonrpc')}")
        lines.append(f"method: {body.get('method', '?')}")
        lines.append(f"id: {body.get('id', '?')}")
        params = body.get("params", {})
        if params:
            lines.append("")
            lines.append("params:")
            if pv := params.get("protocolVersion"):
                lines.append(f"  protocolVersion: {pv}")
            if ci := params.get("clientInfo"):
                lines.append(f"  clientInfo: {ci.get('name', '?')} v{ci.get('version', '?')}")
            caps = params.get("capabilities", {})
            if caps:
                lines.append(f"  capabilities: {list(caps.keys())}")

    elif body.get("type") == "error":
        err = body.get("error", {})
        lines.append(f"type: error")
        lines.append(f"error_type: {err.get('type', '?')}")
        lines.append(f"message: {err.get('message', '?')}")
        if rid := body.get("request_id"):
            lines.append(f"request_id: {rid}")

    elif "result" in body:
        lines.append("type: result")
        result = body["result"]
        if isinstance(result, dict):
            if sv := result.get("serverInfo"):
                lines.append(f"serverInfo: {sv.get('name', '?')} v{sv.get('version', '?')}")
            if pv := result.get("protocolVersion"):
                lines.append(f"protocolVersion: {pv}")
            caps = result.get("capabilities", {})
            if caps:
                lines.append(f"capabilities: {list(caps.keys())}")
            if tools := result.get("tools"):
                lines.append(f"tools: ({len(tools)})")
                for t in tools:
                    lines.append(f"  - {t.get('name', '?')}")
        else:
            lines.append(f"result: {_trunc(str(result))}")

    else:
        lines.append(json.dumps(body, indent=2)[:2000])

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Telemetry / Event Logging
# ---------------------------------------------------------------------------

HIGHLIGHT_EVENTS = {
    "tengu_api_success",
    "tengu_api_error",
    "tengu_tool_use_success",
    "tengu_tool_use_error",
    "tengu_mcp_server_connection_succeeded",
    "tengu_mcp_server_connection_failed",
    "tengu_claudeai_limits_status_changed",
    "tengu_mcp_claudeai_proxy_401",
    "tengu_mcp_server_needs_auth",
}


def _format_telemetry(body: dict[str, Any]) -> str:
    events = body.get("events", [])
    lines: list[str] = []
    lines.append(f"telemetry_batch: {len(events)} events")

    sensitive_fields: list[str] = []
    highlighted: list[str] = []
    other_names: list[str] = []

    for evt in events:
        data = evt.get("event_data", {})
        name = data.get("event_name", "?")

        if data.get("email"):
            sensitive_fields.append("email")
        if data.get("device_id"):
            sensitive_fields.append("device_id")

        if name not in HIGHLIGHT_EVENTS:
            other_names.append(name)
            continue

        meta_str = data.get("additional_metadata", "{}")
        try:
            meta = json.loads(meta_str) if isinstance(meta_str, str) else meta_str
        except json.JSONDecodeError:
            meta = {}

        if name == "tengu_api_success":
            highlighted.append(
                f"  [api_success] model={meta.get('model', '?')} "
                f"in={meta.get('inputTokens', '?')}+cache_read={meta.get('cachedInputTokens', '?')}+"
                f"cache_write={meta.get('uncachedInputTokens', '?')} "
                f"out={meta.get('outputTokens', '?')} "
                f"cost={meta.get('costUSD', '?')} "
                f"dur={meta.get('durationMs', '?')}ms "
                f"stop={meta.get('stop_reason', '?')}"
            )
        elif name == "tengu_api_error":
            highlighted.append(
                f"  [api_error] model={meta.get('model', '?')} "
                f"error={meta.get('errorType', '?')} "
                f"status={meta.get('statusCode', '?')}"
            )
        elif name == "tengu_tool_use_success":
            highlighted.append(
                f"  [tool_success] {meta.get('toolName', '?')} "
                f"dur={meta.get('durationMs', '?')}ms "
                f"result_bytes={meta.get('toolResultSizeBytes', '?')}"
            )
        elif name == "tengu_tool_use_error":
            highlighted.append(
                f"  [tool_error] {meta.get('toolName', '?')} "
                f"error={_trunc(str(meta), 100)}"
            )
        elif name == "tengu_mcp_server_connection_succeeded":
            highlighted.append(
                f"  [mcp_connected] {meta.get('mcpServerBaseUrl', '?')} "
                f"transport={meta.get('transportType', '?')} "
                f"dur={meta.get('connectionDurationMs', '?')}ms"
            )
        elif name == "tengu_mcp_server_connection_failed":
            highlighted.append(
                f"  [mcp_failed] {meta.get('mcpServerBaseUrl', '?')} "
                f"transport={meta.get('transportType', '?')} "
                f"dur={meta.get('connectionDurationMs', '?')}ms"
            )
        elif name == "tengu_claudeai_limits_status_changed":
            highlighted.append(
                f"  [rate_limit] status={meta.get('status', '?')} "
                f"hours_till_reset={meta.get('hoursTillReset', '?')}"
            )
        elif name == "tengu_mcp_claudeai_proxy_401":
            highlighted.append(f"  [mcp_auth_401] token_changed={meta.get('tokenChanged', '?')}")
        elif name == "tengu_mcp_server_needs_auth":
            highlighted.append(
                f"  [mcp_needs_auth] {meta.get('mcpServerBaseUrl', '?')} "
                f"transport={meta.get('transportType', '?')}"
            )

    if sensitive_fields:
        unique = sorted(set(sensitive_fields))
        lines.append(f"[SENSITIVE] fields present: {', '.join(unique)}")

    if highlighted:
        lines.append("")
        lines.append("key_events:")
        lines.extend(highlighted)

    if other_names:
        counts: dict[str, int] = {}
        for n in other_names:
            counts[n] = counts.get(n, 0) + 1
        lines.append("")
        lines.append(f"other_events: ({len(other_names)})")
        for n, c in sorted(counts.items()):
            suffix = f" x{c}" if c > 1 else ""
            lines.append(f"  - {n}{suffix}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# OpenAI Chat Completions API
# ---------------------------------------------------------------------------

def _format_openai_request(body: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(f"model: {body.get('model', '?')}")
    if temp := body.get("temperature"):
        lines.append(f"temperature: {temp}")
    if mt := body.get("max_tokens") or body.get("max_completion_tokens"):
        lines.append(f"max_tokens: {mt}")
    lines.append(f"stream: {body.get('stream', False)}")

    tools = body.get("tools", [])
    if tools:
        lines.append(f"tools: ({len(tools)})")
        for t in tools:
            fn = t.get("function", {})
            lines.append(f"  - {fn.get('name', '?')}")

    messages = body.get("messages", [])
    lines.append("")
    lines.append(f"messages: ({len(messages)} turns)")
    for msg in messages:
        role = msg.get("role", "?")
        content = msg.get("content")
        if isinstance(content, str):
            lines.append(f"  [{role}] {_trunc(content)}")
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    ptype = part.get("type", "?")
                    if ptype == "text":
                        lines.append(f"  [{role}/text] {_trunc(part.get('text', ''))}")
                    elif ptype == "image_url":
                        lines.append(f"  [{role}/image] {_trunc(str(part.get('image_url', {}).get('url', '')), 80)}")
                    else:
                        lines.append(f"  [{role}/{ptype}] ...")
        elif content is None and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                fn = tc.get("function", {})
                lines.append(f"  [{role}/tool_call] {fn.get('name', '?')}({_trunc(fn.get('arguments', ''), 100)})")
        elif content is None and msg.get("role") == "tool":
            lines.append(f"  [tool_result] id={msg.get('tool_call_id', '?')} -> {_trunc(str(msg.get('content', '')), 100)}")

    return "\n".join(lines)


def _format_openai_sse(text: str) -> str:
    clean = _strip_chunked_encoding(text)
    lines_out: list[str] = []
    model = ""
    msg_id = ""
    text_parts: list[str] = []
    tool_calls: dict[int, dict[str, str]] = {}
    finish_reason = ""
    usage: dict[str, Any] = {}

    for line in clean.split("\n"):
        line = line.strip()
        if not line.startswith("data: "):
            continue
        payload = line[6:].strip()
        if payload == "[DONE]":
            continue
        try:
            evt = json.loads(payload)
        except json.JSONDecodeError:
            continue

        if not model:
            model = evt.get("model", "")
        if not msg_id:
            msg_id = evt.get("id", "")
        if evt.get("usage"):
            usage = evt["usage"]

        for choice in evt.get("choices", []):
            delta = choice.get("delta", {})
            if delta.get("content"):
                text_parts.append(delta["content"])
            for tc in delta.get("tool_calls", []):
                idx = tc.get("index", 0)
                if idx not in tool_calls:
                    tool_calls[idx] = {"name": "", "arguments": ""}
                if tc.get("function", {}).get("name"):
                    tool_calls[idx]["name"] = tc["function"]["name"]
                if tc.get("function", {}).get("arguments"):
                    tool_calls[idx]["arguments"] += tc["function"]["arguments"]
            if fr := choice.get("finish_reason"):
                finish_reason = fr

    if model:
        lines_out.append(f"model: {model}")
    if msg_id:
        lines_out.append(f"id: {msg_id}")
    if finish_reason:
        lines_out.append(f"finish_reason: {finish_reason}")

    if usage:
        lines_out.append("")
        lines_out.append("usage:")
        for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
            if k in usage:
                lines_out.append(f"  {k}: {usage[k]}")
        if model:
            cost = _estimate_cost(model, {
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
            })
            if cost:
                lines_out.append(f"  estimated_cost: {cost}")

    if tool_calls:
        lines_out.append("")
        lines_out.append(f"tool_calls: ({len(tool_calls)})")
        for idx in sorted(tool_calls):
            tc = tool_calls[idx]
            args = tc["arguments"]
            try:
                args = json.dumps(json.loads(args), indent=2)
            except json.JSONDecodeError:
                pass
            lines_out.append(f"  - {tc['name']}")
            lines_out.append(_indent(args, "    "))

    full_text = "".join(text_parts)
    if full_text:
        lines_out.append("")
        lines_out.append("assistant_response:")
        lines_out.append(_indent(_trunc(full_text, 2000)))

    return "\n".join(lines_out) if lines_out else text


def _format_openai_json_response(body: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(f"model: {body.get('model', '?')}")
    lines.append(f"id: {body.get('id', '?')}")

    usage = body.get("usage", {})
    if usage:
        lines.append("")
        lines.append("usage:")
        for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
            if k in usage:
                lines.append(f"  {k}: {usage[k]}")

    for choice in body.get("choices", []):
        msg = choice.get("message", {})
        fr = choice.get("finish_reason", "")
        lines.append("")
        lines.append(f"finish_reason: {fr}")
        if msg.get("content"):
            lines.append(f"assistant: {_trunc(msg['content'], 2000)}")
        for tc in msg.get("tool_calls", []):
            fn = tc.get("function", {})
            lines.append(f"tool_call: {fn.get('name', '?')}({_trunc(fn.get('arguments', ''), 200)})")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Google Gemini (stub)
# ---------------------------------------------------------------------------

def _format_gemini_request(body: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(f"model: (from URL)")
    contents = body.get("contents", [])
    lines.append(f"contents: ({len(contents)} turns)")
    for turn in contents:
        role = turn.get("role", "?")
        parts = turn.get("parts", [])
        for part in parts:
            if "text" in part:
                lines.append(f"  [{role}] {_trunc(part['text'])}")
            elif "functionCall" in part:
                fc = part["functionCall"]
                lines.append(f"  [{role}/functionCall] {fc.get('name', '?')}")
    tools = body.get("tools", [])
    if tools:
        lines.append(f"tools: ({len(tools)} tool groups)")
    return "\n".join(lines)


def _format_gemini_response(body: dict[str, Any]) -> str:
    lines: list[str] = []
    for candidate in body.get("candidates", []):
        content = candidate.get("content", {})
        role = content.get("role", "?")
        for part in content.get("parts", []):
            if "text" in part:
                lines.append(f"[{role}] {_trunc(part['text'], 2000)}")
            elif "functionCall" in part:
                fc = part["functionCall"]
                lines.append(f"[{role}/functionCall] {fc.get('name', '?')}")
        if fr := candidate.get("finishReason"):
            lines.append(f"finish_reason: {fr}")
    usage = body.get("usageMetadata", {})
    if usage:
        lines.append("")
        lines.append("usage:")
        for k in ("promptTokenCount", "candidatesTokenCount", "totalTokenCount"):
            if k in usage:
                lines.append(f"  {k}: {usage[k]}")
    return "\n".join(lines) if lines else json.dumps(body, indent=2)[:2000]


# ---------------------------------------------------------------------------
# Traffic type detection
# ---------------------------------------------------------------------------

def _get_flow_info(metadata: contentviews.Metadata) -> tuple[str, str, bool]:
    """Return (host, path, is_request) from metadata."""
    host = ""
    path = ""
    is_request = False
    flow = metadata.flow
    if flow and hasattr(flow, "request"):
        req = flow.request
        host = getattr(req, "pretty_host", "") or getattr(req, "host", "") or ""
        path = getattr(req, "path", "") or ""
    if metadata.http_message and hasattr(metadata.http_message, "method"):
        is_request = True
    elif metadata.content_type and "event-stream" in metadata.content_type:
        is_request = False
    return host, path, is_request


def _is_ai_traffic(host: str, path: str) -> bool:
    if host in AI_HOSTS:
        return True
    for prefix in AI_PATH_PREFIXES:
        if path.startswith(prefix):
            return True
    return False


# ---------------------------------------------------------------------------
# Content View class
# ---------------------------------------------------------------------------

class AITrafficView(contentviews.Contentview):
    name = "AI Traffic"

    @property
    def syntax_highlight(self) -> contentviews.SyntaxHighlight:
        return "yaml"

    def prettify(self, data: bytes, metadata: contentviews.Metadata) -> str:
        host, path, is_request = _get_flow_info(metadata)
        text = data.decode("utf-8", errors="replace")
        ct = metadata.content_type or ""

        # --- Anthropic Messages API ---
        if "anthropic.com" in host and "/v1/messages" in path:
            if is_request:
                try:
                    body = json.loads(text)
                    return _format_anthropic_request(body)
                except json.JSONDecodeError:
                    return text
            elif "event-stream" in ct:
                return _format_anthropic_sse(text)
            else:
                clean = _strip_chunked_encoding(text)
                try:
                    body = json.loads(clean)
                    return _format_anthropic_json_response(body)
                except json.JSONDecodeError:
                    return text

        # --- MCP Protocol ---
        if "anthropic.com" in host and "/v1/mcp/" in path:
            clean = _strip_chunked_encoding(text)
            try:
                body = json.loads(clean)
                return _format_mcp(body, path)
            except json.JSONDecodeError:
                return text

        # --- Telemetry ---
        if "anthropic.com" in host and "/api/event_logging/" in path:
            try:
                body = json.loads(text)
                return _format_telemetry(body)
            except json.JSONDecodeError:
                return text

        # --- OpenAI Chat Completions ---
        if ("openai.com" in host or "/v1/chat/completions" in path):
            if is_request:
                try:
                    body = json.loads(text)
                    return _format_openai_request(body)
                except json.JSONDecodeError:
                    return text
            elif "event-stream" in ct:
                return _format_openai_sse(text)
            else:
                clean = _strip_chunked_encoding(text)
                try:
                    body = json.loads(clean)
                    return _format_openai_json_response(body)
                except json.JSONDecodeError:
                    return text

        # --- Google Gemini ---
        if "googleapis.com" in host and "/v1beta/models/" in path:
            clean = _strip_chunked_encoding(text)
            try:
                body = json.loads(clean)
            except json.JSONDecodeError:
                return text
            if is_request:
                return _format_gemini_request(body)
            else:
                return _format_gemini_response(body)

        return text

    def render_priority(self, data: bytes, metadata: contentviews.Metadata) -> float:
        host, path, _ = _get_flow_info(metadata)
        if _is_ai_traffic(host, path):
            return 2.0
        return 0


contentviews.add(AITrafficView)


# ---------------------------------------------------------------------------
# AI Explain — calls Sonnet to explain any HTTP transaction
# ---------------------------------------------------------------------------

_EXPLAIN_MODEL = "claude-sonnet-4-20250514"
_MAX_BODY_CHARS = 4000
_CREDENTIAL_PATHS = (
    os.path.expanduser("~/.claude/.credentials.json"),
    "/root/.claude/.credentials.json",
)


def _get_api_key() -> str | None:
    """Resolve an Anthropic API key from env var or mounted credentials."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key

    for path in _CREDENTIAL_PATHS:
        try:
            with open(path) as f:
                creds = json.load(f)
            # credentials.json stores {"claudeAiOauth": {"token": "..."}} or
            # {"apiKey": "sk-ant-..."}
            if api_key := creds.get("apiKey"):
                return api_key
            if oauth := creds.get("claudeAiOauth"):
                if token := oauth.get("token"):
                    return token
        except (OSError, json.JSONDecodeError, KeyError):
            continue
    return None


def _truncate_body(text: str, limit: int = _MAX_BODY_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated, {len(text)} chars total]"


def _format_headers(headers: list[tuple[str, str]]) -> str:
    """Format headers, redacting sensitive values."""
    sensitive = {"x-api-key", "authorization", "cookie", "set-cookie"}
    lines = []
    for name, value in headers:
        if name.lower() in sensitive:
            lines.append(f"{name}: [REDACTED]")
        else:
            lines.append(f"{name}: {value}")
    return "\n".join(lines)


def _build_explain_prompt(flow: http.HTTPFlow) -> str:
    """Build a compact prompt describing the full HTTP transaction."""
    req = flow.request
    parts = []

    parts.append("[Request]")
    parts.append(f"{req.method} {req.pretty_url}")
    parts.append(_format_headers(list(req.headers.items())))
    if req.content:
        body = req.get_text(strict=False) or ""
        clean = _strip_chunked_encoding(body)
        parts.append("")
        parts.append(_truncate_body(clean))

    resp = flow.response
    if resp:
        parts.append("")
        parts.append("[Response]")
        parts.append(f"HTTP {resp.status_code} {resp.reason or ''}")
        parts.append(_format_headers(list(resp.headers.items())))
        if resp.content:
            body = resp.get_text(strict=False) or ""
            clean = _strip_chunked_encoding(body)
            parts.append("")
            parts.append(_truncate_body(clean))
    else:
        parts.append("")
        parts.append("[Response]")
        parts.append("(no response received)")

    transaction = "\n".join(parts)

    return (
        "Explain this HTTP transaction in plain language. "
        "Be concise but thorough.\n\n"
        f"{transaction}\n\n"
        "Provide:\n"
        "1. What this request does (purpose, API being called)\n"
        "2. Key parameters and their significance\n"
        "3. What the response contains\n"
        "4. Any notable observations (errors, unusual patterns, security concerns)\n\n"
        "FORMATTING RULES (strict):\n"
        "- Output PLAIN TEXT only. Do NOT use markdown.\n"
        "- Use ALL-CAPS for section headers, followed by a line of dashes.\n"
        "- Use  - for bullet points and  > for quoted/highlighted values.\n"
        "- Use indentation (2 spaces) for hierarchy.\n"
        "- Separate sections with a blank line.\n"
        "- For code or field names, just write them as-is — no backticks.\n\n"
        "Example format:\n"
        "PURPOSE\n"
        "-------\n"
        "This request sends a chat completion to the OpenAI API.\n\n"
        "KEY PARAMETERS\n"
        "--------------\n"
        "  - model: gpt-4o\n"
        "  - messages: 3 conversation turns\n"
        "  - temperature: 0.7\n"
    )


def _call_sonnet(prompt: str) -> str:
    """Call Claude Sonnet and return the explanation text."""
    if _anthropic_sdk is None:
        return (
            "ERROR: The 'anthropic' Python package is not installed.\n"
            "Install it with: pip install anthropic"
        )

    api_key = _get_api_key()
    if not api_key:
        return (
            "NO API KEY FOUND\n"
            "================\n"
            "\n"
            "The AI Explain feature requires an Anthropic API key to call Claude Sonnet.\n"
            "\n"
            "How to fix:\n"
            "\n"
            "  Option 1 — Set the ANTHROPIC_API_KEY environment variable:\n"
            "\n"
            "    ANTHROPIC_API_KEY=sk-ant-... ./run.sh\n"
            "\n"
            "  Option 2 — Pass it when running the container:\n"
            "\n"
            "    docker run -e ANTHROPIC_API_KEY=sk-ant-... mitm-ai-observability\n"
            "\n"
            "  Option 3 — Mount your Claude credentials file:\n"
            "\n"
            "    The container looks for an API key in ~/.claude/.credentials.json\n"
            "    which is automatically mounted if you use run.sh.\n"
            "\n"
            "Checked locations (all empty):\n"
            "  - ANTHROPIC_API_KEY environment variable\n"
            "  - ~/.claude/.credentials.json (apiKey field)\n"
            "  - ~/.claude/.credentials.json (claudeAiOauth.token field)\n"
        )

    try:
        import httpx as _httpx
        # trust_env=False bypasses HTTP_PROXY/HTTPS_PROXY so the explain call
        # goes directly to api.anthropic.com instead of looping through mitmproxy.
        client = _anthropic_sdk.Anthropic(
            api_key=api_key,
            http_client=_httpx.Client(trust_env=False),
        )
        response = client.messages.create(
            model=_EXPLAIN_MODEL,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text
    except _anthropic_sdk.AuthenticationError:
        return (
            "ERROR: Authentication failed.\n"
            "The API key may be invalid or expired."
        )
    except _anthropic_sdk.RateLimitError:
        return (
            "ERROR: Rate limited by Anthropic API.\n"
            "Wait a moment and try again."
        )
    except Exception as exc:
        log.warning("AI Explain call failed: %s", exc)
        return f"ERROR: API call failed — {type(exc).__name__}: {exc}"


_LOADING_MSG = (
    "GENERATING AI EXPLANATION\n"
    "========================\n"
    "\n"
    "Calling Claude Sonnet to analyze this HTTP transaction...\n"
    "\n"
    "This typically takes 5-10 seconds. Re-select the AI Explain view (select something else and back)\n"
    "to see the result once it is ready.\n"
)


class AIExplainView(contentviews.Contentview):
    name = "AI Explain"
    _pending_keys: set[str] = set()
    _lock = threading.Lock()

    @property
    def syntax_highlight(self) -> contentviews.SyntaxHighlight:
        return "yaml"

    def prettify(self, data: bytes, metadata: contentviews.Metadata) -> str:
        flow = metadata.flow
        if not flow or not hasattr(flow, "request"):
            return "(no flow data available)"

        cache_key = self._cache_key(flow)
        cached = flow.metadata.get("ai_explanation", {})
        if isinstance(cached, dict) and cached.get("key") == cache_key:
            return cached["text"]

        with self._lock:
            if cache_key in self._pending_keys:
                return _LOADING_MSG
            self._pending_keys.add(cache_key)

        def _generate() -> None:
            try:
                prompt = _build_explain_prompt(flow)
                explanation = _call_sonnet(prompt)
                flow.metadata["ai_explanation"] = {"key": cache_key, "text": explanation}
            finally:
                with self._lock:
                    self._pending_keys.discard(cache_key)

        thread = threading.Thread(target=_generate, daemon=True)
        thread.start()
        return _LOADING_MSG

    def render_priority(self, data: bytes, metadata: contentviews.Metadata) -> float:
        return 0

    @staticmethod
    def _cache_key(flow: http.HTTPFlow) -> str:
        h = hashlib.sha256()
        h.update(flow.request.method.encode())
        h.update(flow.request.pretty_url.encode())
        if flow.request.content:
            h.update(flow.request.content[:2048])
        if flow.response and flow.response.content:
            h.update(flow.response.content[:2048])
        return h.hexdigest()[:16]


contentviews.add(AIExplainView)


# ---------------------------------------------------------------------------
# Flow Marker — tags AI flows with icons and summary comments in the flow list
# ---------------------------------------------------------------------------

class AITrafficMarker:
    """Mark AI-related flows with emoji markers and descriptive comments."""

    def request(self, flow: http.HTTPFlow) -> None:
        host = flow.request.pretty_host
        path = flow.request.path

        if not _is_ai_traffic(host, path):
            return

        if "/v1/messages" in path:
            flow.marked = ":robot:"
            try:
                body = json.loads(flow.request.get_text())
                model = body.get("model", "?")
                n_msgs = len(body.get("messages", []))
                n_tools = len(body.get("tools", []))
                max_tok = body.get("max_tokens", "?")
                parts = [f"LLM | {model} | {n_msgs} msgs"]
                if n_tools:
                    parts.append(f"{n_tools} tools")
                if max_tok == 1:
                    parts.append("quota check")
                flow.comment = " | ".join(parts)
            except (json.JSONDecodeError, ValueError):
                flow.comment = "LLM request"

        elif "/v1/mcp/" in path:
            flow.marked = ":globe_with_meridians:"
            server_match = re.search(r"/v1/mcp/(\S+)", path)
            server_id = server_match.group(1) if server_match else "?"
            try:
                body = json.loads(flow.request.get_text())
                method = body.get("method", "?")
                flow.comment = f"MCP | {method} | {server_id}"
            except (json.JSONDecodeError, ValueError):
                flow.comment = f"MCP | {server_id}"

        elif "/api/event_logging/" in path:
            flow.marked = ":bar_chart:"
            try:
                body = json.loads(flow.request.get_text())
                n_events = len(body.get("events", []))
                flow.comment = f"Telemetry | {n_events} events"
            except (json.JSONDecodeError, ValueError):
                flow.comment = "Telemetry"

        elif "/v1/chat/completions" in path:
            flow.marked = ":robot:"
            try:
                body = json.loads(flow.request.get_text())
                model = body.get("model", "?")
                n_msgs = len(body.get("messages", []))
                flow.comment = f"OpenAI | {model} | {n_msgs} msgs"
            except (json.JSONDecodeError, ValueError):
                flow.comment = "OpenAI request"

        elif "googleapis.com" in host:
            flow.marked = ":robot:"
            flow.comment = "Gemini request"

    def response(self, flow: http.HTTPFlow) -> None:
        if not flow.response:
            return
        host = flow.request.pretty_host
        path = flow.request.path

        if not _is_ai_traffic(host, path):
            return

        existing = flow.comment or ""
        ct = flow.response.headers.get("content-type", "")
        status = flow.response.status_code

        if status >= 400:
            flow.marked = ":warning:"
            flow.comment = f"{existing} | HTTP {status}"
            return

        # Enrich LLM responses with token usage
        if "/v1/messages" in path:
            try:
                if "event-stream" in ct:
                    text = flow.response.get_text()
                    usage = self._extract_sse_usage(text)
                    if usage:
                        tok = self._format_token_summary(usage)
                        cost = _estimate_cost(flow.comment.split("|")[1].strip() if "|" in flow.comment else "", usage)
                        parts = [existing, tok]
                        if cost:
                            parts.append(cost)
                        flow.comment = " | ".join(p for p in parts if p)
                else:
                    text = _strip_chunked_encoding(flow.response.get_text())
                    body = json.loads(text)
                    usage = body.get("usage", {})
                    if usage:
                        tok = self._format_token_summary(usage)
                        model = body.get("model", "")
                        cost = _estimate_cost(model, usage)
                        parts = [existing, tok]
                        if cost:
                            parts.append(cost)
                        flow.comment = " | ".join(p for p in parts if p)
            except (json.JSONDecodeError, ValueError):
                pass

        elif "/v1/mcp/" in path:
            try:
                text = _strip_chunked_encoding(flow.response.get_text())
                body = json.loads(text)
                if body.get("type") == "error":
                    err_type = body.get("error", {}).get("type", "?")
                    flow.marked = ":warning:"
                    flow.comment = f"{existing} | error: {err_type}"
            except (json.JSONDecodeError, ValueError):
                pass

    @staticmethod
    def _extract_sse_usage(text: str) -> dict[str, Any]:
        clean = _strip_chunked_encoding(text)
        usage: dict[str, Any] = {}
        for line in clean.split("\n"):
            line = line.strip()
            if not line.startswith("data: "):
                continue
            try:
                evt = json.loads(line[6:].strip())
            except json.JSONDecodeError:
                continue
            if evt.get("type") == "message_start":
                usage = evt.get("message", {}).get("usage", {})
            elif evt.get("type") == "message_delta" and evt.get("usage"):
                usage = {**usage, **{k: v for k, v in evt["usage"].items() if v}}
        return usage

    @staticmethod
    def _format_token_summary(usage: dict[str, Any]) -> str:
        inp = usage.get("input_tokens", 0)
        out = usage.get("output_tokens", 0)
        cache_r = usage.get("cache_read_input_tokens", 0)
        parts = [f"in={inp}", f"out={out}"]
        if cache_r:
            parts.append(f"cached={cache_r}")
        return " ".join(parts)


addons = [AITrafficMarker()]
