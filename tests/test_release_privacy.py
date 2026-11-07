"""Verify that distribution never depends on or bundles a private input pack."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile
from uuid import uuid4

from tools.build_release import build, collect_public_files

ROOT = Path(__file__).resolve().parents[1]


class ReleasePrivacyTests(unittest.TestCase):
    def stage_public_project(self, path):
        for name, body in collect_public_files(ROOT).items():
            destination = path / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(body)

    def test_release_excludes_unknown_scripts_keys_raw_outputs_and_caches(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'project'; root.mkdir()
            self.stage_public_project(root)
            canary = 'PRIVATE_' + uuid4().hex
            for name in ['confidential-corpus/episode.txt', 'renamed-private-input.txt', '.env',
                         'output/raw/response.json', '.cache/result.json', 'assignment.pdf', '.streamlit/secrets.toml']:
                path = root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(canary)
            source, portable, inventory = build(root, Path(tmp) / 'release')
            self.assertEqual(set(inventory), set(json.loads((root / 'release_files.json').read_text())))
            for artifact in [source, portable]:
                with zipfile.ZipFile(artifact) as archive:
                    for name in archive.namelist():
                        self.assertNotIn(canary.encode(), archive.read(name))
                        self.assertNotIn('confidential-corpus', name)
                        self.assertNotIn('output/', name)
                        self.assertNotEqual(Path(name).name, '.env')

    def test_portable_product_runs_without_repository_pack_key_or_dependencies(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'project'; root.mkdir()
            self.stage_public_project(root)
            _, portable, _ = build(root, Path(tmp) / 'release')
            isolated = Path(tmp) / 'isolated'; isolated.mkdir()
            destination = isolated / 'result'
            completed = subprocess.run([sys.executable, str(portable), '--input', str(root / 'examples/sports'),
                                        '--provider', 'baseline', '--output', str(destination)],
                                       cwd=isolated, env={'PYTHONDONTWRITEBYTECODE': '1'},
                                       capture_output=True, text=True, timeout=30)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads((destination / 'manifest.json').read_text())
            self.assertTrue(result['complete'])
            self.assertEqual(result['network_calls_this_run'], 0)

    def test_private_path_cannot_be_added_to_allowlist(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ['.env', 'output/secret.txt', '../secret.txt', '/private/secret.txt', '.streamlit/secrets.toml']:
                (root / 'release_files.json').write_text(json.dumps([name]))
                with self.subTest(path=name), self.assertRaises(ValueError):
                    collect_public_files(root)

    def test_symlinks_cannot_smuggle_in_private_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'secret.txt').write_text('PRIVATE')
            (root / 'README.md').symlink_to(root / 'secret.txt')
            (root / 'release_files.json').write_text('["README.md"]')
            with self.assertRaisesRegex(ValueError, 'Symlinks'):
                collect_public_files(root)

    def test_recognizable_credential_refuses_release_without_echoing_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            key = 'AIza' + 'x' * 35
            (root / 'README.md').write_text(key)
            (root / 'release_files.json').write_text('["README.md"]')
            with self.assertRaises(ValueError) as result:
                collect_public_files(root)
            self.assertNotIn(key, str(result.exception))


if __name__ == '__main__':
    unittest.main()
