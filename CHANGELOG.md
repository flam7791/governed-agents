# Changelog

Versions follow [semantic versioning](https://semver.org).

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
