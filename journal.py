#!/usr/bin/env python3
"""Process journal for independent Game and Workshop backends.

record: sample Java processes, readiness, per-app locks, tasks and proxy gates to JSONL.
check: fail on duplicate/unknown Java or an open gate without its exact ready backend.

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
        if (
            len(fields) >= 6
            and fields[1:4] == ["FLOCK", "ADVISORY", "WRITE"]
            and fields[5].rsplit(":", 1)[-1] == str(inode)
        ):
            return int(fields[4])
    return None


def record_readiness(record, processes):
    for app in ("game", "workshop"):
        matching = [
            p
            for p in processes
            if ("workshop" if p.goal.profile == "test" else p.goal.profile) == app
        ]
        record[f"{app}_ready"] = len(matching) == 1 and matching[0].ready
        record[f"{app}_profile"] = (
            matching[0].goal.profile if len(matching) == 1 else None
        )


def record_gate(record, path):
    try:
        gate = json.loads(path.read_text())
        record["gate"], record["backend"] = gate.get("gate"), gate.get("backend")
        if "backends" in gate:
            entries = gate["backends"]
            # The process proof needs states, not repeated account ACLs or messages.
            record["backends"] = {
                app: {"gate": entries[app].get("gate")}
                if isinstance(entries, dict) and isinstance(entries.get(app), dict)
                else None
                for app in ("game", "workshop")
            }
    except (OSError, ValueError, AttributeError):
        record["gate"], record["backend"] = "missing", None


def sample(backend, gate_file: Path, tasks: dict | None) -> dict:
    record = {"t": round(time.time(), 3)}
    try:
        inventory = backend.inventory(tasks=tasks is None)
        for profile in backend.apps:
            record[f"{profile}_java"] = inventory[profile]["java"]
            record[f"{profile}_tasks"] = (
                inventory[profile]["tasks"] if tasks is None else tasks.get(profile)
            )
        record["unknown_java"] = inventory["unknown"]
        record_readiness(record, backend.observe())
    except Exception as error:  # a journal must keep sampling through Docker hiccups
        record["error"] = f"{type(error).__name__}: {error}"
    try:
        locks = (backend.proc / "locks").read_text()
        owners = {
            cid: profile
            for profile in backend.apps
            for cid in backend.containers(profile)
        }
        record["locks"] = {}
        for app in backend.apps:
            lock = backend.lock_dir / app / "backend.lock"
            pid = lock_holder(locks, lock.stat().st_ino) if lock.exists() else None
            record["locks"][app] = (
                owners.get(backend.container_of(pid), "other")
                if pid is not None
                else None
            )
    except Exception as error:
        record["lock_error"] = f"{type(error).__name__}"
    record_gate(record, gate_file)
    return record


def check(records: list[dict]) -> list[str]:
    violations = []
    for r in records:
        counts = ("game_java", "workshop_java", "unknown_java")
        if any(
            type(r.get(key, 0)) is not int
            or r.get(key, 0) < 0
            or "backends" in r
            and key not in r
            for key in counts
        ):
            violations.append(f"{r['t']}: invalid process counts")
            continue
        if "error" in r or "lock_error" in r:
            violations.append(f"{r['t']}: incomplete process sample")
        for app in ("game", "workshop"):
            if r.get(f"{app}_java", 0) > 1:
                violations.append(f"{r['t']}: several {app} Java processes")
        if r.get("unknown_java", 0):
            violations.append(f"{r['t']}: unknown Java process")
        if "backends" in r:
            entries = r["backends"]
            for app, states in (
                ("game", {"public", "draining", "closed"}),
                ("workshop", {"builders", "testers", "closed", "sleeping"}),
            ):
                entry = entries.get(app) if isinstance(entries, dict) else None
                state = entry.get("gate") if isinstance(entry, dict) else None
                if not isinstance(state, str) or state not in states:
                    violations.append(f"{r['t']}: invalid {app} gate")
                elif state == "sleeping":
                    # Cold Workshop: parked at 0 replicas with its goal intact.
                    if app != "workshop" or r.get("workshop_java") != 0:
                        violations.append(f"{r['t']}: sleeping gate needs no backend")
                elif state != "closed" and (
                    r.get(f"{app}_java") != 1
                    or r.get(f"{app}_ready") is not True
                    or r.get(f"{app}_profile")
                    != ("test" if state == "testers" else app)
                ):
                    violations.append(f"{r['t']}: {app} gate lacks its ready backend")
                if r.get("locks", {}).get(app) not in (None, app):
                    violations.append(f"{r['t']}: wrong {app} lock owner")
            continue
        # Preserve the interpretation of historical single-backend journals.
        java = (
            r.get("game_java", 0) + r.get("workshop_java", 0) + r.get("unknown_java", 0)
        )
        if (
            java > 1
            and not r.get("unknown_java")
            and all(r.get(f"{app}_java", 0) <= 1 for app in ("game", "workshop"))
        ):
            violations.append(f"{r['t']}: {java} java processes")
        if r.get("gate") in ("public", "draining") and (
            r.get("workshop_java") or r.get("backend") != "game"
        ):
            violations.append(f"{r['t']}: gate {r['gate']} outside the game backend")
        if r.get("gate") in ("builders", "testers") and (
            r.get("game_java") or r.get("backend") != "workshop"
        ):
            violations.append(
                f"{r['t']}: {r['gate']} gate outside the workshop backend"
            )
    return violations


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)
    record = commands.add_parser("record")
    record.add_argument("--out", required=True)
    record.add_argument("--interval", type=float, default=0.2)
    record.add_argument(
        "--duration",
        type=float,
        default=600,
        help="bounded recording duration in seconds",
    )
    verify = commands.add_parser("check")
    verify.add_argument("journal")
    args = parser.parse_args(argv)
    if args.command == "record" and args.duration <= 0:
        parser.error("record duration must be positive")

    if args.command == "check":
        records = [
            json.loads(line)
            for line in Path(args.journal).read_text().splitlines()
            if line.strip()
        ]
        violations = check(records)
        java = max(
            (
                r.get("game_java", 0)
                + r.get("workshop_java", 0)
                + r.get("unknown_java", 0)
                for r in records
            ),
            default=0,
        )
        gates = sorted({str(r.get("gate")) for r in records})
        errors = sum(1 for r in records if "error" in r)
        print(
            f"samples={len(records)} max_java={java} gates={gates} sample_errors={errors} violations={len(violations)}"
        )
        print("\n".join(violations[:50]))
        return 1 if violations else 0

    from docker_backend import DockerBackend

    backend = DockerBackend.from_env()
    gate_file = Path(
        os.environ.get("BF_GATE_FILE", "/srv/minecraft/workshop/gate/gate.json")
    )
    deadline = time.monotonic() + args.duration
    tasks, tasks_at = None, 0.0
    with open(args.out, "a", buffering=1) as out:
        try:
            while time.monotonic() < deadline:
                started = time.monotonic()
                # Swarm task listing is slow; refresh it once a second, java/lock/gate every sample.
                fresh = started - tasks_at >= 1.0
                record = sample(backend, gate_file, None if fresh else tasks)
                if fresh:
                    tasks, tasks_at = (
                        {p: record.get(f"{p}_tasks") for p in backend.apps},
                        started,
                    )
                out.write(json.dumps(record) + "\n")
                time.sleep(max(0.0, args.interval - (time.monotonic() - started)))
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
