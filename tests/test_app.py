"""Behavioral tests of the real Streamlit application, with no network calls."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

try:
    from streamlit.testing.v1 import AppTest
except ImportError:
    AppTest = None

from promo_finder.parser import load_episodes

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
        self.button("Load fictional demo").click().run()
        self.assertFalse(self.app.exception)

    def test_empty_start_and_recorded_demo(self):
        self.assertIsNone(self.app.session_state["result"])
        self.demo()
        result = self.app.session_state["result"]
        self.assertTrue(result["provenance"]["synthetic"])
        self.assertEqual(len(self.app.session_state["episodes"]), 2)
        self.assertTrue(result["shortlist"])

    def test_review_compare_and_exported_decision(self):
        self.demo()
        self.app.radio(key="page").set_value("Review & compare").run()
        self.app.checkbox(key="compare_enabled").check().run()
        self.assertFalse(self.app.exception)
        cid = self.app.session_state["candidate_selected"]
        self.app.radio(key="edit_decision_" + cid).set_value("Accepted")
        self.app.text_area(key="edit_notes_" + cid).set_value("Verified against source.")
        self.button("Save editor review").click().run()
        self.assertFalse(self.app.exception)
        self.assertEqual(self.app.session_state["reviews"][cid]["decision"], "Accepted")

    def test_invalid_timestamp_does_not_save_review(self):
        self.demo()
        self.app.radio(key="page").set_value("Review & compare").run()
        cid = self.app.session_state["candidate_selected"]
        self.app.text_input(key="edit_start_" + cid).set_value("00:00.111")
        self.button("Save editor review").click().run()
        self.assertFalse(self.app.exception)
        self.assertTrue(self.app.error)
        self.assertNotIn(cid, self.app.session_state["reviews"])

    def test_all_product_views_render(self):
        self.demo()
        for page in ("Why excluded", "Episode timeline", "Benchmark", "Discover"):
            with self.subTest(page=page):
                self.app.radio(key="page").set_value(page).run()
                self.assertFalse(self.app.exception)

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
        self.button("Clear workspace").click().run()
        self.assertFalse(self.app.exception)
        self.assertFalse(Path(temp.name).exists())
        self.assertEqual(self.app.session_state["episodes"], [])
        self.assertEqual(self.app.session_state["visitor_key"], "")
        self.assertGreater(self.app.session_state["upload_epoch"], epoch)

    def test_uploaded_source_runs_real_baseline_in_session(self):
        self.app.session_state["episodes"] = load_episodes(ROOT / "examples/scifi/airlock.txt")
        self.app.radio(key="mode").set_value("Timing-only baseline").run()
        self.button("Analyze loaded scripts").click().run()
        self.assertFalse(self.app.exception)
        self.assertEqual(self.app.session_state["result"]["manifest"]["mode"], "baseline")
        self.assertTrue(self.app.session_state["result"]["manifest"]["complete"])
        self.button("Clear workspace").click().run()


if __name__ == "__main__":
    unittest.main()
