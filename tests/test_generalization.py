"""Public tests use only original synthetic inputs, never the private benchmark."""

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import re
import tempfile
import unittest

from promo_finder.benchmark import inspect_dataset
from promo_finder.config import Policy
from promo_finder.parser import InputError, load_episodes, parse_json_episodes, parse_script
from promo_finder.pipeline import run
from promo_finder.prompts import selection_prompt, critic_prompt
from promo_finder.selection import baseline_candidates, validate_candidate, weighted_score

ROOT = Path(__file__).resolve().parents[1]


def space_story():
    return load_episodes(ROOT / "examples/scifi")[0]


def cut_times(episode, policy=None):
    return [(s['start_ms'], s['end_ms']) for c in baseline_candidates(episode, policy) for s in c['segments']]


class GeneralizationTests(unittest.TestCase):
    def test_character_names_and_plot_text_do_not_drive_code_selection(self):
        original = space_story()
        transformed = replace(original, sha256='rewritten-fiction', logline='A completely different story',
                              beats=[replace(b, label=f'PERSON {i}', text=f'Unrelated invented event {i}')
                                     for i, b in enumerate(original.beats)])
        self.assertEqual(original.legal_continuous_cuts(), transformed.legal_continuous_cuts())
        self.assertEqual(cut_times(original), cut_times(transformed))
        self.assertTrue(all(set(c['scores'].values()) == {0} for c in baseline_candidates(transformed)))

    def test_nonsequential_episode_numbers(self):
        text = (ROOT / 'examples/scifi/airlock.txt').read_text()
        original = parse_script(text)
        changed = parse_script(text.replace('Episode 204', 'Episode 10007'))
        self.assertEqual(cut_times(original), cut_times(changed))
        self.assertTrue(all(b.id.startswith('E10007-') for b in changed.beats))

    def test_timestamps_are_derived_from_input_not_fixed_offsets(self):
        original = space_story()
        offset = 123456
        changed = replace(original, beats=[replace(b, start_ms=b.start_ms + offset, end_ms=b.end_ms + offset)
                                           for b in original.beats])
        self.assertEqual(cut_times(original), [(a-offset, b-offset) for a, b in cut_times(changed)])

    def test_unrelated_genres_and_formats_run_without_private_inputs(self):
        for source in ['examples/scifi', 'examples/sports']:
            with self.subTest(source=source), tempfile.TemporaryDirectory() as tmp:
                episodes = load_episodes(ROOT / source)
                manifest, candidates = run(episodes, Path(tmp), progress=lambda _: None)
                self.assertTrue(manifest['complete'])
                self.assertGreater(len(candidates), 0)
                for candidate in candidates:
                    validate_candidate(candidate, episodes)
                self.assertIn('Promo Finder', (Path(tmp) / 'report.html').read_text())

    def test_utf8_and_arbitrary_speaker_labels(self):
        text = (ROOT / 'examples/scifi/airlock.txt').read_text().replace('RAE', 'रेवा').replace('The airlock opened from inside!', 'दरवाज़ा अंदर से खुला है!')
        episode = parse_script(text)
        self.assertEqual(episode.beats[0].kind, 'DIALOGUE')
        self.assertIn('दरवाज़ा', episode.beats[0].text)
        self.assertTrue(episode.legal_continuous_cuts())

    def test_json_roundtrip_preserves_timing_and_source_text(self):
        episode = space_story()
        value = {'episodes': [{'episode': episode.number, 'logline': episode.logline,
                              'beats': [{'kind': b.kind, 'label': b.label, 'text': b.text,
                                         'start_ms': b.start_ms, 'end_ms': b.end_ms} for b in episode.beats]}]}
        imported = parse_json_episodes(json.dumps(value))[0]
        self.assertEqual(cut_times(episode), cut_times(imported))
        self.assertEqual([b.text for b in episode.beats], [b.text for b in imported.beats])

    def test_json_invalid_units_missing_beats_and_duplicate_episodes_fail(self):
        value = json.loads((ROOT / 'examples/sports/scoreboard.json').read_text())
        for bad_time in [True, -1, 3.5, '00:02']:
            bad = json.loads(json.dumps(value))
            bad['episodes'][0]['beats'][0]['start_ms'] = bad_time
            with self.subTest(value=bad_time), self.assertRaises(InputError):
                parse_json_episodes(json.dumps(bad))
        with self.assertRaises(InputError):
            parse_json_episodes('{"episodes": []}')
        with tempfile.TemporaryDirectory() as tmp:
            value['episodes'] *= 2
            (Path(tmp) / 'duplicate.json').write_text(json.dumps(value))
            with self.assertRaises(InputError):
                load_episodes(Path(tmp))

    def test_settings_change_every_timing_check_and_prompt(self):
        policy = Policy(min_duration_ms=12000, max_duration_ms=25000, hook_window_ms=2500,
                        ending_window_ms=4000, language='Spanish', audience='New viewers worldwide')
        episode = space_story()
        candidates = baseline_candidates(episode, policy)
        self.assertTrue(candidates)
        for candidate in candidates:
            self.assertTrue(12 <= candidate['duration_seconds'] <= 25)
            validate_candidate(candidate, [episode], policy)
        prompt = selection_prompt([episode], policy)
        self.assertIn('12-25 seconds', prompt)
        self.assertIn('Spanish', prompt)
        self.assertIn('2.5 seconds', prompt)
        self.assertIn('4 seconds', critic_prompt(candidates, policy))
        self.assertEqual(baseline_candidates(episode, replace(policy, hook_window_ms=2000)), [])

    def test_invalid_policy_rejected(self):
        for kwargs in [{'min_duration_ms': 70000}, {'hook_window_ms': 30000}, {'min_quality_score': 6},
                       {'max_duration_ms': True}, {'weights': {'hook': 100}}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                Policy(**kwargs)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'policy.json'; path.write_text('{"prefer_episode": 999}')
            with self.assertRaises(ValueError):
                Policy.from_file(path)

    def test_weights_are_run_configuration(self):
        scores = dict(hook=5, cliffhanger=0, standalone=0, emotion=0, escalation=0)
        self.assertEqual(weighted_score(scores), 30)
        self.assertEqual(weighted_score(scores, Policy(weights=dict(hook=50, cliffhanger=20, standalone=15, emotion=10, escalation=5))), 50)

    def test_clip_identity_is_scoped_to_source_content(self):
        original = space_story()
        changed = replace(original, sha256=hashlib.sha256(b'different story').hexdigest())
        first, second = baseline_candidates(original)[0], baseline_candidates(changed)[0]
        self.assertNotEqual(first['id'], second['id'])
        self.assertEqual(first['duration_seconds'], second['duration_seconds'])

    def test_engine_contains_no_literal_source_ids_or_cut_timecodes(self):
        import ast
        for path in (ROOT / 'promo_finder').glob('*.py'):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    self.assertIsNone(re.search(r'\bE\d+-B\d+\b|\b\d{2}:\d{2}\.\d{2,3}\b', node.value), path.name)

    def test_benchmark_expectations_are_data_not_engine_constants(self):
        episodes = [space_story()]
        summary = inspect_dataset(episodes, {'expected_episodes': 1, 'expected_beats': 11}, Policy())
        self.assertEqual(summary['legal_cut_pairs'], summary['validated_cut_pairs'])
        with self.assertRaises(ValueError):
            inspect_dataset(episodes, {'expected_episodes': 2}, Policy())


if __name__ == '__main__':
    unittest.main()
