"""Turn guards: the checks the agent loop runs on every turn, as a chain of small policies.

The loop in runtime.py does one thing: ask the model for an action and carry it out. Every rule
about *whether* to carry it out lives here or in policy.py, so a new rule is a new guard, not a
new branch in the loop (the middleware pattern; Barbaste et al., "Harness Engineering", 2026,
Recommendation 1). The policy engine decides what a tool call may do; guards decide what a turn
may do.

A guard can act at five points of a turn, in this order:

    before_model     stop the run before the next model call (kill switch)
    offer            narrow the tools offered to the model this turn
    after_model      stop the agent after a model call (cost budget)
    check_finish     reject a finish, with a message the model sees
    check_proposal   reject a tool call before the policy engine sees it

Guards run in the order of the chain, and the first rejection wins. The default chain is in
`default_guards`; its behaviour is the runtime's behaviour, and the tests and the recorded
trajectory evaluations hold it in place.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .models import AgentManifest


class RunHalted(RuntimeError):
    pass


@dataclass
class Turn:
    """What a guard may look at: the run, the agent, and the agent's state in this stage."""

    run_id: str
    agent: AgentManifest
    state: dict  # the whole run state (spent_usd, case file, ...)
    store: object  # the durable store (run status, events)

    @property
    def agent_state(self) -> dict:
        return self.state["agent_state"]

    @property
    def done(self) -> list[str]:
        return self.agent_state.setdefault("done", [])

    @property
    def calls(self) -> list[list[str]]:
        return self.agent_state.setdefault("calls", [])

    @property
    def refused(self) -> list[str]:
        return self.agent_state.setdefault("refused", [])


@dataclass
class Rejection:
    """A refused finish or proposal: the message goes back to the model as a tool error."""

    message: str
    event: str | None = None  # an audit-trail event to record, if any
    detail: dict = field(default_factory=dict)


class Guard:
    """No-op base: a guard overrides only the points it cares about."""

    def before_model(self, turn: Turn) -> None:
        return None

    def offer(self, turn: Turn, tools: list) -> list:
        return tools

    def after_model(self, turn: Turn) -> str | None:
        return None

    def check_finish(self, turn: Turn, output: dict) -> Rejection | None:
        return None

    def check_proposal(self, turn: Turn, name: str, spec, arguments: dict) -> Rejection | None:
        return None


def call_signature(name: str, arguments: dict) -> list[str]:
    return [name, json.dumps(arguments, sort_keys=True)]


def spent(tool, done: list, refused: list) -> bool:
    """True when offering the tool again in this turn cannot help."""
    if tool.name in refused:
        return True
    if tool.name not in done:
        return False
    no_arguments = not (tool.input_schema or {}).get("properties")
    return tool.action_class != "read" or no_arguments


# --------------------------------------------------------------------------- the guards


class KillSwitch(Guard):
    """A HALT file in the data folder stops every run; `govagents halt` stops one."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir

    def halted(self, turn: Turn) -> bool:
        return (self.data_dir / "HALT").exists() or turn.store.run_status(turn.run_id) == "halted"

    def before_model(self, turn: Turn) -> None:
        if self.halted(turn):
            raise RunHalted()


class RunCostBudget(Guard):
    """Stop the agent once the run has spent more than the scenario's cost limit."""

    def __init__(self, max_run_cost_usd: float):
        self.max_run_cost_usd = max_run_cost_usd

    def after_model(self, turn: Turn) -> str | None:
        if turn.state["spent_usd"] > self.max_run_cost_usd:
            return f"run budget of {self.max_run_cost_usd} USD exceeded"
        return None


class OfferOnlyUseful(Guard):
    """With structured output, nothing that cannot help is offered again in a turn: a write or
    external tool once done, a tool without arguments once called (same answer), and a tool
    refused by the policy or by a person. A constrained model then moves on or finishes."""

    def offer(self, turn: Turn, tools: list) -> list:
        return [t for t in tools if not spent(t, turn.done, turn.refused)]


class OutputSchema(Guard):
    """A finish must carry the fields the agent's output schema requires, with their types."""

    def check_finish(self, turn: Turn, output: dict) -> Rejection | None:
        from .runtime import missing_fields  # the schema check lives with the manifests

        problems = missing_fields(output, turn.agent.output_schema)
        if problems:
            return Rejection("Output rejected: " + "; ".join(problems))
        return None


class VerifyOnStop(Guard):
    """An output cannot report work that was not done: each required tool must have run, or been
    refused by the policy or a person, before the finish is accepted. Found with Qwen 2.5 7B,
    which reported a decision record as submitted and a draft id as saved without calling
    either tool."""

    def check_finish(self, turn: Turn, output: dict) -> Rejection | None:
        missing = [
            t for t in turn.agent.required_tools if t not in turn.done and t not in turn.refused
        ]
        if not missing:
            return None
        return Rejection(
            f"Output rejected: {', '.join(missing)} has not run in this turn, so the output "
            f"would report work that was not done. Call {missing[0]} first, then finish.",
            event="finish_refused",
            detail={"missing": missing},
        )


class NoRetryAfterRefusal(Guard):
    """A person's (or the policy's) no is final for this turn: no second request."""

    def check_proposal(self, turn: Turn, name: str, spec, arguments: dict) -> Rejection | None:
        if name in turn.refused and spec.action_class != "read":
            return Rejection(
                f"{name} was refused in this turn. Do not propose it again: finish and say what "
                "was not done.",
                event="repeat_skipped",
                detail={"tool": name},
            )
        return None


class NoRepeat(Guard):
    """Never run the same write twice in one turn, nor the same call: the result is already
    above (a small model may loop on it)."""

    def check_proposal(self, turn: Turn, name: str, spec, arguments: dict) -> Rejection | None:
        repeated_write = spec.action_class != "read" and name in turn.done
        if repeated_write or call_signature(name, arguments) in turn.calls:
            return Rejection(
                f"{name} already ran in this turn; its result is above. Do not repeat it: use "
                "another tool or finish.",
                event="repeat_skipped",
                detail={"tool": name},
            )
        return None


def default_guards(data_dir: Path, max_run_cost_usd: float, structured_output: bool) -> list:
    """The runtime's guard chain, in order."""
    chain: list[Guard] = [KillSwitch(data_dir), RunCostBudget(max_run_cost_usd)]
    if structured_output:
        chain.append(OfferOnlyUseful())
    chain += [OutputSchema(), VerifyOnStop(), NoRetryAfterRefusal(), NoRepeat()]
    return chain


def first_rejection(results) -> Rejection | None:
    return next((r for r in results if r is not None), None)
