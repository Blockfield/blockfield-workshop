"""Fetch verified private map packages and apply world/config pairs to a release tree."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import tempfile
import zipfile

import map_package

REPOSITORY = 'netherg-io/blockfield-modpack'


def package(map_id, pin, cache):
    if not re.fullmatch(r'[a-z0-9_]{1,32}', map_id):
        raise ValueError('invalid map id')
    if not isinstance(pin, dict) or set(pin) != {'release', 'asset', 'sha256'}:
        raise ValueError('invalid map pin')
    if not re.fullmatch(r'map-[a-z0-9_-]{1,110}', pin['release']) or pin['asset'] != map_id + '.zip':
        raise ValueError('invalid map release/asset')
    if not re.fullmatch(r'[a-f0-9]{64}', pin['sha256']):
        raise ValueError('invalid package checksum')
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / (pin['sha256'] + '.zip')
    if not target.exists():
        with tempfile.TemporaryDirectory(dir=cache) as directory:
            subprocess.run(['gh', 'release', 'download', pin['release'], '--repo', REPOSITORY,
                            '--pattern', pin['asset'], '--dir', directory], check=True, timeout=180)
            source = Path(directory) / pin['asset']
            manifest = map_package.validate(source, pin['sha256'])
            if manifest['mapId'] != map_id:
                raise ValueError('pinned map identity mismatch')
            source.replace(target)
    manifest = map_package.validate(target, pin['sha256'])
    if manifest['mapId'] != map_id:
        raise ValueError('pinned map identity mismatch')
    return target


def apply(files, pins, cache):
    replacements = {}
    removed = set()
    for map_id, pin in pins.items():
        path = package(map_id, pin, cache)
        with zipfile.ZipFile(path) as archive:
            config = archive.read('map.json')
            existing = [name for name in files if name.startswith('blockfield/maps/')
                        and name.endswith('.json') and json.loads(files[name]).get('id') == map_id]
            if len(existing) != 1 or json.loads(files[existing[0]]) != json.loads(config):
                raise ValueError(f'{map_id}: committed map JSON differs from the pinned package')
            removed.update(name for name in files if name.startswith(f'maps/{map_id}/'))
            replacements[existing[0]] = config
            for entry in archive.infolist():
                if entry.filename.startswith('world/') and not entry.is_dir():
                    replacements[f'maps/{map_id}/' + entry.filename.removeprefix('world/')] = archive.read(entry)
    for name in removed:
        del files[name]
    files.update(replacements)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lock', type=Path, default=Path('distribution/maps.lock.json'))
    parser.add_argument('--cache', type=Path, default=Path('sources/maps'))
    args = parser.parse_args()
    for map_id, pin in json.loads(args.lock.read_text()).items():
        print(package(map_id, pin, args.cache))


if __name__ == '__main__':
    main()
