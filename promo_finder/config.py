"""Product rules independent of any show, episode, character, or source file."""

from dataclasses import asdict, dataclass, field
import json
from pathlib import Path

DIMENSIONS = ("hook", "cliffhanger", "standalone", "emotion", "escalation")


@dataclass(frozen=True)
class Policy:
    min_duration_ms: int = 20_000
    max_duration_ms: int = 60_000
    hook_window_ms: int = 3_000
    ending_window_ms: int = 5_000
    min_quality_score: int = 3
    candidates_per_episode: int = 2
    language: str = "Use the input's language; preserve source text verbatim."
    audience: str = "New viewers unfamiliar with the story."
    weights: dict = field(default_factory=lambda: dict(zip(DIMENSIONS, (30, 25, 20, 15, 10))))

    def __post_init__(self):
        for name in ("min_duration_ms", "max_duration_ms", "hook_window_ms", "ending_window_ms",
                     "min_quality_score", "candidates_per_episode"):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError(f"{name} must be a positive integer.")
        if self.min_duration_ms > self.max_duration_ms:
            raise ValueError("Minimum duration exceeds maximum duration.")
        if max(self.hook_window_ms, self.ending_window_ms) > self.min_duration_ms:
            raise ValueError("Opening/ending windows must fit within the minimum clip duration.")
        if self.min_quality_score > 5 or self.candidates_per_episode > 4:
            raise ValueError("Quality threshold must be 1-5; candidates_per_episode must be 1-4.")
        if not isinstance(self.weights, dict) or set(self.weights) != set(DIMENSIONS):
            raise ValueError("Weights must contain exactly the five quality dimensions.")
        if any(type(v) is not int or v < 0 for v in self.weights.values()) or sum(self.weights.values()) != 100:
            raise ValueError("Weights must be nonnegative integers totaling 100.")
        for name in ("language", "audience"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or len(value) > 500:
                raise ValueError(f"{name} must be nonempty text of at most 500 characters.")

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_file(cls, path: Path):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Policy must be a JSON object.")
        unknown = set(payload) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError("Unknown policy fields: " + ", ".join(sorted(unknown)))
        return cls(**payload)
