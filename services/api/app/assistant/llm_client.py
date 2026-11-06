"""Thin wrapper over the OpenAI-compatible endpoint exposed by llama-server.

Nothing outside this module knows which model is running - swapping
LLM_MODEL_FILE in .env is the whole migration path (spec 10.1).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any, Protocol, cast

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI

from app.config import settings
from app.errors import ServiceUnavailableError

log = logging.getLogger(__name__)


class LLMClient(Protocol):
    """The surface the assistant loop depends on. Tests inject a fake."""

    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
    ) -> Any: ...

    async def stream(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
    ) -> AsyncIterator[str]: ...


class LlamaServerClient:
    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
    ) -> None:
        self.base_url = base_url or settings.llm_base_url
        self.model = model or settings.llm_model
        self.temperature = settings.llm_temperature if temperature is None else temperature
        self._client = AsyncOpenAI(
            base_url=self.base_url,
            api_key=settings.llm_api_key,
            timeout=settings.llm_timeout_seconds,
            max_retries=1,
        )

    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
    ) -> Any:
        try:
            # The SDK types messages and tools as closed TypedDicts. We build
            # them as plain dicts on purpose: llama-server accepts the wire
            # format, not the OpenAI SDK's Python surface.
            return await self._client.chat.completions.create(
                model=self.model,
                messages=cast(Any, messages),
                tools=cast(Any, tools or None),
                tool_choice="auto" if tools else cast(Any, None),
                temperature=self.temperature,
            )
        except (APIConnectionError, APITimeoutError) as exc:
            raise ServiceUnavailableError(
                f"Assistant model unreachable at {self.base_url}"
            ) from exc
        except APIStatusError as exc:
            raise ServiceUnavailableError(f"Assistant model returned {exc.status_code}") from exc

    async def stream(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
    ) -> AsyncIterator[str]:
        try:
            stream = await self._client.chat.completions.create(
                model=self.model,
                messages=cast(Any, messages),
                tools=cast(Any, tools or None),
                temperature=self.temperature,
                stream=True,
            )
            async for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if delta and delta.content:
                    yield delta.content
        except (APIConnectionError, APITimeoutError) as exc:
            raise ServiceUnavailableError(
                f"Assistant model unreachable at {self.base_url}"
            ) from exc
        except APIStatusError as exc:
            raise ServiceUnavailableError(f"Assistant model returned {exc.status_code}") from exc

    async def health(self) -> bool:
        """Used by /assistant endpoints to return a clean 503 instead of a hang."""
        url = self.base_url.rstrip("/").removesuffix("/v1") + "/health"
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                return (await client.get(url)).status_code == 200
        except httpx.HTTPError:
            return False


_client: LlamaServerClient | None = None


def get_llm_client() -> LlamaServerClient:
    global _client
    if _client is None:
        _client = LlamaServerClient()
    return _client


def set_llm_client(client: Any) -> None:
    """Test seam - the assistant tests inject a scripted client here."""
    global _client
    _client = client
