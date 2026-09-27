import pytest

from govagents.models import AgentManifest, ToolSpec
from govagents.policy import Policy


def agent(autonomy: str, tools=("read_tool", "write_tool", "email", "publish")) -> AgentManifest:
    return AgentManifest("a", "s", "p", "o", autonomy, tuple(tools), "fast", 3, "")


READ = ToolSpec("read_tool", "", {}, "read")
WRITE = ToolSpec("write_tool", "", {}, "write_internal")
EMAIL = ToolSpec("email", "", {}, "external")
PUBLISH = ToolSpec("publish", "", {}, "external")
POLICY = Policy(
    deny_tools=frozenset({"publish"}),
    always_approve=frozenset(),
    allowed_email_domains=("aurora.example",),
)


@pytest.mark.parametrize(
    "autonomy, tool, verdict",
    [
        ("observe", READ, "allow"),
        ("observe", WRITE, "deny"),
        ("observe", EMAIL, "deny"),
        ("draft", WRITE, "allow"),
        ("draft", EMAIL, "deny"),
        ("act_with_approval", WRITE, "allow"),
        ("act_with_approval", EMAIL, "approve"),
        ("act", EMAIL, "allow"),
    ],
)
def test_autonomy_by_action_class(autonomy, tool, verdict):
    assert POLICY.decide(agent(autonomy), tool, {"to": "x@aurora.example"}).verdict == verdict


def test_least_privilege_comes_first():
    decision = POLICY.decide(agent("act", tools=("read_tool",)), EMAIL, {})
    assert decision.verdict == "deny" and "tool list" in decision.reason


def test_deny_list_beats_autonomy():
    assert POLICY.decide(agent("act"), PUBLISH, {}).verdict == "deny"


def test_recipients_outside_allowed_domains_are_refused():
    decision = POLICY.decide(agent("act"), EMAIL, {"to": "Leak <x@external-archive.com>"})
    assert decision.verdict == "deny" and "external-archive.com" in decision.reason
    listed = POLICY.decide(agent("act"), EMAIL, {"to": ["a@aurora.example", "b@evil.test"]})
    assert listed.verdict == "deny"


def test_always_approve_adds_a_person_even_when_autonomy_allows():
    policy = Policy(always_approve=frozenset({"write_tool"}))
    assert policy.decide(agent("draft"), WRITE, {}).verdict == "approve"


def test_unknown_labels_are_rejected_at_load():
    with pytest.raises(ValueError):
        ToolSpec("t", "", {}, "dangerous")
    with pytest.raises(ValueError):
        agent("superuser")
