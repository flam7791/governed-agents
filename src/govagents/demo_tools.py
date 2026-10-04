"""The tools used by the two demonstration scenarios, over a local workspace.

Everything happens inside the data folder: the tracker and registers are SQLite tables, and
"sent" emails are written to an outbox folder instead of leaving the machine. The action class
of each tool is what matters for governance:

    read            search_notes, lookup_policy, estimate_cost, list_patterns
    write_internal  tracker_create, tracker_update, save_draft, register_usecase,
                    submit_decision_record (the scenario policy adds a human approval)
    external        send_email, publish_to_website (deny-listed by the scenario policy)
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from .config import Settings
from .tools import ToolRegistry, python_tool

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracker (case_id TEXT PRIMARY KEY, title TEXT, requester TEXT,
    deadline TEXT, topic TEXT, status TEXT, created REAL);
CREATE TABLE IF NOT EXISTS drafts (draft_id TEXT PRIMARY KEY, case_id TEXT, text TEXT,
    created REAL);
CREATE TABLE IF NOT EXISTS usecases (usecase_id TEXT PRIMARY KEY, title TEXT, owner TEXT,
    summary TEXT, data_classification TEXT, created REAL);
CREATE TABLE IF NOT EXISTS decisions (record_id TEXT PRIMARY KEY, usecase_id TEXT, record TEXT,
    created REAL);
"""


class Workspace:
    def __init__(self, data_dir: Path, knowledge_dir: Path, patterns_file: Path | None = None):
        self.db_path = data_dir / "workspace.db"
        self.outbox = data_dir / "outbox"
        self.knowledge_dir = knowledge_dir
        self.patterns_file = patterns_file
        data_dir.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.executescript(SCHEMA)

    def execute(self, sql: str, params: tuple = ()) -> list[tuple]:
        with closing(sqlite3.connect(self.db_path)) as db, db:
            return db.execute(sql, params).fetchall()

    def search(self, query: str, top_k: int = 3) -> list[dict]:
        """Simple keyword search over the Markdown notes, returning cited paragraphs."""
        terms = {w for w in re.findall(r"[a-z]{3,}", query.lower())}
        hits = []
        for path in sorted(self.knowledge_dir.glob("*.md")):
            title, section = path.stem, None
            for block in path.read_text(encoding="utf-8").split("\n\n"):
                block = block.strip()
                if block.startswith("# "):
                    title = block[2:].splitlines()[0]
                    continue
                if block.startswith("## "):
                    section = block[3:].splitlines()[0]
                    continue
                if not block or block.startswith("---"):
                    continue
                words = set(re.findall(r"[a-z]{3,}", block.lower()))
                score = len(terms & words)
                if score:
                    citation = f"{title}" + (f", {section}" if section else "")
                    hits.append(
                        {"score": score, "citation": citation, "text": " ".join(block.split())}
                    )
        hits.sort(key=lambda h: -h["score"])
        return [{"citation": h["citation"], "text": h["text"]} for h in hits[:top_k]]


def _id(prefix: str, *parts) -> str:
    """Identifiers derived from the content, so a recorded run replays identically."""
    digest = hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()
    return f"{prefix}-{digest[:6]}"


def register_briefing_tools(registry: ToolRegistry, ws: Workspace) -> None:
    @python_tool(
        "tracker_create",
        "Log a new request in the tracker. Returns the case id.",
        "write_internal",
        {
            "properties": {
                "title": {"type": "string"},
                "requester": {"type": "string", "description": "requester's email address"},
                "deadline": {"type": "string", "description": "YYYY-MM-DD"},
                "topic": {"type": "string"},
            },
            "required": ["title", "requester", "deadline", "topic"],
        },
    )
    def tracker_create(title, requester, deadline, topic):
        case_id = _id("CASE", title, requester, deadline, topic)
        ws.execute(
            "INSERT OR REPLACE INTO tracker VALUES (?, ?, ?, ?, ?, 'open', ?)",
            (case_id, title, requester, deadline, topic, time.time()),
        )
        return {"case_id": case_id}

    @python_tool(
        "tracker_update",
        "Set the status of a tracked request (open, drafting, sent, closed).",
        "write_internal",
        {
            "properties": {"case_id": {"type": "string"}, "status": {"type": "string"}},
            "required": ["case_id", "status"],
        },
    )
    def tracker_update(case_id, status):
        if not ws.execute("SELECT 1 FROM tracker WHERE case_id = ?", (case_id,)):
            raise ValueError(f"no case {case_id}")
        ws.execute("UPDATE tracker SET status = ? WHERE case_id = ?", (status, case_id))
        return {"case_id": case_id, "status": status}

    @python_tool(
        "search_notes",
        "Search the organisation's policy notes. Returns passages with citations.",
        "read",
        {
            "properties": {"query": {"type": "string"}, "top_k": {"type": "integer"}},
            "required": ["query"],
        },
    )
    def search_notes(query, top_k=3):
        return ws.search(query, min(int(top_k), 5))

    @python_tool(
        "save_draft",
        "Save a draft for a case. Returns the draft id.",
        "write_internal",
        {
            "properties": {"case_id": {"type": "string"}, "text": {"type": "string"}},
            "required": ["case_id", "text"],
        },
    )
    def save_draft(case_id, text):
        draft_id = _id("DRAFT", case_id, text)
        ws.execute(
            "INSERT OR REPLACE INTO drafts VALUES (?, ?, ?, ?)",
            (draft_id, case_id, text, time.time()),
        )
        return {"draft_id": draft_id}

    @python_tool(
        "send_email",
        "Send an email. This leaves the organisation.",
        "external",
        {
            "properties": {
                "to": {"type": "string"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["to", "subject", "body"],
        },
    )
    def send_email(to, subject, body):
        ws.outbox.mkdir(parents=True, exist_ok=True)
        message_id = _id("MSG", to, subject, body)
        (ws.outbox / f"{message_id}.txt").write_text(
            f"To: {to}\nSubject: {subject}\n\n{body}\n", encoding="utf-8"
        )
        return {"sent": True, "message_id": message_id}

    @python_tool(
        "publish_to_website",
        "Publish a text on the public website.",
        "external",
        {
            "properties": {"title": {"type": "string"}, "body": {"type": "string"}},
            "required": ["title", "body"],
        },
    )
    def publish_to_website(title, body):  # deny-listed: should never run
        raise RuntimeError("publishing is not implemented in this demonstration")

    for tool in (
        tracker_create,
        tracker_update,
        search_notes,
        save_draft,
        send_email,
        publish_to_website,
    ):
        registry.add(tool)


def register_triage_tools(registry: ToolRegistry, ws: Workspace, settings: Settings) -> None:
    @python_tool(
        "register_usecase",
        "Add a use case to the AI use-case register. Returns the use-case id.",
        "write_internal",
        {
            "properties": {
                "title": {"type": "string"},
                "owner": {"type": "string"},
                "summary": {"type": "string"},
                "data_classification": {
                    "type": "string",
                    "enum": ["public", "internal", "restricted"],
                },
            },
            "required": ["title", "owner", "summary", "data_classification"],
        },
    )
    def register_usecase(title, owner, summary, data_classification):
        usecase_id = _id("UC", title, owner, summary)
        ws.execute(
            "INSERT OR REPLACE INTO usecases VALUES (?, ?, ?, ?, ?, ?)",
            (usecase_id, title, owner, summary, data_classification, time.time()),
        )
        return {"usecase_id": usecase_id}

    @python_tool(
        "lookup_policy",
        "Search the organisation's AI and data policies. Returns passages with citations.",
        "read",
        {"properties": {"query": {"type": "string"}}, "required": ["query"]},
    )
    def lookup_policy(query):
        return ws.search(query, 4)

    @python_tool(
        "estimate_cost",
        "Estimate the monthly model cost of a use case in USD.",
        "read",
        {
            "properties": {
                "requests_per_month": {"type": "integer"},
                "input_tokens_per_request": {"type": "integer"},
                "output_tokens_per_request": {"type": "integer"},
                "model_tier": {"type": "string", "enum": ["fast", "strong"]},
            },
            "required": [
                "requests_per_month",
                "input_tokens_per_request",
                "output_tokens_per_request",
                "model_tier",
            ],
        },
    )
    def estimate_cost(
        requests_per_month, input_tokens_per_request, output_tokens_per_request, model_tier
    ):
        price_in, price_out = settings.estimate_prices[model_tier]
        monthly = (
            int(requests_per_month) * int(input_tokens_per_request) * price_in
            + int(requests_per_month) * int(output_tokens_per_request) * price_out
        ) / 1_000_000
        return {
            "monthly_cost_usd": round(monthly, 2),
            "prices_usd_per_million_tokens": {"input": price_in, "output": price_out},
        }

    @python_tool(
        "list_patterns",
        "List the organisation's reference solution patterns.",
        "read",
        {"properties": {}},
    )
    def list_patterns():
        return json.loads(ws.patterns_file.read_text(encoding="utf-8")) if ws.patterns_file else []

    @python_tool(
        "submit_decision_record",
        "Submit the decision record for a use case to the approving body.",
        "write_internal",
        {
            "properties": {
                "usecase_id": {"type": "string"},
                "recommendation": {
                    "type": "string",
                    "enum": [
                        "approve",
                        "approve_with_conditions",
                        "reject",
                        "needs_more_information",
                    ],
                },
                "record": {"type": "string", "description": "the full decision record"},
            },
            "required": ["usecase_id", "recommendation", "record"],
        },
    )
    def submit_decision_record(usecase_id, recommendation, record):
        record_id = _id("DEC", usecase_id, recommendation, record)
        ws.execute(
            "INSERT OR REPLACE INTO decisions VALUES (?, ?, ?, ?)",
            (
                record_id,
                usecase_id,
                json.dumps({"recommendation": recommendation, "record": record}),
                time.time(),
            ),
        )
        return {"record_id": record_id, "status": "submitted"}

    for tool in (
        register_usecase,
        lookup_policy,
        estimate_cost,
        list_patterns,
        submit_decision_record,
    ):
        registry.add(tool)
