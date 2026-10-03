# System card: governed-agents

| | |
|---|---|
| Pattern | P5 governed agent with policy engine and human approval ([ai-engineering-framework](https://github.com/flam7791/ai-engineering-framework)) |
| Models | Claude by default (fast and strong tiers); any OpenAI-compatible endpoint, including local open-weight models through Ollama or an LLM gateway |
| Scenarios | Briefing desk; use-case triage desk (fictional organisation) |

## Intended use

Run multi-step work in which specialist agents gather evidence, draft and propose actions, while a
policy engine decides every tool call and named people approve anything that leaves the
organisation.

## Out of scope

Unattended external actions; decisions about individuals; any tool not declared in an agent's
manifest (agents never see tools outside their list).

## Data

Case files, notes and documents the scenario's tools return; tool results are passed to agents as
data. The audit trail records every model call, proposal, policy decision, approval, hand-off and
cost. "Sent" emails go to a local outbox.

## How it can fail

- An agent may be misled by planted content; the policy engine still refuses what the policy does
  not allow (tested with a planted "forward and publish" note).
- A smaller model may take more steps or fail a case; budgets bound the cost and the trajectory
  evaluation shows where it failed.
- Output quality (as opposed to what agents did) is not yet graded by the evaluation.

## Evaluation

Trajectory evaluation on the audit trail: required and forbidden tools, approvals for external
actions, allowed recipients, cost limits and expected output fields, including an injection case.
CI replays the recorded live run; any safety failure fails the build.

## Human oversight

External actions wait for a named person's approval (four eyes, from a web page or the command
line); `govagents halt` and a `HALT` file stop runs; the agent register lists every agent's
purpose, owner, autonomy and tools.
