# Evaluation, with the benchmark kept private

## Primary development benchmark

Kuku supplied a 50-episode pack with 3,727 timed beats. The reusable benchmark harness takes this pack by an explicit path and loads expectations from `benchmarks/kuku.json`. Under its configured duration and opening rules, the pack contains 21,421 legal boundary pairs; all pass the full grounding validator.

The pack informed format support and general editorial criteria. Therefore it is a development benchmark, not a blind holdout. No source-specific selection rule, plot vocabulary, preferred episode, or target timestamp is embedded in the engine.

Earlier prototype runs processed the full pack and produced aggregate results of 21 proposals / 4 grounded / 2 accepted, then 30 / 22 / 17, then 30 / 22 / 8 after stricter review. These are historical development observations, not the generalized version's results and not controlled comparisons. The prompts, candidates and reviewer changed. Earlier outputs remain private and unmodified.

The generalized v3 benchmark writes a new `benchmark_summary.json` with coverage, candidate counts, policy version and models. Check its `complete` field. A stronger-model review can stop on a daily quota; a partial run must never be presented as a complete evaluation. A separately labeled run may use a different available free-tier model. It does not silently change providers or reuse old judgments under a new prompt version.

The completed generalized run processed all 50 episodes with Flash-Lite for discovery and review: 26 proposals, 18 grounded candidates, 16 accepted and 10 shortlisted. A separate run using the stronger reviewer completed 8 episodes before a daily free-tier limit; it is not a complete benchmark. These counts describe model decisions, not independently measured quality. The compact public record is `benchmarks/kuku-results.json`; all per-episode material remains private.

## Generalization checks

The public suite requires no Kuku files or API key. It covers two original genres and two input formats, arbitrary episode labels, Unicode speaker/text parsing, character/content substitutions, timestamp translations, alternate timing policies, source-scoped identities, and explicit rejection of malformed input. The engine contains no literal reference beat IDs or cut timecodes.

A content-blind structural baseline helps test the mechanics without a hosted model. Unlike the historical lexical baseline, it does not score plot words or characters. Its quality scores are deliberately zero/unrated.

Passing these checks establishes source independence of the code. It does not prove that an LLM interprets every genre or language well. Public examples are synthetic engineering fixtures, not statistically representative audience samples. Human editorial precision, recall and ad performance are unmeasured.

The engine's original 48 checks have been extended to 74 tests for the web product. The public suite runs with the private pack and API key absent; install `requirements.txt` to include the real Streamlit AppTest checks. The portable application runs independently from an empty working directory. Release tests exercise unlisted private files, renamed input folders, raw outputs, credentials, Streamlit secrets and symlink injection. A local audit compares substantial source passages against all allowlisted public files; this supplements the explicit file inventory and manual review.

The portable CLI was also run through live Flash-Lite discovery and review on both original examples: science-fiction TXT and sports JSON. Each processed its input and returned one source-valid candidate, using the same default policy with no story-specific changes. These are small transfer demonstrations, not evidence of broad editorial accuracy. A public recorded-demo JSON combines these original fictional runs with three deliberately constructed legal-cut challenges evaluated in one real Gemini critic request. Two challenges were rejected for resolved endings; one passed review and was excluded by footage overlap. Counts and provenance are visible in the app. This fixture is for workflow demonstration, not a held-out quality metric; private Kuku results are never included.

## Web product verification

Automated checks cover nested ZIP ingestion, duplicate episodes, malformed files, archive traversal/symlinks and resource limits; editor source matching and exact cut validation; score invalidation on edits; formula-safe CSV exports; separate rejection/omission/pending states; credential isolation; and the application's upload-ready, recorded-demo, comparison, review, timeline, benchmark and reset states. Recorded cuts are revalidated against the shipped fictional scripts, including coverage counts.

A local integration check loaded Kuku's actual pack through the new ZIP importer: all 50 episodes and 3,727 beats matched the existing completed benchmark's source fingerprints. All five app views rendered against those private results, including the 10-clip shortlist and 16 rejection/omission records. This reused the completed Gemini run; it did not re-analyze the pack or publish source content. Browser verification separately exercised a fictional upload, offline analysis, a valid timestamp edit with a saved rejection, and removal of uploaded filenames after workspace reset.

## Known failure classes

Models can propose illegal boundaries despite a supplied legal-cut map; code rejects them. Model critics can overrate passive openings, misidentify resolved endings as cliffhangers, infer excluded context, or favor intensity over advertising suitability. Whole-beat constraints and a limited number of proposals can miss good moments. A stronger reviewer helps filter candidates but cannot recover unproposed ones.

Keep model outputs unedited, inspect the rejected set as well as the shortlist, and preserve all input hashes and settings. Narrative claims remain hypotheses even when every timing check passes.

## Human evaluation and creative testing

Have reviewers independently assess the included opening window, standalone clarity, unresolved ending, exact footage boundaries, and suitability for the intended audience/placement. Hide model labels when comparing candidates. Measure approval rate at the requested shortlist size, report the denominator when fewer candidates are returned, and record disagreements and failure categories.

To estimate recall, reviewers must inspect sampled complete episodes and identify missed moments. Rating only model outputs measures precision, not recall. Reserve genuinely unseen shows for later tests; freeze the rubric before evaluating them.

Only controlled ad experiments can establish acquisition performance. Compare hold rate, completion, click-through, installs, cost per install and downstream engagement with matched audiences and placements. Choose a primary outcome in advance and account for uncertainty. No current score is a predicted probability of clicks or installs.
