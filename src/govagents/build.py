"""Wiring: build a Runner for a scenario from settings (the only place real services meet)."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from .config import Settings
from .demo_tools import Workspace, register_briefing_tools, register_triage_tools
from .llm import AgentLLM, AnthropicAgentLLM, JsonActionLLM, RecordingAgentLLM
from .runtime import Runner, Scenario
from .store import Store
from .tools import ToolRegistry

log = logging.getLogger(__name__)


def llm_factory(settings: Settings):
    cache: dict[str, AgentLLM] = {}

    def for_tier(tier: str) -> AgentLLM:
        if tier not in cache:
            model = settings.models[tier]
            inner: AgentLLM | None = None
            if not settings.offline:
                if settings.provider == "anthropic":
                    if not os.environ.get("ANTHROPIC_API_KEY"):
                        raise RuntimeError("ANTHROPIC_API_KEY is not set.")
                    inner = AnthropicAgentLLM(model)
                else:
                    if not settings.base_url:
                        raise RuntimeError("GOVAGENTS_BASE_URL is required for openai_compatible.")
                    inner = JsonActionLLM(settings.base_url, model, api_key=settings.api_key)
            if settings.recordings or settings.offline:
                root = (settings.recordings or settings.data_dir / "recordings") / tier
                inner = RecordingAgentLLM(
                    inner, root, key=f"{settings.provider}:{model}", offline=settings.offline
                )
            cache[tier] = inner
        return cache[tier]

    return for_tier


@asynccontextmanager
async def open_runner(settings: Settings, scenario_name: str, llm_for_tier=None):
    scenario = Scenario.load(settings.scenarios_dir / scenario_name)
    store = Store(settings.data_dir / "runs.db")
    registry = ToolRegistry()
    patterns = scenario.directory / "patterns.json"
    workspace = Workspace(
        settings.data_dir, scenario.directory / "knowledge", patterns if patterns.exists() else None
    )
    if scenario.toolset == "briefing":
        register_briefing_tools(registry, workspace)
    elif scenario.toolset == "triage":
        register_triage_tools(registry, workspace, settings)

    for server in scenario.mcp_servers if settings.enable_mcp else []:
        from mcp import StdioServerParameters

        params = StdioServerParameters(
            command=server["command"], args=server.get("args", []), env=server.get("env")
        )
        try:
            added = await registry.connect_mcp(
                server["name"], params, server.get("action_overrides")
            )
            log.warning("connected MCP server %s: %s", server["name"], ", ".join(added))
        except Exception as exc:  # e.g. the server is not installed on this machine
            if not server.get("optional"):
                raise
            log.warning("optional MCP server %s not available (%s)", server["name"], exc)

    try:
        yield Runner(scenario, store, registry, llm_for_tier or llm_factory(settings), settings)
    finally:
        await registry.aclose()
