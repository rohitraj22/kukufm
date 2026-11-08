# PromoPilot AI · powered by Promo Finder

A reusable Python engine and Streamlit editor workspace that turn timed story scripts into ranked promo candidates with exact cut points, original source evidence, a hook, an unresolved ending, and editorial reasons.

**The engine is independent of any show.** Names, episode identifiers, story events and timecodes enter through input files only. It contains no preferred character, episode, plot vocabulary or reference clip. Duration, audience, language and scoring settings are configurable. Kuku's private 50-episode pack is the primary development benchmark, separated from the product and public examples.

**Verified generalized run:** 50/50 episodes, 26 proposals, 18 source/timing-valid candidates, 16 model-approved candidates and a 10-item shortlist, using Flash-Lite for both stages. A separate stronger-reviewer run stopped on its daily free quota and is labeled partial. [Aggregate results](benchmarks/kuku-results.json) contain no screenplay content. Automated checks cover the engine, generalization, uploads, editor behavior, the real Streamlit app, and release privacy.

## Open the web product

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m streamlit run app.py --server.address 127.0.0.1
```

The studio workspace supports:

- Timed TXT / normalized JSON / ZIP upload, episode timelines, and legal cut boundaries.
- Live Gemini discovery and review, configurable duration/weights/audience, and a clearly unrated offline baseline.
- Ranked recommendations with source evidence, score contributions and diversity explanations.
- Side-by-side comparison, validated timestamp edits, accept/reject decisions and failure notes. Changed cuts are explicitly **not AI-rescored**.
- A stage-by-stage exclusion audit, including actual reviewer rejections, overlapping alternatives and unfinished reviews.
- JSON results, CSV shortlists with review decisions, and a CSV containing only editor-accepted cuts.

**Try a demo** needs no key. It replays real Gemini results on two independently authored examples, including explicitly labeled, deliberately constructed review challenges. It makes no API calls and contains no Kuku content. The benchmark page shows only aggregate private-run metrics. Session-owned uploads, results, notes and temporary caches clear with **Start over**; export reviews before leaving.

The package is prepared for **free Streamlit Community Cloud hosting**. See [deployment instructions](docs/HOSTING.md) and the [three-minute walkthrough](docs/DEMO_WALKTHROUGH.md). Nothing is deployed by these setup commands. Publish only the clean allowlisted source ZIP, never the original workspace.

## Run the CLI

Python 3.10+. No runtime dependencies or installation are required:

```sh
# Original, public science-fiction example; no API call
python3 -m promo_finder --input examples/scifi --provider baseline

# An unrelated sports story using the normalized JSON format
python3 -m promo_finder --input examples/sports --provider baseline

# Inspect your own input and request estimate without sending it anywhere
python3 -m promo_finder --input /path/to/your/scripts --dry-run
```

The command prints the report's location. The offline baseline chooses legal time windows without reading story words; all quality scores are zero/unrated. It verifies the pipeline, not narrative quality.

For AI analysis, place your key in a private `.env` file using `.env.example` as the format:

```sh
python3 -m promo_finder --input /path/to/your/scripts \
  --config config/default.json --provider gemini \
  --model gemini-3.1-flash-lite --critic-model gemini-3.5-flash
```

`--critic-model` is optional. When omitted, the discovery model also reviews the candidates. Models are easy to swap; Ollama is supported with `--provider ollama --model YOUR_INSTALLED_MODEL --batch-size 1 --min-interval 0`. No models are automatically downloaded. Local model performance has not been benchmarked.

A key on a free-tier project is required for zero API spend; the tool cannot determine its billing tier. Check current model availability, pricing and your project's quota. Requests stop explicitly on a quota failure; there is no automatic model or paid-provider switch. The two stages share a request budget, with sequential calls and cached exact responses. CLI flags include `--max-calls`, `--min-interval`, `--batch-size`, `--top`, and `--output`.

Default output folders are scoped by input hashes, policy and models. Use an explicit output folder to resume the same run. Reports, prompts, raw responses and caches contain source-derived material and stay private by default. Changing a prompt/model creates a different cache key. Completed responses can be reused after a quota interruption.

## Inputs and settings

- **Timed TXT:** the documented screenplay layout, with episode metadata and timed speaker/action/camera/sound blocks. Labels and names are arbitrary; episode numbers need not be sequential.
- **Normalized JSON:** a small adapter contract for other transcript sources. See [input format](docs/INPUT_FORMAT.md). Preserve source timing and text when converting.
- **Web ZIP upload:** nested folders of TXT/JSON, with ZIP traversal/symlink/size checks. The CLI accepts TXT/JSON files or a flat folder directly.
- Process one show per run. Duplicate episode numbers, missing timings, unsupported blocks and malformed records fail explicitly; timestamps are never estimated.
- Language and audience are policy inputs, not global assumptions. Parsing preserves Unicode; narrative quality outside the demonstrated languages/genres remains unmeasured.

Edit a copy of [the default policy](config/default.json) for clip lengths, opening/ending windows, minimum review scores, candidates per episode, audience, language and weights. The default 20–60 second duration and three-second hook are product constraints, not timestamps from a screenplay.

## How it works

1. Parse input into source-linked timed beats. Camera/dialogue overlaps are preserved.
2. Enumerate legal opening/ending pairs using timing and complete speech boundaries only.
3. Ask the model to choose continuous clips from those pairs, based on general editorial criteria.
4. Validate every returned ID and cut against the current source. Clip identities include the source fingerprint to prevent collisions across shows.
5. Review selected material without the logline, proposal reasoning or proposal scores. Opening and ending windows are explicit. The model must identify immediate interest and a concrete unresolved question from included evidence.
6. Retry episodes with no approved clip once, using feedback and different cuts; apply the same independent review.
7. Rank accepted clips using configurable weights and suppress repeated footage. The tool can return fewer than requested, including none.

The runtime has no benchmark import, story keyword list, fixed reference timestamps, or retrieval of previous Kuku outputs. Scripts are untrusted reference data, never executable instructions.

## Results

Every run saves a local HTML report, `shortlist.csv`, `shortlist.json`, complete episode coverage, a manifest with settings/model versions, raw responses, accepted/rejected candidates and an editor review sheet. CSV contains one row per segment. Exact source evidence remains available for inspection. The compatibility command `python3 -m kuku_promos` delegates to the same generic engine.

## Benchmark and generalization

[The Kuku benchmark spec](benchmarks/kuku.json) contains only dataset expectations and editorial settings. It supplies no source content, target clips, character names, or file location. Obtain the pack through its authorized private route and pass its local path explicitly:

```sh
python3 -m promo_finder.benchmark --spec benchmarks/kuku.json \
  --input "$KUKU_PACK_DIR" --output output/benchmarks/kuku-local \
  --provider gemini --model gemini-3.1-flash-lite
```

The harness checks input coverage, validates every legal pair, and runs the same product engine used for arbitrary inputs. Omit the provider/model flags for a no-network structural baseline. Raw benchmark outputs remain private. See [evaluation](docs/EVALUATION.md) for aggregate evidence and limits.

Kuku is a **development benchmark**, since it informed format support and general evaluation criteria. It is not an unseen test set. Public tests use only original science-fiction and sports examples, including renamed/renumbered/time-shifted variants. Such tests establish code independence; they do not establish universal LLM quality or conversion lift.

## Local distribution and privacy

```sh
.venv/bin/python -m unittest discover -s tests -v
python3 tools/build_release.py
```

The release builder creates a source ZIP and a portable `dist/promo-finder.pyz` from the explicit [public-file allowlist](release_files.json). The ZIP contains the web app, engine, docs, configuration, tests, original synthetic examples and their recorded demo results. Private scripts, the assignment PDF, keys, real-data outputs and caches are excluded. The builder rejects symlinks, private paths and recognizable credentials. Nothing is uploaded or published. Web tests require `requirements.txt`; without Streamlit they are skipped explicitly.

You can run the portable CLI with `python3 dist/promo-finder.pyz --input /path/to/your/scripts --provider baseline`. Alternatively, install the clean source distribution with `python3 -m pip install .` and use `promo-finder` / `promo-benchmark`.

Do not upload the entire working directory. Read [privacy and release rules](docs/PRIVACY.md). Hosted AI processes the supplied scripts with the selected provider; use the local adapter if external processing is not authorized.

## Limits

Continuous clips only; no automatic montage assembly, video editing, music, captions, app publishing, transcription or untimed input support. Very large individual episodes exceed the bounded prompt size and need a timestamp-preserving adapter. Whole-beat and short opening constraints can miss good clips. Model reviews can be wrong even with exact timestamps. Validate rendered footage and measure editor acceptance before creative testing; scores are not click/install probabilities.

Provider references: [Gemini pricing](https://ai.google.dev/gemini-api/docs/pricing), [quotas](https://ai.google.dev/gemini-api/docs/rate-limits), [structured output](https://ai.google.dev/gemini-api/docs/structured-output), [Ollama API](https://docs.ollama.com/api/generate).

### When the first search finds no approved promo

For episodes with no approved clip, the engine makes at most one additional discovery attempt using rejection feedback, then independently reviews the new cuts under the same rules. Previously proposed cuts cannot be approved by repetition. Episodes with approved clips are not retried. A batch uses up to four model calls (excluding transport retries), within the same hard request budget. An interrupted second attempt remains incomplete and resumes from cached responses. Both attempts and rejection reasons are preserved in raw outputs. An empty shortlist shows the reasons directly and offers a JSON download. This does not guarantee a promo for every input; subtitle-only inputs may lack necessary scene context. Existing recorded demos and benchmark metrics describe the earlier pipeline version and have not been rerun by this change.
