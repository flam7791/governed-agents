# Design decisions

## 1. A deterministic graph with autonomous agents inside

**Context.** A single agent that decides everything is hard to predict, test and audit. A
fixed pipeline cannot adapt to what it finds.
**Decision.** The order of stages, the hand-offs and the review loops are code. Within its
stage, each agent chooses its own actions, bounded by its tool list, autonomy and step budget.
**Consequences.** Runs are explainable and testable stage by stage; agents still handle
variety. A task whose path cannot be known in advance would need a model-chosen next stage,
bounded to a named set (on the roadmap).

## 2. Governance in code, not in prompts

**Context.** Instructions in a prompt are suggestions a model can ignore, or that injected text
can override.
**Decision.** Every tool call passes through one policy engine: least privilege, deny list,
egress rules, autonomy × action class, always-approve. The prompt tells agents the rules so
they plan well; the engine enforces them whatever the agents do.
**Consequences.** The injection test can "succeed" at fooling an agent and still leak nothing.
The evaluation counts refused attempts, which shows how much work the controls are doing.

## 3. Action classes on tools, autonomy levels on agents

**Context.** Risk depends on both who acts and what the action does.
**Decision.** Tools declare read, write_internal (reversible) or external (leaves the
organisation or cannot be undone). Agents declare observe, draft, act_with_approval or act.
A small matrix gives the decision.
**Consequences.** Adding a tool or an agent means classifying it, which is a governance
conversation worth having. Unknown MCP tools default to external, so they start locked down.

## 4. Durable, resumable approvals

**Context.** A person may take hours to approve; the process may restart meanwhile.
**Decision.** When an action needs approval, the whole run (stage, case file, agent
conversation) is saved and the run pauses. The decision is recorded with the person's name
and note. Approval executes the exact action that was proposed; rejection is reported back to
the agent as a result it must deal with.
**Consequences.** No action runs on a stale or modified request, and a rejection is part of the
record, not a crash.

## 5. One action per turn

**Context.** Parallel tool calls make approvals and audit ambiguous.
**Decision.** Claude is required to make exactly one tool call per turn, and the JSON protocol
allows one action.
**Consequences.** Slightly more turns; a clean, linear trail where every action has one policy
decision.

## 6. Two ways to reach models

**Context.** Organisations route model calls through a gateway, or run open-weight models
locally, and native tool calling is not always available there.
**Decision.** Claude with native tool use, or any OpenAI-compatible endpoint with a JSON action
protocol. Malformed replies are sent back as errors and never executed.
**Consequences.** The same agents run through a governed gateway or on a local model; quality
varies by model, which is what the evaluation is for.

## 7. Evaluate trajectories, record and replay

**Context.** Agents can write a good final answer after doing something they should not have
done.
**Decision.** Evaluation cases check the audit trail (what ran, what was refused, what went
through a person, where data went) as well as outputs. Model replies are recorded, with
identifiers made content-derived, so runs replay exactly and CI can run them without a key.
**Consequences.** Safety regressions fail the build. Changing a prompt means re-recording,
which is correct: it is a different system.

## 8. Identity from credentials, and four eyes

**Context.** An approval is only worth something if we know who gave it, and if the person who
wanted the action is not the one who allows it.
**Decision.** The service takes identity from the caller's token, never from the request body,
and records that name on every decision. Roles separate requesting from deciding, and the
runtime refuses a decision by the person who requested the run, whatever their role.
**Consequences.** The audit trail answers "who allowed this" reliably. Tokens are a stand-in:
in production, single sign-on would provide the identity and the roles.

## 9. Monitoring from the durable record

**Context.** Counters kept in memory reset on restart and disagree between instances.
**Decision.** The metrics endpoint computes its figures from the SQLite record (runs, policy
decisions, model calls, approvals) at scrape time.
**Consequences.** Figures survive restarts and match the audit trail exactly. The cost is a few
queries per scrape, which is fine at this scale; a large deployment would pre-aggregate.

## 10. Turn rules as a chain of guards, not branches in the loop

**Context.** By 0.3.6 the agent loop carried five rules of its own besides the policy engine: the
kill switch, the run cost budget, no second request after a refusal, no repeated write or call,
and no finish before the required tools ran. Each had been added as one more branch inside the
loop, so the loop was growing with every live run that found a new failure. A source-level study
of eleven production coding agents recommends a linear loop until three or more independent turn
policies appear, then a middleware pipeline ([Barbaste et al., 2026][harness], Recommendation 1).
**Decision.** Move the rules into `guards.py`: each is a guard that can act at fixed points of a
turn (before the model call, on the tools offered, after the model call, on a finish, on a tool
proposal), run in a fixed order, first rejection wins. The policy engine stays separate: it
decides what a tool call may do; guards decide what a turn may do.
**Consequences.** Same behaviour, proven by replaying every recorded trajectory run (Claude,
Llama 3.1 8B with and without structured output, Qwen 2.5 7B) with identical results before and
after. A new rule is a guard with its own test. The order of the chain is now part of the design
and is tested.

[harness]: https://arxiv.org/abs/2609.00006
