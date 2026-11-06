"""A scripted stand-in for llama-server.

The assistant tests must exercise the loop - tool rounds, the cap, invalid
arguments, persistence - without a 5 GB model in the test image, so this returns
pre-baked tool calls and text.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from app.errors import ServiceUnavailableError


@dataclass
class FakeToolCall:
    name: str
    arguments: Any
    id: str = "call_1"

    @property
    def function(self) -> Any:
        return _Function(self.name, self.arguments)


@dataclass
class _Function:
    name: str
    arguments: Any


@dataclass
class _Message:
    content: str | None = None
    tool_calls: list[FakeToolCall] | None = None


@dataclass
class _Choice:
    message: _Message


@dataclass
class _Response:
    choices: list[_Choice]


@dataclass
class FakeLLMClient:
    """`script` is consumed one entry per `complete()` call.

    An entry is either a list of tool calls to request, or a string to answer
    with. The last entry repeats once the script runs out, so a test that only
    cares about tool rounds need not spell out the final answer.
    """

    script: list[Any] = field(default_factory=list)
    answer: str = "Here is what I found."
    healthy: bool = True
    unavailable: bool = False
    calls: list[list[dict[str, Any]]] = field(default_factory=list)
    stream_calls: int = 0

    async def health(self) -> bool:
        return self.healthy

    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
    ) -> Any:
        if self.unavailable:
            raise ServiceUnavailableError("Assistant model unreachable")
        self.calls.append(list(messages))
        step = self.script.pop(0) if self.script else self.answer
        if isinstance(step, str):
            return _Response([_Choice(_Message(content=step))])
        return _Response([_Choice(_Message(content=None, tool_calls=list(step)))])

    async def stream(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
    ) -> AsyncIterator[str]:
        if self.unavailable:
            raise ServiceUnavailableError("Assistant model unreachable")
        self.stream_calls += 1
        self.calls.append(list(messages))
        for chunk in self.answer.split(" "):
            yield chunk + " "


def tool_call(name: str, arguments: dict[str, Any] | str, call_id: str = "call_1") -> FakeToolCall:
    return FakeToolCall(
        name=name,
        arguments=arguments if isinstance(arguments, str) else json.dumps(arguments),
        id=call_id,
    )
