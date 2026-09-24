"""Backend request/response plumbing with mocked transports (no network)."""

from __future__ import annotations

import json

import anthropic
import httpx
import pytest
import httpx2

from unscanner.backends.anthropic_backend import AnthropicBackend
from unscanner.backends.base import BackendError
from unscanner.backends.openai_compat import OpenAICompatBackend
from unscanner.prompts import GUIDELINES

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
    # a library's edited guidelines (prompts.guidelines(work)) replace the built-in system prompt
    be.transcribe(b"\x89PNG", "This is PDF page 3 of 9.", system="CONTEXT: ours")
    assert seen["body"]["messages"][0] == {"role": "system", "content": "CONTEXT: ours"}


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
    assert body["system"][0]["text"] == GUIDELINES
    be.transcribe(b"\x89PNG", "This is PDF page 1 of 2.", system="CONTEXT: ours")
    assert seen["body"]["system"][0] == {"type": "text", "text": "CONTEXT: ours", "cache_control": {"type": "ephemeral"}}


def test_anthropic_missing_key_is_a_clear_backend_error(monkeypatch):
    import pytest

    from unscanner.backends.base import BackendError

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    be = AnthropicBackend(model="claude-haiku-4-5", client=anthropic.Anthropic(api_key=None, max_retries=0))
    with pytest.raises(BackendError, match="API key"):
        be.describe_image(b"\x89PNG", "alt text please")
    with pytest.raises(BackendError, match="API key"):
        be.transcribe(b"\x89PNG", "This is PDF page 1 of 1.")


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


def test_openai_compat_accepts_full_endpoint_url():
    from unscanner.backends.openai_compat import normalize_base_url
    assert normalize_base_url("https://genai.example.edu/api/chat/completions") == "https://genai.example.edu/api"
    assert normalize_base_url("https://genai.example.edu/api/chat/completions/") == "https://genai.example.edu/api"
    assert normalize_base_url("http://localhost:11434/v1/") == "http://localhost:11434/v1"
    be = OpenAICompatBackend(model="m", base_url="https://genai.example.edu/api/chat/completions")
    assert be.base_url == "https://genai.example.edu/api"


def _ok_response():
    return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(RESULT)}, "finish_reason": "stop"}],
                                     "usage": {"prompt_tokens": 1, "completion_tokens": 1}, "model": "m"})


def test_openai_compat_disables_thinking_by_default():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return _ok_response()

    be = OpenAICompatBackend(model="qwen3.6:27b", base_url="http://ollama.local:11434/v1")
    be.client = httpx.Client(transport=httpx.MockTransport(handler))
    be.transcribe(b"\x89PNG", "p")
    assert seen[0]["chat_template_kwargs"] == {"enable_thinking": False}
    assert seen[0]["reasoning_effort"] == "none"

    be = OpenAICompatBackend(model="qwen3.6:27b", base_url="http://ollama.local:11434/v1", disable_thinking=False)
    be.client = httpx.Client(transport=httpx.MockTransport(handler))
    be.transcribe(b"\x89PNG", "p")
    assert "chat_template_kwargs" not in seen[1] and "reasoning_effort" not in seen[1]


def test_openai_compat_drops_thinking_fields_when_server_rejects_them():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if "chat_template_kwargs" in body:
            return httpx.Response(400, json={"error": "unknown field chat_template_kwargs"})
        return _ok_response()

    be = OpenAICompatBackend(model="m", base_url="http://ollama.local:11434/v1")
    be.client = httpx.Client(transport=httpx.MockTransport(handler))
    result, _ = be.transcribe(b"\x89PNG", "p")
    assert result["label"] == "12"
    assert len(seen) == 2 and "reasoning_effort" not in seen[1]
    be.transcribe(b"\x89PNG", "p")
    assert len(seen) == 3 and "chat_template_kwargs" not in seen[2]  # remembered for later pages


def test_openai_compat_explains_reasoning_exhaustion():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "", "reasoning_content": "Let me think..."},
                                                      "finish_reason": "length"}], "usage": {}})

    be = OpenAICompatBackend(model="m", base_url="http://ollama.local:11434/v1")
    be.client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(BackendError, match="reasoning"):
        be.transcribe(b"\x89PNG", "p")


def test_openai_compat_describe_image_budget_and_unreachable_host():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            return httpx.Response(200, json={"choices": [{"message": {"content": " <table></table> "}, "finish_reason": "stop"}]})
        raise httpx.ConnectTimeout("timed out")

    be = OpenAICompatBackend(model="m", base_url="http://ollama.local:11434/v1")
    be.client = httpx.Client(transport=httpx.MockTransport(handler))
    assert be.describe_image(b"\x89PNG", "table please", max_tokens=4000) == "<table></table>"
    assert calls[0]["max_tokens"] == 4000
    # a person is waiting on this: an unreachable endpoint fails at once, and says what to check
    with pytest.raises(BackendError, match="cannot reach http://ollama.local:11434/v1"):
        be.describe_image(b"\x89PNG", "alt text please")
    assert len(calls) == 2 and calls[1]["max_tokens"] == 600


def test_openai_compat_retries_rate_limits(monkeypatch):
    import unscanner.backends.openai_compat as oc

    monkeypatch.setattr(oc.time, "sleep", lambda s: None)
    n = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        n["calls"] += 1
        if n["calls"] == 1:
            return httpx.Response(400, json={"detail": "Rate limit exceeded. Please try again later."})
        if n["calls"] == 2:
            return httpx.Response(429, text="slow down")
        return _ok_response()

    be = OpenAICompatBackend(model="m", base_url="http://ollama.local:11434/v1")
    be.client = httpx.Client(transport=httpx.MockTransport(handler))
    result, _ = be.transcribe(b"\x89PNG", "p")
    assert result["label"] == "12" and n["calls"] == 3


def test_openai_compat_gives_up_after_persistent_rate_limit(monkeypatch):
    import unscanner.backends.openai_compat as oc

    monkeypatch.setattr(oc.time, "sleep", lambda s: None)
    n = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        n["calls"] += 1
        return httpx.Response(429, text="slow down")

    be = OpenAICompatBackend(model="m", base_url="http://ollama.local:11434/v1")
    be.client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(BackendError, match="rate limited"):
        be.transcribe(b"\x89PNG", "p")
    assert n["calls"] == oc.MAX_ATTEMPTS


def test_dot_leaders_are_collapsed():
    from unscanner.prompts import collapse_dot_leaders, normalize_result

    assert collapse_dot_leaders("<li>Allen, David....................898</li>") == "<li>Allen, David 898</li>"
    assert collapse_dot_leaders("<li>Allen, David . . . . . . . 898</li>") == "<li>Allen, David 898</li>"
    assert collapse_dot_leaders("<p>Wait... no. The end.</p>") == "<p>Wait... no. The end.</p>"
    r = normalize_result({"html": "<p>Entry.........12</p>"})
    assert r["html"] == "<p>Entry 12</p>"


def test_openai_compat_retries_truncated_page_with_sampling():
    """A model looping under greedy decoding hits the budget; the page is asked for again, warmer."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        if len(seen) == 1:
            return httpx.Response(200, json={"choices": [{"message": {"content": '{"html": "<p>a a a a'},
                                                          "finish_reason": "length"}], "usage": {}})
        return _ok_response()

    be = OpenAICompatBackend(model="m", base_url="http://ollama.local:11434/v1", max_tokens=24000)
    be.client = httpx.Client(transport=httpx.MockTransport(handler))
    result, _ = be.transcribe(b"\x89PNG", "p")
    assert result["label"] == "12"
    assert [b["temperature"] for b in seen] == [0.0, 0.3]
    assert seen[0]["max_tokens"] == 24000


def test_openai_compat_reports_truncation_after_retries():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"html": "<table>'},
                                                      "finish_reason": "length"}], "usage": {}})

    be = OpenAICompatBackend(model="m", base_url="http://ollama.local:11434/v1", max_tokens=24000)
    be.client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(BackendError, match="24000 tokens.*repeating itself"):
        be.transcribe(b"\x89PNG", "p")
    assert len(seen) == 3
