"""Portable, offline HTML report plus machine-readable and editor CSV exports."""

import csv
from html import escape
from pathlib import Path

from .parser import format_time


def safe_cell(value):
    text = str(value)
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text


def write_reports(output: Path, manifest, candidates, coverage):
    with (output / "shortlist.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["rank", "candidate_id", "title", "segment_order", "episode", "source_file", "start", "end",
                         "segment_seconds", "total_seconds", "score", "hook", "cliffhanger", "why_it_works", "risks"])
        for candidate in candidates:
            for order, segment in enumerate(candidate["segments"], 1):
                writer.writerow([safe_cell(x) for x in [candidate["rank"], candidate["id"], candidate["title"], order,
                    segment["episode"], segment["source_file"], segment["start"], segment["end"],
                    segment["duration_seconds"], candidate["duration_seconds"], candidate["score"], candidate["hook"],
                    candidate["cliffhanger"], candidate["why_it_works"], " | ".join(candidate["risks"])]])
    with (output / "editor_review.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["candidate_id", "hook_in_3s_0_or_1", "understandable_0_or_1", "open_ending_0_or_1",
                         "accurate_cut_0_or_1", "post_first_0_or_1", "failure_reason", "editor_notes"])
        for c in candidates:
            writer.writerow([c["id"], "", "", "", "", "", "", ""])
    h = lambda value: escape(str(value), quote=True)
    cards = []
    for c in candidates:
        segments_html = []
        for order, seg in enumerate(c["segments"], 1):
            beat_rows = []
            for b in seg['beats']:
                partial = b["start_ms"] < seg["start_ms"] or b["end_ms"] > seg["end_ms"]
                beat_rows.append(f'<tr><td class="mono">{format_time(b["start_ms"])}<br>{format_time(b["end_ms"])}</td>'
                                 f'<td><span class="label">{h(b["label"])}</span>'
                                 f'<p>{h(b["text"])}</p><small>{h(b["id"])} · {("source line " + str(b["source_line"])) if b["source_line"] is not None else "JSON beat " + h(b["id"])}'
                                 f'{" · visual/sound beat only partly included" if partial else ""}</small></td></tr>')
            segments_html.append(f'<div class="segment"><b>{order:02d} / Episode {seg["episode"]}</b>'
                                 f'<strong class="mono">{seg["start"]} → {seg["end"]}</strong>'
                                 f'<span>{seg["duration_seconds"]:g}s</span></div>'
                                 f'<details><summary>Read source beats · {h(seg["source_file"])}</summary>'
                                 f'<table>{"".join(beat_rows)}</table></details>')
        chips = ''.join(f'<span class="chip">{h(k)} <b>{v}/5</b></span>' for k, v in c['scores'].items())
        risks = ''.join(f'<li>{h(r)}</li>' for r in c['risks'])
        critic = f'<p class="note"><b>Independent AI review:</b> {h(c["critic"]["reason"])}</p>' if c.get('critic') else ''
        cards.append(f'''<article data-search="{h((c['title'] + ' ' + c['angle']).lower())}">
          <div class="card-top"><span class="rank">{c['rank']:02d}</span><div><span class="eyebrow">{h(c['angle'].replace('_',' '))} · {c['duration_seconds']:g} seconds</span>
          <h2>{h(c['title'])}</h2></div><div class="score">{c['score']:g}<small>/100</small></div></div>
          {''.join(segments_html)}
          <div class="story"><div><h3>The hook</h3><p>{h(c['hook'])}</p><blockquote>{h(c['hook_source']['text'])}</blockquote></div>
          <div><h3>The open question</h3><p>{h(c['cliffhanger'])}</p><blockquote>{h(c['cliffhanger_source']['text'])}</blockquote></div></div>
          <p><b>Why this cut:</b> {h(c['why_it_works'])}</p><div class="chips">{chips}</div>
          <p class="note">{h(c['rank_reason'])}</p>{critic}
          <details><summary>Quality checks &amp; editor notes</summary><p>{h(c['validation'])}</p><ul>{risks}</ul><p>Watch the rendered footage before posting. Script timing is not verified against video.</p></details>
        </article>''')
    baseline = manifest['mode'] == 'baseline'
    status = 'OFFLINE BASELINE · NO LLM USED' if baseline else f"{manifest['mode'].upper()} · {manifest['model']}"
    if not baseline and manifest.get('critic_model') and manifest['critic_model'] != manifest['model']:
        status += f" · REVIEW: {manifest['critic_model']}"
    caveat = ('These are content-blind timing candidates for testing the pipeline. All scores are zero/unrated. Run a model for narrative analysis.' if baseline else
              'AI-reviewed candidates for an editor. Scores are editorial judgments, not predicted clicks or installs. Test shortlisted cuts with real viewers.')
    cover_rows = ''.join(f'<tr><td>{e["episode"]}</td><td>{h(e["status"])}</td><td>{e["beats"]}</td><td>{e["candidate_count"]}</td></tr>' for e in coverage)
    errors = ''.join(f'<p>{h(e["error"])}</p>' for e in manifest['errors'])
    page = f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
    <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; base-uri 'none'">
    <title>Promo Finder · Shortlist</title><style>
    :root{{--ink:#172c29;--muted:#60716b;--line:#d9e0d8;--accent:#235f4a;--paper:#f5f4ed}}
    *{{box-sizing:border-box}}body{{margin:0;background:var(--paper);color:var(--ink);font:15px/1.55 system-ui,-apple-system,sans-serif}}
    main{{max-width:1040px;margin:auto;padding:36px 24px 70px}}header{{border-bottom:1px solid var(--line);padding-bottom:28px;margin-bottom:24px}}
    .brand{{font-weight:800;letter-spacing:-1px;font-size:27px}}.brand span{{font-weight:400;font-size:14px;letter-spacing:0;color:var(--muted);margin-left:14px}}
    .eyebrow{{font-size:11px;font-weight:750;text-transform:uppercase;letter-spacing:1.6px;color:var(--accent)}}h1{{font-size:clamp(34px,5vw,54px);line-height:1.1;letter-spacing:-2px;max-width:740px;margin:32px 0 14px}}
    .intro{{max-width:720px;color:var(--muted);font-size:17px}}.notice{{background:#fff2cf;border:1px solid #e6d69e;border-radius:10px;padding:14px 18px;margin:22px 0}}
    .metrics{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:25px 0}}.metric{{padding:18px;background:#e9ede5;border-radius:10px}}.metric strong{{display:block;font-size:30px}}.metric span{{color:var(--muted)}}
    nav{{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin:22px 0}}a{{color:var(--accent);text-decoration:underline;text-underline-offset:3px}}nav a{{padding:8px 12px;border:1px solid var(--line);border-radius:7px;text-decoration:none;background:white}}
    input{{font:inherit;padding:10px 12px;background:transparent;border:1px solid #aabbb2;border-radius:7px;flex:1;min-width:180px}}
    article{{background:#fff;border:1px solid var(--line);border-radius:15px;padding:26px;margin:20px 0;box-shadow:0 3px 14px #183b2605}}
    .card-top{{display:flex;gap:16px;align-items:center;margin-bottom:22px}}.rank{{font-size:20px;color:#819b8d;border:1px solid var(--line);padding:8px 12px;border-radius:50%}}h2{{font-size:23px;line-height:1.25;letter-spacing:-.5px;margin:6px 0}}.score{{margin-left:auto;font-size:30px;font-weight:750;white-space:nowrap}}.score small{{font-size:13px;color:var(--muted)}}
    .segment{{display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;background:#edf3ee;padding:12px 16px;border-radius:8px;margin-top:12px}}.mono{{font-family:ui-monospace,monospace}}.story{{display:grid;grid-template-columns:1fr 1fr;gap:28px;margin:16px 0}}h3{{font-size:12px;text-transform:uppercase;letter-spacing:1px;color:var(--accent)}}blockquote{{margin:12px 0;padding-left:13px;border-left:3px solid #b3cbb7;color:#39594d;font-size:14px}}
    .chips{{display:flex;flex-wrap:wrap;gap:8px}}.chip{{font-size:12px;background:#f3f5f1;padding:5px 9px;border-radius:5px}}.chip b{{margin-left:5px}}.note,small{{font-size:12px;color:var(--muted)}}details{{margin:12px 0}}summary{{cursor:pointer;color:var(--accent);font-size:13px;font-weight:650}}table{{border-collapse:collapse;width:100%;font-size:13px}}td,th{{text-align:left;padding:12px 8px;border-bottom:1px solid var(--line);vertical-align:top}}td:first-child{{width:115px}}td p{{margin:7px 0}}.label{{font-weight:700;font-size:11px}}footer{{margin-top:35px;color:var(--muted);font-size:12px}}[hidden]{{display:none!important}}
    @media(max-width:650px){{main{{padding:20px 14px}}article{{padding:18px}}.story{{grid-template-columns:1fr;gap:0}}.metrics{{gap:7px}}.metric{{padding:12px}}h2{{font-size:19px}}.rank{{display:none}}.score{{font-size:24px}}}}
    @media print{{nav{{display:none}}details{{display:none}}article{{break-inside:avoid}}body{{background:white}}}}
    </style></head><body><main><header><div class="brand">Promo Finder<span>Story-to-promo discovery</span></div>
    <h1>Find the moment.<br>Leave them wanting more.</h1><p class="intro">Source-backed cut points from your storytelling content. A hook, a story, and a reason to keep watching.</p></header>
    <div class="notice"><div class="eyebrow">{h(status)}</div>{h(caveat)}{errors}</div>
    <div class="metrics"><div class="metric"><strong>{manifest['episodes_processed']}/{manifest['input_count']}</strong><span>episodes processed</span></div><div class="metric"><strong>{manifest['grounded_candidates']}</strong><span>valid candidate cuts</span></div><div class="metric"><strong>{len(candidates)}</strong><span>shortlisted for review</span></div></div>
    <nav><input id="search" type="search" aria-label="Filter promos" placeholder="Filter by title or story angle…"><a href="shortlist.csv" download>Editor CSV ↗</a><a href="shortlist.json" download>Full JSON ↗</a><a href="editor_review.csv" download>Review sheet ↗</a></nav>
    <div id="cards">{''.join(cards) if cards else '<p>No promos passed the checks. Inspect coverage and rejections; the tool does not force a winner.</p>'}</div><p id="empty" hidden>No matching promos.</p>
    <details><summary>Coverage of every input episode</summary><table><tr><th>Episode</th><th>Status</th><th>Beats</th><th>Candidates</th></tr>{cover_rows}</table></details>
    <footer>Private local report · {h(manifest['created_utc'])} · {h(manifest['prompt_version'])}<br>Contains source material from your input. Keep input material and derived excerpts private unless you have permission to share them.</footer>
    </main><script>document.getElementById('search').addEventListener('input',e=>{{let count=0;document.querySelectorAll('article').forEach(c=>{{c.hidden=!c.dataset.search.includes(e.target.value.toLowerCase());if(!c.hidden)count++;}});document.getElementById('empty').hidden=count!==0;}});</script></body></html>'''
    (output / "report.html").write_text(page, encoding="utf-8")
