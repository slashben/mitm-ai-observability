#!/usr/bin/env python3
"""Smoke test for ai_contentview parsers using extracted sample data."""

import json
import sys
import os
import types

# Stub out mitmproxy so we can import the addon without installing mitmproxy
_cv_mod = types.ModuleType("mitmproxy.contentviews")
_cv_mod.Contentview = type("Contentview", (), {})
_cv_mod.Metadata = type("Metadata", (), {})
_cv_mod.SyntaxHighlight = str
_cv_mod.add = lambda *a, **kw: None
sys.modules["mitmproxy"] = types.ModuleType("mitmproxy")
sys.modules["mitmproxy.contentviews"] = _cv_mod

sys.path.insert(0, os.path.dirname(__file__))

from ai_contentview import (
    _format_anthropic_request,
    _format_anthropic_json_response,
    _format_anthropic_sse,
    _format_mcp,
    _format_telemetry,
)

SAMPLE = os.path.join(os.path.dirname(__file__), "..", "sample.txt")


def load_sample():
    with open(SAMPLE) as f:
        return f.read()


def extract_blocks(raw: str) -> list[dict]:
    """Parse the sample.txt into request/response pairs."""
    blocks = []
    lines = raw.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("POST ") or line.startswith("GET "):
            method_path = line.split(" ")[1]
            host = ""
            # Read headers until blank line
            i += 1
            headers = {}
            while i < len(lines) and lines[i].strip():
                if ":" in lines[i]:
                    k, v = lines[i].split(":", 1)
                    headers[k.strip().lower()] = v.strip()
                i += 1
            host = headers.get("host", "")
            i += 1  # skip blank line
            # Read body until next HTTP/ or POST or end
            body_lines = []
            while i < len(lines):
                if lines[i].startswith("HTTP/") or lines[i].startswith("POST ") or lines[i].startswith("GET "):
                    break
                body_lines.append(lines[i])
                i += 1
            blocks.append({
                "type": "request",
                "path": method_path,
                "host": host,
                "headers": headers,
                "body": "\n".join(body_lines).strip(),
            })
        elif line.startswith("HTTP/"):
            status = line
            i += 1
            headers = {}
            while i < len(lines) and lines[i].strip():
                if ":" in lines[i]:
                    k, v = lines[i].split(":", 1)
                    headers[k.strip().lower()] = v.strip()
                i += 1
            ct = headers.get("content-type", "")
            i += 1  # skip blank
            body_lines = []
            while i < len(lines):
                if lines[i].startswith("HTTP/") or lines[i].startswith("POST ") or lines[i].startswith("GET "):
                    break
                body_lines.append(lines[i])
                i += 1
            blocks.append({
                "type": "response",
                "status": status,
                "headers": headers,
                "content_type": ct,
                "body": "\n".join(body_lines).strip(),
            })
        else:
            i += 1
    return blocks


def test_all():
    raw = load_sample()
    blocks = extract_blocks(raw)
    
    passed = 0
    failed = 0
    
    for idx, block in enumerate(blocks):
        host = block.get("host", "")
        path = block.get("path", "")
        ct = block.get("content_type", "")
        body = block.get("body", "")
        
        if not body:
            continue
        
        try:
            if block["type"] == "request":
                if "anthropic.com" in host and "/v1/messages" in path:
                    parsed = json.loads(body)
                    result = _format_anthropic_request(parsed)
                    assert "model:" in result, "Missing model in LLM request"
                    assert "messages:" in result, "Missing messages"
                    print(f"[PASS] Block {idx}: Anthropic request ({path[:50]})")
                    print(f"       Model: {parsed.get('model')}, messages: {len(parsed.get('messages', []))}")
                    print(f"       Output length: {len(result)} chars")
                    print()
                    passed += 1
                    
                elif "anthropic.com" in host and "/v1/mcp/" in path:
                    parsed = json.loads(body)
                    result = _format_mcp(parsed, path)
                    assert "method:" in result or "type:" in result or "jsonrpc" in result, "Missing MCP fields"
                    print(f"[PASS] Block {idx}: MCP request ({path[:50]})")
                    print(f"       Method: {parsed.get('method', 'N/A')}")
                    print()
                    passed += 1
                    
                elif "anthropic.com" in host and "/api/event_logging/" in path:
                    parsed = json.loads(body)
                    result = _format_telemetry(parsed)
                    assert "telemetry_batch:" in result, "Missing telemetry header"
                    events = parsed.get("events", [])
                    print(f"[PASS] Block {idx}: Telemetry ({len(events)} events)")
                    print(f"       Output length: {len(result)} chars")
                    print()
                    passed += 1
                    
            elif block["type"] == "response":
                if "event-stream" in ct:
                    result = _format_anthropic_sse(body)
                    assert "model:" in result, "Missing model in SSE response"
                    print(f"[PASS] Block {idx}: Anthropic SSE response")
                    # Extract model from output
                    for line in result.split("\n"):
                        if line.startswith("model:"):
                            print(f"       {line}")
                        elif line.startswith("stop_reason:"):
                            print(f"       {line}")
                        elif "usage:" == line.strip():
                            pass
                        elif line.strip().startswith("estimated_cost:"):
                            print(f"       {line.strip()}")
                        elif line.strip().startswith("input_tokens:"):
                            print(f"       {line.strip()}")
                        elif line.strip().startswith("output_tokens:"):
                            print(f"       {line.strip()}")
                    print()
                    passed += 1
                    
                elif "application/json" in ct:
                    # Try to parse after stripping chunked markers
                    import re
                    clean = re.sub(r"^[0-9a-fA-F]+\r?\n", "", body, flags=re.MULTILINE)
                    # Remove trailing "0" chunk marker
                    clean = clean.strip()
                    if clean.endswith("\n0"):
                        clean = clean[:-2].strip()
                    elif clean == "0":
                        continue
                    try:
                        parsed = json.loads(clean)
                    except json.JSONDecodeError:
                        continue
                    
                    if "anthropic.com" in str(blocks[max(0,idx-1)].get("host", "")):
                        if parsed.get("type") == "error":
                            result = _format_mcp(parsed)
                            assert "error" in result, "Missing error in MCP response"
                            print(f"[PASS] Block {idx}: MCP error response")
                            print(f"       Error: {parsed['error'].get('type', '?')}")
                            print()
                            passed += 1
                        elif "model" in parsed:
                            result = _format_anthropic_json_response(parsed)
                            assert "model:" in result, "Missing model in JSON response"
                            print(f"[PASS] Block {idx}: Anthropic JSON response")
                            print(f"       Model: {parsed.get('model')}")
                            print()
                            passed += 1

        except Exception as e:
            print(f"[FAIL] Block {idx}: {e}")
            failed += 1
    
    print("=" * 60)
    print(f"Results: {passed} passed, {failed} failed")
    
    if failed:
        sys.exit(1)
    
    # Print a sample of formatted output
    print("\n" + "=" * 60)
    print("SAMPLE OUTPUT: Anthropic SSE response")
    print("=" * 60)
    for block in blocks:
        if block["type"] == "response" and "event-stream" in block.get("content_type", ""):
            result = _format_anthropic_sse(block["body"])
            print(result)
            break
    
    print("\n" + "=" * 60)
    print("SAMPLE OUTPUT: Anthropic JSON response (quota check)")
    print("=" * 60)
    for block in blocks:
        if block["type"] == "response" and "application/json" in block.get("content_type", ""):
            import re
            clean = re.sub(r"^[0-9a-fA-F]+\r?\n", "", block["body"], flags=re.MULTILINE).strip()
            if clean.endswith("\n0"):
                clean = clean[:-2].strip()
            try:
                parsed = json.loads(clean)
                if "model" in parsed:
                    result = _format_anthropic_json_response(parsed)
                    print(result)
                    break
            except json.JSONDecodeError:
                continue

    print("\n" + "=" * 60)
    print("SAMPLE OUTPUT: MCP request")
    print("=" * 60)
    for block in blocks:
        if block["type"] == "request" and "/v1/mcp/" in block.get("path", ""):
            parsed = json.loads(block["body"])
            result = _format_mcp(parsed, block["path"])
            print(result)
            break

    print("\n" + "=" * 60)
    print("SAMPLE OUTPUT: Telemetry (first batch)")
    print("=" * 60)
    for block in blocks:
        if block["type"] == "request" and "/api/event_logging/" in block.get("path", ""):
            parsed = json.loads(block["body"])
            result = _format_telemetry(parsed)
            print(result)
            break


if __name__ == "__main__":
    test_all()
