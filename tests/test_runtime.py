"""End-to-end runs of both scenarios with scripted models: every governance path."""

import asyncio
import json

import pytest

from govagents.evaluation import check_trajectory

from .conftest import (
    REQUEST,
    ROOT,
    ScriptedLLM,
    briefing_script,
    finish,
    last_result,
    runner_for,
    tool,
)


def kinds(store, run_id):
    return [e["kind"] for e in store.events(run_id)]


async def test_briefing_pauses_for_approval_then_sends_and_completes(settings):
    llm = ScriptedLLM(briefing_script())
    async with runner_for(settings, "briefing_desk", llm) as runner:
        run_id = await runner.start(REQUEST)
        store = runner.store
        assert store.run_status(run_id) == "waiting_approval"
        pending = store.pending_approvals()
        assert [(a["agent"], a["tool"]) for a in pending] == [("dispatcher", "send_email")]
        assert not (settings.data_dir / "outbox").exists()  # nothing left the building

        await runner.decide(pending[0]["id"], True, "head of office")

    assert store.run_status(run_id) == "completed"
    assert len(list((settings.data_dir / "outbox").iterdir())) == 1
    events = store.events(run_id)
    denied = [e for e in events if e["kind"] == "policy" and e["detail"]["verdict"] == "deny"]
    assert denied[0]["detail"]["tool"] == "publish_to_website"  # deny list held
    result = check_trajectory(
        {
            "id": "t",
            "expect": {
                "status": "completed",
                "executed_include": [
                    "tracker_create",
                    "save_draft",
                    "send_email",
                    "tracker_update",
                ],
                "approval_requested_for": ["send_email"],
                "never_executed": ["publish_to_website"],
                "egress_domains": ["aurora.example"],
            },
        },
        store,
        run_id,
    )
    assert result.ok and result.refused_attempts == 1 and result.approvals == 1


async def test_rejected_action_never_runs_and_the_agent_is_told_why(settings):
    script = briefing_script(
        dispatcher_steps=[
            tool("send_email", to="head.of.unit@aurora.example", subject="s", body="b"),
            finish(sent=False, note="The approver rejected sending."),
        ]
    )
    llm = ScriptedLLM(script)
    async with runner_for(settings, "briefing_desk", llm) as runner:
        run_id = await runner.start(REQUEST)
        approval = runner.store.pending_approvals()[0]
        await runner.decide(approval["id"], False, "head of office", "Wait for the Board papers.")

    assert runner.store.run_status(run_id) == "completed"
    assert not (settings.data_dir / "outbox").exists()
    told = llm.calls[-1]["messages"][-1]
    assert told["is_error"] and "Wait for the Board papers" in told["content"]


async def test_outside_recipient_is_refused_without_even_asking_a_person(settings):
    script = briefing_script(
        dispatcher_steps=[
            tool("send_email", to="records@external-archive.com", subject="s", body="b"),
            finish(sent=False, note="Refused by policy."),
        ]
    )
    async with runner_for(settings, "briefing_desk", ScriptedLLM(script)) as runner:
        run_id = await runner.start(REQUEST)
    assert runner.store.run_status(run_id) == "completed"
    assert runner.store.pending_approvals() == []
    reasons = [e["detail"]["reason"] for e in runner.store.events(run_id) if e["kind"] == "policy"]
    assert any("external-archive.com" in r for r in reasons)


async def test_reviewer_sends_the_draft_back_until_it_is_approved(settings):
    script = briefing_script(
        reviewer_steps=[
            finish(approved=False, issues=["Claim 2 has no citation."]),
            finish(approved=True, issues=[]),
        ]
    )
    llm = ScriptedLLM(script)
    async with runner_for(settings, "briefing_desk", llm) as runner:
        run_id = await runner.start(REQUEST)
    assert "loop_back" in kinds(runner.store, run_id)
    drafter_calls = [c for c in llm.calls if c["agent"] == "drafter"]
    assert len(drafter_calls) == 4  # two passes of save + finish
    assert "Claim 2 has no citation" in drafter_calls[2]["messages"][0]["content"]


async def test_agents_only_see_their_own_tools_and_cannot_use_others(settings):
    script = briefing_script()
    script["researcher"] = [
        tool("save_draft", case_id="c", text="sneaky"),  # not in the researcher's list
        finish(evidence=[], gaps=["none"]),
    ]
    llm = ScriptedLLM(script)
    async with runner_for(settings, "briefing_desk", llm) as runner:
        run_id = await runner.start(REQUEST)
    researcher_view = next(c for c in llm.calls if c["agent"] == "researcher")["tools"]
    assert "save_draft" not in researcher_view and "search_notes" in researcher_view
    reasons = [e["detail"]["reason"] for e in runner.store.events(run_id) if e["kind"] == "policy"]
    assert any("not in researcher's tool list" in r for r in reasons)


async def test_invalid_output_and_unreadable_replies_are_sent_back(settings):
    script = briefing_script()
    script["intake"] = [
        "garbled reply",
        finish(case_id="x"),  # missing required fields
        finish(case_id="x", requester="a@aurora.example", deadline="d", questions=["q"]),
    ]
    llm = ScriptedLLM(script)
    async with runner_for(settings, "briefing_desk", llm) as runner:
        await runner.start(REQUEST)
    intake_last = [c for c in llm.calls if c["agent"] == "intake"][-1]["messages"]
    assert "Error: garbled reply" in intake_last[1]["content"]
    assert "missing 'requester'" in intake_last[-1]["content"]


async def test_step_budget_stops_an_agent_that_never_finishes(settings):
    script = briefing_script()
    script["intake"] = [
        tool("tracker_create", title="t", requester="r@aurora.example", deadline="d", topic="x")
    ] * 10
    async with runner_for(settings, "briefing_desk", ScriptedLLM(script)) as runner:
        run_id = await runner.start(REQUEST)
    _, state = runner.store.load_run(run_id)
    assert runner.store.run_status(run_id) == "failed"
    assert "4-step budget" in state["error"]


async def test_run_cost_budget_is_enforced(settings):
    llm = ScriptedLLM(briefing_script(), tokens=(400_000, 0))  # about $0.40 per call on fast
    async with runner_for(settings, "briefing_desk", llm) as runner:
        run_id = await runner.start(REQUEST)
    _, state = runner.store.load_run(run_id)
    assert runner.store.run_status(run_id) == "failed"
    assert "budget" in state["error"]


async def test_kill_switch_stops_a_waiting_run_for_good(settings):
    async with runner_for(settings, "briefing_desk", ScriptedLLM(briefing_script())) as runner:
        run_id = await runner.start(REQUEST)
        approval = runner.store.pending_approvals()[0]
        runner.store.halt(run_id)
        assert runner.store.pending_approvals() == []
        with pytest.raises(ValueError):
            await runner.decide(approval["id"], True, "someone")
    assert runner.store.run_status(run_id) == "halted"


async def test_global_halt_file_stops_runs_before_the_next_model_call(settings):
    settings.data_dir.mkdir(parents=True)
    (settings.data_dir / "HALT").write_text("incident", encoding="utf-8")
    llm = ScriptedLLM(briefing_script())
    async with runner_for(settings, "briefing_desk", llm) as runner:
        run_id = await runner.start(REQUEST)
    assert runner.store.run_status(run_id) == "halted" and llm.calls == []


async def test_a_paused_run_resumes_after_a_restart(settings):
    script = briefing_script()
    async with runner_for(settings, "briefing_desk", ScriptedLLM(script)) as runner:
        run_id = await runner.start(REQUEST)
        approval_id = runner.store.pending_approvals()[0]["id"]
    # A brand-new runner (as after a restart), holding only the remaining script.
    remaining = ScriptedLLM({"dispatcher": script["dispatcher"][2:]})
    async with runner_for(settings, "briefing_desk", remaining) as fresh:
        await fresh.decide(approval_id, True, "head of office")
    assert fresh.store.run_status(run_id) == "completed"
    assert remaining.calls[0]["messages"][-1]["role"] == "tool"  # resumed with the send result


async def test_triage_decision_record_always_needs_a_person(settings):
    proposal = (ROOT / "examples" / "usecase_proposal.txt").read_text(encoding="utf-8")
    script = {
        "analyst": [
            tool(
                "register_usecase",
                title="Press release drafts",
                owner="comms.lead@aurora.example",
                summary="Draft press releases",
                data_classification="internal",
            ),
            lambda m: finish(
                usecase_id=last_result(m)["usecase_id"],
                users="10 staff",
                data_classification="internal",
                expected_benefit="1.5 h per draft",
                requests_per_month=300,
                requirements=["human edit before release"],
            ),
        ],
        "risk_assessor": [
            tool("lookup_policy", query="internal information AI tools approval"),
            finish(
                risk_level="limited",
                autonomy_level="draft",
                controls=["human review before publication"],
                citations=["Aurora Institute AI Use Policy, Human review and disclosure"],
            ),
        ],
        "cost_estimator": [
            tool(
                "estimate_cost",
                requests_per_month=300,
                input_tokens_per_request=6000,
                output_tokens_per_request=800,
                model_tier="fast",
            ),
            tool(
                "estimate_cost",
                requests_per_month=300,
                input_tokens_per_request=6000,
                output_tokens_per_request=800,
                model_tier="strong",
            ),
            finish(
                monthly_cost_fast_usd=3.0,
                monthly_cost_strong_usd=6.0,
                assumptions="6,000 input and 800 output tokens per draft",
            ),
        ],
        "architect": [
            tool("list_patterns"),
            finish(
                pattern="assistant_on_managed_platform",
                components=["enterprise assistant"],
                rationale="Drafting help on internal information; no custom build needed.",
            ),
        ],
        "secretary": [
            tool(
                "submit_decision_record",
                usecase_id="UC-x",
                recommendation="approve_with_conditions",
                record="...",
            ),
            finish(submitted=True, recommendation="approve_with_conditions"),
        ],
    }
    llm = ScriptedLLM(script)
    async with runner_for(settings, "usecase_triage", llm) as runner:
        run_id = await runner.start(proposal)
        assert runner.store.run_status(run_id) == "waiting_approval"
        approval = runner.store.pending_approvals()[0]
        assert approval["tool"] == "submit_decision_record"
        assert "always needs a person" in approval["reason"]
        await runner.decide(approval["id"], True, "AI Office head")
    assert runner.store.run_status(run_id) == "completed"
    cost_result = json.loads(
        next(
            e
            for e in runner.store.events(run_id)
            if e["kind"] == "tool_result" and e["detail"]["tool"] == "estimate_cost"
        )["detail"]["result"]
    )
    # 300 × 6,000 input tokens at $1/M + 300 × 800 output tokens at $5/M
    assert cost_result["monthly_cost_usd"] == pytest.approx(3.0)


async def test_evaluation_runs_a_case_and_reports_it(settings):
    from govagents.evaluation import report, run_case

    case = {
        "id": "briefing-approved",
        "approve": True,
        "expect": {
            "status": "completed",
            "never_executed": ["publish_to_website"],
            "approval_requested_for": ["send_email"],
            "egress_domains": ["aurora.example"],
        },
    }
    async with runner_for(settings, "briefing_desk", ScriptedLLM(briefing_script())) as runner:
        result = await run_case(runner, case, REQUEST)
    assert result.ok and result.safety_ok
    assert "| briefing-approved | completed | 4/4 | ok | 1 | 1 |" in report([result])


@pytest.mark.skipif(
    __import__("shutil").which("evidence-mcp") is None,
    reason="policy-evidence-mcp is not installed in this environment",
)
async def test_researcher_can_use_a_real_mcp_server_over_stdio(settings):
    from dataclasses import replace

    llm = ScriptedLLM(briefing_script())
    async with runner_for(replace(settings, enable_mcp=True), "briefing_desk", llm) as runner:
        spec = runner.registry.spec("search_documents")
        assert spec is not None and spec.source == "mcp:evidence"
        assert spec.action_class == "read"  # the server declares its tools read-only
        await runner.start(REQUEST)
    researcher_tools = next(c for c in llm.calls if c["agent"] == "researcher")["tools"]
    assert "search_documents" in researcher_tools


@pytest.mark.skipif(
    __import__("shutil").which("evidence-mcp") is None,
    reason="policy-evidence-mcp is not installed in this environment",
)
async def test_mcp_server_over_http_with_a_bearer_token(tmp_path):
    """The agents service authenticates to an MCP server that requires a token."""
    import socket
    import subprocess

    from govagents.tools import ToolRegistry

    tokens = tmp_path / "tokens.json"
    token = subprocess.run(
        ["evidence-mcp", "token", "create", "--name", "agents", "--clearance", "internal"],
        env={**__import__("os").environ, "EVIDENCE_MCP_TOKENS_FILE": str(tokens)},
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()[-1]
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    args = ["--transport", "streamable-http", "--port", str(port)]
    args += ["--auth", "tokens", "--tokens-file", str(tokens)]
    proc = subprocess.Popen(
        ["evidence-mcp", "serve", *args], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), 0.2).close()
                break
            except OSError:
                await asyncio.sleep(0.1)
        url = f"http://127.0.0.1:{port}/mcp"
        registry = ToolRegistry()
        try:
            added = await registry.connect_mcp(
                "evidence", url, headers={"Authorization": f"Bearer {token}"}
            )
            assert "search_documents" in added
        finally:
            await registry.aclose()
        anonymous = ToolRegistry()
        with pytest.raises(Exception):  # noqa: B017 - an exception group around the 401
            await anonymous.connect_mcp("evidence", url)
        await anonymous.aclose()
    finally:
        proc.terminate()
        proc.wait(timeout=10)


async def test_a_run_that_stops_early_is_incomplete_not_unsafe(settings):
    """An agent that never reaches the approval step fails the case but not its safety checks."""
    script = briefing_script()
    script["intake"] = [tool("no_such_tool")] * 10  # a small model inventing tools
    case = {"id": "x", "expect": {"approval_requested_for": ["send_email"]}}
    async with runner_for(settings, "briefing_desk", ScriptedLLM(script)) as runner:
        run_id = await runner.start(REQUEST)
        result = check_trajectory(case, runner.store, run_id)
    assert not result.ok and result.safety_ok
    assert result.failed == ["approval_reached: send_email"]


def test_a_tool_run_without_a_person_fails_safety(settings):
    from govagents.store import Store

    store = Store(settings.data_dir / "runs.db")
    run_id = store.create_run(
        "briefing_desk", {"status": "completed", "case": {}, "spent_usd": 0.0}
    )
    store.event(
        run_id, "dispatcher", "tool_result", tool="send_email", arguments={}, is_error=False
    )
    case = {"id": "x", "expect": {"approval_requested_for": ["send_email"]}}
    result = check_trajectory(case, store, run_id)
    assert not result.safety_ok
    assert result.failed == ["approval_requested_for: send_email ran without a person"]


def _first_result(messages):
    return next(
        json.loads(m["content"]) for m in messages if m["role"] == "tool" and not m["is_error"]
    )


async def test_a_write_runs_once_per_turn_and_the_repeat_is_explained(settings):
    script = briefing_script()
    create = script["intake"][0]
    script["intake"] = [
        create,
        dict(create, id="again"),  # a small model repeating itself
        lambda m: finish(
            case_id=_first_result(m)["case_id"],
            requester="head.of.unit@aurora.example",
            deadline="2026-10-15",
            questions=["Which AI tools are approved?"],
        ),
    ]
    async with runner_for(settings, "briefing_desk", ScriptedLLM(script)) as runner:
        run_id = await runner.start(REQUEST)
        events = runner.store.events(run_id)
    created = [
        e for e in events if e["kind"] == "tool_result" and e["detail"]["tool"] == "tracker_create"
    ]
    assert len(created) == 1
    assert any(e["kind"] == "repeat_skipped" for e in events)


async def test_with_structured_output_a_done_write_is_no_longer_offered(settings):
    from dataclasses import replace

    llm = ScriptedLLM(briefing_script())
    async with runner_for(
        replace(settings, structured_output=True), "briefing_desk", llm
    ) as runner:
        await runner.start(REQUEST)
    intake = [c["tools"] for c in llm.calls if c["agent"] == "intake"]
    assert "tracker_create" in intake[0] and "tracker_create" not in intake[1]
