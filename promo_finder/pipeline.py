"""Discover -> ground -> critique -> diversify. Save every stage for inspection."""

from datetime import datetime, timezone
from pathlib import Path

from .config import Policy
from .prompts import CRITIC_SCHEMA, CRITIC_VERSION, SELECTION_SCHEMA, VERSION, critic_prompt, selection_prompt
from .providers import ProviderError, write_json
from .selection import ValidationError, baseline_candidates, rank_candidates, validate_candidate, validate_scores, weighted_score


def batches_for(episodes, batch_size=5, max_chars=110_000, policy=None):
    policy = policy or Policy()
    batches, current = [], []
    for episode in episodes:
        if len(selection_prompt([episode], policy)) > max_chars:
            raise ValueError(f"Episode {episode.number} exceeds the prompt size limit; split into timestamp-preserving scenes first.")
        if current and (len(current) >= batch_size or len(selection_prompt(current + [episode], policy)) > max_chars):
            batches.append(current)
            current = []
        current.append(episode)
    if current:
        batches.append(current)
    return batches


def apply_reviews(candidates, payload, policy=None):
    policy = policy or Policy()
    reviews = payload.get("reviews")
    if not isinstance(reviews, list):
        raise ProviderError("Critic response has no reviews list.")
    expected = {c["id"] for c in candidates}
    ids = [r.get("candidate_id") for r in reviews if isinstance(r, dict)]
    if len(ids) != len(reviews) or any(not isinstance(i, str) for i in ids):
        raise ProviderError("Critic returned malformed candidate IDs.")
    if len(ids) != len(set(ids)) or set(ids) != expected:
        raise ProviderError("Critic omitted, duplicated, or invented an ID; this batch is not approved.")
    kept, rejected = [], []
    lookup = {c["id"]: c for c in candidates}
    for review in reviews:
        if not isinstance(review.get("verdict"), str) or review["verdict"] not in {"keep", "reject"} or not isinstance(review.get("reason"), str) or not review['reason'].strip():
            raise ProviderError("Critic returned an invalid verdict/reason.")
        try:
            scores = validate_scores(review.get("scores"))
        except ValidationError as exc:
            raise ProviderError(str(exc)) from None
        candidate = lookup[review["candidate_id"]]
        approved = review["verdict"] == "keep" and all(scores[k] >= policy.min_quality_score for k in ("hook", "cliffhanger", "standalone"))
        if approved:
            kept.append({**candidate, "proposer_scores": candidate["scores"], "scores": scores,
                         "score": weighted_score(scores, policy), "critic": review,
                         "review_status": "ai_reviewed_editor_pending"})
        else:
            rejected.append({"candidate_id": candidate["id"], "stage": "critic", "reason": review["reason"], "review": review})
    return kept, rejected


def run(episodes, output: Path, provider=None, top=10, batch_size=5, progress=print, critic_provider=None, policy=None):
    policy = policy or Policy()
    if not episodes or len({e.number for e in episodes}) != len(episodes):
        raise ValueError("A run requires at least one episode and unique episode numbers.")
    output.mkdir(parents=True, exist_ok=True)
    baseline = provider is None
    reviewer = critic_provider or provider
    batches = [[e] for e in episodes] if baseline else batches_for(episodes, batch_size, policy=policy)
    coverage = {e.number: {"episode": e.number, "file": e.filename, "sha256": e.sha256,
                           "beats": len(e.beats), "status": "not_processed", "candidate_count": 0} for e in episodes}
    accepted, grounded_all, rejected, errors = [], [], [], []
    manifest = {"created_utc": datetime.now(timezone.utc).isoformat(), "prompt_version": VERSION,
                "mode": "baseline" if baseline else provider.name, "model": "structural-baseline-v1" if baseline else provider.model,
                "critic_model": None if baseline else reviewer.model,
                "critic_version": None if baseline else CRITIC_VERSION,
                "input_count": len(episodes), "batch_count": len(batches), "weights": policy.weights, "policy": policy.to_dict(),
                "score_meaning": "Zero means unrated; structural comparison only." if baseline else "Editorial judgment, not measured CTR/install probability.",
                "limitations": ["Script timestamps must be checked against the final video/audio.",
                                "No audience or ad performance data; human review is required.",
                                "Discovery selects continuous clips only; stitched assembly is not generated.",
                                "Input formats: timed screenplay TXT or normalized JSON; no timing estimates."]}
    write_json(output / "coverage.json", list(coverage.values()))
    for index, batch in enumerate(batches, 1):
        progress(f"Batch {index}/{len(batches)}: episodes {', '.join(str(e.number) for e in batch)}")
        prefix = output / "raw" / f"batch-{index:03d}"
        try:
            if baseline:
                generated = baseline_candidates(batch[0], policy)
                payload = {"candidates": generated, "mode": "structural-baseline-not-LLM"}
            else:
                payload = provider.generate(selection_prompt(batch, policy), SELECTION_SCHEMA, "discovery")
                generated = payload.get("candidates")
                if not isinstance(generated, list) or len(generated) > 20:
                    raise ProviderError("Invalid candidates list or too many candidates; no truncation performed.")
            write_json(prefix.with_suffix(".candidates.json"), payload)
            grounded, seen = [], set()
            for raw in generated:
                try:
                    if not baseline and isinstance(raw, dict):
                        segments = raw.get("segments")
                        if not isinstance(segments, list) or len(segments) != 1:
                            raise ValidationError("Discovery requires one continuous legal cut.")
                        segment = segments[0]
                        if not isinstance(segment, dict):
                            raise ValidationError("Malformed segment.")
                        episode = next((e for e in batch if e.number == segment.get("episode")), None)
                        start, end = segment.get("start_beat"), segment.get("end_beat")
                        if episode is None or not isinstance(start, str) or end not in episode.legal_continuous_cuts(policy).get(start, []):
                            raise ValidationError("Cut pair is not in the supplied legal-cuts map.")
                        if raw.get("hook_beat") != start or raw.get("cliffhanger_beat") != end:
                            raise ValidationError("Hook/ending evidence must match the selected legal boundaries.")
                    candidate = validate_candidate(raw, batch, policy)
                    if candidate['id'] in seen:
                        raise ValidationError("Duplicate candidate cut.")
                    seen.add(candidate['id'])
                    grounded.append(candidate)
                except ValidationError as exc:
                    rejected.append({"batch": index, "stage": "grounding", "reason": str(exc), "candidate": raw})
            grounded_all.extend(grounded)
            if baseline or not grounded:
                kept, bad = grounded, []
            else:
                review = reviewer.generate(critic_prompt(grounded, policy), CRITIC_SCHEMA, "critic")
                write_json(prefix.with_suffix(".reviews.json"), review)
                kept, bad = apply_reviews(grounded, review, policy)
            rejected.extend(bad)
            accepted.extend(kept)
            for episode in batch:
                count = sum(any(s["episode"] == episode.number for s in c["segments"]) for c in kept)
                coverage[episode.number].update(status="baseline_processed" if baseline else "ai_processed", candidate_count=count)
        except ProviderError as exc:
            errors.append({"batch": index, "episodes": [e.number for e in batch], "error": str(exc)})
            for episode in batch:
                coverage[episode.number]["status"] = "failed"
            progress(f"Stopped: {exc}")
            break
        finally:
            write_json(output / "coverage.json", list(coverage.values()))
    shortlisted = rank_candidates(accepted, top)
    providers = [] if baseline else [provider] + ([reviewer] if reviewer is not provider else [])
    manifest.update({"complete": all(e["status"] in {"baseline_processed", "ai_processed"} for e in coverage.values()),
                     "episodes_processed": sum(e["status"] in {"baseline_processed", "ai_processed"} for e in coverage.values()),
                     "grounded_candidates": len(grounded_all), "accepted_candidates": len(accepted),
                     "shortlist_count": len(shortlisted), "rejections": len(rejected), "errors": errors,
                     "network_calls_this_run": sum(p.calls for p in providers),
                     "cache_hits": sum(p.cache_hits for p in providers)})
    write_json(output / "manifest.json", manifest)
    write_json(output / "grounded_candidates.json", grounded_all)
    write_json(output / "accepted_candidates.json", accepted)
    write_json(output / "rejections.json", rejected)
    write_json(output / "shortlist.json", shortlisted)
    from .reports import write_reports
    write_reports(output, manifest, shortlisted, list(coverage.values()))
    return manifest, shortlisted
