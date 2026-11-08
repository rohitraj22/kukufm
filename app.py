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
    st.session_state.update(upload_epoch=epoch, visitor_key="", page="Discover")


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


def controls():
    with st.sidebar:
        st.markdown("### Your workspace")
        st.caption("From screenplay to an editor-ready shortlist.")
        mode = st.radio("Analysis mode", ["Gemini AI", "Timing-only baseline"], key="mode")
        st.caption("The baseline checks cut boundaries; it does not assess narrative quality.")
        key, model, max_calls = "", DEFAULT_MODEL, 30
        if mode == "Gemini AI":
            key = st.text_input("Gemini API key", type="password", key="visitor_key", placeholder="Optional if a server key is configured")
            if not key and configured_key():
                st.caption("✓ Local / server API key configured")
            with st.expander("Model & request budget"):
                model = st.text_input("Model ID", DEFAULT_MODEL, key="model_id")
                max_calls = st.number_input("Maximum API requests per click", 2, 60, 30, step=1, key="call_limit")
                st.caption("Discovery and review share this cap. Free quotas depend on your Google account. No automatic model fallback.")
        top = st.slider("Shortlist size", 1, 30, 10, key="top_count")
        duration = st.slider("Clip duration · seconds", 10, 90, (20, 60), key="duration_range")
        with st.expander("Editorial rubric"):
            st.caption("Weights must total 100. Used for new analyses; recorded results retain their original rubric.")
            weights = {name: st.number_input(LABELS[name], 0, 100, Policy().weights[name], key="weight_" + name) for name in DIMENSIONS}
            per_episode = st.number_input("Candidates per episode", 1, 4, 2, key="per_episode")
            audience = st.text_area("Target audience", Policy().audience, max_chars=500, key="audience")
        try:
            policy = Policy(min_duration_ms=duration[0] * 1000, max_duration_ms=duration[1] * 1000,
                            weights=weights, candidates_per_episode=per_episode, audience=audience)
        except ValueError as exc:
            st.error(str(exc))
            policy = None
        st.divider()
        st.caption("Uploads and review notes belong to this session. Clear your workspace to remove its temporary files, results, and entered key.")
        st.button("Clear workspace", on_click=reset_workspace, width="stretch")
        st.caption("PromoPilot AI · v0.3 · Script-to-promo intelligence")
    return mode, key, model, max_calls, top, policy


def card(candidate, baseline=False, review=None):
    segment = candidate["segments"][0]
    score = "Unrated" if baseline else f"{candidate['score']:g}<small>/100</small>"
    modified = review and review["modified"]
    status = (("Edited cut: " if modified else "") + review["decision"]) if review else "Editor pending"
    score_label = "Timing check" if baseline else ("Original cut score" if modified else "Editorial score")
    rank = candidate.get("rank", "—")
    st.markdown(f'''<article class="promo-card"><div class="card-top"><div class="rank">{rank}</div>
      <div class="card-title"><h3>{escape(candidate['title'])}</h3><p>Episode {segment['episode']} · {segment['start']} — {segment['end']} · {candidate['duration_seconds']:g}s</p></div>
      <div class="score">{score}<span>{score_label}</span></div></div>
      <div class="score-track"><div style="width:{0 if baseline else candidate['score']}%"></div></div>
      <div class="chips"><span>{escape(candidate['angle'])}</span><span>{escape(status)}</span>
      <span>{'Edited · not rescored' if review and review['modified'] else 'Source-grounded'}</span></div></article>''', unsafe_allow_html=True)


def show_evidence(candidate, weights, baseline=False):
    if candidate.get("scores") is not None:
        st.dataframe([{"Criterion": LABELS[name], "Score / 5": candidate["scores"][name], "Weight %": weights[name],
                       "Contribution / 100": round(candidate["scores"][name] * weights[name] / 5, 1)} for name in DIMENSIONS],
                     hide_index=True, width="stretch")
        st.caption("Zero means unrated in baseline mode." if baseline else "Weighted editorial judgment; not measured click or install probability.")
    st.markdown("**Opening evidence**")
    st.text(candidate["hook_source"]["text"])
    st.caption(candidate["hook"])
    st.markdown("**Ending evidence**")
    st.text(candidate["cliffhanger_source"]["text"])
    st.caption(candidate["cliffhanger"])
    if candidate.get("critic"):
        st.markdown("**AI reviewer’s reasoning**")
        st.text(candidate["critic"]["reason"])
    else:
        st.text(candidate["why_it_works"])
    if candidate.get("rank_reason"):
        st.caption(candidate["rank_reason"])
    if candidate.get("risks"):
        st.markdown("**Review flags**")
        for risk in candidate["risks"]:
            st.text("• " + risk)
    with st.expander("Included source beats"):
        for segment in candidate["segments"]:
            st.caption(f"Episode {segment['episode']} · source fingerprint {segment['source_sha256'][:12]}")
            st.dataframe([{"Start": format_time(b["start_ms"]), "End": format_time(b["end_ms"]), "Type": b["kind"],
                           "Speaker / label": b["label"], "Text": b["text"], "Whole beat": b["fully_included"]}
                          for b in segment["beats"]], hide_index=True, width="stretch")
    st.caption(candidate["validation"] + " Final-media timing still needs verification.")


def download_panel(result):
    reviews = st.session_state.reviews
    bundle = export_bundle(result, reviews)
    with st.container(key="exports_panel"):
        columns = st.columns(3)
    columns[0].download_button("↓ Full results · JSON", json.dumps(bundle, indent=2, ensure_ascii=False).encode(),
                               "promopilot-results.json", "application/json", width="stretch")
    columns[1].download_button("↓ Shortlist + reviews · CSV", export_csv(result, reviews),
                               "promopilot-shortlist.csv", "text/csv", width="stretch")
    columns[2].download_button("↓ Editor-accepted cuts · CSV", export_csv(result, reviews, accepted_only=True),
                               "promopilot-approved.csv", "text/csv", width="stretch",
                               disabled=not any(r["decision"] == "Accepted" for r in reviews.values()))
    st.caption("Exports include exact source timestamps, evidence, validation and review notes. Files derived from private scripts must remain private.")


def analyze(settings):
    mode, entered_key, model, max_calls, top, policy = settings
    episodes = st.session_state.episodes
    try:
        if mode == "Gemini AI":
            key = entered_key.strip() or configured_key()
            if not key:
                st.error("Enter a Gemini API key in the sidebar, or choose the timing-only baseline.")
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
            status.update(label="Analysis complete" if result["manifest"]["complete"] else "Partial results saved", state="complete")
        st.rerun()
    except (InputError, ProviderError, ValueError) as exc:
        st.error(str(exc))


def discover(settings):
    st.markdown("## Find the moment that makes them stay.")
    st.caption("Upload timed scripts. Discover strong openings, unresolved endings, and clips a new viewer can follow.")
    with st.container(border=True, key="upload_panel"):
        upload_column, demo_column = st.columns([2, 1], gap="large")
        with upload_column:
            st.markdown("#### Upload your screenplay")
            uploaded = st.file_uploader("TXT, JSON or ZIP of episodes", type=["txt", "json", "zip"], accept_multiple_files=True,
                                        key=f"uploads_{st.session_state.upload_epoch}", help="Timed TXT or normalized JSON. 25 MB total, 200 episodes. No estimated timestamps.")
            files = [(file.name, file.getvalue()) for file in uploaded]
            signature = hashlib.sha256(json.dumps([
                (name, hashlib.sha256(body).hexdigest()) for name, body in files
            ]).encode()).hexdigest() if files else None
            loaded = bool(signature and signature == st.session_state.get("loaded_upload"))
            retry = st.button("Scripts loaded" if loaded else "Load uploaded scripts",
                              disabled=not files or loaded, width="stretch")
            # Process each new selection once. A separate button click is optional;
            # unrelated widget reruns must not erase results or editor decisions.
            if files and (retry or signature != st.session_state.get("attempted_upload")):
                st.session_state.attempted_upload = signature
                try:
                    with st.spinner("Reading screenplay files…"):
                        episodes, ignored = ingest_uploads(files)
                    set_workspace(episodes, label="Uploaded screenplay")
                    st.session_state.update(attempted_upload=signature, loaded_upload=signature,
                        notice=f"Loaded {len(episodes)} episodes · {sum(len(e.beats) for e in episodes):,} timed beats. "
                               f"{len(ignored)} metadata or non-script files skipped.")
                    st.rerun()
                except InputError as exc:
                    st.session_state.upload_error = str(exc)
            if files and st.session_state.get("upload_error"):
                st.error(st.session_state.upload_error)
            elif st.session_state.get("notice"):
                st.success(st.session_state.notice)
            elif not files:
                st.caption("Files load automatically after upload. Then choose Analyze loaded scripts.")
            if st.session_state.episodes and (not files or signature == st.session_state.get("loaded_upload")):
                mode, entered_key, _, _, _, policy = settings
                if mode == "Gemini AI":
                    if not (entered_key.strip() or configured_key()):
                        st.info("Next: open the sidebar using » at the top left and enter your Gemini API key, "
                                "or select Timing-only baseline to test without a key.")
                    st.caption("Analyze sends the loaded scripts to Google’s Gemini API. Use authorized inputs only.")
                if st.button("Analyze loaded scripts" if not st.session_state.result else "Run / resume analysis",
                             type="primary", disabled=policy is None, width="stretch"):
                    analyze(settings)
        with demo_column:
            st.markdown("#### Explore a real AI run")
            st.write("Try two original fictional scripts with recorded Gemini results. No API key needed.")
            if st.button("Load fictional demo", type="primary", width="stretch"):
                load_demo()
                st.rerun()
            with st.expander("Download sample inputs"):
                stream = io.BytesIO()
                with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
                    archive.writestr("airlock.txt", (ROOT / "examples/scifi/airlock.txt").read_bytes())
                    archive.writestr("scoreboard.json", (ROOT / "examples/sports/scoreboard.json").read_bytes())
                st.download_button("Fictional screenplay ZIP", stream.getvalue(), "fictional-screenplays.zip", "application/zip")
                st.caption("The TXT adapter expects explicit episode headers and timed speaker/action blocks. Untimed screenplays need a separate alignment step.")
    if files and signature != st.session_state.get("loaded_upload"):
        st.info("The selected files could not be loaded. Correct the input and upload again, or retry above.")
        return
    episodes = st.session_state.episodes
    if not episodes:
        st.markdown('<div class="empty"><span>01 · UPLOAD</span><span>02 · DISCOVER & CHECK</span><span>03 · REVIEW & EXPORT</span></div>', unsafe_allow_html=True)
        st.info("Kuku’s private screenplay pack is not bundled with this app. Use your own authorized inputs, or the fictional demo.")
        return
    st.caption(st.session_state.workspace_label)
    result = st.session_state.result
    if not result:
        left, right = st.columns(2)
        left.metric("Episodes ready", len(episodes))
        right.metric("Timed beats", sum(len(e.beats) for e in episodes))
        st.info("Ready to analyze. The Episode timeline tab lets you inspect the parsed source first.")
        return
    manifest = result["manifest"]
    if result.get("provenance", {}).get("kind") == "recorded_synthetic_demo":
        st.info("Recorded Gemini output on original fictional scripts. Includes deliberately constructed review challenges, identified in the audit. Loading this demo makes no API calls.")
    if not manifest["complete"]:
        st.warning(f"Partial run: {manifest['episodes_processed']}/{manifest['input_count']} episodes processed. Completed responses are cached within this session; run again to resume.")
        for error in manifest["errors"]:
            st.error(error["error"])
    if manifest["mode"] == "baseline":
        st.warning("Timing-only baseline. These windows are unrated and have not passed an AI quality review.")
    with st.container(key="run_metrics"):
        columns = st.columns(4)
    for column, (label, value) in zip(columns, [("Episodes processed", f"{manifest['episodes_processed']}/{manifest['input_count']}"),
                                               ("Grounded candidates", manifest["grounded_candidates"]),
                                               ("Passed review" if manifest["mode"] != "baseline" else "Unrated windows", manifest["accepted_candidates"]),
                                               ("Shortlisted", len(result["shortlist"]))]):
        column.metric(label, value)
    st.markdown("### Top promo recommendations")
    st.caption(f"{manifest['model']} · {manifest['policy']['min_duration_ms']/1000:g}–{manifest['policy']['max_duration_ms']/1000:g}s · Full opening evidence within {manifest['policy']['hook_window_ms']/1000:g}s")
    if not result["shortlist"]:
        st.info("No candidates met the current checks. Inspect Why excluded, review the source, or revise the rubric for a new run.")
    for candidate in result["shortlist"]:
        card(candidate, manifest["mode"] == "baseline", st.session_state.reviews.get(candidate["id"]))
        with st.expander("Why this clip? · " + candidate["title"]):
            show_evidence(candidate, manifest["weights"], manifest["mode"] == "baseline")
    st.divider()
    download_panel(result)


def review_panel():
    result = st.session_state.result
    st.markdown("## Review with the source in view.")
    st.caption("Compare competing moments, adjust exact cut boundaries, and capture your editorial decision.")
    if not result or not candidate_catalog(result):
        st.info("Load the fictional demo or analyze your scripts to start reviewing.")
        return
    catalog = candidate_catalog(result)
    ids = list(dict.fromkeys([c["id"] for c in result["shortlist"]] + list(catalog)))
    label = lambda cid: f"{catalog[cid]['title']} · {cid[-6:]}"
    cid = st.selectbox("Clip to review", ids, format_func=label, key="candidate_selected")
    candidate = catalog[cid]
    approved = {c["id"] for c in result["accepted_candidates"]}
    if cid not in approved:
        st.warning("This proposal did not pass or complete AI review. An editor can still inspect it and record a separate decision.")
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
    st.markdown("### Editor decision")
    st.caption("Edits must match legal source boundaries and preserve full dialogue. A changed cut loses the original AI score and rationale; your decision is stored separately.")
    with st.form("review_form_" + cid):
        columns = st.columns(2)
        start = columns[0].text_input("Start timestamp", segment["start"], key="edit_start_" + cid)
        end = columns[1].text_input("End timestamp", segment["end"], key="edit_end_" + cid)
        decision = st.radio("Decision", DECISIONS, index=DECISIONS.index(current.get("decision", "Pending")), horizontal=True, key="edit_decision_" + cid)
        reason = st.selectbox("Feedback reason", FEEDBACK_REASONS, index=FEEDBACK_REASONS.index(current.get("feedback_reason", "No issue")), key="edit_reason_" + cid)
        notes = st.text_area("Editor notes", current.get("notes", ""), max_chars=3000, key="edit_notes_" + cid,
                             placeholder="What makes this work—or what did the model miss?")
        save = st.form_submit_button("Save editor review", type="primary")
    if save:
        try:
            st.session_state.reviews[cid] = review_candidate(candidate, st.session_state.episodes, policy, start, end, decision, reason, notes)
            st.success("Review saved to this session. Download the results to keep it.")
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
    st.markdown("## Why wasn’t this clip selected?")
    st.caption("See the recorded reason at each stage. An invalid cut, an AI rejection, and a ranking omission are different outcomes.")
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
    st.markdown("## Every recommendation traces to the source.")
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
    st.set_page_config(page_title="PromoPilot AI", page_icon="🎬", layout="wide", initial_sidebar_state="auto")
    st.markdown("<style>" + (ROOT / "web/style.css").read_text() + "</style>", unsafe_allow_html=True)
    for key, default in {"episodes": [], "result": None, "reviews": {}, "workspace_label": "", "upload_epoch": 0}.items():
        st.session_state.setdefault(key, default)
    settings = controls()
    st.markdown('<header class="brand"><div class="brand-mark">P<span>↗</span></div><div><h1>PromoPilot <em>AI</em></h1><p>Script-to-promo intelligence</p></div><span class="brand-tag">EDITOR WORKSPACE</span></header>', unsafe_allow_html=True)
    tab = st.radio("Workspace", ["Discover", "Review & compare", "Why excluded", "Episode timeline", "Benchmark"], horizontal=True, label_visibility="collapsed", key="page")
    st.divider()
    if tab == "Discover":
        discover(settings)
    elif tab == "Review & compare":
        review_panel()
    elif tab == "Why excluded":
        exclusion_panel()
    elif tab == "Episode timeline":
        timeline_panel()
    else:
        benchmark_panel()


if __name__ == "__main__":
    main()
