"""Assistant endpoints. The message endpoint returns an SSE stream so the user
sees tokens as the local model produces them (it is slow on CPU)."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, status
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sse_starlette.sse import EventSourceResponse

from app.assistant import loop, prompts
from app.assistant.llm_client import get_llm_client
from app.assistant.tools import ToolContext
from app.db.models import AssistantConversation
from app.deps import CurrentUser, DbSession, MongoDb
from app.errors import ForbiddenError, NotFoundError, ServiceUnavailableError
from app.schemas.assistant import ConversationCreate, ConversationOut, MessageCreate, MessageOut

router = APIRouter(prefix="/assistant", tags=["assistant"])


async def _owned_conversation(
    session: DbSession, user: CurrentUser, conversation_id: uuid.UUID
) -> AssistantConversation:
    conversation = (
        await session.execute(
            select(AssistantConversation)
            .options(selectinload(AssistantConversation.messages))
            .where(AssistantConversation.id == conversation_id)
        )
    ).scalar_one_or_none()
    if conversation is None:
        raise NotFoundError(f"Conversation {conversation_id} not found")
    if conversation.user_id != user.id:
        raise ForbiddenError("Conversation belongs to another user")
    return conversation


@router.post("/conversations", response_model=ConversationOut, status_code=status.HTTP_201_CREATED)
async def create_conversation(
    session: DbSession, user: CurrentUser, payload: ConversationCreate | None = None
) -> ConversationOut:
    conversation = AssistantConversation(
        user_id=user.id, title=(payload.title if payload and payload.title else "New conversation")
    )
    session.add(conversation)
    await session.commit()
    await session.refresh(conversation)
    return ConversationOut(
        id=conversation.id,
        title=conversation.title,
        created_at=conversation.created_at,
        messages=[],
    )


@router.get("/conversations", response_model=list[ConversationOut])
async def list_conversations(session: DbSession, user: CurrentUser) -> list[ConversationOut]:
    rows = (
        (
            await session.execute(
                select(AssistantConversation)
                .where(AssistantConversation.user_id == user.id)
                .order_by(AssistantConversation.created_at.desc())
                .limit(50)
            )
        )
        .scalars()
        .all()
    )
    return [
        ConversationOut(id=c.id, title=c.title, created_at=c.created_at, messages=[]) for c in rows
    ]


@router.get("/conversations/{conversation_id}", response_model=ConversationOut)
async def get_conversation(
    session: DbSession, user: CurrentUser, conversation_id: uuid.UUID
) -> ConversationOut:
    conversation = await _owned_conversation(session, user, conversation_id)
    return ConversationOut(
        id=conversation.id,
        title=conversation.title,
        created_at=conversation.created_at,
        messages=[MessageOut.model_validate(m) for m in conversation.messages],
    )


@router.post("/conversations/{conversation_id}/messages")
async def send_message(
    session: DbSession,
    mongo: MongoDb,
    user: CurrentUser,
    conversation_id: uuid.UUID,
    payload: MessageCreate,
) -> EventSourceResponse:
    conversation = await _owned_conversation(session, user, conversation_id)
    client = get_llm_client()
    if not await client.health():
        raise ServiceUnavailableError(prompts.LLM_UNAVAILABLE)

    # Title the conversation from the first question before streaming starts, so
    # the sidebar is right even if the model then fails mid-answer.
    if conversation.title == "New conversation":
        conversation.title = payload.content[:60]
        await session.commit()

    ctx = ToolContext(session=session, mongo=mongo, user=user)
    history = list(conversation.messages)

    async def event_stream() -> AsyncIterator[dict[str, str]]:
        """SSE events: `tool` per tool call, `token` per chunk, then `done`."""
        answer_parts: list[str] = []
        try:
            messages = loop.build_messages(history, payload.content)
            outcome = await loop.run_tool_rounds(client, ctx, messages)

            for invocation in outcome.invocations:
                yield {
                    "event": "tool",
                    "data": json.dumps(
                        {
                            "name": invocation.name,
                            "arguments": invocation.arguments,
                            "error": invocation.error,
                        },
                        default=str,
                    ),
                }

            async for chunk in loop.stream_answer(client, outcome):
                answer_parts.append(chunk)
                yield {"event": "token", "data": chunk}

            answer = "".join(answer_parts).strip()
            await loop.persist(
                session,
                conversation.id,
                user_text=payload.content,
                invocations=outcome.invocations,
                answer=answer,
            )
            yield {"event": "done", "data": json.dumps({"length": len(answer)})}
        except ServiceUnavailableError as exc:
            yield {"event": "error", "data": json.dumps({"message": exc.message})}
        except Exception as exc:  # noqa: BLE001 - the stream must close cleanly
            yield {
                "event": "error",
                "data": json.dumps({"message": f"Assistant failed: {exc.__class__.__name__}"}),
            }

    return EventSourceResponse(event_stream())
