"""PromoPilot AI: local/private-session UI for the reusable promo_finder engine."""

from html import escape
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import zipfile

import streamlit as st

from promo_finder.config import DIMENSIONS, Policy
from promo_finder.parser import InputError, format_time
from promo_finder.pipeline import batches_for, run
from promo_finder.providers import JSONProvider, ProviderError
from promo_finder.workspace import (DECISIONS, FEEDBACK_REASONS, candidate_catalog, exclusion_audit,
                                    export_bundle, export_csv, ingest_uploads, read_run, review_candidate)
from promo_finder.selection import ValidationError

ROOT = Path(__file__).resolve().parent
DEFAULT_MODEL = "gemini-3.1-flash-lite"
LABELS = {"hook": "Opening hook", "cliffhanger": "Cliffhanger", "standalone": "Standalone clarity",
          "emotion": "Emotional impact", "escalation": "Escalation"}


def reset_workspace():
    temporary = st.session_state.get("job_directory")
    if temporary:
        temporary.cleanup()
    epoch = st.session_state.get("upload_epoch", 0) + 1
    settings = {"mode", "model_id", "call_limit", "top_count", "duration_range", "per_episode", "audience"}
    settings.update("weight_" + name for name in DIMENSIONS)
    for key in list(st.session_state):
        if key not in settings:
            del st.session_state[key]
    # File-upload widget values cannot be assigned. A new identity removes the
    # browser's old file chips and releases the old widget on the next render.
    st.session_state.update(upload_epoch=epoch, visitor_key="", page="Find promos")


def set_workspace(episodes, result=None, label="Uploaded screenplay"):
    temporary = st.session_state.pop("job_directory", None)
    if temporary:
        temporary.cleanup()
    for key in list(st.session_state):
        if key.startswith(("edit_", "review_", "compare_", "candidate_", "timeline_")) or key in {"job_signature", "notice", "upload_error", "loaded_upload", "attempted_upload"}:
            del st.session_state[key]
    st.session_state.update(episodes=episodes, result=result, reviews={}, workspace_label=label)


def load_demo():
    files = [("airlock.txt", (ROOT / "examples/scifi/airlock.txt").read_bytes()),
             ("scoreboard.json", (ROOT / "examples/sports/scoreboard.json").read_bytes())]
    episodes, _ = ingest_uploads(files)
    result = json.loads((ROOT / "examples/demo/recorded_run.json").read_text())
    set_workspace(episodes, result, "Fictional showcase · recorded Gemini run")
    st.session_state.upload_epoch = st.session_state.get("upload_epoch", 0) + 1


def configured_key():
    # Never write a visitor's key to os.environ or disk. .env is local-only and
    # excluded from the source distribution. Cloud keys are optional secrets.
    value = os.environ.get("GEMINI_API_KEY", "")
    if not value and (ROOT / ".env").is_file():
        for line in (ROOT / ".env").read_text().splitlines():
            if line.strip().startswith("GEMINI_API_KEY="):
                value = line.split("=", 1)[1].strip().strip("\"'")
    if not value:
        try:
            value = st.secrets.get("GEMINI_API_KEY", "")
        except (FileNotFoundError, st.errors.StreamlitSecretNotFoundError):
            pass
    return value


def active_credentials(entered_key):
    if entered_key.strip():
        return entered_key.strip(), "visitor"
    if st.session_state.get("shared_key_unavailable"):
        return "", "unavailable"
    return configured_key(), "shared"


def retry_shared_key():
    st.session_state.pop("shared_key_unavailable", None)
    st.session_state.visitor_key = ""


def controls():
    with st.popover("Settings", width="stretch"):
        top = st.slider("Number of promos", 1, 30, 10, key="top_count")
        duration = st.slider("Clip length (seconds)", 10, 90, (20, 60), key="duration_range")
        with st.expander("Advanced settings"):
            mode = st.radio("Analysis mode", ["Gemini AI", "Timing-only baseline"], key="mode")
            st.caption("Timing-only checks clip boundaries without AI scoring.")
            model = st.text_input("Model ID", DEFAULT_MODEL, key="model_id")
            max_calls = st.number_input("Request limit per run", 2, 60, 30, step=1, key="call_limit")
            audience = st.text_area("Target audience", Policy().audience, max_chars=500, key="audience")
            per_episode = st.number_input("Candidates per episode", 1, 4, 2, key="per_episode")
            st.caption("Scoring weights must total 100.")
            weights = {name: st.number_input(LABELS[name], 0, 100, Policy().weights[name], key="weight_" + name) for name in DIMENSIONS}
    try:
        policy = Policy(min_duration_ms=duration[0] * 1000, max_duration_ms=duration[1] * 1000,
                        weights=weights, candidates_per_episode=per_episode, audience=audience)
    except ValueError as exc:
        st.error(str(exc))
        policy = None
    return mode, "", model, max_calls, top, policy


def access_panel(mode):
    unavailable = st.session_state.get("shared_key_unavailable", False)
    needs_key = mode == "Gemini AI" and (unavailable or not configured_key()) and not st.session_state.get("visitor_key", "").strip()
    with st.expander("Add a Gemini key to continue" if needs_key else "Gemini access · optional", expanded=needs_key):
        if unavailable:
            st.warning("Shared access is unavailable. Use your own key or retry later. Your progress is saved.")
            st.button("Retry shared access", on_click=retry_shared_key)
        key = st.text_input("Gemini API key", type="password", key="visitor_key", placeholder="Optional when shared access is available")
        st.caption("Your key stays in this session. Leave blank to use shared access.")
    return key


def open_review(candidate_id):
    st.session_state.candidate_selected = candidate_id
    st.session_state.page = "Review & download"


def card(candidate, baseline=False, review=None):
    segment = candidate["segments"][0]
    score = "Unrated" if baseline else f"{candidate['score']:g}<small>/100</small>"
    modified = review and review["modified"]
    status = (("Edited cut: " if modified else "") + review["decision"]) if review else "Editor pending"
    score_label = "Timing check" if baseline else ("Original cut score" if modified else "Editorial score")
    rank = candidate.get("rank", "—")
    rank = f"{rank:02d}" if isinstance(rank, int) else rank
    st.markdown(f'''<article class="promo-card"><div class="card-top"><div class="rank">{rank}</div>
      <div class="card-title"><h3>{escape(candidate['title'])}</h3><p>Episode {segment['episode']} · {segment['start']} — {segment['end']} · {candidate['duration_seconds']:g}s</p></div>
      <div class="score">{score}<span>{score_label}</span></div></div>
      <div class="score-track"><div style="width:{0 if baseline else candidate['score']}%"></div></div>
      <div class="chips"><span>{escape(candidate['angle'])}</span><span>{escape(status)}</span>
      <span>{'Edited · not rescored' if review and review['modified'] else 'Source-grounded'}</span></div></article>''', unsafe_allow_html=True)


def show_evidence(candidate, weights, baseline=False):
    st.markdown("**Opening hook**")
    st.markdown('<blockquote class="script-quote">' + escape(candidate["hook_source"]["text"]) + '</blockquote>', unsafe_allow_html=True)
    st.markdown("**Ending**")
    st.markdown('<blockquote class="script-quote">' + escape(candidate["cliffhanger_source"]["text"]) + '</blockquote>', unsafe_allow_html=True)
    with st.expander("Why this clip works"):
        st.write(candidate.get("critic", {}).get("reason") or candidate["why_it_works"])
        st.caption(candidate["hook"])
        st.caption(candidate["cliffhanger"])
        if candidate.get("rank_reason"):
            st.caption(candidate["rank_reason"])
        for risk in candidate.get("risks", []):
            st.write("• " + risk)
    with st.expander("Scores & source details"):
        if candidate.get("scores") is not None:
            st.dataframe([{"Criterion": LABELS[name], "Score / 5": candidate["scores"][name], "Weight %": weights[name]}
                          for name in DIMENSIONS], hide_index=True, width="stretch")
        st.caption("Unrated timing check." if baseline else "AI judgment, not a prediction of clicks or installs.")
        for segment in candidate["segments"]:
            st.dataframe([{"Start": format_time(b["start_ms"]), "End": format_time(b["end_ms"]),
                           "Speaker": b["label"], "Text": b["text"]} for b in segment["beats"]],
                         hide_index=True, width="stretch")
            st.caption(f"Source fingerprint: {segment['source_sha256']}")
        st.caption(candidate["validation"] + " Check timing against the final audio or video.")


def download_panel(result):
    reviews = st.session_state.reviews
    with st.container(key="exports_panel"):
        left, right = st.columns(2)
        left.download_button("Download promos · CSV", export_csv(result, reviews),
                             "promopilot-shortlist.csv", "text/csv", width="stretch")
        with right.popover("More downloads", width="stretch"):
            st.download_button("Kept clips · CSV", export_csv(result, reviews, accepted_only=True),
                               "promopilot-approved.csv", "text/csv", width="stretch",
                               disabled=not any(r["decision"] == "Accepted" for r in reviews.values()))
            st.download_button("Full results · JSON", json.dumps(export_bundle(result, reviews), indent=2, ensure_ascii=False).encode(),
                               "promopilot-results.json", "application/json", width="stretch")
            st.caption("Keep downloads from private scripts private.")


def analyze(settings):
    mode, entered_key, model, max_calls, top, policy = settings
    episodes = st.session_state.episodes
    try:
        if mode == "Gemini AI":
            key, key_source = active_credentials(entered_key)
            if not key:
                st.error("Add a Gemini key in Gemini access above to continue.")
                return
            batches_for(episodes, policy=policy)
        signature = hashlib.sha256(json.dumps({"sources": [e.sha256 for e in episodes], "policy": policy.to_dict(),
                                              "mode": mode, "model": model, "top": top}, sort_keys=True).encode()).hexdigest()
        if signature != st.session_state.get("job_signature"):
            prior = st.session_state.pop("job_directory", None)
            if prior:
                prior.cleanup()
            st.session_state.job_directory = tempfile.TemporaryDirectory(prefix="promopilot-session-")
            st.session_state.job_signature = signature
            st.session_state.reviews = {}
        directory = Path(st.session_state.job_directory.name)
        provider = (JSONProvider("gemini", model, directory / "cache", max_calls=max_calls, min_interval=12, api_key=key)
                    if mode == "Gemini AI" else None)
        with st.status("Analyzing screenplay…", expanded=True) as status:
            progress = st.empty()
            run(episodes, directory, provider=provider, top=top, policy=policy, progress=progress.write)
            result = read_run(directory)
            result["provenance"] = {"kind": "live_session_run", "synthetic": False}
            st.session_state.result = result
            if mode == "Gemini AI" and key_source == "shared" and any(
                    error.get("credential_issue") for error in result["manifest"]["errors"]):
                st.session_state.shared_key_unavailable = True
            status.update(label="Analysis complete" if result["manifest"]["complete"] else "Partial results saved", state="complete")
        st.rerun()
    except (InputError, ProviderError, ValueError) as exc:
        st.error(str(exc))


def studio_intro():
    # Decorative illustration only; no invented clips, timestamps or model scores.
    bars = "".join(f'<i style="--h:{height}%"></i>' for height in
                   [18, 32, 55, 28, 68, 88, 48, 65, 96, 60, 38, 72, 90, 54, 28, 44, 76, 58, 34, 18])
    st.markdown(f'''<section class="studio-hero">
      <div class="hero-copy"><div class="eyebrow"><span></span> THE PROMO STUDIO</div>
      <h2>Big stories.<br><em>Unmissable moments.</em></h2>
      <p>Find the part that makes people want the rest.</p>
      <div class="hero-tags"><span>Strong hooks</span><span>Cliffhanger endings</span><span>Your final cut</span></div></div>
      <div class="hero-art" aria-hidden="true">
        <div class="script-sheet"><div class="sheet-label">YOUR SCRIPT <span>↗</span></div>
        <div class="script-lines"><i></i><i></i><i></i><i></i></div>
        <div class="highlight-line"><span></span> A moment worth sharing.</div>
        <div class="script-lines bottom-lines"><i></i><i></i></div></div>
        <div class="cut-card"><div class="cut-heading"><span class="play-mark">▶</span><span>THE PROMO CUT</span><span>✦</span></div>
        <div class="waveform">{bars}</div><div class="cut-labels"><span>HOOK</span><span>CLIFFHANGER ↗</span></div></div>
        <span class="art-note">SCRIPT → SHORTLIST</span>
      </div></section>''', unsafe_allow_html=True)


def workflow_steps():
    current = 3 if st.session_state.result and st.session_state.result["manifest"]["complete"] else (2 if st.session_state.episodes else 1)
    steps = "".join(f'<div class="flow-step {"active" if number == current else "done" if number < current else ""}">'
                    f'<span class="step-number">{"✓" if number < current else f"0{number}"}</span><span>{label}</span></div>'
                    for number, label in [(1, "Upload a story"), (2, "Find the moments"), (3, "Make your picks")])
    st.markdown(f'<div class="workflow-steps">{steps}</div>', unsafe_allow_html=True)


def discover(settings):
    result = st.session_state.result
    if not result:
        studio_intro()
    workflow_steps()
    with st.expander("1 · Upload scripts" if not result else "Change scripts", expanded=not bool(result)):
        uploaded = st.file_uploader("TXT, JSON or ZIP", type=["txt", "json", "zip"], accept_multiple_files=True,
                                    key=f"uploads_{st.session_state.upload_epoch}", help="Scripts need timestamps. Up to 25 MB total.")
        files = [(file.name, file.getvalue()) for file in uploaded]
        signature = hashlib.sha256(json.dumps([(name, hashlib.sha256(body).hexdigest()) for name, body in files]).encode()).hexdigest() if files else None
        loaded = bool(signature and signature == st.session_state.get("loaded_upload"))
        if st.button("Scripts loaded" if loaded else "Load scripts", disabled=not files or loaded, width="stretch", type="primary" if files and not loaded else "secondary"):
            st.session_state.attempted_upload = signature
            try:
                with st.spinner("Loading scripts…"):
                    episodes, ignored = ingest_uploads(files)
                set_workspace(episodes, label="Uploaded screenplay")
                st.session_state.update(attempted_upload=signature, loaded_upload=signature,
                    notice=f"Loaded {len(episodes)} {'episode' if len(episodes) == 1 else 'episodes'}.",
                    upload_skipped=len(ignored))
                st.rerun()
            except InputError as exc:
                st.session_state.upload_error = str(exc)
        if files and signature == st.session_state.get("attempted_upload") and st.session_state.get("upload_error"):
            st.error(st.session_state.upload_error)
        elif files and not loaded:
            st.caption("Files selected. Click Load scripts to continue.")
        elif st.session_state.get("notice"):
            st.success(st.session_state.notice)
            if st.session_state.get("upload_skipped"):
                st.caption(f"Skipped {st.session_state.upload_skipped} non-script files.")
    if files and signature != st.session_state.get("loaded_upload"):
        return
    episodes = st.session_state.episodes
    if not episodes:
        return
    mode, entered_key, _, _, top, policy = settings
    key_ready, source = active_credentials(entered_key)
    if not result:
        st.markdown("### 2 · Find promos")
    if mode == "Gemini AI":
        if not key_ready:
            st.warning("Add your Gemini key in Gemini access above to continue.")
        st.caption("Uses Gemini to analyze your scripts. Only upload content you’re allowed to share with Google.")
    button = "Find promos" if not result else ("Continue analysis" if not result["manifest"]["complete"] else "Find promos again")
    if st.button(button, type="primary", disabled=policy is None or (mode == "Gemini AI" and not key_ready)):
        analyze(settings)
    if not result:
        st.caption(f"{len(episodes)} episodes ready · up to {top} promos · adjust length in Settings")
        return
    manifest = result["manifest"]
    st.markdown('<div class="section-heading"><span>THE SHORTLIST</span><h2>Your next great promos.</h2></div>', unsafe_allow_html=True)
    st.caption(f"{len(result['shortlist'])} picks · {manifest['episodes_processed']}/{manifest['input_count']} episodes analyzed")
    if result.get("provenance", {}).get("kind") == "recorded_synthetic_demo":
        st.caption("Demo: saved Gemini results on fictional scripts.")
    if not manifest["complete"]:
        st.warning("Some episodes are unfinished. Continue analysis to resume.")
        with st.expander("Analysis details", expanded=not bool(result["shortlist"])):
            for error in manifest["errors"]:
                st.error(error["error"])
    if manifest["mode"] == "baseline":
        st.warning("Timing-only results. These clips have not been scored by AI.")
    if not result["shortlist"]:
        attempts = max((row.get("discovery_attempts", 1) for row in result["coverage"]), default=1)
        if not manifest["complete"]:
            st.info("No approved promos yet. Continue analysis to finish the pending checks.")
        else:
            st.info("No clips passed after checking alternative cuts." if attempts > 1 else "No clips passed the checks.")
        rows = exclusion_audit(result)
        if rows:
            st.markdown("**Why these clips weren't selected**")
            for row in rows[:4]:
                st.text(row["reason"])
            if len(rows) > 4:
                st.caption("More tools → Skipped clips has the remaining reasons.")
        elif manifest["complete"]:
            st.caption("No candidate was proposed that met the current timing and story requirements. Try a fuller timed screenplay; subtitles can omit important scene context.")
        st.download_button("Download analysis · JSON", json.dumps(export_bundle(result, st.session_state.reviews), indent=2, ensure_ascii=False).encode(),
                           "promopilot-results.json", "application/json")
    else:
        download_panel(result)
        st.caption("Review a promo to keep it, reject it, or adjust its timing.")
    for candidate in result["shortlist"]:
        card(candidate, manifest["mode"] == "baseline", st.session_state.reviews.get(candidate["id"]))
        st.button("Review promo", key="review_open_" + candidate["id"], on_click=open_review, args=(candidate["id"],))
        with st.expander("View script · " + candidate["title"]):
            show_evidence(candidate, manifest["weights"], manifest["mode"] == "baseline")


def review_panel():
    result = st.session_state.result
    st.markdown('<div class="section-heading"><span>THE EDITING ROOM</span><h2>Make the final call.</h2></div>', unsafe_allow_html=True)
    if not result or not candidate_catalog(result):
        st.info("Find promos or try the demo to start reviewing.")
        return
    catalog = candidate_catalog(result)
    ids = list(dict.fromkeys([c["id"] for c in result["shortlist"]] + list(catalog)))
    label = lambda cid: f"{catalog[cid]['title']} · Episode {catalog[cid]['segments'][0]['episode']}"
    cid = st.selectbox("Clip to review", ids, format_func=label, key="candidate_selected")
    candidate = catalog[cid]
    approved = {c["id"] for c in result["accepted_candidates"]}
    if cid not in approved:
        st.warning("This clip has not passed AI review. Check it before keeping it.")
    baseline = result["manifest"]["mode"] == "baseline"
    if st.checkbox("Compare with another clip", key="compare_enabled"):
        alternatives = [other for other in ids if other != cid]
        if alternatives:
            other = st.selectbox("Comparison clip", alternatives, format_func=label, key="compare_clip")
            with st.container(key="compare_panel"):
                columns = st.columns(2)
            for column, compare_id in zip(columns, (cid, other)):
                with column:
                    card(catalog[compare_id], baseline, st.session_state.reviews.get(compare_id))
                    show_evidence(catalog[compare_id], result["manifest"]["weights"], baseline)
        else:
            st.info("This run has only one grounded candidate.")
    else:
        card(candidate, baseline, st.session_state.reviews.get(cid))
        show_evidence(candidate, result["manifest"]["weights"], baseline)
    current = st.session_state.reviews.get(cid, {})
    effective = current.get("effective_candidate", candidate)
    segment = effective["segments"][0]
    policy = Policy(**result["manifest"]["policy"])
    st.markdown("### Your decision")
    with st.form("review_form_" + cid):
        decision = st.radio("Decision", DECISIONS, index=DECISIONS.index(current.get("decision", "Pending")), horizontal=True,
                            format_func=lambda value: {"Pending": "Not reviewed", "Accepted": "Keep", "Rejected": "Reject"}[value], key="edit_decision_" + cid)
        with st.expander("Adjust timing or add a note"):
            columns = st.columns(2)
            start = columns[0].text_input("Start", segment["start"], key="edit_start_" + cid)
            end = columns[1].text_input("End", segment["end"], key="edit_end_" + cid)
            st.caption("Use exact script boundaries. Edited clips need a fresh quality check.")
            reason = st.selectbox("Reason", FEEDBACK_REASONS, index=FEEDBACK_REASONS.index(current.get("feedback_reason", "No issue")), key="edit_reason_" + cid)
            notes = st.text_area("Notes", current.get("notes", ""), max_chars=3000, key="edit_notes_" + cid,
                                 placeholder="Add a reason if rejecting this promo.")
        save = st.form_submit_button("Save decision", type="primary")
    if save:
        try:
            st.session_state.reviews[cid] = review_candidate(candidate, st.session_state.episodes, policy, start, end, decision, reason, notes)
            st.success("Decision saved. Download your promos to keep a copy.")
            current = st.session_state.reviews[cid]
        except (InputError, ValidationError) as exc:
            st.error(str(exc))
    if current:
        effective = current["effective_candidate"]
        st.info(f"Saved: {current['decision']} · {effective['segments'][0]['start']}–{effective['segments'][0]['end']} · "
                + ("Edited cut · not AI-rescored" if current["modified"] else "Original cut"))
        if current["modified"]:
            with st.expander("Inspect the edited cut"):
                show_evidence(effective, result["manifest"]["weights"])
    download_panel(result)


def exclusion_panel():
    st.markdown("### Skipped clips")
    result = st.session_state.result
    if not result:
        st.info("Analyze a screenplay or load the fictional demo to inspect decisions.")
        return
    rows = exclusion_audit(result)
    if not rows:
        st.success("No rejections or shortlist omissions were recorded in this run.")
    else:
        stage = st.selectbox("Decision stage", ["All stages"] + sorted({r["stage"] for r in rows}))
        for row in rows:
            if stage != "All stages" and row["stage"] != stage:
                continue
            with st.container(border=True):
                st.markdown("**" + escape(row["title"]) + "**")
                st.caption(row["stage"])
                st.text(row["reason"])
                if row.get("review"):
                    st.dataframe([{"Criterion": LABELS[name], "Score / 5": value} for name, value in row["review"]["scores"].items()], hide_index=True)
                if row["candidate"].get("hook_source"):
                    with st.expander("Check the evidence · " + row["id"]):
                        show_evidence(row["candidate"], result["manifest"]["weights"])
                if row["id"] in result.get("provenance", {}).get("challenge_candidate_ids", []):
                    st.caption("Synthetic challenge: a deliberately constructed cut, reviewed by the real model. It was not a discovery recommendation.")
    manual = [review for review in st.session_state.reviews.values() if review["decision"] == "Rejected"]
    if manual:
        st.markdown("### Editor rejections")
        for review in manual:
            st.text(f"{review['effective_candidate']['title']}: {review['feedback_reason']}. {review['notes']}")
    with st.expander("Episode processing coverage"):
        st.dataframe(result["coverage"], hide_index=True, width="stretch")


def timeline_panel():
    st.markdown("### Script timeline")
    episodes = st.session_state.episodes
    if not episodes:
        st.info("Load scripts to inspect their episode timelines.")
        return
    selected = st.selectbox("Episode", [e.number for e in episodes], key="timeline_episode")
    episode = next(e for e in episodes if e.number == selected)
    result = st.session_state.result
    policy = Policy(**result["manifest"]["policy"]) if result else Policy()
    st.caption(f"{episode.filename} · {len(episode.beats)} beats · Last source time {format_time(episode.duration_ms)}")
    import altair as alt
    import pandas as pd
    rows = [{"Start (s)": b.start_ms / 1000, "End (s)": b.end_ms / 1000, "Type": b.kind, "Beat": b.id,
             "Speaker / label": b.label, "Text": b.text} for b in episode.beats]
    chart = alt.Chart(pd.DataFrame(rows)).mark_bar(cornerRadius=3).encode(
        x=alt.X("Start (s):Q", title="Episode source time · seconds"), x2="End (s):Q", y=alt.Y("Type:N", title=None),
        color=alt.Color("Type:N", legend=None, scale=alt.Scale(range=["#30c9ad", "#8795df", "#6a8296", "#d7ae69"])),
        tooltip=["Beat", "Speaker / label", "Start (s)", "End (s)"]).properties(height=180)
    st.altair_chart(chart, width="stretch")
    kind = st.multiselect("Show beat types", sorted({b.kind for b in episode.beats}), default=sorted({b.kind for b in episode.beats}), key="timeline_kinds_" + str(selected))
    st.dataframe([{"Beat": b.id, "Start": format_time(b.start_ms), "End": format_time(b.end_ms), "Type": b.kind,
                   "Speaker / label": b.label, "Text": b.text, "Source line": b.source_line}
                  for b in episode.beats if b.kind in kind], hide_index=True, width="stretch")
    with st.expander("Legal cut boundaries for the editor"):
        legal = episode.legal_continuous_cuts(policy)
        lookup = {b.id: b for b in episode.beats}
        if not legal:
            st.info("No legal continuous cuts for this episode under the current duration/opening rules.")
        else:
            start = st.selectbox("Opening beat", list(legal), format_func=lambda bid: f"{format_time(lookup[bid].start_ms)} · {lookup[bid].label}", key="timeline_start_" + str(selected))
            st.dataframe([{"Start": format_time(lookup[start].start_ms), "End": format_time(lookup[end].end_ms),
                           "Duration (s)": (lookup[end].end_ms - lookup[start].start_ms) / 1000,
                           "Ending evidence": lookup[end].text} for end in legal[start]], hide_index=True, width="stretch")
    st.caption("Times are copied from the supplied script, not inferred from video. Overlapping action/camera/dialogue beats are preserved.")


def benchmark_panel():
    data = json.loads((ROOT / "benchmarks/kuku-results.json").read_text())
    complete = data["complete_run"]
    st.markdown("## Built for new scripts. Benchmarked on a full pack.")
    st.write("The Kuku pack is the primary private development benchmark. It informed format support and evaluation criteria; it is not an unseen test set.")
    for col, (label, value) in zip(st.columns(4), [("Episodes processed", complete["episodes_processed"]),
                                                  ("Timed beats", complete["beats"]), ("Legal cuts validated", complete["validated_cut_pairs"]),
                                                  ("Final shortlist", complete["shortlisted"])]):
        col.metric(label, value)
    st.dataframe([{"Stage": "Model proposals", "Count": complete["proposed"]}, {"Stage": "Source-grounded", "Count": complete["grounded"]},
                   {"Stage": "Passed AI review", "Count": complete["accepted"]}, {"Stage": "Diversified shortlist", "Count": complete["shortlisted"]}],
                  hide_index=True, width="stretch")
    st.caption(f"Recorded benchmark · {complete['model']} · critic: {complete['critic_model']} · {data['prompt_version']}")
    st.markdown("### What these results establish")
    st.write("All episodes were parsed and processed. Returned cuts are checked against their source IDs, timestamps, duration and spoken boundaries. The same engine accepts different names, episode identifiers, time offsets and genres.")
    st.write(f"AI approval is an editorial estimate. Human precision, recall, click-through rate and installs have not been measured. A stronger-reviewer experiment stopped after {data['stronger_reviewer_run']['episodes_processed']} episodes when its free quota ran out; its partial results are not presented as a completed benchmark.")
    st.info("Only aggregate metrics are included here. Kuku’s source scripts, character names, cut points, and raw results remain outside the public distribution.")
    st.markdown("### Quality control in the product")
    st.write("Parse → legal cut catalog → AI discovery → source validation → blind AI review → diverse ranking → human review. Invalid proposals and unfinished work remain visible in the audit.")


def main():
    st.set_page_config(page_title="PromoPilot AI", page_icon="🎬", layout="wide")
    st.markdown("<style>" + (ROOT / "web/style.css").read_text() + "</style>", unsafe_allow_html=True)
    for key, default in {"episodes": [], "result": None, "reviews": {}, "workspace_label": "", "upload_epoch": 0}.items():
        st.session_state.setdefault(key, default)
    with st.container(key="app_header"):
        brand, demo_col, settings_col, reset_col = st.columns([4.5, 1.5, 1.5, 1.5], vertical_alignment="center")
        brand.markdown('<div class="brand"><div class="brand-mark">▶</div><div><h1>PromoPilot <em>AI</em></h1><span class="brand-subtitle">A little spark. A bigger audience.</span></div></div>', unsafe_allow_html=True)
        with demo_col.popover("Sample demo", width="stretch"):
            st.caption("Preview sample promos using fictional scripts. No upload or API key needed.")
            if st.session_state.episodes:
                st.caption("Opening the demo replaces your current workspace.")
            st.button("Try a demo", on_click=load_demo, width="stretch")
        with settings_col:
            settings = controls()
        reset_col.button("Start over", on_click=reset_workspace, width="stretch", help="Clears scripts, results, reviews and your entered key.")
    mode, _, model, max_calls, top, policy = settings
    if st.session_state.get("page") not in {"Find promos", "Review & download"}:
        st.session_state.page = "Find promos"
    with st.container(key="workspace_bar"):
        nav, access = st.columns([3, 2], vertical_alignment="top")
        with nav:
            page = st.radio("Workspace", ["Find promos", "Review & download"], horizontal=True, label_visibility="collapsed", key="page")
        with access:
            key = access_panel(mode)
    settings = mode, key, model, max_calls, top, policy
    if page == "Find promos":
        discover(settings)
    else:
        review_panel()
    st.divider()
    with st.expander("More tools"):
        tool = st.selectbox("Open a tool", ["Choose a tool", "Script timeline", "Skipped clips", "Benchmark", "Sample files"], key="more_tool")
        if tool == "Script timeline":
            timeline_panel()
        elif tool == "Skipped clips":
            exclusion_panel()
        elif tool == "Benchmark":
            benchmark_panel()
        elif tool == "Sample files":
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("airlock.txt", (ROOT / "examples/scifi/airlock.txt").read_bytes())
                archive.writestr("scoreboard.json", (ROOT / "examples/sports/scoreboard.json").read_bytes())
            st.download_button("Download sample ZIP", stream.getvalue(), "fictional-screenplays.zip", "application/zip")
            st.caption("Original fictional scripts. Kuku’s private screenplay pack is not included.")


if __name__ == "__main__":
    main()
