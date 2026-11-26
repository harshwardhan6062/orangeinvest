"""Run the eval cases against the real model and report pass rate per case.

    uv run python -m evals.run [--runs 3] [--case NAME ...] [--out evals/report.json]

Uses ORANGEINVEST_MODEL / ANTHROPIC_API_KEY like the app. Exits 1 if any run failed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import traceback
from typing import Any

from app.llm import AnthropicLLM, LLMResponse
from app.mcp_client import ToolSchema
from evals.cases import CASES
from evals.harness import Case, Outcome, run_case


class Metered:
    """Counts model calls and time spent in them for one run."""

    def __init__(self, inner: AnthropicLLM) -> None:
        self.inner, self.calls, self.seconds = inner, 0, 0.0

    async def complete(self, system: str, messages: list[dict[str, Any]],
                       tools: list[ToolSchema]) -> LLMResponse:
        t = time.monotonic()
        try:
            return await self.inner.complete(system, messages, tools)
        finally:
            self.calls += 1
            self.seconds += time.monotonic() - t


async def one_run(case: Case, llm: AnthropicLLM, sem: asyncio.Semaphore) -> dict[str, Any]:
    async with sem:
        metered = Metered(llm)
        try:
            out = await run_case(case, metered)
        except Exception:  # a crash is a failed run, not a crashed eval
            return {"case": case.name, "passed": False, "error": traceback.format_exc(limit=3),
                    "model_calls": metered.calls, "model_seconds": round(metered.seconds, 1)}
        return report_run(out, metered)


def report_run(out: Outcome, metered: Metered) -> dict[str, Any]:
    w = out.world
    return {
        "case": out.case, "passed": out.passed, "error": out.error,
        "model_calls": metered.calls, "model_seconds": round(metered.seconds, 1),
        "replies": w.replies,  # for reading failures; never asserted on
        "audit": [{"event": e.event, "tool": e.tool, "args": e.args, "detail": e.detail}
                  for e in w.audit._entries],
    }


async def main(runs: int, names: list[str], out: str, concurrency: int) -> int:
    cases = [c for c in CASES if not names or c.name in names]
    llm = AnthropicLLM()
    sem = asyncio.Semaphore(concurrency)
    print(f"model: {llm.model}   cases: {len(cases)}   runs each: {runs}\n")
    results = await asyncio.gather(*(one_run(c, llm, sem) for c in cases for _ in range(runs)))

    width = max(len(c.name) for c in cases)
    print(f"{'case':<{width}}  pass   rate   calls/run  model s/run  first failure")
    for c in cases:
        rs = [r for r in results if r["case"] == c.name]
        ok = sum(r["passed"] for r in rs)
        fail = next((r["error"].strip().splitlines()[-1] for r in rs if not r["passed"]), "")
        print(f"{c.name:<{width}}  {ok}/{len(rs)}  {ok / len(rs):>5.0%}  "
              f"{sum(r['model_calls'] for r in rs) / len(rs):>9.1f}  "
              f"{sum(r['model_seconds'] for r in rs) / len(rs):>11.1f}  {fail[:90]}")
    total = sum(r["passed"] for r in results)
    print(f"\noverall {total}/{len(results)}   report: {out}")

    with open(out, "w") as f:
        json.dump({"model": llm.model, "runs": runs, "results": results}, f, indent=2, default=str)
    return 0 if total == len(results) else 1


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--runs", type=int, default=3)
    p.add_argument("--case", action="append", default=[], help="run only this case (repeatable)")
    p.add_argument("--out", default="evals/report.json")
    p.add_argument("--concurrency", type=int, default=6)
    a = p.parse_args()
    raise SystemExit(asyncio.run(main(a.runs, a.case, a.out, a.concurrency)))
