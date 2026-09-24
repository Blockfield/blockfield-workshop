#!/usr/bin/env python3
"""Bring a map published through the workshop back into this repo and a local world directory.

    tools/workshop/fetch-map.py <mapId> [--host homeserver] [--world-dir ~/.cache/bf-test-server/maps]

- JSON: the published overlay (lobby/blockfield-published/maps/<id>.json) replaces server/lobby/blockfield/maps/<id>.json.
  Commit it and release: once the game's image copy equals the overlay, the controller retires the overlay and later
  git edits of that map apply again (candidates.MapStore.retire_merged_overlays).
- World: the prod template (templates/maps/<id>) is mirrored into <world-dir>/<id> (the rig server's maps by default).

ponytail: the world-seed release (fresh installs, devstack) is not rebuilt here; cut a new world-seed-vN when fresh
installs must get the published world too.
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("map_id")
    parser.add_argument("--host", default="homeserver")
    parser.add_argument("--world-dir", type=Path, default=Path.home() / ".cache/bf-test-server/maps")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9_]{1,32}", args.map_id):
        parser.error("invalid map id")
    remote = f"/srv/minecraft/lobby/blockfield-published/maps/{args.map_id}.json"
    found = subprocess.run(["ssh", args.host, "cat", remote], capture_output=True, text=True)
    if found.returncode == 0:
        config = json.loads(found.stdout)
        if config.get("id") != args.map_id:
            sys.exit(f"{remote} is for {config.get('id')!r}, not {args.map_id}")
        target = ROOT / "server/lobby/blockfield/maps" / f"{args.map_id}.json"
        target.write_text(json.dumps(config, ensure_ascii=False, indent=2))
        print(f"{target.relative_to(ROOT)} <- {args.host}:{remote}")
    else:
        print(f"{args.map_id}: nothing published through the workshop, repo JSON unchanged")
    world = args.world_dir / args.map_id
    world.mkdir(parents=True, exist_ok=True)
    subprocess.run(["rsync", "-a", "--delete", f"{args.host}:/srv/minecraft/templates/maps/{args.map_id}/", f"{world}/"], check=True)
    print(f"{world} <- {args.host}:/srv/minecraft/templates/maps/{args.map_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
