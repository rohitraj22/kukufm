"""Parse screenplay beats without asking an LLM to invent timestamps."""

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
import hashlib
import json
import re

from .config import Policy


class InputError(ValueError):
    pass


def parse_time(value: str) -> int:
    """MM:SS.dd or HH:MM:SS.dd -> integer milliseconds, never frame numbers."""
    if not re.fullmatch(r"\d{1,3}:\d{2}(?::\d{2})?(?:[.,]\d{1,3})?", value):
        raise InputError(f"Invalid timestamp: {value!r}")
    parts = value.replace(",", ".").split(":")
    seconds = Decimal(parts[-1])
    minutes = int(parts[-2])
    if seconds >= 60 or (len(parts) == 3 and minutes >= 60):
        raise InputError(f"Invalid timestamp: {value!r}")
    return int((seconds + minutes * 60 + (int(parts[0]) * 3600 if len(parts) == 3 else 0)) * 1000)


def format_time(ms: int) -> str:
    minutes, remaining = divmod(ms, 60_000)
    seconds, millis = divmod(remaining, 1000)
    return f"{minutes:02d}:{seconds:02d}.{millis:03d}"


@dataclass(frozen=True)
class Beat:
    id: str
    episode: int
    label: str
    kind: str
    start_ms: int
    end_ms: int
    text: str
    source_line: int | None


@dataclass
class Episode:
    number: int
    filename: str
    sha256: str
    logline: str
    beats: list[Beat]

    @property
    def duration_ms(self):
        return max(b.end_ms for b in self.beats)

    def prompt_data(self):
        return {"episode": self.number, "logline_for_editor_only": self.logline,
                "beats": [{"id": b.id, "label": b.label, "kind": b.kind,
                           "start_seconds": b.start_ms / 1000, "end_seconds": b.end_ms / 1000, "text": b.text}
                          for b in self.beats]}

    def legal_continuous_cuts(self, policy=None):
        """Permitted opening -> ending IDs; every pair obeys the run's policy.

        Full short dialogue/action openings are eligible. No keywords determine
        narrative quality here; the model chooses among these legal boundaries.
        """
        policy = policy or Policy()
        speech = [b for b in self.beats if b.kind == "DIALOGUE"]
        starts = [b for b in self.beats if b.kind in {"DIALOGUE", "ACTION"}
                  and b.end_ms - b.start_ms <= policy.hook_window_ms
                  and not any(d.start_ms < b.start_ms < d.end_ms for d in speech)]
        ends = [b for b in self.beats if b.kind != "SOUND FX"
                and not any(d.start_ms < b.end_ms < d.end_ms for d in speech)]
        return {s.id: [e.id for e in ends if policy.min_duration_ms <= e.end_ms - s.start_ms <= policy.max_duration_ms
                       and e.start_ms >= s.start_ms]
                for s in starts
                if any(policy.min_duration_ms <= e.end_ms - s.start_ms <= policy.max_duration_ms and e.start_ms >= s.start_ms for e in ends)}


BLOCK = re.compile(
    r"^\*\*(?P<label>[^\n]+?)\*\*\s*\n"
    r"Start:\s*\((?P<start>[^)]+)\)\s*\|\s*End:\s*\((?P<end>[^)]+)\)\s*\n"
    r"(?P<text>.*?)(?=^\*\*|^SCENE\s+\d+\s*$|^--- SCRIPT END ---|\Z)",
    re.MULTILINE | re.DOTALL,
)


def parse_script(text: str, filename: str = "script.txt") -> Episode:
    text = text.lstrip("\ufeff").replace("\r\n", "\n")
    number = re.search(r"^EPISODE NUMBER:\s*(?:Episode\s*)?(\d+)\s*$", text, re.M | re.I)
    if not number:
        raise InputError(f"{filename}: expected 'EPISODE NUMBER: Episode N'.")
    episode = int(number.group(1))
    logline = re.search(r"^EPISODE LOGLINE:\s*(.*)$", text, re.M)
    matches = list(BLOCK.finditer(text))
    headers = len(re.findall(r"^\*\*[^\n]+\*\*\s*$", text, re.M))
    timestamps = len(re.findall(r"^Start:", text, re.M))
    if not matches or len(matches) != headers or len(matches) != timestamps:
        raise InputError(f"{filename}: found {len(matches)} valid beats, {headers} headers, "
                         f"{timestamps} timings. Unsupported/missing timings; no estimates made.")
    beats = []
    for index, match in enumerate(matches, 1):
        label = match['label'].rstrip(":").strip()
        kind = label if label in {"ACTION", "CAMERA", "SOUND FX"} else "DIALOGUE"
        start, end = parse_time(match['start']), parse_time(match['end'])
        content = match['text'].strip()
        if start >= end or not content:
            raise InputError(f"{filename}: empty/reversed beat {index}.")
        beats.append(Beat(f"E{episode:03d}-B{index:03d}", episode, label, kind,
                          start, end, content, text.count("\n", 0, match.start()) + 1))
    return Episode(episode, filename, hashlib.sha256(text.encode()).hexdigest(),
                   logline.group(1) if logline else "", beats)


def parse_json_episodes(text: str, filename="screenplay.json") -> list[Episode]:
    """Normalize other transcript formats through a small, documented JSON contract."""
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise InputError(f"{filename}: invalid JSON.") from exc
    if not isinstance(payload, dict) or set(payload) != {"episodes"} or not isinstance(payload["episodes"], list) or not payload["episodes"]:
        raise InputError(f"{filename}: expected a nonempty episodes array.")
    episodes = []
    for item in payload["episodes"]:
        if not isinstance(item, dict) or set(item) - {"episode", "logline", "beats"}:
            raise InputError(f"{filename}: invalid episode fields.")
        number, rows = item.get("episode"), item.get("beats")
        if type(number) is not int or number < 0 or not isinstance(rows, list) or not rows:
            raise InputError(f"{filename}: episode must be a nonnegative integer with nonempty beats.")
        logline = item.get("logline", "")
        if not isinstance(logline, str):
            raise InputError(f"{filename}: logline must be text.")
        beats = []
        for index, row in enumerate(rows, 1):
            fields = {"kind", "label", "start_ms", "end_ms", "text"}
            if not isinstance(row, dict) or set(row) != fields:
                raise InputError(f"{filename}: beat {index} must contain {sorted(fields)}.")
            kind, start, end = row["kind"], row["start_ms"], row["end_ms"]
            if not isinstance(kind, str) or kind not in {"DIALOGUE", "ACTION", "CAMERA", "SOUND FX"}:
                raise InputError(f"{filename}: invalid kind in beat {index}.")
            if type(start) is not int or type(end) is not int or not 0 <= start < end:
                raise InputError(f"{filename}: beat times must be integer milliseconds with 0 <= start < end.")
            if any(not isinstance(row[k], str) or not row[k].strip() for k in ("label", "text")):
                raise InputError(f"{filename}: beat label/text cannot be empty.")
            beats.append(Beat(f"E{number:03d}-B{index:03d}", number, row["label"], kind, start, end,
                              row["text"], None))
        fingerprint = hashlib.sha256(json.dumps(item, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        episodes.append(Episode(number, filename, fingerprint, logline, beats))
    return episodes


def load_episodes(source: Path) -> list[Episode]:
    paths = sorted(p for p in source.iterdir() if p.suffix.lower() in {".txt", ".json"}) if source.is_dir() else [source]
    if not paths:
        raise InputError("No .txt or .json screenplays found.")
    episodes = []
    for path in paths:
        text = path.read_text(encoding="utf-8-sig")
        if path.suffix.lower() == ".txt":
            episodes.append(parse_script(text, path.name))
        elif path.suffix.lower() == ".json":
            episodes.extend(parse_json_episodes(text, path.name))
        else:
            raise InputError("Supported input formats are .txt and .json.")
    numbers = [e.number for e in episodes]
    if len(numbers) != len(set(numbers)):
        raise InputError("Duplicate episode numbers. Process one show at a time.")
    return sorted(episodes, key=lambda e: e.number)
