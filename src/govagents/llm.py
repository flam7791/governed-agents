"""Model adapters. The runtime asks one question of a model: "what is your next action?"

The conversation is stored in a neutral format, so it can be saved while a run waits for an
approval and resumed later, with any model:

    {"role": "user", "content": "..."}
    {"role": "assistant", "action": {"type": "tool", "id": "...", "name": "...", "arguments": {}}}
    {"role": "assistant", "action": {"type": "finish", "id": "...", "output": {...}}}
    {"role": "tool", "id": "...", "name": "...", "content": "...", "is_error": false}

Adapters:
- AnthropicAgentLLM: Claude with native tool use; finishing is a "finish" tool whose input
  schema is the agent's output schema, and exactly one tool call per turn is enforced.
- JsonActionLLM: any OpenAI-compatible chat endpoint (an LLM gateway, Ollama). The model
  answers with a JSON action; malformed answers are sent back as errors, never executed.
- RecordingAgentLLM: record/replay wrapper for reproducible, free evaluations.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

import httpx
from opentelemetry import propagate

from .models import ToolSpec


@dataclass
class Turn:
    action: dict | None  # {"type": "tool", ...} or {"type": "finish", ...}
    error: str | None = None  # the model's answer could not be understood
    input_tokens: int = 0
    output_tokens: int = 0


class AgentLLM(Protocol):
    def next_turn(
        self, system: str, messages: list[dict], tools: list[ToolSpec], output_schema: dict
    ) -> Turn: ...


class ReplayMiss(RuntimeError):
    """Offline replay found no recording for this model call."""


def _finish_schema(output_schema: dict) -> dict:
    return output_schema or {"type": "object", "properties": {"summary": {"type": "string"}}}


# --------------------------------------------------------------------------- Anthropic


class AnthropicAgentLLM:
    def __init__(self, model: str, client=None):
        import anthropic

        self.model = model
        self.client = client or anthropic.Anthropic()

    @staticmethod
    def to_api_messages(messages: list[dict]) -> list[dict]:
        """Neutral messages -> Messages API turns (consecutive same-role turns are merged)."""
        out: list[dict] = []
        for m in messages:
            if m["role"] == "user":
                role, block = "user", {"type": "text", "text": m["content"]}
            elif m["role"] == "assistant":
                a = m["action"]
                name = a["name"] if a["type"] == "tool" else "finish"
                data = a["arguments"] if a["type"] == "tool" else a["output"]
                role = "assistant"
                block = {"type": "tool_use", "id": a["id"], "name": name, "input": data}
            else:  # tool result
                role = "user"
                block = {
                    "type": "tool_result",
                    "tool_use_id": m["id"],
                    "content": m["content"],
                    "is_error": m.get("is_error", False),
                }
            if out and out[-1]["role"] == role:
                out[-1]["content"].append(block)
            else:
                out.append({"role": role, "content": [block]})
        return out

    def next_turn(self, system, messages, tools, output_schema) -> Turn:
        api_tools = [
            {"name": t.name, "description": t.description, "input_schema": t.input_schema}
            for t in tools
        ] + [
            {
                "name": "finish",
                "description": "Finish your task and hand over your result.",
                "input_schema": _finish_schema(output_schema),
            }
        ]
        response = self.client.messages.create(
            model=self.model,
            system=system,
            messages=self.to_api_messages(messages),
            tools=api_tools,
            tool_choice={"type": "any", "disable_parallel_tool_use": True},
            max_tokens=2000,
        )
        uses = [b for b in response.content if b.type == "tool_use"]
        usage = (response.usage.input_tokens, response.usage.output_tokens)
        if not uses:
            return Turn(None, "No tool call in the reply.", *usage)
        use = uses[0]
        if use.name == "finish":
            action = {"type": "finish", "id": use.id, "output": dict(use.input)}
        else:
            action = {"type": "tool", "id": use.id, "name": use.name, "arguments": dict(use.input)}
        return Turn(action, None, *usage)


# --------------------------------------------------------------------------- JSON actions


JSON_PROTOCOL = """
You act by replying with exactly one JSON object and nothing else, in one of two forms:
  {{"action": "tool", "tool": "<tool name>", "arguments": {{...}}}}
  {{"action": "finish", "output": {{...}}}}
The output of "finish" must match this JSON schema:
{output_schema}
Available tools (name, description, JSON schema of the arguments):
{tools}
""".strip()


def action_schema(tools, output_schema: dict) -> dict:
    """JSON schema of a valid reply: one of the agent's tools with its arguments, or finish.

    Sent as `response_format` when structured output is on, so a server that supports it
    (Ollama, vLLM, llama.cpp) can only generate actions that exist: a small model cannot invent
    a tool name or leave out a required output field.
    """
    choices = [
        {
            "type": "object",
            "properties": {
                "action": {"const": "tool"},
                "tool": {"const": t.name},
                "arguments": t.input_schema or {"type": "object"},
            },
            "required": ["action", "tool", "arguments"],
        }
        for t in tools
    ]
    choices.append(
        {
            "type": "object",
            "properties": {"action": {"const": "finish"}, "output": _finish_schema(output_schema)},
            "required": ["action", "output"],
        }
    )
    return {"anyOf": choices}


class JsonActionLLM:
    """For any OpenAI-compatible endpoint, e.g. an LLM gateway or a local Ollama server."""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str | None = None,
        client=None,
        structured: bool = False,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.structured = structured
        self.headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        # A model on a laptop CPU can take minutes for a long prompt: GOVAGENTS_HTTP_TIMEOUT.
        timeout = float(os.environ.get("GOVAGENTS_HTTP_TIMEOUT", "180"))
        self.client = client or httpx.Client(timeout=timeout)

    @staticmethod
    def to_chat_messages(system: str, messages: list[dict]) -> list[dict]:
        out = [{"role": "system", "content": system}]
        for m in messages:
            if m["role"] == "user":
                out.append({"role": "user", "content": m["content"]})
            elif m["role"] == "assistant":
                a = m["action"]
                payload = (
                    {"action": "tool", "tool": a["name"], "arguments": a["arguments"]}
                    if a["type"] == "tool"
                    else {"action": "finish", "output": a["output"]}
                )
                out.append({"role": "assistant", "content": json.dumps(payload)})
            elif m["role"] == "tool":
                label = "Error from" if m.get("is_error") else "Result of"
                out.append({"role": "user", "content": f"{label} {m['name']}:\n{m['content']}"})
        return out

    @staticmethod
    def parse(text: str) -> dict:
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise ValueError("no JSON object found")
        data = json.loads(match.group(0))
        if data.get("action") == "tool" and isinstance(data.get("tool"), str):
            return {
                "type": "tool",
                "id": "call_" + uuid.uuid4().hex[:8],
                "name": data["tool"],
                "arguments": data.get("arguments") or {},
            }
        if data.get("action") == "finish" and isinstance(data.get("output"), dict):
            return {
                "type": "finish",
                "id": "call_" + uuid.uuid4().hex[:8],
                "output": data["output"],
            }
        raise ValueError('expected {"action": "tool", ...} or {"action": "finish", ...}')

    def next_turn(self, system, messages, tools, output_schema) -> Turn:
        catalogue = "\n".join(
            f"- {t.name}: {t.description} {json.dumps(t.input_schema)}" for t in tools
        )
        protocol = JSON_PROTOCOL.format(
            output_schema=json.dumps(_finish_schema(output_schema)), tools=catalogue or "(none)"
        )
        headers = dict(self.headers)
        propagate.inject(headers)  # W3C traceparent: the gateway continues this trace
        body = {
            "model": self.model,
            "messages": self.to_chat_messages(f"{system}\n\n{protocol}", messages),
            "max_tokens": 2000,
            "temperature": 0,
        }
        if self.structured:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "action", "schema": action_schema(tools, output_schema)},
            }
        response = self.client.post(f"{self.base_url}/chat/completions", headers=headers, json=body)
        if response.status_code == 400 and self.structured:
            # The server does not accept this schema: carry on with the prompt alone.
            body.pop("response_format")
            response = self.client.post(
                f"{self.base_url}/chat/completions", headers=headers, json=body
            )
        if response.status_code >= 400:  # e.g. the gateway refused (budget, policy) or failed
            try:
                detail = response.json()["error"]["message"]
            except (ValueError, KeyError, TypeError):
                detail = response.text[:200]
            raise RuntimeError(f"model endpoint answered HTTP {response.status_code}: {detail}")
        data = response.json()
        usage = data.get("usage") or {}
        tokens = (int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0)))
        text = data["choices"][0]["message"].get("content") or ""
        try:
            return Turn(self.parse(text), None, *tokens)
        except (ValueError, json.JSONDecodeError) as exc:
            return Turn(None, f"Could not read your reply as a JSON action ({exc}).", *tokens)


# --------------------------------------------------------------------------- record/replay


class RecordingAgentLLM:
    def __init__(self, inner: AgentLLM | None, root: Path, key: str, offline: bool = False):
        self.inner = inner
        self.root = root
        self.key = key  # e.g. the model id, so recordings of different models never mix
        self.offline = offline

    def next_turn(self, system, messages, tools, output_schema) -> Turn:
        # Generated tool-call ids differ between runs, so they are left out of the key.
        stable = [
            {k: v for k, v in m.items() if k != "id"}
            if m["role"] != "assistant"
            else {
                "role": "assistant",
                "action": {k: v for k, v in m["action"].items() if k != "id"},
            }
            for m in messages
        ]
        request = [self.key, system, stable, [t.name for t in tools], output_schema]
        digest = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
        path = self.root / f"{digest}.json"
        if path.exists():
            return Turn(**json.loads(path.read_text(encoding="utf-8")))
        if self.offline or self.inner is None:
            raise ReplayMiss("No recorded model reply for this step.")
        turn = self.inner.next_turn(system, messages, tools, output_schema)
        self.root.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(turn)), encoding="utf-8")
        return turn
