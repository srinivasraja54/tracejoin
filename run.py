#!/usr/bin/env python3
"""M0 spike: can an LLM recover a hashed join key from source code?

  python run.py                      # real Claude if a key is present, else stub
  python run.py --proposer stub      # force the offline scripted proposer
  python run.py --proposer anthropic # force the real API
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from spike.loop import run_spike, to_correlation_map          # noqa: E402
from spike.models import Boundary                              # noqa: E402
from spike.proposer import (AnthropicProposer, StubProposer,   # noqa: E402
                            estimate_cost)
from spike.verifier import build_target_index                  # noqa: E402

DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"
GREEN, RED, YELLOW = "\033[32m", "\033[31m", "\033[33m"


def load_jsonl(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        sys.exit(f"missing {path} - run:  python data/generate.py")
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def build_boundary(source_rows: list[dict], target_values: set[str]) -> Boundary:
    with_ids = [r for r in source_rows if "request_id" in r]
    return Boundary(
        edge_id="edge_m0_001",
        source_hop="middleware-auth (Splunk)",
        target_hop="payments DB (Azure SQL)",
        target_field="internal_tx_hash",
        source_fields=sorted({k for r in with_ids[:50] for k in r}),
        source_samples=with_ids[:3],
        target_samples=sorted(target_values)[:5],
        code={"sample_app/payment_service.py":
              (HERE / "sample_app" / "payment_service.py").read_text()},
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--proposer", choices=["auto", "anthropic", "stub"], default="auto")
    ap.add_argument("--model", default="claude-opus-5")
    ap.add_argument("--max-attempts", type=int, default=3)
    ap.add_argument("--sample-size", type=int, default=200)
    ap.add_argument("--runs", type=int, default=1,
                    help="repeat N times and summarise; the model is "
                         "non-deterministic, so N=1 is an anecdote, not a result")
    ap.add_argument("--quiet", action="store_true", help="summary only")
    args = ap.parse_args()

    source_rows = load_jsonl(HERE / "data" / "middleware.jsonl")
    target_rows = load_jsonl(HERE / "data" / "appdb.jsonl")
    target_values = build_target_index(target_rows, "internal_tx_hash")
    boundary = build_boundary(source_rows, target_values)

    mode = args.proposer
    if mode == "auto":
        mode = "anthropic" if os.environ.get("ANTHROPIC_API_KEY") else "stub"

    def make_proposer():
        if mode == "anthropic":
            return AnthropicProposer(model=args.model), f"Claude ({args.model})"
        return StubProposer(), "stub (offline, scripted)"

    _, label = make_proposer() if mode == "stub" else (None, f"Claude ({args.model})")

    print(f"\n{BOLD}AgenticTriage - M0 spike{RESET}")
    print(f"{DIM}boundary : {boundary.source_hop} -> {boundary.target_hop}")
    print(f"target   : {boundary.target_field} "
          f"({len(target_values)} distinct values)")
    print(f"source   : {len(source_rows)} rows "
          f"({sum('request_id' in r for r in source_rows)} carry request_id)")
    print(f"proposer : {label}"
          + (f"   runs: {args.runs}" if args.runs > 1 else "") + f"{RESET}\n")

    def on_event(kind, payload):
        if args.quiet:
            return
        if kind == "attempt":
            print(f"{BOLD}-- attempt {payload} {'-' * 52}{RESET}")
        elif kind == "proposal":
            print(f"  PROPOSE  {payload.expression}")
            print(f"{DIM}           {payload.reasoning}{RESET}")
            for ev in payload.evidence:
                print(f"{DIM}           evidence: {ev.file}:{ev.lines}{RESET}")
        elif kind == "compiled":
            print(f"  COMPILE  {GREEN}ok{RESET}  inputs: {sorted(payload.inputs)}")
        elif kind == "compile_failed":
            print(f"  COMPILE  {RED}rejected{RESET}  {payload}")
        elif kind == "verified":
            colour = GREEN if payload.ok else (
                YELLOW if payload.verdict == "probable" else RED)
            print(f"  VERIFY   {colour}{payload.verdict}{RESET}  "
                  f"join_yield={payload.join_yield:.3f} "
                  f"({payload.matched}/{payload.sampled})")
            if payload.note:
                print(f"{DIM}           {payload.note}{RESET}")
            for ex in ([] if payload.ok else payload.failures[:2]):
                if ex["produced"]:
                    print(f"{DIM}           {ex['inputs']} -> {ex['produced']} "
                          f"(absent on target){RESET}")
            print()

    runs, in_tok, out_tok = [], 0, 0
    for i in range(args.runs):
        if args.runs > 1 and not args.quiet:
            print(f"{BOLD}=== run {i + 1}/{args.runs} ==={RESET}")
        proposer, _ = make_proposer()
        outcome = run_spike(proposer, boundary, source_rows, target_values,
                            max_attempts=args.max_attempts,
                            sample_size=args.sample_size, on_event=on_event)
        runs.append(outcome)
        in_tok += getattr(proposer, "input_tokens", 0)
        out_tok += getattr(proposer, "output_tokens", 0)

    out_dir = HERE / "out"
    out_dir.mkdir(exist_ok=True)
    last = runs[-1]
    (out_dir / "correlation_map.json").write_text(
        json.dumps(to_correlation_map(boundary, last), indent=2) + "\n")
    (out_dir / "run_log.json").write_text(json.dumps(
        {"runs": [r.to_dict() for r in runs],
         "usage": {"input_tokens": in_tok, "output_tokens": out_tok,
                   "estimated_usd": estimate_cost(args.model, in_tok, out_tok)
                   if mode == "anthropic" else 0.0}}, indent=2) + "\n")

    resolved = [r for r in runs if r.resolved]
    print(f"{BOLD}{'=' * 58}{RESET}")
    if args.runs > 1:
        attempts = [len(r.attempts) for r in resolved]
        mean_att = sum(attempts) / len(attempts) if attempts else float("nan")
        mean_yield = (sum(r.join_yield for r in resolved) / len(resolved)
                      if resolved else 0.0)
        colour = GREEN if len(resolved) == args.runs else (
            YELLOW if resolved else RED)
        print(f"  resolved     {colour}{len(resolved)}/{args.runs}{RESET}")
        print(f"  mean attempts {mean_att:.1f}   first-try "
              f"{sum(a == 1 for a in attempts)}/{args.runs}")
        print(f"  mean join_yield {mean_yield:.3f}")
        exprs = {r.proposal.expression for r in resolved}
        print(f"  expressions  {len(exprs)} distinct: "
              + "; ".join(sorted(exprs)[:3]))
    elif last.resolved:
        print(f"  {GREEN}{BOLD}RESOLVED{RESET} after {len(last.attempts)} attempt(s)")
        print(f"  {last.proposal.expression}")
        print(f"  join_yield {last.join_yield:.3f} ({last.matched}/{last.sampled})")
    else:
        print(f"  {RED}{BOLD}UNRESOLVED{RESET} after {len(last.attempts)} attempt(s) "
              "- reported as a dark edge rather than guessed")

    if mode == "anthropic":
        cost = estimate_cost(args.model, in_tok, out_tok)
        cost_str = f"${cost:.4f}" if cost is not None else "n/a (unpriced model)"
        print(f"{DIM}  tokens {in_tok} in / {out_tok} out   "
              f"est. {cost_str} total"
              + (f", {cost / args.runs:.4f} per run" if cost and args.runs > 1 else "")
              + f"{RESET}")
    print(f"{DIM}  wrote out/correlation_map.json and out/run_log.json{RESET}\n")
    return 0 if resolved else 1


if __name__ == "__main__":
    raise SystemExit(main())
