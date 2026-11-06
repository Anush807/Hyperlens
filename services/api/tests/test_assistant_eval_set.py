"""The eval set itself is checked: a typo'd tool name would silently score 0."""

from __future__ import annotations

from pathlib import Path

import yaml

from app.assistant.tools import TOOLS_BY_NAME
from tests.run_assistant_eval import _arguments_match, _expected_tools, render_section

CASES = yaml.safe_load((Path(__file__).parent / "assistant_eval.yaml").read_text())["cases"]


def test_the_set_has_twenty_questions():
    assert len(CASES) == 20


def test_every_expected_tool_exists():
    for case in CASES:
        for name in _expected_tools(case):
            assert name in TOOLS_BY_NAME, f"{name} is not a registered tool"


def test_every_expected_argument_is_in_its_tool_schema():
    for case in CASES:
        for name in _expected_tools(case):
            fields = set(TOOLS_BY_NAME[name].args_model.model_fields)
            unknown = set(case.get("args", {})) - fields
            assert not unknown, f"{name} has no argument(s) {unknown}"


def test_expected_arguments_validate_against_the_schema():
    for case in CASES:
        args = case.get("args")
        if not args:
            continue
        for name in _expected_tools(case):
            TOOLS_BY_NAME[name].args_model.model_validate(args)


def test_questions_are_unique():
    questions = [case["question"] for case in CASES]
    assert len(set(questions)) == len(questions)


def test_argument_matching_ignores_extras_and_is_case_insensitive():
    ok, problems = _arguments_match({"kind": "idle"}, {"kind": "idle", "limit": 10})
    assert ok and not problems

    ok, _ = _arguments_match({"cluster_name": "DC0_C0"}, {"cluster_name": "dc0_c0"})
    assert ok

    ok, problems = _arguments_match({"days": 7}, {"days": 30})
    assert not ok and problems


def test_an_omitted_argument_is_correct_when_the_default_matches():
    """`status` left out of list_anomalies calls exactly the same thing as
    status="open", because that is the schema default. Scoring it wrong
    measures verbosity, not capability."""
    ok, problems = _arguments_match({"status": "open"}, {"days": 30}, "list_anomalies")
    assert ok, problems


def test_an_omitted_argument_is_still_wrong_when_the_default_differs():
    ok, problems = _arguments_match({"status": "resolved"}, {"days": 30}, "list_anomalies")
    assert not ok
    assert "omitted; schema default" in problems[0]


def test_report_section_states_the_verdict():
    good = render_section(
        {
            "model": "qwen2.5-7b-instruct",
            "cases": 20,
            "tool_selection_accuracy": 0.9,
            "argument_accuracy": 0.85,
            "argument_accuracy_overall": 0.8,
            "elapsed_seconds": 42.0,
            "results": [],
        }
    )
    assert "Meets the 80%" in good

    bad = render_section(
        {
            "model": "tiny",
            "cases": 20,
            "tool_selection_accuracy": 0.5,
            "argument_accuracy": 0.5,
            "argument_accuracy_overall": 0.25,
            "elapsed_seconds": 10.0,
            "results": [
                {
                    "question": "Which VMs are idle?",
                    "expected": ["list_recommendations"],
                    "called": "search_vms",
                    "tool_ok": False,
                    "args_ok": False,
                    "problems": ["wrong tool"],
                }
            ],
        }
    )
    assert "Below the 80%" in bad
    assert "search_vms" in bad
