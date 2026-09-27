"""HTTP service: start runs, follow them, and let named people approve or reject actions.

    GET  /                              the approvals page, for the people who decide
    GET  /healthz
    GET  /metrics                       Prometheus metrics (token if GOVAGENTS_METRICS_TOKEN set)
    GET  /api/me                        who this token belongs to
    GET  /api/scenarios                 the agent register
    POST /api/runs                      {"scenario", "request"} -> 202 {"run_id"}  requester, admin
    GET  /api/runs                      recent runs                                 any role
    GET  /api/runs/{id}                 status and audit trail                      any role
    POST /api/runs/{id}/halt            kill switch                                 admin
    GET  /api/approvals                 actions waiting for a person                any role
    POST /api/approvals/{id}/approve    {"note"}                                    approver, admin
    POST /api/approvals/{id}/reject     {"note"} (required)                         approver, admin

Identity comes from the bearer token, never from the request body. GOVAGENTS_API_TOKENS maps
tokens to a person and a role: "alice:requester:TOKEN,bob:approver:TOKEN,ops:admin:TOKEN". The
recorded approver is the token's owner, and whoever requested a run cannot decide on its actions
(four eyes). In production the tokens would give way to single sign-on.

Runs execute in the background: a POST returns at once, and the run continues until it
completes, fails or waits for a person.
"""

# No "from __future__ import annotations" in this module: FastAPI reads the dependency
# annotations at runtime, and they refer to functions defined inside create_app.

import asyncio
import hmac
import logging
import time
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily
from pydantic import BaseModel, Field

from . import __version__
from .build import open_runner
from .config import Settings
from .runtime import FourEyesViolation, Scenario, check_decision, create_run
from .store import Store
from .web import APPROVALS_PAGE

log = logging.getLogger(__name__)

PERMISSIONS = {
    "requester": {"run", "read"},
    "approver": {"read", "decide"},
    "admin": {"run", "read", "decide", "halt"},
}


@dataclass(frozen=True)
class Caller:
    name: str
    role: str


def parse_tokens(text: str) -> dict[str, Caller]:
    """Parse "name:role:token,..." (tokens of at least 16 characters)."""
    tokens: dict[str, Caller] = {}
    for item in filter(None, (part.strip() for part in text.split(","))):
        name, role, token = (piece.strip() for piece in item.split(":", 2))
        if role not in PERMISSIONS:
            raise ValueError(f"{name}: role must be one of {sorted(PERMISSIONS)}")
        if len(token) < 16:
            raise ValueError(f"{name}: tokens must be at least 16 characters")
        tokens[token] = Caller(name, role)
    return tokens


class RunBody(BaseModel):
    scenario: str
    request: str = Field(min_length=1, max_length=20_000)


class DecisionBody(BaseModel):
    note: str = Field(default="", max_length=2_000)


class StoreCollector:
    """Prometheus figures computed from the durable record, so they survive restarts."""

    def __init__(self, store: Store):
        self.store = store

    def collect(self):
        figures = self.store.monitoring_figures()
        runs = GaugeMetricFamily(
            "govagents_runs", "Runs by scenario and current status", labels=["scenario", "status"]
        )
        for row in figures["runs"]:
            runs.add_metric([row["scenario"], row["status"]], row["n"])
        policy = CounterMetricFamily(
            "govagents_policy_decisions",
            "Tool calls decided by the policy engine (allow, approve, deny)",
            labels=["agent", "tool", "verdict"],
        )
        for row in figures["policy"]:
            policy.add_metric([row["agent"], row["tool"] or "", row["verdict"] or ""], row["n"])
        calls = CounterMetricFamily("govagents_model_calls", "Model calls", labels=["scenario"])
        cost = CounterMetricFamily(
            "govagents_model_cost_usd", "Estimated model spend", labels=["scenario"]
        )
        tokens = CounterMetricFamily(
            "govagents_model_tokens", "Model tokens", labels=["scenario", "direction"]
        )
        for row in figures["model_calls"]:
            calls.add_metric([row["scenario"]], row["calls"])
            cost.add_metric([row["scenario"]], row["cost"] or 0.0)
            tokens.add_metric([row["scenario"], "input"], row["input_tokens"] or 0)
            tokens.add_metric([row["scenario"], "output"], row["output_tokens"] or 0)
        approvals = GaugeMetricFamily(
            "govagents_approvals", "Approval requests by status", labels=["status"]
        )
        oldest = GaugeMetricFamily(
            "govagents_oldest_pending_approval_age_seconds",
            "How long the oldest action has been waiting for a person",
        )
        pending_since = None
        for row in figures["approvals"]:
            approvals.add_metric([row["status"]], row["n"])
            if row["status"] == "pending":
                pending_since = row["oldest"]
        oldest.add_metric([], time.time() - pending_since if pending_since else 0.0)
        yield from (runs, policy, calls, cost, tokens, approvals, oldest)


def create_app(
    settings: Settings,
    tokens: dict[str, Caller],
    llm_for_tier=None,
    metrics_token: str | None = None,
) -> FastAPI:
    if not tokens:
        raise ValueError("No API tokens configured: set GOVAGENTS_API_TOKENS.")
    app = FastAPI(title="governed-agents", version=__version__)
    store = Store(settings.data_dir / "runs.db")
    scenarios = {
        p.parent.name: Scenario.load(p.parent)
        for p in sorted(settings.scenarios_dir.glob("*/scenario.json"))
    }
    registry = CollectorRegistry()
    registry.register(StoreCollector(store))
    background: set[asyncio.Task] = set()
    deciding: set[str] = set()
    app.state.background = background

    def caller(authorization: Annotated[str | None, Header()] = None) -> Caller:
        scheme, _, presented = (authorization or "").partition(" ")
        presented = presented.strip() if scheme.lower() == "bearer" else ""
        for token, who in tokens.items():
            if presented and hmac.compare_digest(presented, token):
                return who
        raise HTTPException(401, "Missing or invalid token.")

    def allowed(permission: str):
        def check(who: Annotated[Caller, Depends(caller)]) -> Caller:
            if permission not in PERMISSIONS[who.role]:
                raise HTTPException(403, f"The {who.role} role cannot do this.")
            return who

        return check

    def in_background(coroutine, label: str) -> None:
        async def guarded():
            try:
                await coroutine
            except Exception:
                log.exception("background %s failed", label)

        task = asyncio.create_task(guarded())
        background.add(task)
        task.add_done_callback(background.discard)

    async def continue_run(run_id: str, scenario: str) -> None:
        async with open_runner(settings, scenario, llm_for_tier) as runner:
            await runner.continue_run(run_id)

    async def decide(approval_id, scenario, approved, by, note) -> None:
        try:
            async with open_runner(settings, scenario, llm_for_tier) as runner:
                await runner.decide(approval_id, approved, by, note)
        finally:
            deciding.discard(approval_id)

    @app.get("/", response_class=HTMLResponse)
    def page():
        return APPROVALS_PAGE

    @app.get("/healthz")
    def health():
        return {"status": "ok", "version": __version__}

    @app.get("/metrics")
    def metrics(authorization: Annotated[str | None, Header()] = None):
        if metrics_token and not hmac.compare_digest(
            authorization or "", f"Bearer {metrics_token}"
        ):
            raise HTTPException(401, "Metrics need the monitoring token.")
        return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)

    @app.get("/api/me")
    def me(who: Annotated[Caller, Depends(caller)]):
        return {"name": who.name, "role": who.role, "permissions": sorted(PERMISSIONS[who.role])}

    @app.get("/api/scenarios")
    def register(who: Annotated[Caller, Depends(allowed("read"))]):
        return {
            name: {
                "description": s.description,
                "agents": [a.to_row() for a in s.agents.values()],
            }
            for name, s in scenarios.items()
        }

    @app.post("/api/runs", status_code=202)
    async def start_run(body: RunBody, who: Annotated[Caller, Depends(allowed("run"))]):
        if body.scenario not in scenarios:
            raise HTTPException(404, f"No scenario '{body.scenario}'.")
        run_id = create_run(store, body.scenario, body.request, requested_by=who.name)
        in_background(continue_run(run_id, body.scenario), f"run {run_id}")
        return {"run_id": run_id, "status": "running"}

    @app.get("/api/runs")
    def runs(who: Annotated[Caller, Depends(allowed("read"))], limit: int = 20):
        return store.list_runs(min(max(limit, 1), 200))

    @app.get("/api/runs/{run_id}")
    def run(run_id: str, who: Annotated[Caller, Depends(allowed("read"))]):
        try:
            scenario, state = store.load_run(run_id)
        except KeyError:
            raise HTTPException(404, f"No run {run_id}.") from None
        return {
            "id": run_id,
            "scenario": scenario,
            "status": store.run_status(run_id),
            "requested_by": state.get("requested_by"),
            "spent_usd": state.get("spent_usd", 0.0),
            "error": state.get("error"),
            "events": store.events(run_id),
        }

    @app.post("/api/runs/{run_id}/halt")
    def halt(run_id: str, who: Annotated[Caller, Depends(allowed("halt"))]):
        try:
            store.run_status(run_id)
        except KeyError:
            raise HTTPException(404, f"No run {run_id}.") from None
        store.halt(run_id)
        store.event(run_id, None, "halt_requested", by=who.name)
        return {"run_id": run_id, "status": "halted"}

    @app.get("/api/approvals")
    def approvals(who: Annotated[Caller, Depends(allowed("read"))]):
        return store.pending_approvals()

    def _decision(approval_id: str, approved: bool, note: str, who: Caller):
        if approval_id in deciding:
            raise HTTPException(409, "A decision on this action is already being processed.")
        try:
            approval = check_decision(store, approval_id, who.name)
        except KeyError:
            raise HTTPException(404, f"No approval {approval_id}.") from None
        except FourEyesViolation as exc:
            raise HTTPException(403, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        scenario, _ = store.load_run(approval["run_id"])
        deciding.add(approval_id)
        in_background(
            decide(approval_id, scenario, approved, who.name, note), f"decision {approval_id}"
        )
        return {
            "approval_id": approval_id,
            "run_id": approval["run_id"],
            "decision": "approved" if approved else "rejected",
            "by": who.name,
        }

    @app.post("/api/approvals/{approval_id}/approve", status_code=202)
    async def approve(
        approval_id: str, body: DecisionBody, who: Annotated[Caller, Depends(allowed("decide"))]
    ):
        return _decision(approval_id, True, body.note, who)

    @app.post("/api/approvals/{approval_id}/reject", status_code=202)
    async def reject(
        approval_id: str, body: DecisionBody, who: Annotated[Caller, Depends(allowed("decide"))]
    ):
        if not body.note.strip():
            raise HTTPException(422, "Say why the action is rejected: the agent is told.")
        return _decision(approval_id, False, body.note, who)

    return app
