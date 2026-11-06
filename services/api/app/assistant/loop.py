"""The tool-calling loop (spec 10.3).

Tool rounds use non-streaming completions because the full message has to be
parsed for tool calls anyway; only the final answer is streamed to the browser.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.assistant import prompts
from app.assistant.tools import ToolContext, ToolExecutionError, execute, openai_tool_schemas
from app.config import settings
from app.db.models import AssistantMessage
from app.db.models.enums import MessageRole
from app.errors import ServiceUnavailableError

log = logging.getLogger(__name__)

HISTORY_LIMIT = 10


@dataclass
class ToolInvocation:
    name: str
    arguments: dict[str, Any]
    result: dict[str, Any] | None = None
    error: str | None = None


@dataclass
class LoopOutcome:
    messages: list[dict[str, Any]]
    invocations: list[ToolInvocation] = field(default_factory=list)
    hit_round_cap: bool = False
    # The answer text from the round that asked for no further tools. Holding
    # it here is what stops the final turn being generated twice: the loop has
    # already paid for this generation, so the stream replays it rather than
    # asking the model to continue a conversation that already ends with its
    # own reply - which produced an empty answer.
    answer: str | None = None


def _extract_tool_calls(message: Any) -> list[Any]:
    return list(getattr(message, "tool_calls", None) or [])


def _parse_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ToolExecutionError(f"arguments were not valid JSON: {raw!r}") from exc
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def _message_to_dict(message: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"role": "assistant", "content": message.content or ""}
    calls = _extract_tool_calls(message)
    if calls:
        payload["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.function.name, "arguments": call.function.arguments},
            }
            for call in calls
        ]
    return payload


def build_messages(history: list[AssistantMessage], user_text: str) -> list[dict[str, Any]]:
    """System prompt + the last 10 stored turns + the new question.

    Tool rows are replayed as plain text rather than as tool messages: the stored
    call ids are not valid for a fresh request.
    """
    messages: list[dict[str, Any]] = [{"role": "system", "content": prompts.SYSTEM_PROMPT}]
    for row in history[-HISTORY_LIMIT:]:
        if row.role == MessageRole.TOOL:
            messages.append(
                {
                    "role": "assistant",
                    "content": f"[previous tool result from {row.tool_name}] {row.content}"[:2000],
                }
            )
        else:
            messages.append({"role": str(row.role), "content": row.content})
    messages.append({"role": "user", "content": user_text})
    return messages


async def run_tool_rounds(
    client: Any,
    ctx: ToolContext,
    messages: list[dict[str, Any]],
    *,
    max_rounds: int | None = None,
) -> LoopOutcome:
    """Resolve tool calls until the model answers or the cap is reached."""
    cap = max_rounds if max_rounds is not None else settings.llm_max_tool_rounds
    schemas = openai_tool_schemas()
    outcome = LoopOutcome(messages=list(messages))
    retried: set[str] = set()

    for round_index in range(cap):
        response = await client.complete(outcome.messages, schemas)
        choice = response.choices[0]
        message = choice.message
        calls = _extract_tool_calls(message)

        if not calls:
            outcome.messages.append(_message_to_dict(message))
            outcome.answer = message.content or ""
            return outcome

        outcome.messages.append(_message_to_dict(message))

        for call in calls:
            name = call.function.name
            invocation = ToolInvocation(name=name, arguments={})
            try:
                invocation.arguments = _parse_arguments(call.function.arguments)
                invocation.result = await execute(ctx, name, invocation.arguments)
                content = json.dumps(invocation.result, default=str)
            except ToolExecutionError as exc:
                # One correction round per tool: feed the validation error back so
                # the model can fix its arguments. A second failure is reported.
                invocation.error = str(exc)
                if name in retried:
                    content = json.dumps({"error": str(exc), "hint": "do not call this tool again"})
                else:
                    retried.add(name)
                    content = json.dumps(
                        {"error": str(exc), "hint": "correct the arguments and retry once"}
                    )
                log.warning("tool call failed", extra={"tool": name, "error": str(exc)})
            except Exception as exc:  # noqa: BLE001 - a tool bug must not kill the chat
                invocation.error = repr(exc)
                content = json.dumps({"error": "tool failed", "detail": exc.__class__.__name__})
                log.exception("tool raised", extra={"tool": name})

            outcome.invocations.append(invocation)
            outcome.messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "name": name,
                    "content": content[:6000],
                }
            )

        if round_index == cap - 1:
            outcome.hit_round_cap = True

    return outcome


# Words per SSE frame when replaying an answer the loop already generated.
REPLAY_CHUNK_WORDS = 3


async def stream_answer(client: Any, outcome: LoopOutcome) -> AsyncIterator[str]:
    """Emit the final answer as a stream of chunks.

    Two paths, and the distinction matters on CPU where a generation costs
    seconds:

    * The loop finished normally, so the answer is already generated. It is
      replayed in small chunks - the client still renders progressively, and
      the model is not asked to produce the same turn twice.
    * The round cap was hit, so no answer exists yet. One streaming call is
      made with no tools offered, which forces prose rather than a fifth tool
      call.
    """
    if outcome.hit_round_cap:
        yield prompts.TOO_MANY_ROUNDS + "\n\n"
        async for chunk in client.stream(outcome.messages, None):
            yield chunk
        return

    answer = (outcome.answer or "").strip()
    if not answer:
        # The model returned an empty turn; ask once more rather than showing
        # the user nothing.
        async for chunk in client.stream(outcome.messages, None):
            yield chunk
        return

    words = answer.split(" ")
    for start in range(0, len(words), REPLAY_CHUNK_WORDS):
        chunk = " ".join(words[start : start + REPLAY_CHUNK_WORDS])
        yield chunk if start + REPLAY_CHUNK_WORDS >= len(words) else chunk + " "


async def persist(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    *,
    user_text: str,
    invocations: list[ToolInvocation],
    answer: str,
) -> None:
    """One row per user turn, per tool call and per assistant answer."""
    session.add(
        AssistantMessage(conversation_id=conversation_id, role=MessageRole.USER, content=user_text)
    )
    for invocation in invocations:
        session.add(
            AssistantMessage(
                conversation_id=conversation_id,
                role=MessageRole.TOOL,
                content=json.dumps(invocation.result, default=str)[:4000]
                if invocation.result is not None
                else (invocation.error or ""),
                tool_name=invocation.name,
                tool_args=invocation.arguments,
            )
        )
    session.add(
        AssistantMessage(
            conversation_id=conversation_id, role=MessageRole.ASSISTANT, content=answer
        )
    )
    await session.commit()


async def summarize_anomaly(
    client: Any, vm_name: str, window_start: str, window_end: str, features: dict[str, float]
) -> str | None:
    """Two-sentence anomaly summary (spec 10.5). Returns None if the LLM is down -
    never blocks anomaly creation."""
    try:
        response = await client.complete(
            [
                {"role": "system", "content": prompts.ANOMALY_SUMMARY_PROMPT},
                {
                    "role": "user",
                    "content": prompts.anomaly_summary_user_prompt(
                        vm_name, window_start, window_end, features
                    ),
                },
            ],
            None,
        )
        return (response.choices[0].message.content or "").strip() or None
    except ServiceUnavailableError:
        log.info("LLM unavailable; leaving anomaly summary empty")
        return None
    except Exception:  # noqa: BLE001
        log.exception("anomaly summary failed")
        return None
