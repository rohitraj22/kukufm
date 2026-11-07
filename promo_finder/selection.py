"""Deterministic grounding, duration, speech-boundary and diversity checks."""

from dataclasses import asdict
import hashlib
import json

from .parser import format_time
from .config import DIMENSIONS, Policy


class ValidationError(ValueError):
    pass


def validate_scores(scores):
    if not isinstance(scores, dict) or set(scores) != set(DIMENSIONS):
        raise ValidationError("Missing/extra score dimensions.")
    if any(type(v) is not int or not 0 <= v <= 5 for v in scores.values()):
        raise ValidationError("Scores must be integers between 0 and 5.")
    return dict(scores)


def weighted_score(scores, policy=None):
    policy = policy or Policy()
    return round(sum(scores[k] * weight / 5 for k, weight in policy.weights.items()), 1)


def validate_candidate(candidate, episodes, policy=None):
    policy = policy or Policy()
    if not isinstance(candidate, dict):
        raise ValidationError("Candidate must be an object.")
    for field in ["title", "angle", "hook_beat", "cliffhanger_beat", "hook", "cliffhanger", "why_it_works"]:
        if not isinstance(candidate.get(field), str) or not candidate[field].strip():
            raise ValidationError(f"Missing text: {field}.")
    scores = validate_scores(candidate.get("scores"))
    risks = candidate.get("risks")
    if not isinstance(risks, list) or any(not isinstance(v, str) for v in risks):
        raise ValidationError("Risks must be a list of strings.")
    lookup = {e.number: e for e in episodes}
    beats = {b.id: b for e in episodes for b in e.beats}
    proposed = candidate.get("segments")
    if not isinstance(proposed, list) or not 1 <= len(proposed) <= 3:
        raise ValidationError("Need 1-3 ordered segments.")
    segments = []
    total = 0
    for segment in proposed:
        if not isinstance(segment, dict) or type(segment.get("episode")) is not int:
            raise ValidationError("Invalid segment episode.")
        ep = lookup.get(segment["episode"])
        start = beats.get(segment.get("start_beat")) if isinstance(segment.get("start_beat"), str) else None
        end = beats.get(segment.get("end_beat")) if isinstance(segment.get("end_beat"), str) else None
        if ep is None or start is None or end is None or start.episode != ep.number or end.episode != ep.number:
            raise ValidationError("Unknown or cross-episode beat reference.")
        a, b = start.start_ms, end.end_ms
        if b <= a or end.start_ms < a or start.end_ms > b:
            raise ValidationError("Reversed/incomplete boundary beats.")
        for beat in ep.beats:
            if beat.kind == "DIALOGUE" and (beat.start_ms < a < beat.end_ms or beat.start_ms < b < beat.end_ms):
                raise ValidationError(f"Cut splits spoken line {beat.id}.")
        if any(s["episode"] == ep.number and min(b, s["end_ms"]) > max(a, s["start_ms"]) for s in segments):
            raise ValidationError("Repeated/overlapping footage inside promo.")
        selected = [{**asdict(beat), "fully_included": beat.start_ms >= a and beat.end_ms <= b}
                    for beat in ep.beats if beat.start_ms < b and beat.end_ms > a]
        selected.sort(key=lambda beat: (beat["start_ms"], beat["source_line"] or 0))
        segments.append({"episode": ep.number, "source_file": ep.filename, "source_sha256": ep.sha256,
                         "start_beat": start.id, "end_beat": end.id,
                         "start_ms": a, "end_ms": b, "start": format_time(a), "end": format_time(b),
                         "duration_seconds": (b - a) / 1000, "beats": selected})
        total += b - a
    if not policy.min_duration_ms <= total <= policy.max_duration_ms:
        raise ValidationError(f"Duration {total / 1000:.3f}s outside {policy.min_duration_ms / 1000:g}-{policy.max_duration_ms / 1000:g}s.")
    first, last = segments[0], segments[-1]
    hook, cliff = beats.get(candidate["hook_beat"]), beats.get(candidate["cliffhanger_beat"])
    if hook is None or hook.episode != first["episode"] or hook.start_ms < first["start_ms"] or hook.end_ms > min(first["end_ms"], first["start_ms"] + policy.hook_window_ms):
        raise ValidationError("Full hook evidence is not within the configured opening window.")
    if cliff is None or cliff.episode != last["episode"] or cliff.start_ms < last["start_ms"] or cliff.end_ms > last["end_ms"] or last["end_ms"] - cliff.end_ms > policy.ending_window_ms:
        raise ValidationError("Cliffhanger evidence missing from configured ending window.")
    identity = [(s['source_sha256'], s['episode'], s['start_ms'], s['end_ms']) for s in segments]
    cid = "promo-" + hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:12]
    return {**candidate, "id": cid, "segments": segments, "duration_seconds": total / 1000,
            "hook_source": asdict(hook), "cliffhanger_source": asdict(cliff),
            "scores": scores, "score": weighted_score(scores, policy),
            "validation": "Source IDs, duration, hook timing, ending timing and spoken boundaries passed.",
            "review_status": "needs_editor_review"}


def overlap_ratio(a, b):
    overlap = sum(max(0, min(x["end_ms"], y["end_ms"]) - max(x["start_ms"], y["start_ms"]))
                  for x in a["segments"] for y in b["segments"] if x["episode"] == y["episode"])
    return overlap / (1000 * min(a["duration_seconds"], b["duration_seconds"]))


def rank_candidates(candidates, top=10):
    """Greedy shortlist: penalize repeated angles/episodes; suppress repeated footage."""
    pool = {c["id"]: c for c in sorted(candidates, key=lambda c: c["score"])}
    selected = []
    while pool and len(selected) < top:
        scored = []
        for c in pool.values():
            if any(overlap_ratio(c, s) > .6 for s in selected):
                continue
            angles = sum(c["angle"] == s["angle"] for s in selected)
            eps = {s["episode"] for s in c["segments"]}
            repeats = sum(bool(eps & {seg["episode"] for seg in s["segments"]}) for s in selected)
            penalty = angles * 3 + repeats * 5
            scored.append((c["score"] - penalty, c["score"], c["id"], penalty, c))
        if not scored:
            break
        adjusted, _, _, penalty, candidate = max(scored, key=lambda row: row[:3])
        chosen = {**candidate, "rank": len(selected) + 1, "selection_score": adjusted,
                  "diversity_penalty": penalty,
                  "rank_reason": f"Rubric score {candidate['score']}/100; {penalty} points deducted for repeated angle/episode."}
        selected.append(chosen)
        del pool[candidate["id"]]
    return selected


def baseline_candidates(episode, policy=None):
    """Content-blind structural baseline. It makes no narrative-quality claims.

    Select legal windows at evenly spaced positions, preferring the duration-range
    midpoint. No words, names, genres or story events affect these selections.
    """
    policy = policy or Policy()
    catalog = episode.legal_continuous_cuts(policy)
    if not catalog:
        return []
    lookup = {b.id: b for b in episode.beats}
    starts = sorted(catalog, key=lambda bid: (lookup[bid].start_ms, bid))
    count = min(policy.candidates_per_episode, len(starts))
    indices = [round(i * (len(starts) - 1) / max(1, count - 1)) for i in range(count)]
    target = (policy.min_duration_ms + policy.max_duration_ms) / 2
    candidates = []
    for index in indices:
        start = starts[index]
        end = min(catalog[start], key=lambda bid: (abs(lookup[bid].end_ms - lookup[start].start_ms - target), bid))
        raw = {"title": f"Episode {episode.number}: structural candidate", "angle": "unrated",
               "segments": [{"episode": episode.number, "start_beat": start, "end_beat": end}],
               "hook_beat": start, "cliffhanger_beat": end,
               "hook": "Unrated opening; selected only from legal timing boundaries.",
               "cliffhanger": "Unrated ending; an unresolved question has not been established.",
               "why_it_works": "Timing-only baseline for comparison. Narrative quality is not assessed.",
               "scores": {dimension: 0 for dimension in DIMENSIONS},
               "risks": ["Not AI-reviewed. All quality scores are zero/unrated; an editor must assess the content."]}
        candidates.append(validate_candidate(raw, [episode], policy))
    return candidates
