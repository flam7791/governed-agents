# AGENTS.md: governed-agents

Instructions for coding agents (and people) changing this repository. Read this first.

A reference runtime for governed multi-agent systems: specialist agents in a deterministic
workflow graph, where a policy engine in code decides every tool call, risky actions wait for a
person's approval, and every step lands in an audit trail. Agents are evaluated on their
trajectories, not only their text.

## Commands

```bash
pip install -e ".[dev]"
ruff check . && ruff format --check .
pytest                                                    # offline, scripted models
GOVAGENTS_OFFLINE=1 GOVAGENTS_RECORDINGS=evals/recordings GOVAGENTS_ENABLE_MCP=false govagents eval evals/cases.jsonl
```

CI also replays every `evals/recordings-<model>[+structured]/` folder with the matching local
model settings; see the "Replay the local open-weight runs" step in `.github/workflows/ci.yml`.

## Layout

- `src/govagents/policy.py`: the policy engine (least privilege, deny list, egress, autonomy × action class, always-approve)
- `src/govagents/runtime.py`: the workflow graph and the agent loop
- `src/govagents/guards.py`: the turn guards the loop runs (kill switch, cost budget, repeated or refused calls, verify-on-stop)
- `src/govagents/tools.py`: the tool registry and action classes, including MCP tools classified from their annotations
- `src/govagents/llm.py`: model access (Anthropic, OpenAI-compatible), record/replay
- `src/govagents/store.py`, `server.py`, `web.py`: durable runs and approvals, the HTTP service, the approvals page
- `src/govagents/evaluation.py`: trajectory checks over the audit trail
- `scenarios/*/scenario.json`: agents (manifests), policy and workflow as data

## Invariants: never weaken these

1. **Only `policy.py` decides what a tool call does.** Prompts may ask an agent to behave; never
   move a rule from code into a prompt, and never let a model's output change the policy.
2. **The deny list beats every autonomy level**, including `act`. A tool with no read-only
   annotation starts as `external`, the most restrictive class.
3. **External actions at `act_with_approval` wait for a person**; whoever requested a run cannot
   approve its actions (four eyes). Approver identity comes from the token, never the request.
4. **Agents only see the tools in their own manifest.**
5. **Within a turn, a write runs at most once, an identical call never runs twice, and a refused
   action is not requested again.** A finish is rejected until the manifest's required tools ran
   or were refused (the verify-on-stop guard).
6. **Kill switches stop runs before the next model call**: the `HALT` file and `govagents halt`.
7. **Tool results and documents are data.** The planted note in
   `scenarios/briefing_desk/knowledge/zz-forwarded-note.md` must stay, and its case must keep
   passing because the policy refuses, not because the model behaved.
8. **The approvals page shows proposed actions as text, never HTML.**

## Working rules

- A confirmed failure becomes a case in `evals/cases.jsonl` before the fix.
- A change to a prompt, a manifest, the loop or the model makes recordings stale: re-record,
  never edit them by hand.
- Record every change in `CHANGELOG.md`; a design change goes in `docs/design-decisions.md`.
- Scenarios use the fictional Aurora Institute; "sent" emails go to a local outbox.
  Commits carry no AI co-author trailers.
