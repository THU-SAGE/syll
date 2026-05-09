"""Regression tests for DeepSeek thinking-mode tool-call turns."""

import asyncio
import builtins
import importlib
import sys
from types import SimpleNamespace

from syll.agent.context import ContextBuilder
from syll.providers.litellm_provider import LiteLLMProvider
from syll.session.manager import Session
from syll.web.streaming import process_streaming


def test_litellm_provider_import_and_construction_are_lazy(monkeypatch):
    """Constructing the provider should not import the heavy LiteLLM package."""
    monkeypatch.delitem(sys.modules, "syll.providers.litellm_provider", raising=False)
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "litellm" or name.startswith("litellm."):
            raise AssertionError("litellm should not be imported during provider construction")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    module = importlib.import_module("syll.providers.litellm_provider")
    provider = module.LiteLLMProvider(
        api_key="sk-test",
        api_base="https://api.deepseek.com",
        default_model="deepseek/deepseek-v4-pro",
    )

    assert provider.api_key == "sk-test"
    assert provider.api_base == "https://api.deepseek.com"
    assert provider.default_model == "deepseek/deepseek-v4-pro"


def test_litellm_preload_can_import_heavy_module_before_first_chat(monkeypatch):
    """Wake can warm LiteLLM without making the first chat pay import cost."""
    import syll.providers.litellm_provider as litellm_provider

    calls: list[str] = []
    monkeypatch.setattr(litellm_provider, "_litellm_module", None)
    monkeypatch.setattr(
        litellm_provider,
        "_get_litellm",
        lambda: calls.append("imported") or object(),
    )

    litellm_provider.preload_litellm()

    assert calls == ["imported"]


def test_litellm_response_preserves_reasoning_content():
    """DeepSeek thinking responses must keep reasoning_content for follow-up calls."""
    provider = LiteLLMProvider(default_model="deepseek/deepseek-v4-pro")
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content="",
                    reasoning_content="I should call a weather tool.",
                    tool_calls=[
                        SimpleNamespace(
                            id="call_weather",
                            function=SimpleNamespace(
                                name="weather",
                                arguments='{"city":"Beijing"}',
                            ),
                        )
                    ],
                ),
                finish_reason="tool_calls",
            )
        ],
        usage=None,
    )

    parsed = provider._parse_response(response)

    assert parsed.provider_extra["reasoning_content"] == "I should call a weather tool."


def test_add_assistant_message_can_round_trip_reasoning_content(tmp_path):
    """Assistant tool-call messages can carry DeepSeek reasoning_content back."""
    context = ContextBuilder(tmp_path)
    messages: list[dict] = []

    context.add_assistant_message(
        messages,
        "",
        [
            {
                "id": "call_weather",
                "type": "function",
                "function": {"name": "weather", "arguments": "{}"},
            }
        ],
        reasoning_content="I should call a weather tool.",
    )

    assert messages == [
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": "I should call a weather tool.",
            "tool_calls": [
                {
                    "id": "call_weather",
                    "type": "function",
                    "function": {"name": "weather", "arguments": "{}"},
                }
            ],
        }
    ]


def test_session_history_preserves_reasoning_content_for_deepseek_replay():
    """Saved tool turns must remain valid when replayed on the next request."""
    session = Session(key="cli:dashboard")
    session.add_message(
        "assistant",
        "",
        reasoning_content="I should call a weather tool.",
        tool_calls=[
            {
                "id": "call_weather",
                "type": "function",
                "function": {"name": "weather", "arguments": "{}"},
            }
        ],
    )

    history = session.get_history()

    assert history == [
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": "I should call a weather tool.",
            "tool_calls": [
                {
                    "id": "call_weather",
                    "type": "function",
                    "function": {"name": "weather", "arguments": "{}"},
                }
            ],
        }
    ]


def test_session_history_drops_orphan_tool_message_for_deepseek_replay():
    """Old/bad sessions may contain tool rows without a preceding tool_call."""
    session = Session(key="cli:dashboard")
    session.add_message("user", "hello")
    session.add_message(
        "tool",
        "weather result",
        tool_call_id="call_weather",
        name="weather",
    )
    session.add_message("assistant", "done")

    history = session.get_history()

    assert history == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "done"},
    ]


def test_session_history_keeps_tool_message_when_preceded_by_matching_tool_call():
    """Valid tool turns should still replay with their assistant tool_calls."""
    session = Session(key="cli:dashboard")
    session.add_message(
        "assistant",
        "",
        tool_calls=[
            {
                "id": "call_weather",
                "type": "function",
                "function": {"name": "weather", "arguments": "{}"},
            }
        ],
    )
    session.add_message(
        "tool",
        "weather result",
        tool_call_id="call_weather",
        name="weather",
    )
    session.add_message("assistant", "done")

    history = session.get_history()

    assert history == [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_weather",
                    "type": "function",
                    "function": {"name": "weather", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "content": "weather result",
            "tool_call_id": "call_weather",
            "name": "weather",
        },
        {"role": "assistant", "content": "done"},
    ]


def test_litellm_streaming_tool_calls_include_reasoning_content(monkeypatch):
    """Streaming DeepSeek reasoning deltas should be attached to tool calls."""

    async def fake_acompletion(**kwargs):
        async def stream():
            yield SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            content=None,
                            reasoning_content="I should ",
                            tool_calls=None,
                        ),
                        finish_reason=None,
                    )
                ]
            )
            yield SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            content=None,
                            reasoning_content="call a weather tool.",
                            tool_calls=[
                                SimpleNamespace(
                                    index=0,
                                    id="call_weather",
                                    function=SimpleNamespace(
                                        name="weather",
                                        arguments='{"city"',
                                    ),
                                )
                            ],
                        ),
                        finish_reason=None,
                    )
                ]
            )
            yield SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            content=None,
                            reasoning_content=None,
                            tool_calls=[
                                SimpleNamespace(
                                    index=0,
                                    id=None,
                                    function=SimpleNamespace(
                                        name=None,
                                        arguments=':"Beijing"}',
                                    ),
                                )
                            ],
                        ),
                        finish_reason=None,
                    )
                ]
            )
            yield SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            content=None,
                            reasoning_content=None,
                            tool_calls=None,
                        ),
                        finish_reason="tool_calls",
                    )
                ]
            )

        return stream()

    import syll.providers.litellm_provider as litellm_provider

    monkeypatch.setattr(
        litellm_provider,
        "_get_litellm",
        lambda: SimpleNamespace(api_base=None, suppress_debug_info=False),
    )
    monkeypatch.setattr(litellm_provider, "acompletion", fake_acompletion)
    provider = litellm_provider.LiteLLMProvider(default_model="deepseek/deepseek-v4-pro")

    events = asyncio.run(_collect_events(provider.chat_stream(messages=[])))

    tool_event = next(event for event in events if event["type"] == "tool_calls")
    assert tool_event["reasoning_content"] == "I should call a weather tool."
    assert tool_event["calls"] == [
        {
            "id": "call_weather",
            "name": "weather",
            "arguments": {"city": "Beijing"},
        }
    ]


def test_streaming_tool_call_passes_reasoning_content_to_assistant_message(monkeypatch):
    """Web streaming should preserve DeepSeek reasoning for the tool-call replay."""
    seen: dict[str, object] = {"assistant_messages": []}

    class _Session:
        def get_history(self):
            return []

        def add_message(self, role, content, **extras):
            pass

    class _Sessions:
        def get_or_create(self, session_key):
            return _Session()

        def save(self, session):
            pass

    class _Context:
        memory = SimpleNamespace(append_today=lambda summary: None)

        def build_messages(self, **kwargs):
            return []

        def add_assistant_message(
            self,
            messages,
            content,
            tool_calls,
            *,
            reasoning_content=None,
        ):
            seen["assistant_messages"].append({
                "content": content,
                "tool_calls": tool_calls,
                "reasoning_content": reasoning_content,
            })
            messages.append({"role": "assistant", "content": content or ""})
            return messages

        def add_tool_result(self, messages, tool_call_id, tool_name, result):
            messages.append({"role": "tool", "content": str(result)})
            return messages

    class _Provider:
        def __init__(self):
            self.calls = 0

        async def chat_stream(self, messages, tools, model):
            self.calls += 1
            if self.calls == 1:
                yield {
                    "type": "tool_calls",
                    "calls": [
                        {
                            "id": "call_weather",
                            "name": "weather",
                            "arguments": {"city": "Beijing"},
                        }
                    ],
                    "reasoning_content": "I should call a weather tool.",
                }
                yield {"type": "done"}
            else:
                yield {"type": "token", "content": "晴"}
                yield {"type": "done"}

    class _Tools:
        def get_definitions(self):
            return []

        async def execute(self, name, arguments):
            return "weather result"

    class _EventStore:
        def log_event(self, event):
            pass

    monkeypatch.setattr("syll.web.streaming.inject_skill_hint", lambda agent_loop, text: text)
    agent_loop = SimpleNamespace(
        sessions=_Sessions(),
        context=_Context(),
        provider=_Provider(),
        tools=_Tools(),
        model="deepseek/deepseek-v4-pro",
        max_iterations=2,
        event_store=_EventStore(),
    )

    events = asyncio.run(
        _collect_events(process_streaming(agent_loop, "查天气", "web:test"))
    )

    assert events[-1]["type"] == "done"
    assert seen["assistant_messages"][0]["reasoning_content"] == (
        "I should call a weather tool."
    )


async def _collect_events(generator):
    return [event async for event in generator]
