# Private benchmark, reusable public product

Kuku's supplied pack is private. Do not publicly share the screenplay files, assignment PDF, source excerpts, prompts containing them, model responses, CSV/HTML/JSON reports, screenshots or caches derived from the pack. Keep authorized submission material in a private channel.

The product does not embed the private benchmark, automatically locate it, or load private saved selections as exemplars. The Kuku benchmark spec contains only a public-facing label, expected aggregate counts and configurable evaluation settings; it has no file path or source content. The user explicitly supplies the private input location. The web benchmark tab reads aggregate results only. A separate public demo deliberately loads recorded outputs from original fictional examples; these never enter prompts for a new upload.

All generated outputs are private by default, including the structural baseline. A Git ignore allowlist excludes unknown files and folders rather than relying on the pack's current name. The release builder uses a second, explicit file allowlist. It does not glob the workspace into an archive. It rejects private/output paths, symlinks and recognizable credential strings. The separate distribution inventory lists every included file and its hash.

Public materials may include implementation, abstract editorial criteria, aggregate metrics, and independently authored synthetic examples. Aggregate benchmark metrics contain no quotes, names, per-episode cuts, source filenames or recovered story events. Inspect the generated release inventory before sharing. Building a ZIP is a local operation and does not publish it.

Gemini inference sends scripts and selected clips to Google when that provider is explicitly chosen. This is separate from public publishing. An API key and provider choice are not an assurance of local-only processing. Check applicable data-use terms; free-tier provider terms may permit product improvement. The local Ollama option keeps model calls on loopback. Never put credentials in source, reports, examples, screenshots or chat.

The application does not upload videos, publish ads, deploy a website, create a repository or submit an assignment. API responses and local reports are untrusted text and are escaped when rendered in HTML/CSV.

The Streamlit app keeps user inputs/results in browser-session state and uses a unique temporary server directory for resumable model requests. It has no shared upload cache or database. **Clear workspace** deletes its temporary directory and clears the inputs, results, review notes and entered API key. Disconnect cleanup follows the hosting runtime's session lifetime; clearing does not delete already downloaded exports. A hosting operator can access server storage/memory, so confidential evaluation should use an appropriately restricted environment.

The public release includes `.streamlit/config.toml` for theme/settings but rejects `.streamlit/secrets.toml`. Do not add a server API key to a public demo unless you intend visitors to use its quota; bring-your-own-key mode and the recorded fictional demo work without one. Review edits and exports preserve the original model record separately. Editor-modified cuts carry no transferred AI score or approval.
