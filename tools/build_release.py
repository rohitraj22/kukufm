"""Create local distribution artifacts from an explicit public-file allowlist."""

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import zipfile

ROOT_FILES = {'.gitignore', '.env.example', 'README.md', 'pyproject.toml', 'MANIFEST.in', 'release_files.json', 'app.py', 'requirements.txt'}
ROOT_DIRS = {'promo_finder', 'kuku_promos', 'config', 'benchmarks', 'docs', 'examples', 'tests', 'tools', 'web', '.streamlit'}
BLOCKED = {'private', 'output', 'raw', '.git', '.cache', '.venv', '__pycache__', 'dist'}


def collect_public_files(root):
    root = Path(root).resolve()
    names = json.loads((root / 'release_files.json').read_text())
    if not isinstance(names, list) or not names or any(not isinstance(n, str) for n in names) or len(set(names)) != len(names):
        raise ValueError('Release allowlist must be a nonempty list of unique relative paths.')
    files = {}
    for name in names:
        path = PurePosixPath(name)
        if path.is_absolute() or '..' in path.parts or '\\' in name or name != str(path):
            raise ValueError('Unsafe release path.')
        if any(part in BLOCKED or (part.startswith('.env') and name != '.env.example') for part in path.parts):
            raise ValueError('Private data or credentials cannot enter a release.')
        if path.parts[0] == '.streamlit' and name != '.streamlit/config.toml':
            raise ValueError('Only the public Streamlit theme/config may enter a release; secrets must stay private.')
        if name not in ROOT_FILES and (len(path.parts) < 2 or path.parts[0] not in ROOT_DIRS):
            raise ValueError('Release file is outside the public project surfaces.')
        current = root
        for part in path.parts:
            current = current / part
            if current.is_symlink():
                raise ValueError('Symlinks are not allowed in a release.')
        if not current.is_file() or not current.resolve().is_relative_to(root):
            raise ValueError('Release file is missing or outside the project.')
        body = current.read_bytes()
        if re.search(rb'AIza[A-Za-z0-9_-]{25,}|sk-[A-Za-z0-9_-]{24,}', body):
            raise ValueError(f'Possible credential in public file: {name}')
        files[name] = body
    return files


def build(root, destination):
    files = collect_public_files(root)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    inventory = {name: hashlib.sha256(body).hexdigest() for name, body in sorted(files.items())}
    source = destination / 'promo-finder-source.zip'
    with zipfile.ZipFile(source, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, body in sorted(files.items()):
            archive.writestr('promo-finder/' + name, body)
        archive.writestr('promo-finder/RELEASE_CONTENTS.json', json.dumps(inventory, indent=2))
    application = destination / 'promo-finder.pyz'
    with zipfile.ZipFile(application, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, body in files.items():
            if name.startswith('promo_finder/') and name.endswith('.py'):
                archive.writestr(name, body)
        archive.writestr('__main__.py', 'from promo_finder.__main__ import main\nraise SystemExit(main())\n')
    (destination / 'release_inventory.json').write_text(json.dumps(inventory, indent=2) + '\n')
    return source, application, inventory


def main():
    parser = argparse.ArgumentParser(description='Build local, code-only release artifacts. Does not publish anything.')
    parser.add_argument('--output', type=Path, default=Path('dist'))
    args = parser.parse_args()
    source, app, inventory = build(Path(__file__).resolve().parents[1], args.output)
    print(f'Public allowlist: {len(inventory)} files')
    print(f'Source: {source.resolve()}')
    print(f'Portable CLI: {app.resolve()}')


if __name__ == '__main__':
    main()
