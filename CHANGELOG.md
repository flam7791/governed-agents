# Changelog

Versions follow [semantic versioning](https://semver.org).

## 0.3.0

- OpenTelemetry tracing (optional `tracing` extra): a span per run, agent step, model call and
  tool call, with policy verdicts; trace context sent to the model endpoint. No content on spans.
- MCP servers reached by URL can require a bearer token (`"token_env"` in the scenario): the
  agents service authenticates as itself and the server applies that identity's clearance.
- `GOVAGENTS_HTTP_TIMEOUT` for slow local models; CI replays a local open-weight run when one
  is recorded.

## 0.2.0

- `govagents serve`: HTTP service with background runs, an approvals page, and roles
  (requester, approver, admin) taken from bearer tokens.
- Four eyes: whoever requested a run cannot decide on its actions.
- Prometheus metrics computed from the audit trail.
- MCP servers reachable by URL (Streamable HTTP), chosen per environment with `url_env`.
- Container image: non-root, health check, state on a volume.
- Refusals from a gateway (budget, policy) reach the audit trail with their reason.

## 0.1.0

- Runtime, policy engine, durable approvals, audit trail, kill switches, budgets, two scenarios,
  CLI, trajectory evaluation with record/replay.
