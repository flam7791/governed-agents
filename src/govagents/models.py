"""Core records: what an agent is allowed to be, what a tool can do, what the policy decided.

Two vocabularies carry the governance model:

- Action classes, declared per tool:
    read             no side effects (search, look up, calculate)
    write_internal   changes internal records; reversible (a tracker entry, a saved draft)
    external         leaves the organisation or cannot be undone (an email, a publication)

- Autonomy levels, declared per agent in its manifest:
    observe            may only read
    draft              may read and change internal records
    act_with_approval  as draft, and may take external actions once a person approves each one
    act                may take external actions on its own (no scenario here uses it)

The policy engine (policy.py) combines the two for every single tool call.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

ACTION_CLASSES = ("read", "write_internal", "external")
AUTONOMY_LEVELS = ("observe", "draft", "act_with_approval", "act")


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict
    action_class: str
    source: str = "python"  # "python", or "mcp:<server>"

    def __post_init__(self):
        if self.action_class not in ACTION_CLASSES:
            raise ValueError(f"tool {self.name}: unknown action class {self.action_class}")


@dataclass(frozen=True)
class AgentManifest:
    """One row of the agent register: who the agent is and what it may do."""

    name: str
    scenario: str
    purpose: str
    owner: str
    autonomy: str
    tools: tuple[str, ...]
    model: str  # a model tier ("fast", "strong"), mapped to a real model in settings
    max_steps: int
    instructions: str
    output_schema: dict = field(default_factory=dict)
    # Tools the agent must have called (run, or refused by the policy or a person) before its
    # finish is accepted: an output cannot report work that was not done.
    required_tools: tuple[str, ...] = ()

    def __post_init__(self):
        if self.autonomy not in AUTONOMY_LEVELS:
            raise ValueError(f"agent {self.name}: unknown autonomy level {self.autonomy}")

    def to_row(self) -> dict:
        row = asdict(self)
        row["tools"] = ", ".join(self.tools)
        row["required_tools"] = ", ".join(self.required_tools)
        row.pop("instructions")
        row.pop("output_schema")
        return row


@dataclass(frozen=True)
class Decision:
    verdict: str  # "allow", "approve" (needs a person) or "deny"
    reason: str
