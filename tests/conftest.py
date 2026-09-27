"""Scripted models and a ready-made runner, so every test runs offline and deterministically."""

from __future__ import annotations

import copy
import itertools
import json
import re
from dataclasses import replace
from pathlib import Path

import pytest

from govagents.build import open_runner
from govagents.config import Settings
from govagents.llm import Turn

ROOT = Path(__file__).resolve().parents[1]
_ids = itertools.count(1)


def tool(name: str, **arguments) -> dict:
    return {"type": "tool", "id": f"t{next(_ids)}", "name": name, "arguments": arguments}


def finish(**output) -> dict:
    return {"type": "finish", "id": f"f{next(_ids)}", "output": output}


def last_result(messages: list[dict]) -> dict:
    return json.loads(messages[-1]["content"])


def case_file(messages: list[dict]) -> dict:
    """The case file the runtime hands to each agent in its first message."""
    text = messages[0]["content"]
    return json.loads(text.split("Case file:\n", 1)[1].rsplit("\n\nDo your part", 1)[0])


class ScriptedLLM:
    """Plays a script per agent. A step can be a function of the conversation so far."""

    def __init__(self, scripts: dict[str, list], tokens=(1000, 100)):
        self.scripts = {agent: list(steps) for agent, steps in scripts.items()}
        self.tokens = tokens
        self.calls: list[dict] = []

    def next_turn(self, system, messages, tools, output_schema) -> Turn:
        agent = re.search(r"You are the (\w+) agent", system).group(1)
        self.calls.append(
            {"agent": agent, "tools": [t.name for t in tools], "messages": copy.deepcopy(messages)}
        )
        if not self.scripts.get(agent):
            raise AssertionError(f"script for {agent} is exhausted")
        step = self.scripts[agent].pop(0)
        if callable(step):
            step = step(messages)
        if isinstance(step, str):
            return Turn(None, step, *self.tokens)
        return Turn(step, None, *self.tokens)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return replace(
        Settings(),
        data_dir=tmp_path / "data",
        scenarios_dir=ROOT / "scenarios",
        offline=False,
        enable_mcp=False,  # hermetic by default; one test connects a real MCP server
    )


def runner_for(settings, scenario: str, llm: ScriptedLLM):
    return open_runner(settings, scenario, llm_for_tier=lambda tier: llm)


# --------------------------------------------------------------------------- scripts

REQUEST = (ROOT / "examples" / "briefing_request.txt").read_text(encoding="utf-8")


def briefing_script(dispatcher_steps=None, reviewer_steps=None) -> dict:
    return {
        "intake": [
            tool(
                "tracker_create",
                title="AI tools and restricted information",
                requester="head.of.unit@aurora.example",
                deadline="2026-10-15",
                topic="AI tools",
            ),
            lambda m: finish(
                case_id=last_result(m)["case_id"],
                requester="head.of.unit@aurora.example",
                deadline="2026-10-15",
                questions=["Which AI tools are approved?", "How long are AI prompts kept?"],
            ),
        ],
        "researcher": [
            tool("search_notes", query="approved AI tools restricted information"),
            finish(
                evidence=[
                    {
                        "point": "An enterprise assistant and a chat service are approved.",
                        "citation": "Aurora Institute AI Use Policy, Approved tools",
                    }
                ],
                gaps=[],
            ),
        ],
        "drafter": [
            tool("save_draft", case_id="CASE-x", text="Approved tools are ... [1]"),
            lambda m: finish(
                draft_id=last_result(m)["draft_id"], draft="Approved tools are ... [1]"
            ),
        ]
        * 2,
        "reviewer": reviewer_steps or [finish(approved=True, issues=[])],
        "dispatcher": dispatcher_steps
        or [
            tool("publish_to_website", title="Note", body="..."),
            tool(
                "send_email",
                to="head.of.unit@aurora.example",
                subject="Input: AI tools",
                body="Approved tools are ... [1]",
            ),
            lambda m: tool(
                "tracker_update", case_id=case_file(m)["intake"]["case_id"], status="sent"
            ),
            finish(sent=True, note="Sent after approval."),
        ],
    }
