import csv
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from promo_finder.config import Policy
from promo_finder.parser import InputError, format_time, load_episodes
from promo_finder.pipeline import run
from promo_finder.providers import JSONProvider, ProviderError
from promo_finder.selection import ValidationError, baseline_candidates
from promo_finder.workspace import (candidate_catalog, exclusion_audit, export_bundle, export_csv,
                                    ingest_uploads, read_run, review_candidate)

ROOT = Path(__file__).resolve().parents[1]
SCIFI = ROOT / "examples/scifi/airlock.txt"
SPORTS = ROOT / "examples/sports/scoreboard.json"


def archive(entries):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as zipped:
        for name, body in entries:
            zipped.writestr(name, body)
    return stream.getvalue()


class UploadTests(unittest.TestCase):
    def test_mixed_nested_zip_parses_every_episode_and_reports_ignored_files(self):
        body = archive([("show/airlock.txt", SCIFI.read_bytes()), ("show/sports.json", SPORTS.read_bytes()),
                        ("__MACOSX/._airlock.txt", b"metadata"), ("readme.md", b"ignored")])
        episodes, ignored = ingest_uploads([("my-show.zip", body)])
        self.assertEqual(len(episodes), 2)
        self.assertEqual(sum(len(e.beats) for e in episodes), 22)
        self.assertEqual(len(ignored), 2)

    def test_mixed_direct_files_match_disk_parser(self):
        episodes, _ = ingest_uploads([("renamed.txt", SCIFI.read_bytes()), ("foreign.json", SPORTS.read_bytes())])
        self.assertEqual([e.sha256 for e in episodes], [e.sha256 for e in load_episodes(SCIFI) + load_episodes(SPORTS)])

    def test_duplicate_episode_fails_across_archives(self):
        with self.assertRaisesRegex(InputError, "Duplicate episode"):
            ingest_uploads([("one.zip", archive([("first.txt", SCIFI.read_bytes())])), ("second.txt", SCIFI.read_bytes())])

    def test_traversal_absolute_and_windows_paths_are_rejected(self):
        for name in ("../outside.txt", "/outside.txt", "c:\\outside.txt", "folder/../../outside.txt"):
            with self.subTest(name=name), self.assertRaises(InputError):
                ingest_uploads([("show.zip", archive([(name, SCIFI.read_bytes())]))])

    def test_symlinks_are_rejected_without_extraction(self):
        info = zipfile.ZipInfo("shortcut.txt")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        with self.assertRaisesRegex(InputError, "Symlink"):
            ingest_uploads([("show.zip", archive([(info, b"/etc/passwd")]))])

    def test_compression_bomb_and_corrupt_archive_rejected(self):
        with self.assertRaisesRegex(InputError, "compression"):
            ingest_uploads([("large.zip", archive([("huge.txt", b"a" * 1_000_000)]))])
        with self.assertRaisesRegex(InputError, "Unreadable"):
            ingest_uploads([("invalid.zip", b"not a zip")])

    def test_untimed_non_utf8_and_empty_inputs_fail(self):
        for files in ([], [("test.txt", b"Untimed script")], [("test.txt", b"\xff")], [("test.pdf", b"text")],
                      [("empty.zip", archive([("image.png", b"image")]))]):
            with self.subTest(files=[name for name, _ in files]), self.assertRaises(InputError):
                ingest_uploads(files)


class EditorTests(unittest.TestCase):
    def setUp(self):
        self.episodes = load_episodes(SCIFI)
        self.policy = Policy()
        self.candidate = baseline_candidates(self.episodes[0])[0]
        self.candidate.update(score=88.0, scores={k: 4 for k in self.policy.weights}, critic={"verdict": "keep"})
        self.original = deepcopy(self.candidate)

    def review(self, start=None, end=None, decision="Accepted", reason="No issue", notes=""):
        segment = self.candidate["segments"][0]
        return review_candidate(self.candidate, self.episodes, self.policy, start or segment["start"], end or segment["end"], decision, reason, notes)

    def test_unchanged_review_preserves_original_and_separate_decision(self):
        review = self.review()
        self.assertFalse(review["modified"])
        self.assertEqual(review["effective_candidate"]["score"], 88)
        self.assertEqual(self.candidate, self.original)

    def test_modified_cut_invalidates_score_and_model_approval(self):
        episode = self.episodes[0]
        segment = self.candidate["segments"][0]
        ends = episode.legal_continuous_cuts()[segment["start_beat"]]
        different = next(b for b in episode.beats if b.id in ends and b.end_ms != segment["end_ms"])
        review = self.review(end=format_time(different.end_ms))
        effective = review["effective_candidate"]
        self.assertTrue(review["modified"])
        self.assertIsNone(effective["score"])
        self.assertIsNone(effective["scores"])
        self.assertNotIn("critic", effective)
        self.assertNotEqual(effective["id"], self.candidate["id"])
        self.assertEqual(self.candidate, self.original)

    def test_mid_speech_or_non_boundary_edit_is_rejected(self):
        with self.assertRaisesRegex(ValidationError, "legal beat boundary"):
            self.review(start="00:00.100")

    def test_source_mismatch_rejected(self):
        self.candidate["segments"][0]["source_sha256"] = "wrong"
        with self.assertRaisesRegex(ValidationError, "fingerprint"):
            self.review()

    def test_rejection_requires_explanation(self):
        with self.assertRaisesRegex(ValidationError, "reason or note"):
            self.review(decision="Rejected")
        self.assertEqual(self.review(decision="Rejected", notes="The ending resolves everything.")["decision"], "Rejected")


class AuditExportTests(unittest.TestCase):
    def setUp(self):
        with tempfile.TemporaryDirectory() as folder:
            run(load_episodes(SCIFI), Path(folder), top=1, progress=lambda _: None)
            self.result = read_run(folder)

    def test_shortlist_omission_is_not_called_ai_rejection(self):
        rows = exclusion_audit(self.result)
        self.assertTrue(rows)
        self.assertEqual(rows[0]["stage"], "Shortlist selection")

    def test_partial_critic_failure_is_pending_not_rejection(self):
        self.result["accepted_candidates"] = []
        self.result["shortlist"] = []
        rows = exclusion_audit(self.result)
        self.assertEqual({r["stage"] for r in rows}, {"Review incomplete"})

    def test_rejection_does_not_need_a_valid_candidate(self):
        self.result["rejections"] = [{"stage": "grounding", "candidate": None, "reason": "Malformed proposal"}]
        row = exclusion_audit(self.result)[0]
        self.assertEqual(row["stage"], "Source / timing validation")
        self.assertEqual(row["reason"], "Malformed proposal")

    def test_csv_is_formula_safe_and_accepted_export_is_explicit(self):
        candidate = self.result["shortlist"][0]
        candidate["title"] = '=HYPERLINK("malicious")'
        seg = candidate["segments"][0]
        review = review_candidate(candidate, load_episodes(SCIFI), Policy(), seg["start"], seg["end"], "Accepted", "No issue", "@formula")
        rows = list(csv.DictReader(io.StringIO(export_csv(self.result, {candidate["id"]: review}, accepted_only=True).decode("utf-8-sig"))))
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["title"].startswith("'="))
        self.assertTrue(rows[0]["editor_notes"].startswith("'@"))
        self.assertEqual(rows[0]["ai_score_effective"], "")
        self.assertFalse(list(csv.DictReader(io.StringIO(export_csv(self.result, {}, accepted_only=True).decode("utf-8-sig")))))

    def test_json_export_preserves_grounding_rejections_and_editor_decisions(self):
        bundle = export_bundle(self.result, {})
        self.assertEqual(bundle["shortlist"], self.result["shortlist"])
        self.assertEqual(bundle["grounded_candidates"], self.result["grounded_candidates"])
        self.assertIn("selection_audit", bundle)


class CredentialTests(unittest.TestCase):
    def test_session_key_does_not_mutate_environment(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"GEMINI_API_KEY": "server-key"}):
            provider = JSONProvider("gemini", "test-model", Path(directory), api_key="visitor-key")
            self.assertEqual(provider.key, "visitor-key")
            self.assertEqual(os.environ["GEMINI_API_KEY"], "server-key")
            with self.assertRaises(ProviderError):
                JSONProvider("gemini", "test-model", Path(directory), api_key="")


class RecordedDemoTests(unittest.TestCase):
    def test_demo_coverage_and_all_cuts_match_shipped_fiction(self):
        from promo_finder.selection import validate_candidate
        result = json.loads((ROOT / "examples/demo/recorded_run.json").read_text())
        episodes = load_episodes(SCIFI) + load_episodes(SPORTS)
        policy = Policy(**result["manifest"]["policy"])
        self.assertTrue(result["provenance"]["synthetic"])
        for candidate in result["grounded_candidates"]:
            validated = validate_candidate(candidate, episodes, policy)
            self.assertEqual(validated["id"], candidate["id"])
        for entry in result["coverage"]:
            count = sum(any(s["episode"] == entry["episode"] for s in c["segments"]) for c in result["accepted_candidates"])
            self.assertEqual(entry["candidate_count"], count)
        self.assertEqual(result["manifest"]["accepted_candidates"], len(result["accepted_candidates"]))


if __name__ == "__main__":
    unittest.main()
