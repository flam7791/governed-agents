"""Tracing: one trace per run segment, a span per agent, model call and tool call, no content."""

import httpx
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from govagents import tracing
from govagents.llm import JsonActionLLM

from .conftest import REQUEST, ScriptedLLM, briefing_script, runner_for

EXPORTER = InMemorySpanExporter()
_provider = TracerProvider()
_provider.add_span_processor(SimpleSpanProcessor(EXPORTER))
trace.set_tracer_provider(_provider)


async def test_run_agent_model_and_tool_spans(settings):
    EXPORTER.clear()
    async with runner_for(settings, "briefing_desk", ScriptedLLM(briefing_script())) as runner:
        run_id = await runner.start(REQUEST)
        pending = runner.store.pending_approvals()
        await runner.decide(pending[0]["id"], True, "head of office")

    spans = EXPORTER.get_finished_spans()
    names = [s.name for s in spans]
    assert "agent_run briefing_desk" in names
    assert "invoke_agent dispatcher" in names
    tool_spans = {
        s.attributes["gen_ai.tool.name"]: s for s in spans if s.name.startswith("execute_tool")
    }
    publish = tool_spans["publish_to_website"]
    assert publish.attributes["govagents.policy.verdict"] == "deny"
    approved = [s for s in spans if s.attributes.get("govagents.approved_by") == "head of office"]
    assert approved and approved[0].attributes["gen_ai.tool.name"] == "send_email"
    run_spans = [s for s in spans if s.name.startswith("agent_run")]
    assert all(s.attributes["govagents.run_id"] == run_id for s in run_spans)
    # no message text or tool arguments on any span
    values = " ".join(str(v) for s in spans for v in s.attributes.values())
    assert "head.of.unit@aurora.example" not in values and "Case file" not in values


def test_model_calls_carry_the_trace_to_the_gateway():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["traceparent"] = request.headers.get("traceparent")
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": '{"action": "finish", "output": {"ok": true}}'}}
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    llm = JsonActionLLM(
        "http://gateway/v1", "fast", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    with tracing.tracer.start_as_current_span("parent") as span:
        llm.next_turn("system", [{"role": "user", "content": "hi"}], [], {})
        trace_id = format(span.get_span_context().trace_id, "032x")
    assert seen["traceparent"].split("-")[1] == trace_id
