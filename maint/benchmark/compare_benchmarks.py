#!/usr/bin/env python
#
# Runs ci_benchmark.py against two Tornado source trees and reports how the
# second ("head") compares to the first ("base"). Exits with status 1 if
# any benchmark is slower in head by more than --threshold, or exceeds its
# absolute budget.
#
# Usage:
#   python maint/benchmark/compare_benchmarks.py --base ../tornado-master --head .
#
# Timing on shared CI machines is noisy, so the two trees are measured
# alternately, in a fresh process each time, and the fastest of --rounds
# runs is used for each. A machine that slows down partway through affects
# both alike, and interference can only make a run slower, so the minimum
# is a stable estimate. The benchmark script always comes from this
# directory, so both trees run the same benchmarks; a benchmark that fails
# against base (for example because it uses a new API) is reported as new.

import argparse
import json
import os
import subprocess
import sys
from typing import Any

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ci_benchmark.py")


def run_once(tree: str) -> dict[str, Any]:
    env = dict(os.environ, PYTHONPATH=os.path.abspath(tree))
    out = subprocess.run(
        [sys.executable, SCRIPT], env=env, check=True, capture_output=True, text=True
    ).stdout
    result: dict[str, Any] = json.loads(out)
    return result


def fastest(runs: list[dict[str, Any]]) -> dict[str, Any]:
    best: dict[str, Any] = {}
    for run in runs:
        for name, r in run.items():
            if name not in best or (
                "us_per_unit" in r
                and r["us_per_unit"] < best[name].get("us_per_unit", float("inf"))
            ):
                best[name] = r
    return best


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True, help="source tree to compare against")
    parser.add_argument("--head", required=True, help="source tree being tested")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.2,
        help="fail if head is slower than base by more than this fraction",
    )
    parser.add_argument("--markdown", help="also write the report to this file")
    args = parser.parse_args()

    runs: dict[str, list[dict[str, Any]]] = {"base": [], "head": []}
    for i in range(args.rounds):
        order = ["base", "head"] if i % 2 == 0 else ["head", "base"]
        for which in order:
            runs[which].append(run_once(getattr(args, which)))
        print(f"round {i + 1}/{args.rounds} done", file=sys.stderr)
    base, head = fastest(runs["base"]), fastest(runs["head"])

    failed = False
    lines = [
        "| Benchmark | Base | Head | Change | Budget | |",
        "| --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for name, h in head.items():
        b = base.get(name, {})
        label = name if h["unit"] == "byte" else f"{name} (per {h['unit']})"
        budget = h["budget"]
        status = ""
        if "us_per_unit" not in h:
            status = "error: " + h["error"]
            failed = True
            lines.append(f"| {label} | | | | | {status} |")
            continue
        hv = h["us_per_unit"]
        bv = b.get("us_per_unit")
        change = ""
        if bv is None:
            status = "new"
        else:
            ratio = hv / bv - 1
            change = f"{ratio:+.0%}"
            if ratio > args.threshold:
                status = "slower"
                failed = True
        if budget is not None and hv > budget:
            status = "over budget"
            failed = True
        lines.append(
            f"| {label} | {fmt(bv)} | {fmt(hv)} | {change} | "
            f"{fmt(budget)} | {status} |"
        )
    lines.append("")
    lines.append(
        f"Microseconds of CPU per unit (byte unless noted), fastest of "
        f"{args.rounds} runs. Fails above {args.threshold:.0%} slower or over budget."
    )
    report = "\n".join(lines) + "\n"
    print(report)
    if args.markdown:
        with open(args.markdown, "a") as f:
            f.write("## Benchmarks\n\n" + report)
    return 1 if failed else 0


def fmt(v: float | None) -> str:
    if v is None:
        return ""
    return f"{v:.3g}"


if __name__ == "__main__":
    sys.exit(main())
