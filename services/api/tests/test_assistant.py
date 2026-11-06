"""Assistant loop, tools, SSE stream and persistence (spec 10)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from app.assistant import loop
from app.assistant.llm_client import set_llm_client
from app.assistant.tools import (
    TOOLS_BY_NAME,
    ToolContext,
    ToolExecutionError,
    execute,
    openai_tool_schemas,
)
from app.db.models import AssistantConversation, AssistantMessage
from app.db.models.enums import MessageRole, RecommendationKind
from app.services import ml_service
from tests.conftest import auth_header
from tests.fake_llm import FakeLLMClient, tool_call


@pytest.fixture(autouse=True)
def _reset_client():
    """Also resets sse-starlette's shutdown Event.

    It is a module-level asyncio.Event that binds to the first loop that waits
    on it, and every test here gets a fresh loop.
    """
    from sse_starlette.sse import AppStatus

    AppStatus.should_exit_event = None
    yield
    set_llm_client(None)
    AppStatus.should_exit_event = None


async def _estate(client, source, service_header):
    await client.post(
        "/internal/inventory",
        json={
            "source_id": str(source.id),
            "collected_at": datetime.now(UTC).isoformat(),
            "clusters": [{"external_id": "c1", "name": "DC0_C0"}],
            "hosts": [
                {
                    "external_id": "h1",
                    "cluster_external_id": "c1",
                    "name": "esx-01",
                    "cpu_cores": 16,
                    "memory_mb": 65536,
                    "power_state": "on",
                }
            ],
            "vms": [
                {
                    "external_id": f"v{i}",
                    "host_external_id": "h1",
                    "name": f"web-{i:02d}",
                    "vcpus": 2 + 2 * i,
                    "memory_mb": 8192,
                    "provisioned_storage_gb": 40.0,
                    "guest_os": "Ubuntu 22.04",
                    "power_state": "on",
                }
                for i in range(3)
            ],
            "datastores": [
                {"external_id": "d1", "name": "LocalDS_0", "capacity_gb": 1000.0, "free_gb": 80.0}
            ],
        },
        headers=service_header,
    )


# --- tool registry --------------------------------------------------------


def test_every_spec_tool_is_registered():
    assert set(TOOLS_BY_NAME) == {
        "get_inventory_summary",
        "search_vms",
        "get_vm_details",
        "get_metric_stats",
        "list_anomalies",
        "list_recommendations",
        "get_capacity_forecast",
    }


def test_tool_schemas_are_openai_shaped():
    schemas = openai_tool_schemas()
    assert len(schemas) == len(TOOLS_BY_NAME)
    for schema in schemas:
        assert schema["type"] == "function"
        assert schema["function"]["name"] in TOOLS_BY_NAME
        assert schema["function"]["description"]
        assert schema["function"]["parameters"]["type"] == "object"


async def test_unknown_tool_is_rejected(session, mongo, viewer):
    ctx = ToolContext(session=session, mongo=mongo, user=viewer)
    with pytest.raises(ToolExecutionError, match="Unknown tool"):
        await execute(ctx, "drop_database", {})


async def test_invalid_arguments_are_rejected(session, mongo, viewer):
    ctx = ToolContext(session=session, mongo=mongo, user=viewer)
    with pytest.raises(ToolExecutionError, match="Invalid arguments"):
        await execute(ctx, "get_metric_stats", {"entity_type": "router", "name": "x"})


async def test_list_limits_are_capped(session, mongo, viewer):
    ctx = ToolContext(session=session, mongo=mongo, user=viewer)
    with pytest.raises(ToolExecutionError, match="Invalid arguments"):
        await execute(ctx, "search_vms", {"limit": 500})


# --- tool behaviour -------------------------------------------------------


async def test_inventory_summary_tool_is_compact(
    client, source, service_header, session, mongo, viewer
):
    await _estate(client, source, service_header)
    ctx = ToolContext(session=session, mongo=mongo, user=viewer)

    result = await execute(ctx, "get_inventory_summary", {})

    assert result["totals"]["vms"] == 3
    assert result["clusters"][0]["name"] == "DC0_C0"
    # Compact enough for an 8k context.
    assert len(json.dumps(result)) < 2000


async def test_inventory_summary_can_filter_to_one_cluster(
    client, source, service_header, session, mongo, viewer
):
    await _estate(client, source, service_header)
    ctx = ToolContext(session=session, mongo=mongo, user=viewer)

    assert (
        len((await execute(ctx, "get_inventory_summary", {"cluster_name": "DC0"}))["clusters"]) == 1
    )
    assert "error" in await execute(ctx, "get_inventory_summary", {"cluster_name": "nope"})


async def test_search_vms_filters_and_truncates(
    client, source, service_header, session, mongo, viewer
):
    await _estate(client, source, service_header)
    ctx = ToolContext(session=session, mongo=mongo, user=viewer)

    result = await execute(ctx, "search_vms", {"name_contains": "web", "limit": 2})
    assert result["total_matching"] == 3
    assert result["returned"] == 2

    big = await execute(ctx, "search_vms", {"min_vcpus": 5})
    assert [vm["name"] for vm in big["vms"]] == ["web-02"]


async def test_vm_details_includes_window_and_findings(
    client, source, service_header, session, mongo, viewer
):
    await _estate(client, source, service_header)
    ctx = ToolContext(session=session, mongo=mongo, user=viewer)

    result = await execute(ctx, "get_vm_details", {"vm_name": "web-01"})

    assert result["name"] == "web-01"
    assert result["cluster"] == "DC0_C0"
    assert "last_14_days" in result
    assert result["open_anomalies"] == []


async def test_vm_details_for_a_missing_vm_says_so(session, mongo, viewer):
    ctx = ToolContext(session=session, mongo=mongo, user=viewer)
    result = await execute(ctx, "get_vm_details", {"vm_name": "ghost"})
    assert "error" in result


async def test_recommendation_tool_reports_reclaimable_totals(
    client, source, service_header, session, mongo, viewer
):
    await _estate(client, source, service_header)
    vm_id = (await client.get("/vms", headers=auth_header(viewer))).json()["items"][0]["id"]
    import uuid

    await ml_service.upsert_recommendation(
        session,
        vm_id=uuid.UUID(vm_id),
        kind=RecommendationKind.IDLE,
        current_value=2,
        suggested_value=0,
        evidence={"rule": "idle", "window_days": 14, "cpu_pct_p95": 1.2},
    )
    await session.commit()

    ctx = ToolContext(session=session, mongo=mongo, user=viewer)
    result = await execute(ctx, "list_recommendations", {"kind": "idle"})

    assert result["total_matching"] == 1
    assert result["total_reclaimable_vcpus"] == 2
    assert result["recommendations"][0]["kind"] == "idle"


async def test_capacity_tool_requires_a_target(session, mongo, viewer):
    ctx = ToolContext(session=session, mongo=mongo, user=viewer)
    assert "error" in await execute(ctx, "get_capacity_forecast", {})


async def test_capacity_tool_samples_the_series(
    client, source, service_header, session, mongo, viewer
):
    await _estate(client, source, service_header)
    ctx = ToolContext(session=session, mongo=mongo, user=viewer)

    result = await execute(ctx, "get_capacity_forecast", {"datastore_name": "LocalDS_0"})

    assert result["name"] == "LocalDS_0"
    assert result["used_pct"] == 92.0
    assert result["threshold_pct"] == 90


async def test_metric_stats_tool_names_its_window(
    client, source, service_header, session, mongo, viewer
):
    await _estate(client, source, service_header)
    ctx = ToolContext(session=session, mongo=mongo, user=viewer)

    result = await execute(
        ctx, "get_metric_stats", {"name": "web-00", "metric": "cpu_pct", "days": 30}
    )

    assert result["window_days"] == 30
    assert result["metric"] == "cpu_pct"
    assert result["name"] == "web-00"


# --- the loop -------------------------------------------------------------


async def test_a_plain_answer_needs_no_tools(session, mongo, viewer):
    client_stub = FakeLLMClient(script=["No tools needed."])
    ctx = ToolContext(session=session, mongo=mongo, user=viewer)

    outcome = await loop.run_tool_rounds(client_stub, ctx, loop.build_messages([], "hello"))

    assert outcome.invocations == []
    assert outcome.hit_round_cap is False
    assert outcome.messages[-1]["content"] == "No tools needed."


async def test_a_tool_call_is_executed_and_fed_back(
    client, source, service_header, session, mongo, viewer
):
    await _estate(client, source, service_header)
    client_stub = FakeLLMClient(
        script=[[tool_call("search_vms", {"name_contains": "web", "limit": 5})], "Three VMs."]
    )
    ctx = ToolContext(session=session, mongo=mongo, user=viewer)

    outcome = await loop.run_tool_rounds(client_stub, ctx, loop.build_messages([], "which VMs?"))

    assert [i.name for i in outcome.invocations] == ["search_vms"]
    assert outcome.invocations[0].error is None
    tool_messages = [m for m in outcome.messages if m["role"] == "tool"]
    assert len(tool_messages) == 1
    assert json.loads(tool_messages[0]["content"])["total_matching"] == 3


async def test_string_json_arguments_are_parsed(
    client, source, service_header, session, mongo, viewer
):
    await _estate(client, source, service_header)
    client_stub = FakeLLMClient(script=[[tool_call("get_inventory_summary", "{}")], "done"])
    ctx = ToolContext(session=session, mongo=mongo, user=viewer)

    outcome = await loop.run_tool_rounds(client_stub, ctx, loop.build_messages([], "how many?"))
    assert outcome.invocations[0].error is None


async def test_malformed_arguments_are_returned_to_the_model_once(session, mongo, viewer):
    client_stub = FakeLLMClient(
        script=[
            [tool_call("get_metric_stats", {"entity_type": "nonsense", "name": "x"})],
            [tool_call("get_metric_stats", {"entity_type": "vm", "name": "x"})],
            "Recovered.",
        ]
    )
    ctx = ToolContext(session=session, mongo=mongo, user=viewer)

    outcome = await loop.run_tool_rounds(client_stub, ctx, loop.build_messages([], "stats?"))

    assert outcome.invocations[0].error is not None
    first_result = json.loads([m for m in outcome.messages if m["role"] == "tool"][0]["content"])
    assert "correct the arguments" in first_result["hint"]
    # The retry ran, so the model got a second chance.
    assert len(outcome.invocations) == 2


async def test_repeated_bad_arguments_stop_being_retried(session, mongo, viewer):
    bad = [tool_call("get_metric_stats", {"entity_type": "nonsense", "name": "x"})]
    client_stub = FakeLLMClient(script=[bad, bad, "Giving up."])
    ctx = ToolContext(session=session, mongo=mongo, user=viewer)

    outcome = await loop.run_tool_rounds(client_stub, ctx, loop.build_messages([], "stats?"))

    hints = [json.loads(m["content"]).get("hint") for m in outcome.messages if m["role"] == "tool"]
    assert hints[-1] == "do not call this tool again"


async def test_unparseable_json_arguments_do_not_crash(session, mongo, viewer):
    client_stub = FakeLLMClient(script=[[tool_call("search_vms", "{not json")], "ok"])
    ctx = ToolContext(session=session, mongo=mongo, user=viewer)

    outcome = await loop.run_tool_rounds(client_stub, ctx, loop.build_messages([], "?"))
    assert outcome.invocations[0].error is not None


async def test_the_round_cap_is_enforced(client, source, service_header, session, mongo, viewer):
    await _estate(client, source, service_header)
    # Always asks for another tool; the loop must stop at the cap.
    client_stub = FakeLLMClient(script=[[tool_call("get_inventory_summary", {})]] * 20)
    ctx = ToolContext(session=session, mongo=mongo, user=viewer)

    outcome = await loop.run_tool_rounds(
        client_stub, ctx, loop.build_messages([], "everything"), max_rounds=4
    )

    assert outcome.hit_round_cap is True
    assert len(client_stub.calls) == 4
    assert len(outcome.invocations) == 4


async def test_a_tool_that_raises_does_not_kill_the_turn(session, mongo, viewer, monkeypatch):
    from app.assistant import tools as tools_module

    async def explode(ctx, args):
        raise RuntimeError("boom")

    monkeypatch.setitem(
        tools_module.TOOLS_BY_NAME,
        "get_inventory_summary",
        tools_module.ToolSpec(
            "get_inventory_summary",
            "test",
            tools_module.GetInventorySummaryArgs,
            explode,
        ),
    )
    client_stub = FakeLLMClient(script=[[tool_call("get_inventory_summary", {})], "handled"])
    ctx = ToolContext(session=session, mongo=mongo, user=viewer)

    outcome = await loop.run_tool_rounds(client_stub, ctx, loop.build_messages([], "?"))

    assert "RuntimeError" in outcome.invocations[0].error
    # The failure is reported to the model as a tool result, and the turn goes on.
    tool_messages = [m for m in outcome.messages if m["role"] == "tool"]
    assert json.loads(tool_messages[0]["content"])["error"] == "tool failed"
    assert outcome.messages[-1]["content"] == "handled"


def test_history_is_capped_at_ten_turns():
    history = [AssistantMessage(role=MessageRole.USER, content=f"q{i}") for i in range(25)]
    messages = loop.build_messages(history, "latest")

    assert messages[0]["role"] == "system"
    assert messages[-1] == {"role": "user", "content": "latest"}
    assert len(messages) == 1 + loop.HISTORY_LIMIT + 1


def test_stored_tool_rows_are_replayed_as_text():
    history = [
        AssistantMessage(role=MessageRole.TOOL, content='{"vms": 3}', tool_name="search_vms")
    ]
    messages = loop.build_messages(history, "again")
    assert messages[1]["role"] == "assistant"
    assert "search_vms" in messages[1]["content"]


async def test_stream_prefixes_a_warning_when_the_cap_was_hit(session, mongo, viewer):
    client_stub = FakeLLMClient(answer="partial answer")
    outcome = loop.LoopOutcome(messages=[], hit_round_cap=True)
    chunks = [c async for c in loop.stream_answer(client_stub, outcome)]
    assert "too broad" in "".join(chunks)
    assert client_stub.stream_calls == 1


async def test_the_final_answer_is_replayed_not_regenerated(session, mongo, viewer):
    """The loop has already generated the answer. Asking the model to continue a
    conversation that ends with its own reply returned nothing at all, which is
    how this surfaced - the tool call was right and the answer was blank."""
    client_stub = FakeLLMClient()
    outcome = loop.LoopOutcome(messages=[], answer="Three VMs are idle: a, b and c.")

    chunks = [c async for c in loop.stream_answer(client_stub, outcome)]

    assert "".join(chunks) == "Three VMs are idle: a, b and c."
    assert len(chunks) > 1, "the client should still render progressively"
    assert client_stub.stream_calls == 0, "no second generation"


async def test_an_empty_final_turn_falls_back_to_one_more_call(session, mongo, viewer):
    client_stub = FakeLLMClient(answer="recovered answer")
    outcome = loop.LoopOutcome(messages=[], answer="   ")

    chunks = [c async for c in loop.stream_answer(client_stub, outcome)]

    assert "recovered" in "".join(chunks)
    assert client_stub.stream_calls == 1


async def test_the_loop_captures_the_answer_text(session, mongo, viewer):
    client_stub = FakeLLMClient(script=["The estate has 85 VMs."])
    ctx = ToolContext(session=session, mongo=mongo, user=viewer)

    outcome = await loop.run_tool_rounds(client_stub, ctx, loop.build_messages([], "how many?"))

    assert outcome.answer == "The estate has 85 VMs."
    assert outcome.hit_round_cap is False


# --- persistence and HTTP -------------------------------------------------


async def test_conversation_crud_is_scoped_to_its_owner(client, viewer, operator):
    created = await client.post(
        "/assistant/conversations", json={"title": "capacity"}, headers=auth_header(viewer)
    )
    assert created.status_code == 201
    conversation_id = created.json()["id"]

    assert (
        await client.get(f"/assistant/conversations/{conversation_id}", headers=auth_header(viewer))
    ).status_code == 200
    assert (
        await client.get(
            f"/assistant/conversations/{conversation_id}", headers=auth_header(operator)
        )
    ).status_code == 403


async def test_conversations_require_authentication(client):
    assert (await client.post("/assistant/conversations", json={})).status_code == 401


async def test_unknown_conversation_is_404(client, viewer):
    response = await client.get(
        "/assistant/conversations/11111111-1111-1111-1111-111111111111",
        headers=auth_header(viewer),
    )
    assert response.status_code == 404


async def test_messages_return_503_when_the_model_is_down(client, viewer):
    set_llm_client(FakeLLMClient(healthy=False))
    conversation_id = (
        await client.post("/assistant/conversations", json={}, headers=auth_header(viewer))
    ).json()["id"]

    response = await client.post(
        f"/assistant/conversations/{conversation_id}/messages",
        json={"content": "which VMs are idle?"},
        headers=auth_header(viewer),
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_unavailable"
    assert "docker compose" in response.json()["error"]["message"]


async def test_sending_a_message_streams_tools_then_tokens(
    client, viewer, source, service_header, session
):
    await _estate(client, source, service_header)
    set_llm_client(
        FakeLLMClient(
            script=[[tool_call("search_vms", {"name_contains": "web"})]],
            answer="Three web VMs are running.",
        )
    )
    conversation_id = (
        await client.post("/assistant/conversations", json={}, headers=auth_header(viewer))
    ).json()["id"]

    response = await client.post(
        f"/assistant/conversations/{conversation_id}/messages",
        json={"content": "which web VMs exist?"},
        headers=auth_header(viewer),
    )

    assert response.status_code == 200
    body = response.text
    assert "event: tool" in body
    assert "search_vms" in body
    assert "event: token" in body
    assert "event: done" in body

    rows = (
        (await session.execute(select(AssistantMessage).order_by(AssistantMessage.created_at)))
        .scalars()
        .all()
    )
    roles = [str(r.role) for r in rows]
    assert roles == ["user", "tool", "assistant"]
    assert rows[1].tool_name == "search_vms"
    assert rows[1].tool_args == {"name_contains": "web"}
    assert "Three web VMs" in rows[2].content


async def test_the_first_message_titles_the_conversation(client, viewer, session):
    set_llm_client(FakeLLMClient(script=["Short answer."], answer="Short answer."))
    conversation_id = (
        await client.post("/assistant/conversations", json={}, headers=auth_header(viewer))
    ).json()["id"]

    await client.post(
        f"/assistant/conversations/{conversation_id}/messages",
        json={"content": "How much CPU can I reclaim?"},
        headers=auth_header(viewer),
    )

    row = (await session.execute(select(AssistantConversation.title))).scalar_one()
    assert row == "How much CPU can I reclaim?"


async def test_a_stream_failure_is_reported_as_an_event(client, viewer):
    set_llm_client(FakeLLMClient(healthy=True, unavailable=True))
    conversation_id = (
        await client.post("/assistant/conversations", json={}, headers=auth_header(viewer))
    ).json()["id"]

    response = await client.post(
        f"/assistant/conversations/{conversation_id}/messages",
        json={"content": "anything"},
        headers=auth_header(viewer),
    )

    assert response.status_code == 200
    assert "event: error" in response.text


async def test_empty_messages_are_rejected(client, viewer):
    conversation_id = (
        await client.post("/assistant/conversations", json={}, headers=auth_header(viewer))
    ).json()["id"]
    response = await client.post(
        f"/assistant/conversations/{conversation_id}/messages",
        json={"content": ""},
        headers=auth_header(viewer),
    )
    assert response.status_code == 422


async def test_persist_writes_one_row_per_tool_call(session, viewer):
    conversation = AssistantConversation(user_id=viewer.id, title="t")
    session.add(conversation)
    await session.commit()

    await loop.persist(
        session,
        conversation.id,
        user_text="question",
        invocations=[
            loop.ToolInvocation(name="search_vms", arguments={"limit": 5}, result={"vms": []}),
            loop.ToolInvocation(name="list_anomalies", arguments={}, error="nope"),
        ],
        answer="answer",
    )

    total = (await session.execute(select(func.count(AssistantMessage.id)))).scalar_one()
    assert total == 4  # user + two tools + assistant


# --- anomaly summaries ----------------------------------------------------


async def test_anomaly_summary_is_two_sentences():
    client_stub = FakeLLMClient(script=["CPU hit 95%. Check the backup job."])
    summary = await loop.summarize_anomaly(
        client_stub, "web-01", "2026-01-01T00:00", "2026-01-01T03:00", {"cpu_mean": 95.0}
    )
    assert summary == "CPU hit 95%. Check the backup job."


async def test_anomaly_summary_is_none_when_the_model_is_down():
    """Spec 10.5: never block anomaly creation on the LLM."""
    summary = await loop.summarize_anomaly(
        FakeLLMClient(unavailable=True), "web-01", "a", "b", {"cpu_mean": 95.0}
    )
    assert summary is None


async def test_tool_execution_logs_without_clobbering_reserved_fields(
    session, mongo, viewer, caplog
):
    """`extra={"args": ...}` raises KeyError inside logging, which surfaced as a
    tool failure only when a real model called a tool. Reserved LogRecord
    attribute names must never appear in an `extra` dict."""
    import logging

    with caplog.at_level(logging.INFO, logger="app.assistant.tools"):
        result = await execute(
            ToolContext(session=session, mongo=mongo, user=viewer),
            "get_inventory_summary",
            {},
        )

    assert "error" not in result
    records = [r for r in caplog.records if r.message == "assistant tool call"]
    assert records, "the tool call should have been logged"
    assert getattr(records[0], "tool_args", None) == {"cluster_name": None}


async def test_a_forecast_that_never_crosses_says_so_in_words(
    client, source, service_header, session, mongo, viewer
):
    """A bare `null` for days_to_threshold is something a small model fills in
    with a guess; the 3B reported "18 days" for a datastore at 5.9% used. The
    tool now states the absence explicitly."""
    await _estate(client, source, service_header)
    datastore_id = (await client.get("/datastores", headers=auth_header(viewer))).json()["items"][
        0
    ]["id"]

    import uuid as _uuid
    from datetime import date as _date

    from app.db.models.enums import EntityType, ForecastModel

    await ml_service.save_forecast(
        session,
        entity_type=EntityType.DATASTORE,
        entity_id=_uuid.UUID(datastore_id),
        metric="used_pct",
        model=ForecastModel.NAIVE,
        horizon_days=30,
        points=[
            {"date": _date(2026, 11, i + 1).isoformat(), "value": 6.0, "lower": 5.0, "upper": 7.0}
            for i in range(30)
        ],
        days_to_threshold=None,
    )
    await session.commit()

    ctx = ToolContext(session=session, mongo=mongo, user=viewer)
    result = await execute(ctx, "get_capacity_forecast", {"datastore_name": "LocalDS_0"})

    forecast = result["forecasts"][0]
    assert forecast["days_to_threshold"] is None
    assert "does NOT reach 90%" in forecast["verdict"]


async def test_a_forecast_that_does_cross_states_the_horizon(
    client, source, service_header, session, mongo, viewer
):
    await _estate(client, source, service_header)
    datastore_id = (await client.get("/datastores", headers=auth_header(viewer))).json()["items"][
        0
    ]["id"]

    import uuid as _uuid
    from datetime import date as _date

    from app.db.models.enums import EntityType, ForecastModel

    await ml_service.save_forecast(
        session,
        entity_type=EntityType.DATASTORE,
        entity_id=_uuid.UUID(datastore_id),
        metric="used_pct",
        model=ForecastModel.NAIVE,
        horizon_days=30,
        points=[
            {
                "date": _date(2026, 11, i + 1).isoformat(),
                "value": 95.0,
                "lower": 94.0,
                "upper": 96.0,
            }
            for i in range(30)
        ],
        days_to_threshold=4,
    )
    await session.commit()

    ctx = ToolContext(session=session, mongo=mongo, user=viewer)
    result = await execute(ctx, "get_capacity_forecast", {"datastore_name": "LocalDS_0"})

    assert "crosses 90% in about 4 days" in result["forecasts"][0]["verdict"]
