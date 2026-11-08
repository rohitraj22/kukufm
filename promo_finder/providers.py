"""Swappable JSON model adapters, with bounded calls and resumable local caching."""

import hashlib
import json
import os
from pathlib import Path
import re
import time
from urllib import error, request

from .prompts import SYSTEM, VERSION


class ProviderError(RuntimeError):
    def __init__(self, message, *, credential_issue=False):
        super().__init__(message)
        self.credential_issue = credential_issue


def gemini_wire_schema(value):
    """Avoid Gemini's schema-complexity rejection; enforce bounds in local validators.

    The live 3.1 Flash-Lite API rejected our nested bounded schema with HTTP 400.
    The identical structure without numeric/array bounds succeeds. Required fields,
    types, enums and closed objects remain in the wire contract.
    """
    if isinstance(value, dict):
        return {key: gemini_wire_schema(item) for key, item in value.items()
                if key not in {"minimum", "maximum", "minItems", "maxItems"}}
    if isinstance(value, list):
        return [gemini_wire_schema(item) for item in value]
    return value


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    temp.replace(path)


def load_env(path=Path(".env")):
    """Read literal KEY=value pairs. Never execute a shell or expand secrets."""
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                key = key.strip()
                if key in {"GEMINI_API_KEY", "GEMINI_MODEL"}:
                    os.environ.setdefault(key, value.strip().strip('"\''))


class JSONProvider:
    def __init__(self, name, model, cache: Path, max_calls=40, min_interval=12.0, budget=None, api_key=None):
        if name not in {"gemini", "ollama"}:
            raise ProviderError("Unsupported provider.")
        if not model or not re.fullmatch(r"[A-Za-z0-9_.:/-]+", model):
            raise ProviderError("Invalid model name.")
        self.name, self.model, self.cache = name, model, cache
        self.max_calls, self.min_interval = max_calls, min_interval
        self.calls, self.cache_hits, self.last_call = 0, 0, 0.0
        self.budget = budget if budget is not None else {"max_calls": max_calls, "used": 0, "last_call": 0.0}
        # Web callers pass session-owned credentials explicitly. Never mutate
        # process-wide environment variables with a visitor's key.
        self.key = os.environ.get("GEMINI_API_KEY", "") if api_key is None else api_key
        if name == "gemini" and not self.key:
            raise ProviderError("GEMINI_API_KEY is missing. Put it in local .env (see .env.example), "
                                "or use --provider baseline. Never paste the key into a report.")

    def generate(self, prompt, schema, stage):
        wire_schema = gemini_wire_schema(schema) if self.name == "gemini" else schema
        spec = {"provider": self.name, "model": self.model, "version": VERSION,
                "system": SYSTEM, "prompt": prompt, "schema": schema, "stage": stage,
                "wire_schema": wire_schema,
                "temperature": .2, "max_output_tokens": 16384}
        digest = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
        stem = self.cache / digest
        result_path = stem.with_suffix(".result.json")
        if result_path.exists():
            self.cache_hits += 1
            return json.loads(result_path.read_text(encoding="utf-8"))
        write_json(stem.with_suffix(".request.json"), spec)
        if self.name == "gemini":
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
            headers = {"Content-Type": "application/json", "x-goog-api-key": self.key}
            body = {"systemInstruction": {"parts": [{"text": SYSTEM}]},
                    "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": .2, "maxOutputTokens": 16384,
                                         "responseMimeType": "application/json", "responseJsonSchema": wire_schema}}
        else:
            url = "http://127.0.0.1:11434/api/generate"
            headers = {"Content-Type": "application/json"}
            body = {"model": self.model, "system": SYSTEM, "prompt": prompt,
                    "format": schema, "stream": False,
                    "options": {"temperature": .2, "num_predict": 16384, "num_ctx": 32768}}
        for attempt in range(3):
            if self.budget["used"] >= self.budget["max_calls"]:
                raise ProviderError("Request budget reached. Completed responses are cached; rerun to resume.")
            wait = max(0, self.min_interval - (time.monotonic() - self.budget["last_call"]))
            if wait:
                time.sleep(wait)
            self.calls += 1
            self.last_call = time.monotonic()
            self.budget["used"] += 1
            self.budget["last_call"] = self.last_call
            try:
                req = request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
                with request.urlopen(req, timeout=180) as response:
                    raw_text = response.read().decode("utf-8")
                # Preserve the unedited wire response, including usage and finish reason.
                stem.with_suffix(".response.txt").write_text(raw_text, encoding="utf-8")
                wire = json.loads(raw_text)
                if self.name == "gemini":
                    choices = wire.get("candidates") or []
                    if not choices or choices[0].get("finishReason") != "STOP":
                        reason = choices[0].get("finishReason", "unknown") if choices else "blocked/empty"
                        raise ProviderError(f"Model response incomplete ({reason}); saved raw response, no invented fallback.")
                    content = "".join(p.get("text", "") for p in choices[0].get("content", {}).get("parts", []) if not p.get("thought"))
                else:
                    if not wire.get("done") or wire.get("done_reason") == "length":
                        raise ProviderError("Local model response incomplete; increase its output/context limits.")
                    content = wire.get("response", "")
                result = json.loads(content)
                if not isinstance(result, dict):
                    raise ProviderError("Expected a JSON object; raw response saved.")
                write_json(result_path, result)
                return result
            except error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                exc.close()
                try:
                    message = json.loads(detail).get("error", {}).get("message", "")
                except (ValueError, AttributeError):
                    message = ""
                if not isinstance(message, str):
                    message = ""
                if self.key:
                    message = message.replace(self.key, "[REDACTED]")
                message = re.sub(r"AIza[A-Za-z0-9_-]+", "[REDACTED]", message)[:2000]
                write_json(stem.with_suffix(".error.json"), {"http_status": exc.code, "message": message})
                daily = bool(re.search(r"per.?day|daily|limit:\s*0", detail, re.I))
                if exc.code in {429, 500, 502, 503, 504} and attempt < 2 and not daily:
                    retry_after = exc.headers.get("Retry-After", "") if exc.headers else ""
                    delay = float(retry_after) if retry_after.isdigit() else 5 * (2 ** attempt)
                    time.sleep(min(30, max(1, delay)))
                    continue
                raise ProviderError(f"{self.name} HTTP {exc.code}. " +
                                    ("Quota unavailable/exhausted: check AI Studio limits and resume later. " if exc.code == 429 else
                                     "Check API key, model availability, and provider status. ") +
                                    "Completed work is cached; no paid-provider fallback." +
                                    (f" API detail: {message}" if message else ""),
                                    credential_issue=(exc.code in {401, 403, 429} or
                                        (exc.code == 400 and bool(re.search(r"API_KEY_INVALID|API key not valid|API key expired", detail, re.I))))) from None
            except (error.URLError, TimeoutError, OSError):
                raise ProviderError(f"Cannot reach {self.name}. Check connection/local server. Completed work is cached.") from None
            except (json.JSONDecodeError, KeyError, TypeError, AttributeError):
                raise ProviderError("Malformed model response. Inspect saved raw response; no candidate was silently repaired.") from None
        raise ProviderError("Retries exhausted.")
