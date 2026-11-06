"""`python -m tests.run_assistant_eval`

Runs tests/assistant_eval.yaml against the live model and reports tool-selection
and argument accuracy, then appends the result to docs/ml-evaluation.md.

This is how the model choice is justified (spec 10.6). Run it once per candidate
model and keep the rows.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

import yaml

from app.assistant.llm_client import LlamaServerClient
from app.assistant.loop import build_messages
from app.assistant.tools import TOOLS_BY_NAME, ToolContext, openai_tool_schemas
from app.config import settings
from app.db.mongo import close_client, get_db
from app.db.postgres import SessionLocal, engine
from app.logging_config import configure_logging

CASES_PATH = Path(__file__).parent / "assistant_eval.yaml"


def _expected_tools(case: dict[str, Any]) -> list[str]:
    if case.get("any_of"):
        return list(case["any_of"])
    return [case["tool"]]


async def _ask(client: LlamaServerClient, ctx: ToolContext, question: str) -> dict[str, Any]:
    """One non-streaming round. The tool the model *reaches for* is the signal,
    so the tools are offered but never executed."""
    response = await client.complete(build_messages([], question), openai_tool_schemas())
    message = response.choices[0].message
    calls = list(getattr(message, "tool_calls", None) or [])
    if not calls:
        return {"tool": None, "arguments": {}, "answered_directly": True}

    call = calls[0]
    raw = call.function.arguments
    try:
        arguments = raw if isinstance(raw, dict) else json.loads(raw or "{}")
    except ValueError:
        arguments = {"__unparseable__": str(raw)[:200]}
    return {"tool": call.function.name, "arguments": arguments, "answered_directly": False}


def _schema_defaults(tool_name: str) -> dict[str, Any]:
    """The tool's own pydantic defaults."""
    spec = TOOLS_BY_NAME.get(tool_name)
    if spec is None:
        return {}
    return {
        name: field.default
        for name, field in spec.args_model.model_fields.items()
        if not field.is_required()
    }


def _arguments_match(
    expected: dict[str, Any], actual: dict[str, Any], tool_name: str | None = None
) -> tuple[bool, list[str]]:
    """Check the expected keys only; extra arguments are not errors.

    An omitted argument counts as correct when the tool's own default already
    equals what was expected. The model is being scored on whether the *call*
    does the right thing, not on whether it restated a default - "show me open
    anomalies" with `status` left out calls exactly the same thing as
    `status="open"`, because that is the schema default.
    """
    defaults = _schema_defaults(tool_name) if tool_name else {}
    problems = []
    for key, want in expected.items():
        got = actual[key] if key in actual else defaults.get(key, None)
        source = "" if key in actual else " (omitted; schema default)"
        if isinstance(want, str) and isinstance(got, str):
            if want.lower() != got.lower():
                problems.append(f"{key}: expected {want!r}, got {got!r}{source}")
        elif got != want:
            problems.append(f"{key}: expected {want!r}, got {got!r}{source}")
    return (not problems), problems


async def run(limit: int | None = None) -> dict[str, Any]:
    cases = yaml.safe_load(CASES_PATH.read_text())["cases"]
    if limit:
        cases = cases[:limit]

    client = LlamaServerClient()
    if not await client.health():
        raise SystemExit(
            f"the model at {settings.llm_base_url} is not reachable - start it with "
            "`docker compose --profile llm up -d llm`"
        )

    results: list[dict[str, Any]] = []
    started = time.perf_counter()

    async with SessionLocal() as session:
        ctx = ToolContext(session=session, mongo=get_db(), user=None)  # type: ignore[arg-type]
        for index, case in enumerate(cases, start=1):
            expected = _expected_tools(case)
            outcome = await _ask(client, ctx, case["question"])
            tool_ok = outcome["tool"] in expected
            args_ok, problems = (
                _arguments_match(case.get("args", {}), outcome["arguments"], outcome["tool"])
                if tool_ok
                else (False, ["wrong tool"])
            )
            results.append(
                {
                    "question": case["question"],
                    "expected": expected,
                    "called": outcome["tool"],
                    "arguments": outcome["arguments"],
                    "tool_ok": tool_ok,
                    "args_ok": args_ok,
                    "problems": problems,
                }
            )
            status = "ok " if tool_ok and args_ok else ("tool" if tool_ok else "MISS")
            print(f"[{index:2d}/{len(cases)}] {status}  {case['question'][:62]}")

    await close_client()
    await engine.dispose()

    total = len(results)
    tool_hits = sum(1 for r in results if r["tool_ok"])
    arg_hits = sum(1 for r in results if r["args_ok"])
    return {
        "model": settings.llm_model,
        "base_url": settings.llm_base_url,
        "cases": total,
        "tool_selection_accuracy": round(tool_hits / total, 4) if total else 0.0,
        # Conditional on picking the right tool, which is the number worth tuning.
        "argument_accuracy": round(arg_hits / tool_hits, 4) if tool_hits else 0.0,
        "argument_accuracy_overall": round(arg_hits / total, 4) if total else 0.0,
        "elapsed_seconds": round(time.perf_counter() - started, 1),
        "results": results,
    }


def render_section(summary: dict[str, Any]) -> str:
    rows = [
        "",
        "Twenty questions from `services/api/tests/assistant_eval.yaml`, each with "
        "the tool call it should make. Two things are scored separately: whether "
        "the model reached for the right tool, and whether its arguments were "
        "right given that tool. An argument the model omits counts as correct "
        "when the tool's own default already matches - the question is whether "
        "the call does the right thing, not whether the model restated a default.",
        "",
        f"### `{summary['model']}`",
        "",
        f"- Cases: {summary['cases']}",
        f"- **Tool-selection accuracy: {summary['tool_selection_accuracy']:.0%}**",
        f"- **Argument accuracy (given the right tool): {summary['argument_accuracy']:.0%}**",
        f"- Argument accuracy overall: {summary['argument_accuracy_overall']:.0%}",
        f"- Wall clock: {summary['elapsed_seconds']}s for {summary['cases']} questions",
        "",
    ]
    passed = summary["tool_selection_accuracy"] >= 0.8
    rows.append(
        "✅ Meets the 80% tool-selection bar from spec 14, Phase 6."
        if passed
        else "⚠️ Below the 80% tool-selection bar from spec 14, Phase 6. Try the larger "
        "model, or sharpen the tool descriptions - they are the prompt the model "
        "actually reads."
    )
    misses = [r for r in summary["results"] if not (r["tool_ok"] and r["args_ok"])]
    if misses:
        rows += [
            "",
            "Where it got it wrong:",
            "",
            "| Question | Expected | Called | Problem |",
            "|---|---|---|---|",
        ]
        for row in misses:
            problem = "; ".join(row["problems"]) or "no tool call"
            rows.append(
                f"| {row['question']} | {' / '.join(row['expected'])} | "
                f"{row['called'] or '(answered directly)'} | {problem} |"
            )
    return "\n".join(rows) + "\n"


HEADER_NOTE = (
    "Run `make assistant-eval` with the `llm` profile up to regenerate this "
    "section. Each model tried gets its own subsection, so the rows below are "
    "the evidence for the model choice."
)


def append_to_report(summary: dict[str, Any], path: Path) -> None:
    marker = "## 3. Assistant tool selection"
    section = render_section(summary)
    if not path.exists():
        path.write_text(f"{marker}\n{section}")
        return
    text = path.read_text()
    if marker in text:
        head, _, tail = text.partition(marker)
        # Keep the explanatory paragraph, drop the "run make assistant-eval" stub.
        stub = "Run `make assistant-eval`"
        if stub in tail:
            lines = [ln for ln in tail.splitlines() if stub not in ln]
            tail = "\n".join(lines)
        path.write_text(f"{head}{marker}\n{section}{tail}")
    else:
        path.write_text(f"{text}\n{marker}\n{section}")


def main(argv: list[str] | None = None) -> int:
    configure_logging("WARNING")
    parser = argparse.ArgumentParser(prog="tests.run_assistant_eval")
    parser.add_argument("--limit", type=int, default=None, help="run only the first N cases")
    parser.add_argument("--out", default="docs/ml-evaluation.md")
    parser.add_argument("--json", action="store_true", help="print the full result as JSON")
    args = parser.parse_args(argv)

    summary = asyncio.run(run(args.limit))
    print()
    print(render_section(summary))
    append_to_report(summary, Path(args.out))
    if args.json:
        print(json.dumps(summary, indent=2))
    return 0 if summary["tool_selection_accuracy"] >= 0.8 else 1


if __name__ == "__main__":
    sys.exit(main())
