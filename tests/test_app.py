"""Behavioral tests of the real Streamlit application, with no network calls."""

import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

try:
    from streamlit.testing.v1 import AppTest
except ImportError:
    AppTest = None

from promo_finder.parser import load_episodes
from promo_finder.providers import ProviderError

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipIf(AppTest is None, "Install requirements.txt to run web application tests")
class AppTests(unittest.TestCase):
    def setUp(self):
        self.network_guard = patch("urllib.request.urlopen", side_effect=AssertionError("Unexpected network request"))
        self.network_guard.start()
        self.addCleanup(self.network_guard.stop)
        self.app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=15).run()
        self.assertFalse(self.app.exception)

    def button(self, label):
        return next(button for button in self.app.button if button.label == label)

    def demo(self):
        self.button("Try a demo").click().run()
        self.assertFalse(self.app.exception)

    def test_empty_start_and_recorded_demo(self):
        self.assertIsNone(self.app.session_state["result"])
        self.demo()
        result = self.app.session_state["result"]
        self.assertTrue(result["provenance"]["synthetic"])
        self.assertEqual(len(self.app.session_state["episodes"]), 2)
        self.assertTrue(result["shortlist"])

    def test_upload_waits_for_load_click_and_preserves_results_on_rerun(self):
        source = (ROOT / "examples/scifi/airlock.txt").read_bytes()
        with patch("streamlit.file_uploader", return_value=[SimpleNamespace(name="new.txt", getvalue=lambda: source)]):
            self.app.run()
            self.assertEqual(self.app.session_state["episodes"], [])
            self.assertFalse(self.button("Load scripts").disabled)
            self.assertNotIn("Find promos", [b.label for b in self.app.button])
            self.button("Load scripts").click().run()
            self.assertFalse(self.app.exception)
            self.assertEqual(len(self.app.session_state["episodes"]), 1)
            self.assertTrue(self.button("Scripts loaded").disabled)
            self.assertTrue(any("Loaded 1 episode" in item.value for item in self.app.success))
            self.app.radio(key="mode").set_value("Timing-only baseline").run()
            self.button("Find promos").click().run()
            result = self.app.session_state["result"]
            self.app.run()
            self.assertFalse(self.app.exception)
            self.assertEqual(self.app.session_state["result"], result)

    def test_import_error_survives_rerun_and_new_valid_upload_recovers(self):
        with patch("streamlit.file_uploader", return_value=[SimpleNamespace(name="broken.zip", getvalue=lambda: b"bad zip")]):
            self.app.run()
            self.button("Load scripts").click().run()
            self.assertFalse(self.app.exception)
            self.assertTrue(self.app.error)
            self.app.slider(key="top_count").set_value(5).run()
            self.assertTrue(self.app.error)
            self.assertEqual(self.app.session_state["episodes"], [])
        source = (ROOT / "examples/scifi/airlock.txt").read_bytes()
        with patch("streamlit.file_uploader", return_value=[SimpleNamespace(name="valid.txt", getvalue=lambda: source)]):
            self.app.run()
            self.assertFalse(self.app.error)
            self.assertEqual(self.app.session_state["episodes"], [])
            self.button("Load scripts").click().run()
            self.assertFalse(self.app.exception)
            self.assertFalse(self.app.error)
            self.assertEqual(len(self.app.session_state["episodes"]), 1)

    def test_new_upload_does_not_replace_workspace_until_loaded(self):
        self.demo()
        previous = self.app.session_state["result"]
        source = (ROOT / "examples/scifi/airlock.txt").read_bytes()
        with patch("streamlit.file_uploader", return_value=[SimpleNamespace(name="replacement.txt", getvalue=lambda: source)]):
            self.app.run()
            self.assertEqual(self.app.session_state["result"], previous)
            self.assertFalse(self.button("Load scripts").disabled)
            self.assertNotIn("Continue analysis", [b.label for b in self.app.button])
            self.button("Load scripts").click().run()
            self.assertFalse(self.app.exception)
            self.assertIsNone(self.app.session_state["result"])
            self.assertEqual(len(self.app.session_state["episodes"]), 1)

    def test_shared_quota_failure_switches_to_visitor_key_and_keeps_cache(self):
        used_keys = []
        def quota(provider, *args, **kwargs):
            used_keys.append(provider.key)
            raise ProviderError("Quota exhausted", credential_issue=True)
        with patch.dict(os.environ, {"GEMINI_API_KEY": "shared-test-key"}):
            self.app.session_state["episodes"] = load_episodes(ROOT / "examples/scifi/airlock.txt")
            self.app.run()
            with patch("promo_finder.providers.JSONProvider.generate", quota):
                self.button("Find promos").click().run()
            self.assertFalse(self.app.exception)
            self.assertEqual(used_keys, ["shared-test-key"])
            self.assertTrue(self.app.session_state["shared_key_unavailable"])
            directory = self.app.session_state["job_directory"].name
            self.app.text_input(key="visitor_key").set_value("visitor-test-key").run()
            def success(provider, *args, **kwargs):
                used_keys.append(provider.key)
                return {"candidates": []}
            with patch("promo_finder.providers.JSONProvider.generate", success):
                self.button("Continue analysis").click().run()
            self.assertFalse(self.app.exception)
            self.assertEqual(used_keys[-1], "visitor-test-key")
            self.assertEqual(directory, self.app.session_state["job_directory"].name)
            self.assertEqual(os.environ["GEMINI_API_KEY"], "shared-test-key")
            self.assertTrue(self.app.session_state["result"]["manifest"]["complete"])
            self.button("Retry shared access").click().run()
            self.assertFalse(self.app.session_state.get("shared_key_unavailable", False))
            self.assertEqual(self.app.session_state["visitor_key"], "")

    def test_request_budget_does_not_disable_shared_key(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "shared-test-key"}):
            self.app.session_state["episodes"] = load_episodes(ROOT / "examples/scifi/airlock.txt")
            self.app.run()
            with patch("promo_finder.providers.JSONProvider.generate", side_effect=ProviderError("Request budget reached")):
                self.button("Find promos").click().run()
            self.assertFalse(self.app.exception)
            self.assertFalse(self.app.session_state.get("shared_key_unavailable", False))

    def test_review_compare_and_exported_decision(self):
        self.demo()
        self.app.radio(key="page").set_value("Review & download").run()
        self.app.checkbox(key="compare_enabled").check().run()
        self.assertFalse(self.app.exception)
        cid = self.app.session_state["candidate_selected"]
        self.app.radio(key="edit_decision_" + cid).set_value("Accepted")
        self.app.text_area(key="edit_notes_" + cid).set_value("Verified against source.")
        self.button("Save decision").click().run()
        self.assertFalse(self.app.exception)
        self.assertEqual(self.app.session_state["reviews"][cid]["decision"], "Accepted")

    def test_invalid_timestamp_does_not_save_review(self):
        self.demo()
        self.app.radio(key="page").set_value("Review & download").run()
        cid = self.app.session_state["candidate_selected"]
        self.app.text_input(key="edit_start_" + cid).set_value("00:00.111")
        self.button("Save decision").click().run()
        self.assertFalse(self.app.exception)
        self.assertTrue(self.app.error)
        self.assertNotIn(cid, self.app.session_state["reviews"])

    def test_all_product_views_render(self):
        self.demo()
        for tool in ("Skipped clips", "Script timeline", "Benchmark", "Sample files"):
            with self.subTest(tool=tool):
                self.app.selectbox(key="more_tool").set_value(tool).run()
                self.assertFalse(self.app.exception)

    def test_card_opens_selected_promo_in_review(self):
        self.demo()
        cid = self.app.session_state["result"]["shortlist"][0]["id"]
        self.app.button(key="review_open_" + cid).click().run()
        self.assertFalse(self.app.exception)
        self.assertEqual(self.app.radio(key="page").value, "Review & download")
        self.assertEqual(self.app.session_state["candidate_selected"], cid)
        self.assertTrue(self.button("Save decision"))

    def test_simple_layout_has_no_sidebar_and_only_two_main_views(self):
        self.assertEqual(self.app.radio(key="page").options, ["Find promos", "Review & download"])
        self.assertEqual(len(self.app.sidebar.children), 0)
        self.demo()
        self.app.button(key="review_open_" + self.app.session_state["result"]["shortlist"][0]["id"]).click().run()
        self.button("Start over").click().run()
        self.assertFalse(self.app.exception)
        self.assertEqual(self.app.radio(key="page").value, "Find promos")
        self.assertEqual(self.app.session_state["episodes"], [])

    def test_new_session_does_not_inherit_uploaded_data(self):
        self.demo()
        other = AppTest.from_file(str(ROOT / "app.py"), default_timeout=15).run()
        self.assertIsNone(other.session_state["result"])
        self.assertEqual(other.session_state["episodes"], [])

    def test_clear_removes_user_data_key_and_temporary_files(self):
        self.demo()
        temp = tempfile.TemporaryDirectory(prefix="promopilot-test-")
        self.app.session_state["job_directory"] = temp
        epoch = self.app.session_state["upload_epoch"]
        self.app.text_input(key="visitor_key").set_value("visitor-value").run()
        self.button("Start over").click().run()
        self.assertFalse(self.app.exception)
        self.assertFalse(Path(temp.name).exists())
        self.assertEqual(self.app.session_state["episodes"], [])
        self.assertEqual(self.app.session_state["visitor_key"], "")
        self.assertGreater(self.app.session_state["upload_epoch"], epoch)

    def test_uploaded_source_runs_real_baseline_in_session(self):
        self.app.session_state["episodes"] = load_episodes(ROOT / "examples/scifi/airlock.txt")
        self.app.radio(key="mode").set_value("Timing-only baseline").run()
        self.button("Find promos").click().run()
        self.assertFalse(self.app.exception)
        self.assertEqual(self.app.session_state["result"]["manifest"]["mode"], "baseline")
        self.assertTrue(self.app.session_state["result"]["manifest"]["complete"])
        self.button("Start over").click().run()


if __name__ == "__main__":
    unittest.main()
