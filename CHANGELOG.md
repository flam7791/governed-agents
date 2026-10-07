# Changelog

Versions follow [semantic versioning](https://semver.org).

## Unreleased

- Results of Qwen 2.5 7B with structured output on 0.3.6: all five cases completed and safe,
  one risk judgement wrong. Recorded in `evals/recordings-qwen2.5-7b-ctx8k+structured`, replayed
  in CI, compared with Llama 3.1 8B in the README.

## 0.3.6

- Required tools: an agent's manifest can name tools that must have run (or been refused by the
  policy or a person) before its finish is accepted (event `finish_refused`). The drafter must
  save its draft and the secretary must submit the decision record. Found in the run with
  Qwen 2.5 7B, which reported a draft id and a submitted record without calling either tool.
  Recorded runs of Claude and Llama 3.1 8B replay unchanged.
- Results of Llama 3.1 8B with structured output (0.3.5): all five cases completed and safe,
  one classification judgement still wrong. Recorded in
  `evals/recordings-llama3.1-8b-ctx8k+structured`, replayed in CI, compared with the
  prompt-only run in the README.

## 0.3.5

From the second structured run with Llama 3.1 8B:

- A person's rejection is final for the rest of the agent's turn: the action is not requested
  again (the model had asked twice), and the agent is told to finish. Actions refused by the
  policy are treated the same way.
- The same call is never executed twice in a turn; with structured output, a tool without
  arguments is no longer offered once called (the architect had listed the patterns four
  times).
- Cost estimates for proposed services use the organisation's prices per tier, not the
  prices of the model running the agents (a local run had estimated every service at $0).

## 0.3.4

- A write or external tool runs at most once per turn of an agent: a repeat is not executed and
  the agent is told to move on or finish (event `repeat_skipped`); with structured output, a
  tool already done is no longer offered. Found in the first structured run, where Llama 3.1 8B
  registered the same use case four times.

## 0.3.3

- `GOVAGENTS_STRUCTURED_OUTPUT`: on an OpenAI-compatible endpoint, each reply is constrained by
  a JSON schema to one of the agent's own tools (with its arguments) or a finish that matches
  the output schema, so a small model cannot invent a tool or leave out a required field. If the
  server refuses the schema, the call is repeated without it. Recordings made this way are kept
  apart, and CI replays them with the same setting (`recordings-<model>+structured`).

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
