"""Dataset-agnostic benchmark harness; dataset expectations live in a separate spec."""

import argparse
import json
import os
from pathlib import Path
import sys

from .config import Policy
from .parser import load_episodes
from .pipeline import run
from .providers import JSONProvider, ProviderError, load_env, write_json
from .selection import validate_candidate


def inspect_dataset(episodes, spec, policy):
    summary = {"episodes": len(episodes), "beats": sum(len(e.beats) for e in episodes),
               "legal_cut_pairs": 0, "validated_cut_pairs": 0}
    for field, actual in (("expected_episodes", summary["episodes"]), ("expected_beats", summary["beats"])):
        expected = spec.get(field)
        if expected is not None and (type(expected) is not int or expected != actual):
            raise ValueError(f"Benchmark {field}: expected {expected}, got {actual}.")
    for episode in episodes:
        catalog = episode.legal_continuous_cuts(policy)
        summary["legal_cut_pairs"] += sum(map(len, catalog.values()))
        for start, ends in catalog.items():
            for end in ends:
                probe = {"title": "Structural check", "angle": "unrated",
                         "segments": [{"episode": episode.number, "start_beat": start, "end_beat": end}],
                         "hook_beat": start, "cliffhanger_beat": end, "hook": "Unrated", "cliffhanger": "Unrated",
                         "why_it_works": "Boundary verification only", "scores": {k: 0 for k in policy.weights}, "risks": []}
                validate_candidate(probe, [episode], policy)
                summary["validated_cut_pairs"] += 1
    return summary


def main():
    parser = argparse.ArgumentParser(description="Run the reusable engine against an explicitly supplied private benchmark.")
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--provider", choices=["baseline", "gemini", "ollama"], default="baseline")
    parser.add_argument("--model")
    parser.add_argument("--critic-model")
    parser.add_argument("--max-calls", type=int, default=40)
    parser.add_argument("--min-interval", type=float, default=12)
    parser.add_argument("--batch-size", type=int, default=5)
    args = parser.parse_args()
    try:
        if not 1 <= args.batch_size <= 5 or args.max_calls < 1 or not 0 <= args.min_interval <= 60:
            raise ValueError("Invalid request budget, interval or batch size.")
        spec = json.loads(args.spec.read_text(encoding="utf-8"))
        if not isinstance(spec, dict) or not isinstance(spec.get("name"), str) or not isinstance(spec.get("policy", {}), dict):
            raise ValueError("Benchmark spec requires a name and optional policy object.")
        policy = Policy(**spec.get("policy", {}))
        episodes = load_episodes(args.input)
        print("Checking dataset structure and all legal cut boundaries...", flush=True)
        checks = inspect_dataset(episodes, spec, policy)
        args.output.mkdir(parents=True, exist_ok=True)
        write_json(args.output / "structural_checks.json", checks)
        print(json.dumps(checks), flush=True)
        load_env()
        provider = reviewer = None
        if args.provider != "baseline":
            model = args.model or (os.getenv("GEMINI_MODEL") or "gemini-3.1-flash-lite" if args.provider == "gemini" else "")
            provider = JSONProvider(args.provider, model, args.output / "raw/api", args.max_calls, args.min_interval)
            if args.critic_model and args.critic_model != model:
                reviewer = JSONProvider(args.provider, args.critic_model, args.output / "raw/api", args.max_calls,
                                        args.min_interval, budget=provider.budget)
        manifest, shortlist = run(episodes, args.output, provider, batch_size=args.batch_size,
                                  critic_provider=reviewer, policy=policy)
        proposed = sum(len(json.loads(p.read_text())["candidates"])
                       for p in (args.output / "raw").glob("batch-*.candidates.json"))
        summary = {"benchmark": spec["name"], "evaluation_role": spec.get("evaluation_role", "development benchmark"),
                   **checks, "complete": manifest["complete"], "episodes_processed": manifest["episodes_processed"],
                   "proposed": proposed, "grounded": manifest["grounded_candidates"],
                   "accepted": manifest["accepted_candidates"], "shortlisted": len(shortlist),
                   "prompt_version": manifest["prompt_version"], "model": manifest["model"],
                   "critic_model": manifest["critic_model"], "network_calls": manifest["network_calls_this_run"],
                   "human_precision_measured": False, "ad_performance_measured": False,
                   "private_outputs": True}
        write_json(args.output / "benchmark_summary.json", summary)
        print(json.dumps(summary, indent=2))
        print(f"Private report: {(args.output / 'report.html').resolve()}")
        return 0 if manifest["complete"] else 2
    except (ValueError, TypeError, OSError, ProviderError) as exc:
        print(f"Benchmark error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
