"""Command line.

govagents register [--csv FILE]        the agent register: every agent and what it may do
govagents run SCENARIO INPUT_FILE      start a run (stops when a person must approve)
govagents approvals                    list actions waiting for a person
govagents approve ID [--by NAME]       approve an action and resume the run
govagents reject ID --note TEXT        reject an action and resume the run
govagents trace RUN_ID                 the audit trail of a run
govagents halt RUN_ID                  kill switch for one run
govagents runs                         recent runs
govagents eval CASES_FILE              trajectory evaluation
govagents serve [--host H --port P]    the HTTP service and approvals page
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import getpass
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from .config import Settings
from .runtime import Scenario
from .store import Store


def _scenarios(settings: Settings) -> list[Scenario]:
    return [Scenario.load(p.parent) for p in sorted(settings.scenarios_dir.glob("*/scenario.json"))]


def _register(args, settings) -> int:
    rows = [agent.to_row() for s in _scenarios(settings) for agent in s.agents.values()]
    columns = ["scenario", "name", "purpose", "owner", "autonomy", "model", "max_steps", "tools"]
    for row in rows:
        print(
            f"{row['scenario']:<15} {row['name']:<15} {row['autonomy']:<18} "
            f"{row['model']:<7} steps≤{row['max_steps']:<3} tools: {row['tools'] or '(none)'}"
        )
    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        print(f"\nWrote {args.csv}", file=sys.stderr)
    return 0


def _print_status(store: Store, run_id: str) -> None:
    status = store.run_status(run_id)
    _, state = store.load_run(run_id)
    print(f"Run {run_id}: {status}  (model cost ${state['spent_usd']:.4f})")
    if status == "waiting_approval":
        for a in store.pending_approvals():
            if a["run_id"] == run_id:
                print(f"\nWaiting for approval {a['id']}: {a['agent']} wants to call {a['tool']}")
                print(json.dumps(a["arguments"], indent=2, ensure_ascii=False))
                print(f"\nApprove: govagents approve {a['id']}")
                print(f'Reject:  govagents reject {a["id"]} --note "reason"')
    if state.get("error"):
        print(f"Error: {state['error']}")


async def _run(args, settings) -> int:
    from .build import open_runner

    request = Path(args.input).read_text(encoding="utf-8")
    async with open_runner(settings, args.scenario) as runner:
        run_id = await runner.start(request)
        _print_status(runner.store, run_id)
    return 0


async def _decide(args, settings, approved: bool) -> int:
    from .build import open_runner

    store = Store(settings.data_dir / "runs.db")
    approval = store.get_approval(args.id)
    scenario, _ = store.load_run(approval["run_id"])
    async with open_runner(settings, scenario) as runner:
        await runner.decide(args.id, approved, args.by or getpass.getuser(), args.note or "")
        _print_status(runner.store, approval["run_id"])
    return 0


def _approvals(args, settings) -> int:
    pending = Store(settings.data_dir / "runs.db").pending_approvals()
    if not pending:
        print("Nothing is waiting for approval.")
    for a in pending:
        print(f"{a['id']}  run {a['run_id']}  {a['agent']} -> {a['tool']}  ({a['reason']})")
        print("    " + json.dumps(a["arguments"], ensure_ascii=False)[:300])
    return 0


def _trace(args, settings) -> int:
    store = Store(settings.data_dir / "runs.db")
    for e in store.events(args.run_id):
        when = datetime.fromtimestamp(e["ts"]).strftime("%H:%M:%S")
        detail = json.dumps(e["detail"], ensure_ascii=False)
        print(f"{when} {e['agent'] or '-':<14} {e['kind']:<18} {detail[:160]}")
    _print_status(store, args.run_id)
    return 0


def _halt(args, settings) -> int:
    store = Store(settings.data_dir / "runs.db")
    store.halt(args.run_id)
    print(f"Run {args.run_id} halted; pending approvals cancelled.")
    return 0


def _runs(args, settings) -> int:
    for r in Store(settings.data_dir / "runs.db").list_runs():
        when = datetime.fromtimestamp(r["created"]).strftime("%Y-%m-%d %H:%M")
        print(f"{r['id']}  {when}  {r['scenario']:<15} {r['status']}")
    return 0


async def _eval(args, settings) -> int:
    from .build import open_runner
    from .evaluation import load_cases, report, run_case

    results = []
    for case in load_cases(Path(args.cases)):
        request = case.get("input") or Path(case["input_file"]).read_text(encoding="utf-8")
        async with open_runner(settings, case["scenario"]) as runner:
            results.append(await run_case(runner, case, request))
    text = f"# Trajectory evaluation: {Path(args.cases).name}\n\n" + report(results)
    print(text)
    if args.out:
        Path(args.out).mkdir(parents=True, exist_ok=True)
        (Path(args.out) / "eval.md").write_text(text, encoding="utf-8")
    return 0 if all(r.safety_ok for r in results) else 1


def _serve(args, settings) -> int:
    import os

    import uvicorn

    from .server import create_app, parse_tokens

    tokens = parse_tokens(os.environ.get("GOVAGENTS_API_TOKENS", ""))
    if not tokens:
        print(
            "Set GOVAGENTS_API_TOKENS, e.g. alice:requester:<token>,bob:approver:<token>",
            file=sys.stderr,
        )
        return 2
    app = create_app(
        settings, tokens, metrics_token=os.environ.get("GOVAGENTS_METRICS_TOKEN") or None
    )
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="govagents", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("register")
    p.add_argument("--csv")
    p = sub.add_parser("run")
    p.add_argument("scenario")
    p.add_argument("input")
    sub.add_parser("approvals")
    for name in ("approve", "reject"):
        p = sub.add_parser(name)
        p.add_argument("id")
        p.add_argument("--by")
        p.add_argument("--note", required=(name == "reject"))
    p = sub.add_parser("trace")
    p.add_argument("run_id")
    p = sub.add_parser("halt")
    p.add_argument("run_id")
    sub.add_parser("runs")
    p = sub.add_parser("eval")
    p.add_argument("cases")
    p.add_argument("--out")
    p = sub.add_parser("serve")
    p.add_argument("--host", default="127.0.0.1", help="0.0.0.0 inside a container")
    p.add_argument("--port", type=int, default=8090)

    args = parser.parse_args(argv)
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    settings = Settings.from_env()
    if args.command == "run":
        return asyncio.run(_run(args, settings))
    if args.command in ("approve", "reject"):
        return asyncio.run(_decide(args, settings, args.command == "approve"))
    if args.command == "eval":
        return asyncio.run(_eval(args, settings))
    handlers = {
        "register": _register,
        "approvals": _approvals,
        "trace": _trace,
        "halt": _halt,
        "runs": _runs,
        "serve": _serve,
    }
    return handlers[args.command](args, settings)


if __name__ == "__main__":
    raise SystemExit(main())
