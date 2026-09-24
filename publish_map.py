"""Prepare a private map artifact and review PR; never deploy or create a pack release tag.

Run on the publisher workstation with gh access, outside the Minecraft backend:
python tools/workshop/publish_map.py --candidates /path/to/candidates --id CANDIDATE_ID
An older accepted ID prepares a one-map rollback PR through exactly the same path.
"""
import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import zipfile

import map_package
from release_maps import REPOSITORY


def run(*args, cwd=None):
    return subprocess.check_output(args, cwd=cwd, text=True).strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidates', type=Path, required=True)
    parser.add_argument('--id', required=True)
    args = parser.parse_args()
    if not re.fullmatch(r'[0-9a-f]{32}', args.id):
        parser.error('invalid candidate id')
    meta = json.loads((args.candidates / (args.id + '.json')).read_text())
    if meta.get('id') != args.id or meta.get('status') != 'accepted':
        parser.error('only the exact accepted candidate may be published')
    if run('gh', 'api', 'repos/' + REPOSITORY, '--jq', '.private') != 'true':
        parser.error('map artifacts require a private repository')
    root = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix='blockfield-map-') as directory:
        temporary = Path(directory)
        archive = temporary / 'package.zip'
        shutil.copyfile(args.candidates / (args.id + '.zip'), archive)
        manifest = map_package.validate(archive, meta['sha256'])
        map_id = manifest['mapId']
        if map_id != meta['mapId'] or not re.fullmatch(r'[a-z0-9_]{1,32}', map_id):
            parser.error('map identity mismatch')
        tag = f'map-{map_id}-{meta["sha256"][:16]}'
        branch = 'maps/' + tag
        archive = archive.rename(temporary / (map_id + '.zip'))
        # Existing releases are immutable: on retry compare the downloaded bytes, never --clobber.
        try:
            run('gh', 'release', 'view', tag, '--repo', REPOSITORY)
        except subprocess.CalledProcessError:
            notes = temporary / 'notes.txt'
            notes.write_text(f'Accepted candidate {args.id}\nMap {map_id}\nSHA256 {meta["sha256"]}\n' + str(meta.get('note', '')))
            run('gh', 'release', 'create', tag, str(archive), '--repo', REPOSITORY,
                '--title', f'Map {map_id}: {args.id}', '--notes-file', str(notes), '--latest=false')
        else:
            downloaded = temporary / 'existing'
            downloaded.mkdir()
            run('gh', 'release', 'download', tag, '--repo', REPOSITORY, '--pattern', archive.name, '--dir', str(downloaded))
            map_package.validate(downloaded / archive.name, meta['sha256'])
        run('git', 'fetch', 'origin', 'main', cwd=root)
        worktree = temporary / 'worktree'
        run('git', 'worktree', 'add', '--detach', str(worktree), 'origin/main', cwd=root)
        try:
            pins_path = worktree / 'distribution/maps.lock.json'
            pins = json.loads(pins_path.read_text())
            pins[map_id] = {'release': tag, 'asset': archive.name, 'sha256': meta['sha256']}
            pins_path.write_text(json.dumps(pins, indent=2, sort_keys=True) + '\n')
            configs = [p for p in (worktree / 'server/lobby/blockfield/maps').glob('*.json') if json.loads(p.read_text()).get('id') == map_id]
            if len(configs) != 1:
                raise ValueError('map must have exactly one committed config')
            with zipfile.ZipFile(archive) as package:
                configs[0].write_bytes(package.read('map.json'))
            run('git', 'add', 'distribution/maps.lock.json', str(configs[0]), cwd=worktree)
            if not run('git', 'diff', '--cached', '--name-only', cwd=worktree):
                print('This package is already pinned on main; no PR needed.')
                return
            run('git', 'commit', '-m', f'maps: pin {map_id} candidate {args.id}', cwd=worktree)
            run('git', 'push', 'origin', f'HEAD:refs/heads/{branch}', cwd=worktree)
            existing = json.loads(run('gh', 'pr', 'list', '--repo', REPOSITORY, '--head', branch, '--json', 'url'))
            if existing:
                print(existing[0]['url'])
                return
            body = temporary / 'pr.md'
            body.write_text(f'Pin accepted map `{map_id}` candidate `{args.id}` and its matching JSON.\n\n'
                            f'SHA256: `{meta["sha256"]}`. Package manifest and all member checksums validated.\n\n'
                            'Acceptance and this PR do not deploy. After review, use the ordinary pack version/tag release process. '
                            'Other maps and mod pins remain unchanged.\n')
            print(run('gh', 'pr', 'create', '--repo', REPOSITORY, '--base', 'main', '--head', branch,
                      '--title', f'maps: release {map_id} candidate {args.id}', '--body-file', str(body)))
        finally:
            run('git', 'worktree', 'remove', '--force', str(worktree), cwd=root)


if __name__ == '__main__':
    main()
