# Changelog

Versions follow [semantic versioning](https://semver.org).

## 0.3.2

- First local open-weight run (Llama 3.1 8B on a laptop CPU), recorded and replayed in CI.
- Evaluation: an expected approval that was never reached because the run stopped early is a
  functional failure (`approval_reached`), not a safety failure; safety is "never ran without a
  person". Claude's recorded results are unchanged.
- `GOVAGENTS_PRICE_FAST` / `GOVAGENTS_PRICE_STRONG` set the prices used in the cost report.

## 0.3.1

- A tool's work (an MCP call, for example) is traced as a child of its `execute_tool` span.

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
