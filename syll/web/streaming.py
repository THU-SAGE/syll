"""Streaming chat processor for WebSocket connections."""

import base64
import json
import mimetypes
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from loguru import logger

from syll.agent.tools.base import ToolResult
from syll.web.skill_router import inject_skill_hint


def _encode_media(media_paths: list[str]) -> list[dict[str, str]]:
    """Encode media file paths to base64 dicts for WebSocket transmission.

    Falls back to ``application/octet-stream`` when the extension is
    unknown — an ``image/png`` fallback would mis-label audio files
    (e.g. ``.mp3`` → ``audio/mpeg``) produced by the ``speak`` tool and
    cause the frontend to try rendering them as ``<img>``.
    """
    result = []
    for p in media_paths:
        fp = Path(p)
        if fp.is_file():
            mime = mimetypes.guess_type(p)[0] or "application/octet-stream"
            b64 = base64.b64encode(fp.read_bytes()).decode()
            result.append({"mime": mime, "data": b64})
    return result


async def process_streaming(
    agent_loop,
    content: str,
    session_key: str,
    media: list[str] | None = None,
) -> AsyncGenerator[dict[str, Any], None]:
    """Process a message with streaming output.

    Yields event dicts for each step:
      {"type": "status", "content": "thinking"}
      {"type": "token", "content": "partial text"}
      {"type": "tool_call", "name": "...", "arguments": {...}}
      {"type": "tool_result", "name": "...", "content": "...", "media": [...]}
      {"type": "done", "content": "full reply", "media": [...]}
      {"type": "error", "content": "error message"}
    """
    yield {"type": "status", "content": "thinking"}

    try:
        # Build message and session (reuse agent_loop internals)
        chat_id = session_key.split(":", 1)[-1] if ":" in session_key else "default"
        raw_content = content
        content = inject_skill_hint(agent_loop, content)

        session = agent_loop.sessions.get_or_create(session_key)
        messages = agent_loop.context.build_messages(
            history=session.get_history(),
            current_message=content,
            media=media,
            channel="web",
            chat_id=chat_id,
            language_hint_text=raw_content,
        )

        iteration = 0
        final_content = None
        collected_media: list[str] = []
        # Records every assistant-with-tools and tool-result message so we can
        # persist the full turn into the session JSONL, not just the final reply.
        turn_events: list[dict[str, Any]] = []

        while iteration < agent_loop.max_iterations:
            iteration += 1

            # Try streaming if provider supports it
            provider = agent_loop.provider
            if hasattr(provider, "chat_stream"):
                accumulated_content = ""
                tool_calls_data: list[dict] = []

                async for chunk in provider.chat_stream(
                    messages=messages,
                    tools=agent_loop.tools.get_definitions(),
                    model=agent_loop.model,
                ):
                    if chunk["type"] == "token":
                        accumulated_content += chunk["content"]
                        yield {"type": "token", "content": chunk["content"]}
                    elif chunk["type"] == "tool_calls":
                        tool_calls_data = chunk["calls"]
                    elif chunk["type"] == "done":
                        pass

                # Process tool calls if any
                if tool_calls_data:
                    tool_call_dicts = [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {
                                "name": tc["name"],
                                "arguments": json.dumps(tc["arguments"]),
                            },
                        }
                        for tc in tool_calls_data
                    ]
                    messages = agent_loop.context.add_assistant_message(
                        messages, accumulated_content or None, tool_call_dicts
                    )
                    turn_events.append({
                        "role": "assistant",
                        "content": accumulated_content or "",
                        "tool_calls": tool_call_dicts,
                    })

                    for tc in tool_calls_data:
                        yield {
                            "type": "tool_call",
                            "name": tc["name"],
                            "arguments": tc["arguments"],
                        }
                        result = await agent_loop.tools.execute(tc["name"], tc["arguments"])
                        result_text = result.text if isinstance(result, ToolResult) else str(result)
                        tool_media: list[dict[str, str]] = []
                        if isinstance(result, ToolResult) and result.media:
                            collected_media.extend(result.media)
                            tool_media = _encode_media(result.media)
                        messages = agent_loop.context.add_tool_result(
                            messages, tc["id"], tc["name"], result
                        )
                        turn_events.append({
                            "role": "tool",
                            "content": result_text[:2000],
                            "tool_call_id": tc["id"],
                            "name": tc["name"],
                        })
                        yield {
                            "type": "tool_result",
                            "name": tc["name"],
                            "content": result_text[:2000],
                            "media": tool_media,
                        }
                else:
                    final_content = accumulated_content
                    break
            else:
                # Fallback: non-streaming
                response = await provider.chat(
                    messages=messages,
                    tools=agent_loop.tools.get_definitions(),
                    model=agent_loop.model,
                )

                if response.has_tool_calls:
                    tool_call_dicts = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                "arguments": json.dumps(tc.arguments),
                            },
                        }
                        for tc in response.tool_calls
                    ]
                    messages = agent_loop.context.add_assistant_message(
                        messages, response.content, tool_call_dicts
                    )
                    turn_events.append({
                        "role": "assistant",
                        "content": response.content or "",
                        "tool_calls": tool_call_dicts,
                    })

                    for tc in response.tool_calls:
                        yield {
                            "type": "tool_call",
                            "name": tc.name,
                            "arguments": tc.arguments,
                        }
                        result = await agent_loop.tools.execute(tc.name, tc.arguments)
                        result_text = result.text if isinstance(result, ToolResult) else str(result)
                        tool_media = []
                        if isinstance(result, ToolResult) and result.media:
                            collected_media.extend(result.media)
                            tool_media = _encode_media(result.media)
                        messages = agent_loop.context.add_tool_result(
                            messages, tc.id, tc.name, result
                        )
                        turn_events.append({
                            "role": "tool",
                            "content": result_text[:2000],
                            "tool_call_id": tc.id,
                            "name": tc.name,
                        })
                        yield {
                            "type": "tool_result",
                            "name": tc.name,
                            "content": result_text[:2000],
                            "media": tool_media,
                        }
                else:
                    final_content = response.content
                    if final_content:
                        yield {"type": "token", "content": final_content}
                    break

        if final_content is None:
            final_content = "I've completed processing but have no response to give."

        # Save to session — replay full turn so reload preserves tool_calls.
        session.add_message("user", raw_content)
        for ev in turn_events:
            ev_role = ev["role"]
            ev_content = ev["content"]
            ev_extras = {k: v for k, v in ev.items() if k not in ("role", "content")}
            session.add_message(ev_role, ev_content, **ev_extras)
        session.add_message("assistant", final_content)
        agent_loop.sessions.save(session)

        # Log event to event store
        from syll.agent.events import Event, EventContent, EventSource
        event = Event(
            agent_type="im_agent",
            event_type="message",
            source=EventSource(
                platform="web",
                chat_id=chat_id,
                user_id=session_key,
            ),
            content=EventContent(
                text=f"User: {raw_content}\nAssistant: {final_content}",
                media=collected_media,
                metadata={
                    "iterations": iteration,
                    "session_key": session_key,
                },
            ),
        )
        agent_loop.event_store.log_event(event)

        # Append to daily memory
        try:
            from datetime import datetime as _dt
            summary = f"- [{_dt.now().strftime('%H:%M')}] User: {raw_content[:100]}\n"
            agent_loop.context.memory.append_today(summary)
        except Exception as e:
            logger.debug(f"Failed to append daily memory: {e}")

        # Encode collected media for done event
        done_media = _encode_media(collected_media) if collected_media else []
        yield {"type": "done", "content": final_content, "media": done_media}

    except Exception as e:
        logger.error(f"Streaming error: {e}")
        yield {"type": "error", "content": str(e)}
