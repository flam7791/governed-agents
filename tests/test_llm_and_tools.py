"""Model adapters (against local stand-ins) and MCP tool classification."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import anthropic
import httpx
import pytest
from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

from govagents.llm import AnthropicAgentLLM, JsonActionLLM, RecordingAgentLLM, ReplayMiss, Turn
from govagents.models import ToolSpec
from govagents.tools import ToolRegistry

SEARCH = ToolSpec("search_notes", "Search notes", {"type": "object", "properties": {}}, "read")
SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}
HISTORY = [
    {"role": "user", "content": "Case file: ..."},
    {
        "role": "assistant",
        "action": {
            "type": "tool",
            "id": "toolu_1",
            "name": "search_notes",
            "arguments": {"query": "x"},
        },
    },
    {"role": "tool", "id": "toolu_1", "name": "search_notes", "content": "[]", "is_error": False},
    {"role": "user", "content": "Error: try again."},
]


class FakeMessagesApi(BaseHTTPRequestHandler):
    received: list[dict] = []
    reply_name = "finish"

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeMessagesApi.received.append(body)
        payload = json.dumps(
            {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": body["model"],
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_9",
                        "name": FakeMessagesApi.reply_name,
                        "input": {"answer": "done"},
                    }
                ],
                "stop_reason": "tool_use",
                "stop_sequence": None,
                "usage": {"input_tokens": 50, "output_tokens": 7},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


def test_anthropic_adapter_forces_one_tool_call_and_maps_finish():
    server = HTTPServer(("127.0.0.1", 0), FakeMessagesApi)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        sdk = anthropic.Anthropic(
            api_key="k", base_url=f"http://127.0.0.1:{server.server_port}", max_retries=0
        )
        turn = AnthropicAgentLLM("claude-test", client=sdk).next_turn(
            "sys", HISTORY, [SEARCH], SCHEMA
        )
    finally:
        server.shutdown()
    sent = FakeMessagesApi.received[-1]
    assert sent["tool_choice"] == {"type": "any", "disable_parallel_tool_use": True}
    assert [t["name"] for t in sent["tools"]] == ["search_notes", "finish"]
    assert sent["tools"][1]["input_schema"] == SCHEMA
    # tool result and the follow-up error text are merged into one user turn, result first
    assert [m["role"] for m in sent["messages"]] == ["user", "assistant", "user"]
    assert [b["type"] for b in sent["messages"][2]["content"]] == ["tool_result", "text"]
    assert turn.action == {"type": "finish", "id": "toolu_9", "output": {"answer": "done"}}
    assert (turn.input_tokens, turn.output_tokens) == (50, 7)


def json_llm(reply_text):
    def handler(request):
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": reply_text}}],
                "usage": {"prompt_tokens": 30, "completion_tokens": 5},
            },
        )

    return JsonActionLLM(
        "http://gateway/v1",
        "strong",
        api_key="gw_key",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_json_action_adapter_parses_tool_and_finish_actions():
    turn = json_llm(
        '```json\n{"action": "tool", "tool": "search_notes", "arguments": {"query": "x"}}\n```'
    ).next_turn("sys", HISTORY, [SEARCH], SCHEMA)
    assert turn.action["type"] == "tool" and turn.action["name"] == "search_notes"
    turn = json_llm('{"action": "finish", "output": {"answer": "done"}}').next_turn(
        "sys", HISTORY, [SEARCH], SCHEMA
    )
    assert turn.action["output"] == {"answer": "done"}


def test_json_action_adapter_never_executes_a_malformed_reply():
    turn = json_llm("I think I will send the email now.").next_turn(
        "sys", HISTORY, [SEARCH], SCHEMA
    )
    assert turn.action is None and "JSON" in turn.error


def test_recordings_replay_even_when_generated_ids_differ(tmp_path):
    class Once:
        calls = 0

        def next_turn(self, *args):
            Once.calls += 1
            return Turn({"type": "finish", "id": "a1", "output": {"answer": "x"}}, None, 10, 2)

    RecordingAgentLLM(Once(), tmp_path, "m").next_turn("sys", HISTORY, [SEARCH], SCHEMA)
    other_ids = json.loads(json.dumps(HISTORY).replace("toolu_1", "call_77"))
    replay = RecordingAgentLLM(None, tmp_path, "m", offline=True)
    assert replay.next_turn("sys", other_ids, [SEARCH], SCHEMA).action["output"] == {"answer": "x"}
    with pytest.raises(ReplayMiss):
        replay.next_turn("other system prompt", HISTORY, [SEARCH], SCHEMA)


async def test_mcp_tools_are_classified_from_their_annotations():
    server = MCPServer("demo")

    @server.tool(annotations=ToolAnnotations(read_only_hint=True))
    def lookup(term: str) -> str:
        """Look something up."""
        return f"found {term}"

    @server.tool()
    def delete_record(record_id: str) -> str:
        """Delete a record."""
        return "deleted"

    registry = ToolRegistry()
    try:
        added = await registry.connect_mcp("demo", server)
        assert set(added) == {"lookup", "delete_record"}
        assert registry.spec("lookup").action_class == "read"
        assert registry.spec("delete_record").action_class == "external"  # unknown = locked down
        assert registry.spec("lookup").source == "mcp:demo"
        text, is_error = await registry.call("lookup", {"term": "budget"})
        assert (text, is_error) == ("found budget", False)
    finally:
        await registry.aclose()


async def test_tool_errors_are_reported_not_raised():
    registry = ToolRegistry()
    text, is_error = await registry.call("missing", {})
    assert is_error and "Unknown tool" in text


def test_gateway_refusals_surface_with_their_reason():
    def handler(request):
        return httpx.Response(
            402, json={"error": {"message": "Monthly budget reached for team 'agents'."}}
        )

    llm = JsonActionLLM(
        "http://gateway/v1", "fast", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(RuntimeError, match="HTTP 402: Monthly budget reached"):
        llm.next_turn("sys", HISTORY, [SEARCH], SCHEMA)
