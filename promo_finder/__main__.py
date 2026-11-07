import argparse
import json
import os
from pathlib import Path
import sys
import hashlib

from .config import Policy
from .parser import InputError, load_episodes
from .pipeline import batches_for, run
from .prompts import VERSION, selection_prompt
from .providers import JSONProvider, ProviderError, load_env


def main():
    parser = argparse.ArgumentParser(description="Promo Finder: reusable, source-grounded story promo discovery.")
    parser.add_argument("--input", required=True, type=Path, help="Timed TXT/JSON file or folder for one show")
    parser.add_argument("--config", type=Path, help="JSON policy: timing rules, audience, language and score weights")
    parser.add_argument("--provider", choices=["gemini", "ollama", "baseline"], default="gemini")
    parser.add_argument("--model", help="Gemini model ID or an already installed Ollama model")
    parser.add_argument("--critic-model", help="Optional separate reviewer model on the same provider")
    parser.add_argument("--output", type=Path, help="Private run folder; default: output/<provider>/<input-and-settings-hash>")
    parser.add_argument("--batch-size", type=int, default=5)
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--max-calls", type=int, default=40, help="Hard network request budget, including retries")
    parser.add_argument("--min-interval", type=float, default=12, help="Seconds between requests; adjust to account quota")
    parser.add_argument("--dry-run", action="store_true", help="Check input and estimate request count without calling AI")
    args = parser.parse_args()
    if not 1 <= args.batch_size <= 5 or not 1 <= args.top <= 100 or args.max_calls < 1 or not 0 <= args.min_interval <= 60:
        parser.error("batch-size must be 1-5; top 1-100; max-calls positive; min-interval 0-60.")
    try:
        load_env()
        policy = Policy.from_file(args.config) if args.config else Policy()
        episodes = load_episodes(args.input)
        batches = batches_for(episodes, args.batch_size, policy=policy)
        if args.dry_run:
            print(json.dumps({"episodes": len(episodes), "beats": sum(len(e.beats) for e in episodes),
                              "batches": len(batches), "planned_ai_calls_up_to": len(batches) * 2,
                              "policy": policy.to_dict(),
                              "approx_discovery_input_tokens_chars_div_4": sum(len(selection_prompt(b, policy)) for b in batches) // 4,
                              "note": "Token estimate only, excluding critic/output tokens. No network calls made."}, indent=2))
            return 0
        model = args.model or (os.environ.get("GEMINI_MODEL", "") or "gemini-3.1-flash-lite" if args.provider == "gemini" else "")
        identity = {"inputs": [(e.number, e.sha256) for e in episodes], "policy": policy.to_dict(),
                    "provider": args.provider, "model": model, "critic": args.critic_model, "version": VERSION}
        run_id = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:12]
        output = args.output or Path("output") / args.provider / run_id
        if output.exists() and (output / "editor_review.csv").exists():
            # Preserve filled editorial work on reruns.
            original = (output / "editor_review.csv").read_text(encoding="utf-8")
            from hashlib import sha256
            backup = output / "review_backups" / (sha256(original.encode()).hexdigest()[:12] + ".csv")
            backup.parent.mkdir(parents=True, exist_ok=True)
            if not backup.exists():
                backup.write_text(original, encoding="utf-8")
        provider = None
        reviewer = None
        if args.provider != "baseline":
            if not model:
                parser.error("--model is required for Ollama; choose an already installed model.")
            provider = JSONProvider(args.provider, model, output / "raw" / "api", args.max_calls, args.min_interval)
            if args.critic_model and args.critic_model != model:
                reviewer = JSONProvider(args.provider, args.critic_model, output / "raw" / "api", args.max_calls,
                                        args.min_interval, budget=provider.budget)
        manifest, shortlist = run(episodes, output, provider, args.top, args.batch_size,
                                  critic_provider=reviewer, policy=policy)
        print(f"\n{manifest['episodes_processed']}/{len(episodes)} episodes processed; {len(shortlist)} candidates shortlisted.")
        print(f"Report: {(output / 'report.html').resolve()}")
        if args.provider == "baseline":
            print("OFFLINE BASELINE: no LLM used; narrative quality is unverified.")
        return 0 if manifest["complete"] else 2
    except (InputError, ProviderError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
