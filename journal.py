#!/usr/bin/env python3
"""Process journal for the single-backend acceptance test (blockfield-releases#8).

record: sample java processes per app, lock holder, Swarm tasks and the proxy gate to JSONL.
check:  fail if two java ever ran or the gate was open to the wrong backend.

  journal.py record --out /root/journal.jsonl [--interval 0.2] [--duration 600]
  journal.py check /root/journal.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time


def lock_holder(locks: str, inode: int) -> int | None:
    """Read the recorded lock PID from /proc/locks; ignore blocked waiters (->)."""
    for line in locks.splitlines():
        fields = line.split()
        if len(fields) >= 6 and fields[1:4] == ["FLOCK", "ADVISORY", "WRITE"] and fields[5].rsplit(":", 1)[-1] == str(inode):
            return int(fields[4])
    return None


def sample(backend, gate_file: Path, tasks: dict | None) -> dict:
    record = {"t": round(time.time(), 3)}
    try:
        inventory = backend.inventory(tasks=tasks is None)
        for profile in backend.apps:
            record[f"{profile}_java"] = inventory[profile]["java"]
            record[f"{profile}_tasks"] = inventory[profile]["tasks"] if tasks is None else tasks.get(profile)
        record["unknown_java"] = inventory["unknown"]
    except Exception as error:  # a journal must keep sampling through Docker hiccups
        record["error"] = f"{type(error).__name__}: {error}"
    try:
        lock = backend.lock_dir / "backend.lock"
        pid = lock_holder((backend.proc / "locks").read_text(), lock.stat().st_ino)
        if pid is None:
            record["lock"] = None
        else:
            container = backend.container_of(pid)
            owners = {cid: profile for profile in backend.apps for cid in backend.containers(profile)}
            record["lock"] = owners.get(container, "other")
    except Exception as error:
        record["lock"] = f"error: {type(error).__name__}"
    try:
        gate = json.loads(gate_file.read_text())
        record["gate"], record["backend"] = gate.get("gate"), gate.get("backend")
    except (OSError, ValueError):
        record["gate"], record["backend"] = "missing", None
    return record


def check(records: list[dict]) -> list[str]:
    violations = []
    for r in records:
        java = r.get("game_java", 0) + r.get("workshop_java", 0) + r.get("unknown_java", 0)
        if java > 1:
            violations.append(f"{r['t']}: {java} java processes")
        if r.get("gate") in ("public", "draining") and (r.get("workshop_java") or r.get("backend") != "game"):
            violations.append(f"{r['t']}: gate {r['gate']} outside the game backend")
        if r.get("gate") in ("builders", "testers") and (r.get("game_java") or r.get("backend") != "workshop"):
            violations.append(f"{r['t']}: {r['gate']} gate outside the workshop backend")
    return violations


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    record = commands.add_parser("record")
    record.add_argument("--out", required=True)
    record.add_argument("--interval", type=float, default=0.2)
    record.add_argument("--duration", type=float, default=0, help="seconds; 0 until Ctrl+C")
    verify = commands.add_parser("check")
    verify.add_argument("journal")
    args = parser.parse_args(argv)

    if args.command == "check":
        records = [json.loads(line) for line in Path(args.journal).read_text().splitlines() if line.strip()]
        violations = check(records)
        java = max((r.get("game_java", 0) + r.get("workshop_java", 0) + r.get("unknown_java", 0) for r in records), default=0)
        gates = sorted({str(r.get("gate")) for r in records})
        errors = sum(1 for r in records if "error" in r)
        print(f"samples={len(records)} max_java={java} gates={gates} sample_errors={errors} violations={len(violations)}")
        print("\n".join(violations[:50]))
        return 1 if violations else 0

    from docker_backend import DockerBackend
    backend = DockerBackend.from_env()
    gate_file = Path(os.environ.get("BF_GATE_FILE", "/srv/minecraft/workshop/gate/gate.json"))
    deadline = time.monotonic() + args.duration if args.duration else None
    tasks, tasks_at = None, 0.0
    with open(args.out, "a", buffering=1) as out:
        try:
            while deadline is None or time.monotonic() < deadline:
                started = time.monotonic()
                # Swarm task listing is slow; refresh it once a second, java/lock/gate every sample.
                fresh = started - tasks_at >= 1.0
                record = sample(backend, gate_file, None if fresh else tasks)
                if fresh:
                    tasks, tasks_at = {p: record.get(f"{p}_tasks") for p in backend.apps}, started
                out.write(json.dumps(record) + "\n")
                time.sleep(max(0.0, args.interval - (time.monotonic() - started)))
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
