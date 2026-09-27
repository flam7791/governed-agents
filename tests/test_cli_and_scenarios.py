import json
from dataclasses import replace

import pytest

from govagents.cli import main
from govagents.evaluation import load_cases
from govagents.runtime import Scenario

from .conftest import ROOT


@pytest.mark.parametrize("name", ["briefing_desk", "usecase_triage"])
def test_scenarios_load_and_every_listed_tool_exists(name, tmp_path):
    from govagents.config import Settings
    from govagents.demo_tools import Workspace, register_briefing_tools, register_triage_tools
    from govagents.tools import ToolRegistry

    scenario = Scenario.load(ROOT / "scenarios" / name)
    registry = ToolRegistry()
    ws = Workspace(tmp_path, scenario.directory / "knowledge", scenario.directory / "patterns.json")
    if scenario.toolset == "briefing":
        register_briefing_tools(registry, ws)
    else:
        register_triage_tools(registry, ws, Settings())
    mcp_tools = {"search_documents", "search_datasets", "describe_dataset", "get_data"}
    for agent in scenario.agents.values():
        for tool in agent.tools:
            assert tool in registry.tools or tool in mcp_tools, f"{agent.name}: {tool}"


def test_stage_referring_to_unknown_agent_is_rejected(tmp_path):
    data = json.loads((ROOT / "scenarios" / "briefing_desk" / "scenario.json").read_text())
    data["stages"].append({"agent": "ghost"})
    (tmp_path / "scenario.json").write_text(json.dumps(data))
    with pytest.raises(ValueError):
        Scenario.load(tmp_path)


def test_register_lists_every_agent(capsys, monkeypatch):
    monkeypatch.setenv("GOVAGENTS_SCENARIOS_DIR", str(ROOT / "scenarios"))
    assert main(["register"]) == 0
    out = capsys.readouterr().out
    for name in ("intake", "researcher", "dispatcher", "risk_assessor", "secretary"):
        assert name in out
    assert "act_with_approval" in out


def test_eval_cases_are_well_formed():
    cases = load_cases(ROOT / "evals" / "cases.jsonl")
    assert len({c["id"] for c in cases}) == len(cases) >= 5
    for case in cases:
        assert case["scenario"] in ("briefing_desk", "usecase_triage")
        assert "never_executed" in case["expect"] or "approval_requested_for" in case["expect"]


def test_empty_state_commands(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GOVAGENTS_DATA_DIR", str(tmp_path))
    assert main(["approvals"]) == 0
    assert "Nothing is waiting" in capsys.readouterr().out
    assert main(["runs"]) == 0


def test_settings_map_tiers_for_a_gateway(monkeypatch):
    from govagents.config import Settings

    monkeypatch.setenv("GOVAGENTS_PROVIDER", "openai_compatible")
    monkeypatch.setenv("GOVAGENTS_BASE_URL", "http://127.0.0.1:8080/v1")
    settings = Settings.from_env()
    assert settings.models == {"fast": "fast", "strong": "strong"}
    assert replace(settings).base_url.endswith("/v1")
