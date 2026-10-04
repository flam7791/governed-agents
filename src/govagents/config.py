"""Runtime settings from environment variables (prefix GOVAGENTS_), with safe defaults.

Two ways to reach models:
- provider "anthropic": Claude directly, with native tool use (needs ANTHROPIC_API_KEY);
- provider "openai_compatible": any OpenAI-compatible endpoint, such as an LLM gateway (which
  then applies its own routing, budgets and personal-data masking) or a local Ollama server.

Agents ask for a model tier ("fast" or "strong"); settings map tiers to real models.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_MODELS = {
    "anthropic": {"fast": "claude-haiku-4-5-20251001", "strong": "claude-sonnet-5"},
    # Through an LLM gateway, the tier names themselves are valid model names.
    "openai_compatible": {"fast": "fast", "strong": "strong"},
}
# USD per million tokens (input, output), for the cost report. Check current prices.
DEFAULT_PRICES = {"fast": (1.0, 5.0), "strong": (2.0, 10.0)}


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path("govagents-data")
    scenarios_dir: Path = Path("scenarios")
    provider: str = "anthropic"
    base_url: str | None = None
    api_key: str | None = None
    models: dict = field(default_factory=lambda: dict(DEFAULT_MODELS["anthropic"]))
    prices: dict = field(default_factory=lambda: dict(DEFAULT_PRICES))
    recordings: Path | None = None
    offline: bool = False
    enable_mcp: bool = True  # connect the MCP servers listed in scenarios
    structured_output: bool = False  # constrain replies to the agent's actions (JSON schema)

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ
        provider = env.get("GOVAGENTS_PROVIDER", "anthropic")
        if provider not in DEFAULT_MODELS:
            raise ValueError("GOVAGENTS_PROVIDER must be 'anthropic' or 'openai_compatible'")
        models = dict(DEFAULT_MODELS[provider])
        models["fast"] = env.get("GOVAGENTS_MODEL_FAST", models["fast"])
        models["strong"] = env.get("GOVAGENTS_MODEL_STRONG", models["strong"])
        recordings = env.get("GOVAGENTS_RECORDINGS")
        prices = dict(DEFAULT_PRICES)
        for tier in ("fast", "strong"):
            # "input,output" in USD per million tokens; "0,0" for a model on your own machine
            value = env.get(f"GOVAGENTS_PRICE_{tier.upper()}")
            if value:
                price_in, price_out = (float(x) for x in value.split(","))
                prices[tier] = (price_in, price_out)
        return cls(
            data_dir=Path(env.get("GOVAGENTS_DATA_DIR", "govagents-data")),
            scenarios_dir=Path(env.get("GOVAGENTS_SCENARIOS_DIR", "scenarios")),
            provider=provider,
            base_url=env.get("GOVAGENTS_BASE_URL"),
            api_key=env.get("GOVAGENTS_API_KEY"),
            models=models,
            prices=prices,
            recordings=Path(recordings) if recordings else None,
            offline=env.get("GOVAGENTS_OFFLINE", "").lower() in ("1", "true", "yes"),
            enable_mcp=env.get("GOVAGENTS_ENABLE_MCP", "true").lower() in ("1", "true", "yes"),
            structured_output=env.get("GOVAGENTS_STRUCTURED_OUTPUT", "").lower()
            in ("1", "true", "yes"),
        )

    def cost(self, tier: str, input_tokens: int, output_tokens: int) -> float:
        price_in, price_out = self.prices.get(tier, DEFAULT_PRICES["strong"])
        return (input_tokens * price_in + output_tokens * price_out) / 1_000_000
