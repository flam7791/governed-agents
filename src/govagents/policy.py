"""The policy engine: every tool call an agent proposes is decided here, in code.

Checks, in order (the first that fails decides):
1. Least privilege: the tool must be in the agent's own tool list.
2. Deny list: some tools are never allowed by any agent (for example publishing).
3. Egress rules: recipients of anything sent out must be in the allowed domains.
4. Autonomy: the agent's level and the tool's action class give allow, approve or deny.
5. Always-approve list: tools that need a person even when autonomy would allow them.

Prompts can ask a model to behave; only this module decides what actually happens.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .models import AgentManifest, Decision, ToolSpec

MATRIX = {
    "observe": {"read": "allow", "write_internal": "deny", "external": "deny"},
    "draft": {"read": "allow", "write_internal": "allow", "external": "deny"},
    "act_with_approval": {"read": "allow", "write_internal": "allow", "external": "approve"},
    "act": {"read": "allow", "write_internal": "allow", "external": "allow"},
}
RECIPIENT_FIELDS = ("to", "cc", "bcc", "recipient", "recipients")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")


@dataclass(frozen=True)
class Policy:
    deny_tools: frozenset[str] = frozenset()
    always_approve: frozenset[str] = frozenset()
    allowed_email_domains: tuple[str, ...] = ()
    max_run_cost_usd: float = 1.0

    @classmethod
    def from_dict(cls, data: dict) -> Policy:
        return cls(
            deny_tools=frozenset(data.get("deny_tools", [])),
            always_approve=frozenset(data.get("always_approve", [])),
            allowed_email_domains=tuple(d.lower() for d in data.get("allowed_email_domains", [])),
            max_run_cost_usd=float(data.get("max_run_cost_usd", 1.0)),
        )

    def decide(self, agent: AgentManifest, tool: ToolSpec, arguments: dict) -> Decision:
        if tool.name not in agent.tools:
            return Decision("deny", f"{tool.name} is not in {agent.name}'s tool list")
        if tool.name in self.deny_tools:
            return Decision("deny", f"{tool.name} is on the organisation's deny list")

        outside = self._outside_recipients(arguments)
        if outside:
            return Decision("deny", f"recipient domain not allowed: {', '.join(sorted(outside))}")

        verdict = MATRIX[agent.autonomy][tool.action_class]
        reason = f"autonomy '{agent.autonomy}' × action '{tool.action_class}'"
        if verdict == "allow" and tool.name in self.always_approve:
            return Decision("approve", f"{tool.name} always needs a person's approval")
        return Decision(verdict, reason)

    def _outside_recipients(self, arguments: dict) -> set[str]:
        if not self.allowed_email_domains:
            return set()
        found = set()
        for key in RECIPIENT_FIELDS:
            value = arguments.get(key)
            values = value if isinstance(value, list) else [value] if value else []
            for item in values:
                for domain in EMAIL.findall(str(item)):
                    if domain.lower() not in self.allowed_email_domains:
                        found.add(domain.lower())
        return found
