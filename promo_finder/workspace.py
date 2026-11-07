"""Session-independent upload, review and export logic for the web product.

No Streamlit dependency, global user state, model calls or benchmark-specific
rules. Uploaded archives are read in memory and never extracted to disk.
"""

from copy import deepcopy
from datetime import datetime, timezone
import csv
import io
import json
from pathlib import Path, PurePosixPath
import stat
import zipfile

from .config import DIMENSIONS, Policy
from .parser import InputError, parse_json_episodes, parse_script, parse_time
from .selection import ValidationError, overlap_ratio, validate_candidate, weighted_score

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_EXPANDED_BYTES = 50 * 1024 * 1024
MAX_FILES = 200
MAX_EPISODES = 200
MAX_BEATS = 20_000
MAX_BEATS_PER_EPISODE = 1000
RESULT_FILES = ("manifest", "coverage", "grounded_candidates", "accepted_candidates", "rejections", "shortlist")
DECISIONS = ("Pending", "Accepted", "Rejected")
FEEDBACK_REASONS = ("No issue", "Hook arrives late", "Needs outside context", "Ending resolves the conflict",
                    "Weak emotional stakes", "Duplicate moment", "Timing / boundary problem", "Other")


def _safe_name(name):
    path = PurePosixPath(name)
    if not name or path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name:
        raise InputError("Unsafe upload path. Use relative filenames without parent traversal.")
    return path


def ingest_uploads(files):
    """Accept (filename, bytes) pairs. Fail atomically on malformed script input."""
    files = list(files)
    if not files or len(files) > MAX_FILES:
        raise InputError(f"Upload between 1 and {MAX_FILES} files.")
    if sum(len(body) for _, body in files) > MAX_UPLOAD_BYTES:
        raise InputError("Uploads exceed the 25 MB total limit.")
    script_files, ignored = [], []
    expanded = 0

    def add(name, body):
        nonlocal expanded
        expanded += len(body)
        if expanded > MAX_EXPANDED_BYTES or len(script_files) >= MAX_FILES:
            raise InputError("Archive exceeds the 50 MB / 200 script-file limit.")
        script_files.append((name, body))

    for name, body in files:
        path = _safe_name(name)
        if path.suffix.lower() in {".txt", ".json"}:
            add(str(path), body)
        elif path.suffix.lower() == ".zip":
            try:
                with zipfile.ZipFile(io.BytesIO(body)) as archive:
                    entries = archive.infolist()
                    if len(entries) > MAX_FILES * 4:
                        raise InputError("Archive contains too many entries.")
                    if sum(entry.file_size for entry in entries) > MAX_EXPANDED_BYTES:
                        raise InputError("Archive expands beyond the 50 MB limit.")
                    for entry in entries:
                        member = _safe_name(entry.filename)
                        if stat.S_ISLNK(entry.external_attr >> 16) or entry.flag_bits & 1:
                            raise InputError("Symlink and encrypted ZIP entries are not supported.")
                        if entry.is_dir():
                            continue
                        if any(part.startswith(".") or part == "__MACOSX" for part in member.parts):
                            ignored.append(entry.filename)
                            continue
                        if member.suffix.lower() not in {".txt", ".json"}:
                            ignored.append(entry.filename)
                            continue
                        if entry.file_size > MAX_UPLOAD_BYTES or entry.file_size / max(1, entry.compress_size) > 200:
                            raise InputError("ZIP entry exceeds size or compression-ratio limits.")
                        add(str(member), archive.read(entry))
            except (zipfile.BadZipFile, NotImplementedError, RuntimeError, EOFError) as exc:
                raise InputError("Unreadable or unsupported ZIP archive.") from exc
        else:
            raise InputError("Supported files: timed TXT, normalized JSON, or a ZIP containing them.")
    if not script_files:
        raise InputError("No supported scripts found in the upload.")
    episodes = []
    for name, body in script_files:
        try:
            text = body.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise InputError(f"{name}: expected UTF-8 text.") from None
        episodes.extend([parse_script(text, name)] if name.lower().endswith(".txt") else parse_json_episodes(text, name))
        if len(episodes) > MAX_EPISODES or sum(len(e.beats) for e in episodes) > MAX_BEATS:
            raise InputError("This demo supports up to 200 episodes / 20,000 beats per run.")
        if any(len(e.beats) > MAX_BEATS_PER_EPISODE for e in episodes):
            raise InputError("An episode exceeds 1,000 beats. Split it into timestamp-preserving episodes before uploading.")
    if len({episode.number for episode in episodes}) != len(episodes):
        raise InputError("Duplicate episode numbers. Upload one show at a time, with unique episode numbers.")
    return sorted(episodes, key=lambda episode: episode.number), ignored


def read_run(directory):
    directory = Path(directory)
    return {name: json.loads((directory / (name + ".json")).read_text(encoding="utf-8")) for name in RESULT_FILES}


def candidate_catalog(result):
    """Prefer reviewed/ranked records to the original unreviewed proposal."""
    catalog = {c["id"]: c for group in ("grounded_candidates", "accepted_candidates", "shortlist")
               for c in result[group]}
    policy = Policy(**result["manifest"]["policy"])
    for rejection in result["rejections"]:
        cid, review = rejection.get("candidate_id"), rejection.get("review")
        if cid in catalog and review:
            catalog[cid] = {**catalog[cid], "proposer_scores": catalog[cid]["scores"], "critic": review,
                            "scores": review["scores"], "score": weighted_score(review["scores"], policy),
                            "review_status": "ai_rejected_editor_pending"}
    return catalog


def exclusion_audit(result):
    """Separate actual rejection evidence from selection omissions and pending work."""
    catalog = candidate_catalog(result)
    rows = []
    for index, rejection in enumerate(result["rejections"]):
        candidate = catalog.get(rejection.get("candidate_id"), rejection.get("candidate", {}))
        if not isinstance(candidate, dict):
            candidate = {}
        stage = "AI reviewer" if rejection["stage"] == "critic" else "Source / timing validation"
        rows.append({"id": rejection.get("candidate_id", f"invalid-{index}"), "title": candidate.get("title", "Invalid proposal"),
                     "stage": stage, "reason": rejection["reason"], "candidate": candidate,
                     "review": rejection.get("review")})
    selected = {c["id"] for c in result["shortlist"]}
    approved = {c["id"] for c in result["accepted_candidates"]}
    rejected = {r.get("candidate_id") for r in result["rejections"]}
    for candidate in result["accepted_candidates"]:
        if candidate["id"] in selected:
            continue
        overlapping = next((c for c in result["shortlist"] if overlap_ratio(candidate, c) > .6), None)
        if overlapping:
            reason = (f"More than 60% of the shorter clip overlaps shortlisted clip {overlapping['id']}. "
                      "The shortlist suppresses repeated footage; this is not an AI rejection.")
        else:
            reason = "Passed review but fell outside the requested shortlist after score and diversity ranking."
        rows.append({"id": candidate["id"], "title": candidate["title"], "stage": "Shortlist selection",
                     "reason": reason, "candidate": candidate})
    for candidate in result["grounded_candidates"]:
        if candidate["id"] not in approved | rejected:
            rows.append({"id": candidate["id"], "title": candidate["title"], "stage": "Review incomplete",
                         "reason": "Grounding passed, but this run did not finish the model review. No approval or rejection inferred.",
                         "candidate": candidate})
    return rows


def review_candidate(candidate, episodes, policy, start_text, end_text, decision, reason, notes):
    """Validate a human cut against current source; never transfer an AI verdict."""
    if decision not in DECISIONS or reason not in FEEDBACK_REASONS:
        raise ValidationError("Unknown editor decision or feedback reason.")
    if not isinstance(notes, str) or len(notes) > 3000:
        raise ValidationError("Editor notes must be text up to 3,000 characters.")
    if decision == "Rejected" and reason == "No issue" and not notes.strip():
        raise ValidationError("Record a reason or note when rejecting a clip.")
    if len(candidate["segments"]) != 1:
        raise ValidationError("The editor currently supports continuous clips only.")
    original = candidate["segments"][0]
    episode = next((e for e in episodes if e.number == original["episode"] and e.sha256 == original["source_sha256"]), None)
    if episode is None:
        raise ValidationError("Source fingerprint does not match this candidate.")
    start_ms, end_ms = parse_time(start_text.strip()), parse_time(end_text.strip())
    modified = (start_ms, end_ms) != (original["start_ms"], original["end_ms"])
    effective = deepcopy(candidate)
    if modified:
        legal = episode.legal_continuous_cuts(policy)
        lookup = {b.id: b for b in episode.beats}
        pair = next(((start, end) for start, ends in legal.items() for end in ends
                     if lookup[start].start_ms == start_ms and lookup[end].end_ms == end_ms), None)
        if pair is None:
            raise ValidationError("Choose an exact legal beat boundary: the cut must fit the duration range, keep speech whole, "
                                  "and contain a full opening beat within the hook window. See the timeline for legal cuts.")
        start, end = pair
        raw = {"title": candidate["title"], "angle": candidate["angle"],
               "segments": [{"episode": episode.number, "start_beat": start, "end_beat": end}],
               "hook_beat": start, "cliffhanger_beat": end,
               "hook": "Editor-modified opening; AI rationale and score do not apply to this cut.",
               "cliffhanger": "Editor-modified ending; reassess suspense against the source.",
               "why_it_works": "Human edit; narrative quality has not been rescored by the model.",
               "scores": {name: 0 for name in DIMENSIONS},
               "risks": ["The AI score and verdict apply only to the original cut."]}
        effective = validate_candidate(raw, [episode], policy)
        effective.update(score=None, scores=None, review_status="editor_modified_not_ai_rescored")
    return {"original_candidate_id": candidate["id"], "original_score": candidate["score"],
            "modified": modified, "decision": decision, "feedback_reason": reason, "notes": notes.strip(),
            "reviewed_utc": datetime.now(timezone.utc).isoformat(), "effective_candidate": effective}


def _csv_cell(value):
    text = str(value) if value is not None else ""
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text


def export_bundle(result, reviews):
    """Keep immutable model results and editor decisions together, with explicit scope."""
    return {"schema_version": 1, "manifest": result["manifest"], "coverage": result["coverage"],
            "shortlist": result["shortlist"], "grounded_candidates": result["grounded_candidates"],
            "accepted_candidates": result["accepted_candidates"], "rejections": result["rejections"],
            "editor_reviews": reviews, "selection_audit": exclusion_audit(result),
            "provenance": result.get("provenance", {}),
            "notice": "AI editorial scores are not conversion probabilities. Modified cuts are not AI-rescored. "
                      "Script timestamps need final-media verification. Treat exports of private inputs as private."}


def export_csv(result, reviews, accepted_only=False):
    """One row per segment; reviewed cuts stay distinguishable from model cuts."""
    fields = ["candidate_id", "effective_candidate_id", "rank", "title", "episode", "start", "end", "start_ms", "end_ms",
              "duration_seconds", "ai_score_original", "ai_score_effective", "editor_decision", "editor_modified",
              "feedback_reason", "editor_notes", "hook", "cliffhanger", "reasoning", "validation", "source_sha256"]
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields)
    writer.writeheader()
    candidates = candidate_catalog(result)
    ids = ([cid for cid, review in reviews.items() if review["decision"] == "Accepted"] if accepted_only
           else list(dict.fromkeys([c["id"] for c in result["shortlist"]] + list(reviews))))
    for cid in ids:
        original = candidates[cid]
        review = reviews.get(cid, {})
        effective = review.get("effective_candidate", original)
        for segment in effective["segments"]:
            row = {"candidate_id": cid, "effective_candidate_id": effective["id"], "rank": original.get("rank", ""),
                   "title": effective["title"], "episode": segment["episode"], "start": segment["start"], "end": segment["end"],
                   "start_ms": segment["start_ms"], "end_ms": segment["end_ms"], "duration_seconds": segment["duration_seconds"],
                   "ai_score_original": original["score"] if result["manifest"]["mode"] != "baseline" else None,
                   "ai_score_effective": effective["score"] if result["manifest"]["mode"] != "baseline" else None,
                   "editor_decision": review.get("decision", "Pending"), "editor_modified": review.get("modified", False),
                   "feedback_reason": review.get("feedback_reason", ""), "editor_notes": review.get("notes", ""),
                   "hook": effective["hook"], "cliffhanger": effective["cliffhanger"], "reasoning": effective["why_it_works"],
                   "validation": effective["validation"], "source_sha256": segment["source_sha256"]}
            writer.writerow({key: _csv_cell(value) for key, value in row.items()})
    return stream.getvalue().encode("utf-8-sig")
