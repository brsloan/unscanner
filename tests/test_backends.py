"""Backend request/response plumbing with mocked transports (no network)."""

from __future__ import annotations

import json

import anthropic
import httpx
import httpx2

from remediate.backends.anthropic_backend import AnthropicBackend
from remediate.backends.openai_compat import OpenAICompatBackend
from remediate.prompts import GUIDELINES

RESULT = {"label": "12", "skip": False, "starts_mid_paragraph": False, "ends_mid_paragraph": True,
          "html": "<p>Hello</p>", "figures": [], "notes": ""}


def test_openai_compat_request_shape():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "```json\n" + json.dumps(RESULT) + "\n```"},
                                                      "finish_reason": "stop"}],
                                         "usage": {"prompt_tokens": 100, "completion_tokens": 20}, "model": "m"})

    be = OpenAICompatBackend(model="qwen2.5vl:7b", base_url="http://ollama.local:11434/v1")
    be.client = httpx.Client(transport=httpx.MockTransport(handler))
    result, usage = be.transcribe(b"\x89PNG", "This is PDF page 3 of 9.")
    assert seen["url"] == "http://ollama.local:11434/v1/chat/completions"
    body = seen["body"]
    assert body["model"] == "qwen2.5vl:7b"
    assert body["messages"][0] == {"role": "system", "content": GUIDELINES}
    assert body["messages"][1]["content"][0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert body["response_format"] == {"type": "json_object"}
    assert result["label"] == "12" and result["ends_mid_paragraph"] is True
    assert usage["input_tokens"] == 100


def test_anthropic_request_shape():
    seen = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        seen["beta"] = request.headers.get("anthropic-beta", "")
        return httpx2.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5",
            "content": [{"type": "text", "text": json.dumps(RESULT)}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 3000, "output_tokens": 900, "cache_read_input_tokens": 1200,
                      "cache_creation_input_tokens": 0},
        })

    client = anthropic.Anthropic(api_key="test", http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
                                 max_retries=0)
    be = AnthropicBackend(model="claude-opus-5", effort="low", client=client)
    result, usage = be.transcribe(b"\x89PNG", "This is PDF page 1 of 2.")
    body = seen["body"]
    assert seen["path"].endswith("/v1/messages")
    assert "server-side-fallback-2026-07-01" in seen["beta"] and body["fallbacks"] == "default"
    assert body["model"] == "claude-opus-5"
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert body["messages"][0]["content"][0]["type"] == "image"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["output_config"]["effort"] == "low"
    assert "thinking" not in body  # adaptive thinking is the default on Opus 5
    assert result["html"] == "<p>Hello</p>"
    assert usage["cache_read_input_tokens"] == 1200


def test_anthropic_haiku_omits_effort_and_fallbacks():
    seen = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["body"] = json.loads(request.content)
        seen["beta"] = request.headers.get("anthropic-beta", "")
        return httpx2.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-haiku-4-5",
            "content": [{"type": "text", "text": json.dumps(RESULT)}], "stop_reason": "end_turn",
            "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}})

    client = anthropic.Anthropic(api_key="test", http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
                                 max_retries=0)
    be = AnthropicBackend(model="claude-haiku-4-5", client=client)
    be.transcribe(b"\x89PNG", "This is PDF page 1 of 1.")
    assert "effort" not in seen["body"]["output_config"]
    assert "fallbacks" not in seen["body"] and "server-side-fallback" not in seen["beta"]
