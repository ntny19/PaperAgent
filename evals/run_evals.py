"""Run the golden set against the real agent loop (real index, real model CLI).

  .venv\\Scripts\\python -W ignore -m evals.run_evals                 # all cases, default model
  .venv\\Scripts\\python -W ignore -m evals.run_evals --model sonnet-5-5
  .venv\\Scripts\\python -W ignore -m evals.run_evals --case list-papers --case search-and-cite
  .venv\\Scripts\\python -W ignore -m evals.run_evals --list

Run/edit approvals are always auto-DENIED (nothing is executed or changed). Every case is also written to the
turn log with tag "eval:<id>", so you can inspect it:  python -m app.logview --tag eval -n 5
Results go to data/evals/<timestamp>.json. Exit code is 1 if any case fails.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml

from app import agent
from app.main import INDEX, OLLAMA, ROOT, WORKSPACES
from app.model_config import load_models
from app.secrets_guard import mask_text
from app.tools import Ctx

CITE = re.compile(r"\[[^\]]+\bp\.?\s*\d+", re.I)


def check(case: dict, answer: str, events: list[dict], approvals: int) -> list[str]:
    a = answer.lower()
    calls = [e["name"] for e in events if e["type"] == "tool_call"]
    fails = []
    for t in case.get("must_call", []):
        if t not in calls:
            fails.append(f"did not call {t} (called: {calls or 'nothing'})")
    for t in case.get("must_not_call", []):
        if t in calls:
            fails.append(f"called forbidden tool {t}")
    if "max_tool_calls" in case and len(calls) > case["max_tool_calls"]:
        fails.append(f"{len(calls)} tool calls > max {case['max_tool_calls']}")
    for s in case.get("answer_contains", []):
        if s.lower() not in a:
            fails.append(f"answer missing {s!r}")
    if case.get("answer_contains_any") and not any(s.lower() in a for s in case["answer_contains_any"]):
        fails.append(f"answer has none of {case['answer_contains_any']}")
    for s in case.get("answer_not_contains", []):
        if s.lower() in a:
            fails.append(f"answer contains forbidden {s!r}")
    if case.get("answer_regex") and not re.search(case["answer_regex"], answer, re.I):
        fails.append(f"answer does not match /{case['answer_regex']}/")
    if case.get("must_cite") and not CITE.search(answer):
        fails.append("no citation like [Paper title p.N]")
    if case.get("no_approval") and approvals:
        fails.append(f"asked for {approvals} approval(s) but should not have")
    if case.get("must_request_approval") and not approvals:
        fails.append("did not ask for approval before acting")
    if case.get("answer_regex_any"):
        if not any(re.search(r, answer, re.I) for r in case["answer_regex_any"]):
            fails.append(f"answer matches none of {case['answer_regex_any']}")
    if mask_text(answer) != answer:
        fails.append("answer contains an unmasked secret")
    if answer.strip() in ("", "(no answer)"):
        fails.append("empty answer")
    return fails


async def run_case(case: dict, model: dict) -> dict:
    ws_name = case.get("workspace")
    if ws_name == "$FIRST":
        ws_name = next(iter(WORKSPACES), None)
        if not ws_name:
            return {"id": case["id"], "status": "SKIP", "why": "no workspace configured in .env"}
    ws = WORKSPACES.get(ws_name) if ws_name else None
    if ws_name and not ws:
        return {"id": case["id"], "status": "SKIP", "why": f"workspace {ws_name!r} not in .env"}
    events: list[dict] = []
    approvals = 0

    async def emit(ev):
        events.append(ev)

    async def approve(*_a, **_k):
        nonlocal approvals
        approvals += 1
        return False

    ctx = Ctx(index=INDEX, workspace=ws, emit=emit, approve=approve, loop=asyncio.get_running_loop())
    t0 = time.time()
    stats: dict = {}
    try:
        answer = await agent.run_turn(ctx, [{"role": "user", "content": case["question"]}], model, OLLAMA,
                                      tag=f"eval:{case['id']}", stats=stats)
        fails = check(case, answer, events, approvals)
        if case.get("max_tokens_in") and stats.get("tokens_in", 0) > case["max_tokens_in"]:
            fails.append(f"{stats['tokens_in']} input tokens > max {case['max_tokens_in']}")
    except Exception as e:
        answer, fails = "", [f"agent crashed: {type(e).__name__}: {e}"]
    return {"id": case["id"], "status": "FAIL" if fails else "PASS", "fails": fails, "secs": round(time.time() - t0, 1),
            "tools": [e["name"] for e in events if e["type"] == "tool_call"], "answer": answer,
            "tokens_in": stats.get("tokens_in", 0), "tokens_out": stats.get("tokens_out", 0),
            "tokens_estimated": stats.get("tokens_estimated", False), "log_id": stats.get("id")}


async def main_async(a) -> int:
    cases = yaml.safe_load((Path(__file__).parent / "golden.yaml").read_text(encoding="utf-8"))["cases"]
    if a.list:
        for c in cases:
            print(f"{c['id']:35} {c['question'][:80]}")
        return 0
    if a.case:
        cases = [c for c in cases if c["id"] in a.case]
    models, default = load_models(ROOT / "config.yaml")
    mid = a.model or default
    if mid not in models:
        sys.exit(f"unknown model {mid!r}; choose from: {', '.join(models)}")
    if not INDEX.papers():
        print("Index is empty - indexing papers first (needs Ollama for embeddings)...")
        await asyncio.to_thread(INDEX.scan)
    print(f"Model: {models[mid]['label']}   papers indexed: {len(INDEX.papers())}   cases: {len(cases)}\n")
    results = []
    for c in cases:
        print(f"... {c['id']}", end="", flush=True)
        r = await run_case(c, models[mid])
        results.append(r)
        tok = f"{r['tokens_in']}/{r['tokens_out']}{'~' if r['tokens_estimated'] else ''}" if "tokens_in" in r else ""
        print(f"\r{r['status']:5} {c['id']:34} {r.get('secs', ''):>6}s  tok in/out={tok:>12}  tools={r.get('tools', '')}")
        for f in r.get("fails", []):
            print(f"        - {f}")
        if r["status"] == "SKIP":
            print(f"        ({r['why']})")
        if a.verbose and r.get("answer"):
            print("        answer: " + r["answer"][:600].replace("\n", "\n                "))
    n = {s: sum(r["status"] == s for r in results) for s in ("PASS", "FAIL", "SKIP")}
    print(f"\n{n['PASS']} passed, {n['FAIL']} failed, {n['SKIP']} skipped   "
          f"total tokens in/out = {sum(r.get('tokens_in', 0) for r in results)}/{sum(r.get('tokens_out', 0) for r in results)}"
          f"   total time = {sum(r.get('secs', 0) for r in results):.0f}s   (~ = estimated, backend reports no usage)")
    out = ROOT / "data" / "evals"
    out.mkdir(parents=True, exist_ok=True)
    f = out / f"{datetime.now():%Y%m%d-%H%M%S}-{mid}.json"
    f.write_text(json.dumps({"model": mid, "summary": n, "results": results}, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Saved {f.relative_to(ROOT)}   (inspect prompts/replies: python -m app.logview --tag eval -n {len(cases)})")
    return 1 if n["FAIL"] else 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", help="model id from config.yaml (default: default_model)")
    ap.add_argument("--case", action="append", help="run only this case id (repeatable)")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true", help="print each answer")
    sys.exit(asyncio.run(main_async(ap.parse_args())))


if __name__ == "__main__":
    main()
