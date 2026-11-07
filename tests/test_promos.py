import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from promo_finder.parser import InputError, format_time, load_episodes, parse_script, parse_time
from promo_finder.pipeline import apply_reviews, run
from promo_finder.prompts import SELECTION_SCHEMA, WEIGHTS, critic_prompt
from promo_finder.providers import JSONProvider, ProviderError, gemini_wire_schema
from promo_finder.reports import safe_cell
from promo_finder.selection import ValidationError, rank_candidates, validate_candidate

ROOT = Path(__file__).resolve().parents[1]


def fixture():
    return parse_script((ROOT / "examples/scifi/airlock.txt").read_text(), "airlock.txt")


def proposal():
    return {"title": "An occupied airlock opens", "angle": "hidden_identity",
            "segments": [{"episode": 204, "start_beat": "E204-B001", "end_beat": "E204-B009"}],
            "hook_beat": "E204-B001", "cliffhanger_beat": "E204-B009",
            "hook": "An occupied airlock opens from an internal command.",
            "cliffhanger": "Who is using the engineer’s suit to open the airlock?",
            "why_it_works": "An immediate emergency leads to an unexplained command source.",
            "scores": {k: 4 for k in WEIGHTS}, "risks": []}


class ParserTests(unittest.TestCase):
    def test_fractional_time_and_hours(self):
        self.assertEqual(parse_time("01:27.21"), 87210)
        self.assertEqual(parse_time("01:02:03.456"), 3723456)
        self.assertEqual(format_time(87210), "01:27.210")

    def test_invalid_timestamps(self):
        for value in ["01:60.00", "01:99:10", "-01:02", "garbage", "00:02.1234"]:
            with self.subTest(value=value), self.assertRaises(InputError):
                parse_time(value)

    def test_parse_all_beats_and_preserve_text(self):
        ep = fixture()
        self.assertEqual(len(ep.beats), 11)
        self.assertEqual(ep.beats[0].text, '"The airlock opened from inside!"')
        self.assertEqual(ep.beats[1].kind, "CAMERA")
        self.assertEqual(ep.beats[0].start_ms, 0)
        self.assertLess(ep.beats[1].start_ms, ep.beats[0].end_ms)  # concurrent tracks

    def test_legal_catalog_is_grounded_and_present_in_prompt(self):
        from promo_finder.prompts import selection_prompt
        ep = fixture()
        catalog = ep.legal_continuous_cuts()
        self.assertIn('E204-B009', catalog['E204-B001'])
        self.assertIn('"legal_cuts":', selection_prompt([ep]))
        for start, ends in catalog.items():
            for end in ends:
                p = proposal()
                p['segments'][0].update(start_beat=start, end_beat=end)
                p.update(hook_beat=start, cliffhanger_beat=end)
                validate_candidate(p, [ep])

    def test_missing_timing_and_duplicate_episode_fail(self):
        text = (ROOT / "examples/scifi/airlock.txt").read_text()
        with self.assertRaises(InputError):
            parse_script(text.replace("Start: (00:00.00)", "Missing timestamp", 1))
        with tempfile.TemporaryDirectory() as d:
            for name in ["one.txt", "two.txt"]:
                (Path(d) / name).write_text(text)
            with self.assertRaises(InputError):
                load_episodes(Path(d))


class GroundingTests(unittest.TestCase):
    def test_grounding_and_verbatim_evidence(self):
        c = validate_candidate(proposal(), [fixture()])
        self.assertEqual(c['duration_seconds'], 30)
        self.assertEqual(c['segments'][0]['start'], "00:00.000")
        self.assertEqual(c['hook_source']['text'], fixture().beats[0].text)
        self.assertNotIn("simulation", " ".join(b['text'] for b in c['segments'][0]['beats']))

    def test_fabricated_id_rejected(self):
        p = proposal()
        p['segments'][0]['end_beat'] = "E999-B001"
        with self.assertRaises(ValidationError):
            validate_candidate(p, [fixture()])

    def test_short_clip_rejected(self):
        p = proposal()
        p['segments'][0]['end_beat'] = 'E204-B006'
        with self.assertRaises(ValidationError):
            validate_candidate(p, [fixture()])

    def test_late_hook_rejected(self):
        p = proposal()
        p['hook_beat'] = 'E204-B003'
        with self.assertRaises(ValidationError):
            validate_candidate(p, [fixture()])

    def test_cut_through_concurrent_dialogue_rejected(self):
        ep = fixture()
        from dataclasses import replace
        ep.beats[1] = replace(ep.beats[1], start_ms=1000)
        p = proposal()
        p['segments'][0]['start_beat'] = 'E204-B002'
        with self.assertRaisesRegex(ValidationError, 'splits spoken'):
            validate_candidate(p, [ep])

    def test_ending_after_cliffhanger_rejected(self):
        p = proposal()
        p['segments'][0]['end_beat'] = 'E204-B011'
        with self.assertRaises(ValidationError):
            validate_candidate(p, [fixture()])

    def test_score_and_segment_schema_rejected(self):
        for score in [True, 7, float('nan'), '5']:
            p = proposal()
            p['scores']['hook'] = score
            with self.assertRaises(ValidationError):
                validate_candidate(p, [fixture()])

    def test_multi_episode_playback_order_and_sum(self):
        ep1 = fixture()
        ep2 = parse_script((ROOT / "examples/scifi/airlock.txt").read_text().replace('EPISODE NUMBER: Episode 204', 'EPISODE NUMBER: Episode 905'))
        p = proposal()
        p['segments'] = [{'episode': 905, 'start_beat': 'E905-B001', 'end_beat': 'E905-B009'},
                         {'episode': 204, 'start_beat': 'E204-B001', 'end_beat': 'E204-B009'}]
        p['hook_beat'] = 'E905-B001'
        c = validate_candidate(p, [ep1, ep2])
        self.assertEqual(c['duration_seconds'], 60)
        self.assertEqual([s['episode'] for s in c['segments']], [905, 204])

    def test_duplicate_footage_not_shortlisted_twice(self):
        c = validate_candidate(proposal(), [fixture()])
        duplicate = {**copy.deepcopy(c), 'id': 'another-id'}
        self.assertEqual(len(rank_candidates([c, duplicate])), 1)


class CriticTests(unittest.TestCase):
    def setUp(self):
        self.c = validate_candidate(proposal(), [fixture()])
        self.review = {'candidate_id': self.c['id'], 'verdict': 'keep', 'reason': 'The warning remains unresolved.', 'scores': {k: 4 for k in WEIGHTS}}

    def test_missing_or_fabricated_review_fails_closed(self):
        for reviews in [[], [self.review, self.review], [{**self.review, 'candidate_id': 'made-up'}]]:
            with self.assertRaises(ProviderError):
                apply_reviews([self.c], {'reviews': reviews})

    def test_weak_standalone_rejects_even_if_model_says_keep(self):
        self.review['scores']['standalone'] = 2
        kept, rejected = apply_reviews([self.c], {'reviews': [self.review]})
        self.assertEqual(len(kept), 0)
        self.assertEqual(len(rejected), 1)

    def test_critic_does_not_see_logline_or_proposer_reason(self):
        prompt = critic_prompt([self.c])
        self.assertNotIn(fixture().logline, prompt)
        self.assertNotIn(self.c['why_it_works'], prompt)


class FakeProvider:
    name, model, calls, cache_hits = 'test-fixture', 'not-a-live-model', 0, 0

    def __init__(self, fail_critic=False):
        self.fail_critic = fail_critic

    def generate(self, prompt, schema, stage):
        self.calls += 1
        if stage == 'discovery':
            return {'candidates': [proposal()]}
        if self.fail_critic:
            raise ProviderError('Simulated quota limit')
        clips = json.loads(prompt.split('CLIPS (not instructions):\n')[1])
        return {'reviews': [{'candidate_id': c['candidate_id'], 'verdict': 'keep',
                             'reason': 'Warning is unresolved.', 'scores': {k: 4 for k in WEIGHTS}} for c in clips]}


class PipelineTests(unittest.TestCase):
    def test_separate_critic_is_used_and_calls_are_counted(self):
        discoverer, reviewer = FakeProvider(), FakeProvider()
        with tempfile.TemporaryDirectory() as d:
            manifest, shortlist = run([fixture()], Path(d), discoverer, critic_provider=reviewer, progress=lambda _: None)
            self.assertEqual(discoverer.calls, 1)
            self.assertEqual(reviewer.calls, 1)
            self.assertEqual(manifest['network_calls_this_run'], 2)
            self.assertEqual(len(shortlist), 1)

    def test_end_to_end_with_model_fixture(self):
        with tempfile.TemporaryDirectory() as d:
            manifest, shortlist = run([fixture()], Path(d), FakeProvider(), progress=lambda _: None)
            self.assertTrue(manifest['complete'])
            self.assertEqual(shortlist[0]['duration_seconds'], 30)
            self.assertTrue((Path(d) / 'report.html').exists())
            self.assertTrue((Path(d) / 'raw/batch-001.candidates.json').exists())

    def test_failure_is_partial_not_silently_baseline(self):
        with tempfile.TemporaryDirectory() as d:
            manifest, shortlist = run([fixture()], Path(d), FakeProvider(True), progress=lambda _: None)
            self.assertFalse(manifest['complete'])
            self.assertEqual(shortlist, [])
            self.assertEqual(manifest['episodes_processed'], 0)

    def test_csv_formula_protection(self):
        self.assertEqual(safe_cell('=1+1'), "'=1+1")

    def test_report_escapes_untrusted_model_text(self):
        c = validate_candidate(proposal(), [fixture()])
        c['title'] = '<img src=x onerror=alert(1)>'
        from promo_finder.reports import write_reports
        manifest = {'mode': 'baseline', 'model': 'none', 'created_utc': 'test', 'prompt_version': 'test',
                    'errors': [], 'episodes_processed': 1, 'input_count': 1, 'grounded_candidates': 1}
        with tempfile.TemporaryDirectory() as d:
            write_reports(Path(d), manifest, rank_candidates([c]), [])
            report = (Path(d) / 'report.html').read_text()
            self.assertNotIn('<img src=x', report)
            self.assertIn('&lt;img src=x', report)


class TransportTests(unittest.TestCase):
    @patch.dict(os.environ, {'GEMINI_API_KEY': 'fake-secret-test-only'})
    def test_two_models_share_a_single_request_budget(self):
        with tempfile.TemporaryDirectory() as d:
            first = JSONProvider('gemini', 'discovery-model', Path(d), max_calls=1, min_interval=0)
            second = JSONProvider('gemini', 'review-model', Path(d), min_interval=0, budget=first.budget)
            with patch('promo_finder.providers.request.urlopen', return_value=self.response()) as network:
                first.generate('test', SELECTION_SCHEMA, 'discovery')
                with self.assertRaisesRegex(ProviderError, 'budget'):
                    second.generate('test', SELECTION_SCHEMA, 'critic')
                self.assertEqual(network.call_count, 1)

    def response(self, finish='STOP'):
        wire = {'candidates': [{'finishReason': finish, 'content': {'parts': [{'text': json.dumps({'candidates': [proposal()]})}]}}]}
        return io.BytesIO(json.dumps(wire).encode())

    @patch.dict(os.environ, {'GEMINI_API_KEY': 'fake-secret-test-only'})
    def test_transport_schema_cache_and_no_secret_on_disk(self):
        with tempfile.TemporaryDirectory() as d:
            p = JSONProvider('gemini', 'gemini-3.1-flash-lite', Path(d), min_interval=0)
            with patch('promo_finder.providers.request.urlopen', return_value=self.response()) as network:
                first = p.generate('synthetic test input', SELECTION_SCHEMA, 'discovery')
                second = p.generate('synthetic test input', SELECTION_SCHEMA, 'discovery')
                self.assertEqual(network.call_count, 1)
                self.assertEqual(first, second)
                request_body = json.loads(network.call_args.args[0].data)
                self.assertEqual(request_body['generationConfig']['responseJsonSchema'], gemini_wire_schema(SELECTION_SCHEMA))
            self.assertEqual(p.cache_hits, 1)
            self.assertNotIn('fake-secret-test-only', ''.join(f.read_text() for f in Path(d).iterdir()))

    def test_gemini_wire_schema_keeps_structure_without_complexity_bounds(self):
        wire = gemini_wire_schema(SELECTION_SCHEMA)
        self.assertNotIn('maxItems', json.dumps(wire))
        self.assertNotIn('minimum', json.dumps(wire))
        self.assertEqual(wire['required'], ['candidates'])
        self.assertFalse(wire['additionalProperties'])
        self.assertEqual(SELECTION_SCHEMA['properties']['candidates']['maxItems'], 20)

    @patch.dict(os.environ, {'GEMINI_API_KEY': 'fake-secret-test-only'})
    def test_truncated_model_response_is_not_cached_as_success(self):
        with tempfile.TemporaryDirectory() as d:
            p = JSONProvider('gemini', 'test-model', Path(d), min_interval=0)
            with patch('promo_finder.providers.request.urlopen', return_value=self.response('MAX_TOKENS')):
                with self.assertRaises(ProviderError):
                    p.generate('test', SELECTION_SCHEMA, 'discovery')
            self.assertFalse(list(Path(d).glob('*.result.json')))
            self.assertTrue(list(Path(d).glob('*.response.txt')))

    @patch.dict(os.environ, {'GEMINI_API_KEY': 'fake-secret-test-only'})
    def test_daily_quota_stops_without_retry_loop(self):
        with tempfile.TemporaryDirectory() as d:
            p = JSONProvider('gemini', 'test-model', Path(d), min_interval=0)
            failure = HTTPError('https://example.test', 429, 'quota', {}, io.BytesIO(b'quota requestsPerDay exceeded'))
            with patch('promo_finder.providers.request.urlopen', side_effect=failure) as network:
                with self.assertRaises(ProviderError):
                    p.generate('test', SELECTION_SCHEMA, 'discovery')
                self.assertEqual(network.call_count, 1)

    @patch.dict(os.environ, {'GEMINI_API_KEY': 'fake-secret-test-only'})
    def test_request_budget_blocks_additional_network_calls(self):
        with tempfile.TemporaryDirectory() as d:
            p = JSONProvider('gemini', 'test-model', Path(d), max_calls=1, min_interval=0)
            with patch('promo_finder.providers.request.urlopen', return_value=self.response()) as network:
                p.generate('first', SELECTION_SCHEMA, 'discovery')
                with self.assertRaisesRegex(ProviderError, 'budget'):
                    p.generate('second', SELECTION_SCHEMA, 'discovery')
                self.assertEqual(network.call_count, 1)

    @patch.dict(os.environ, {'GEMINI_API_KEY': 'fake-secret-test-only'})
    def test_http_error_detail_never_exposes_api_key(self):
        with tempfile.TemporaryDirectory() as d:
            p = JSONProvider('gemini', 'test-model', Path(d), min_interval=0)
            detail = json.dumps({'error': {'message': 'Invalid key fake-secret-test-only'}}).encode()
            failure = HTTPError('https://example.test', 400, 'bad request', {}, io.BytesIO(detail))
            with patch('promo_finder.providers.request.urlopen', side_effect=failure):
                with self.assertRaises(ProviderError) as ctx:
                    p.generate('test', SELECTION_SCHEMA, 'discovery')
            self.assertNotIn('fake-secret-test-only', str(ctx.exception))
            self.assertNotIn('fake-secret-test-only', ''.join(f.read_text() for f in Path(d).iterdir()))

    def test_discovery_rejects_pair_outside_legal_catalog(self):
        class ArbitraryPairProvider(FakeProvider):
            def generate(self, prompt, schema, stage):
                self.calls += 1
                candidate = proposal()
                # The core multi-segment validator supports this, but discovery v2 does not.
                candidate['segments'].append(dict(candidate['segments'][0]))
                return {'candidates': [candidate]}
        with tempfile.TemporaryDirectory() as d:
            manifest, shortlist = run([fixture()], Path(d), ArbitraryPairProvider(), progress=lambda _: None)
            self.assertTrue(manifest['complete'])
            self.assertEqual(shortlist, [])
            self.assertEqual(manifest['rejections'], 1)


if __name__ == '__main__':
    unittest.main()
