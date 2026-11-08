# Free hosting: Streamlit Community Cloud

The web entrypoint is `app.py`. The tested runtime is Python 3.12 and Streamlit 1.67.0, pinned in `requirements.txt`. The CLI remains standard-library-only. This is a deployment-ready source package; building or running it does not publish a site.

## Run locally

From the extracted project directory:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m streamlit run app.py --server.address 127.0.0.1
```

Open the printed localhost URL. Click **Try a demo** for a working example without a key. Use Gemini access to enter a key when needed. Settings contains clip length, number of promos, and advanced options such as the timing-only baseline. Your local `.env` key is supported without showing it in the interface.

## Prepare a public deployment

1. Run `python3 tools/build_release.py`. Extract **only** `dist/promo-finder-source.zip` into a new clean directory. Never upload the original working directory.
2. Check `RELEASE_CONTENTS.json`. The allowlisted package contains code, documentation, settings, original fictional inputs, their recorded AI results, and aggregate benchmark metrics. It excludes Kuku's pack, private outputs and keys.
3. Put that clean directory in a GitHub repository. In [Streamlit Community Cloud](https://share.streamlit.io), create an app, select the repository/branch, and set the entrypoint to `app.py`.
4. In Advanced settings, choose **Python 3.12**. `requirements.txt` installs the pinned Streamlit version; `.streamlit/config.toml` supplies the theme and upload limits.
5. For a showcase without shared quota, leave server secrets unset. The fictional demo works immediately, and live analysis uses a visitor's own Gemini key. An entered key stays in session memory and is never written to a release, cache or environment variable.
6. If intentionally offering a shared server key to your visitors, set `GEMINI_API_KEY = "your-key"` in Community Cloud's Secrets settings. Do not commit `.env` or `.streamlit/secrets.toml`. The app uses the shared key by default. On quota or credential rejection, it asks the visitor for their own key and resumes with session-cached responses. A visitor key takes priority and stays in session memory. Retry shared access is available when quota resets; request-budget limits and transient failures do not trigger the key switch. Visitors can consume the shared key's quota; the app has a per-click request cap, not account-wide authentication or rate limiting. Use a Google project without paid billing if zero spend is required.

Community Cloud provides free app hosting; model inference has separate account-dependent limits. The application sends requests only to the explicitly selected Gemini model, and never silently switches to a paid provider. A request budget limits request count, not a dollar charge. See [Gemini pricing](https://ai.google.dev/gemini-api/docs/pricing) and your AI Studio account's quota/billing settings.

## Private data and session lifetime

Uploads are parsed in memory. ZIPs are inspected without filesystem extraction. Network runs use a unique temporary directory for that browser session's raw responses and resumable cache; there is no cross-session cache or global upload registry. **Start over** deletes that directory and clears loaded inputs, results, notes, and entered keys. Streamlit may retain a disconnected session briefly; process cleanup also removes temporary directories. Downloads already saved by a user are outside this cleanup.

On any hosted server, the hosting operator can access server memory and temporary files. Do not upload confidential benchmark material to a public demo without the appropriate authorization. Gemini mode also sends scripts to Google under the account's applicable API terms. The no-network baseline validates timing only and makes no semantic-quality claims. Use the CLI's local Ollama adapter when external AI processing is inappropriate.

The public demo **does not load local Kuku files**, expose filesystem-path inputs, or preload private benchmark selections. Its benchmark tab reads aggregate counts only. Private evaluation results should be delivered through Kuku's authorized submission channel.

## Verify before sharing

```sh
.venv/bin/python -m unittest discover -s tests -v
```

Load the fictional demo; inspect ranking and rejection reasons; compare two clips; accept or reject one; edit a timestamp to an exact legal boundary; download JSON/CSV; then clear the workspace. Test your own authorized upload and verify it is absent in a second browser session. The app displays partial coverage on quota failure and allows retry with session-owned cached responses. Free hosts may sleep or restart; download reviews before closing the session.

Official references, checked November 2026: [free Community Cloud](https://docs.streamlit.io/deploy/streamlit-community-cloud), [deployment and Python version](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/deploy), [dependencies](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/app-dependencies), [secrets](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/secrets-management).
