"""Stand process journal: journal.py's record format for native stand JVMs, plus RSS/heap and controller phase.

python3 journal_stand.py record OUT.jsonl      (runs until stopped)
python3 journal_stand.py check OUT.jsonl       (journal.check from the modpack + summary)
"""

import json
import re
import subprocess
import sys
import time
from pathlib import Path

from paths import STAND, WORKSHOP
from stand import (
    stand_java,
    registered,
    StandBackend,
    JAVA,
)  # e2e dir is on sys.path[0] by default  # noqa: E402

sys.path.append(str(WORKSHOP))
import journal  # noqa: E402

JCMD = str(Path(JAVA).with_name("jcmd"))


def other_fabric():
    count = 0
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            try:
                if (entry / "comm").read_text().strip() == "java" and b"fabric" in (
                    entry / "cmdline"
                ).read_bytes():
                    count += 1
            except OSError:
                pass
    return count


def rss_kb(pid):
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except OSError:
        return None


def heap(pid):
    try:
        out = subprocess.run(
            [JCMD, str(pid), "GC.heap_info"], capture_output=True, text=True, timeout=10
        ).stdout
        m = re.search(r"total (\d+)K, used (\d+)K", out)
        return (
            {
                "committed_mb": int(m.group(1)) // 1024,
                "used_mb": int(m.group(2)) // 1024,
            }
            if m
            else None
        )
    except Exception:
        return None


def record(out, duration=600):
    if duration <= 0:
        raise ValueError("record duration must be positive")
    last_heap = 0
    deadline = time.monotonic() + duration
    backend = StandBackend()
    with open(out, "a", buffering=1) as f:
        while time.monotonic() < deadline:
            started = time.monotonic()
            java_procs = stand_java()
            registered_pids = {}
            for app in {app for _, app in java_procs}:
                try:
                    process = registered(app)
                    if process and process[1]["live"]:
                        registered_pids[app] = process[1]["pid"]
                except (OSError, ValueError, TypeError, KeyError, RuntimeError):
                    pass
            r = {
                "t": round(time.time(), 3),
                "game_java": sum(a == "game" for _, a in java_procs),
                "workshop_java": sum(a == "workshop" for _, a in java_procs),
                "unknown_java": sum(
                    registered_pids.get(app) != pid for pid, app in java_procs
                ),
                "host_fabric_java": other_fabric(),
            }
            try:
                journal.record_readiness(r, backend.observe())
            except Exception as error:
                r["error"] = f"{type(error).__name__}: {error}"
            journal.record_gate(r, STAND / "controller/gate.json")
            try:
                r["phase"] = json.loads((STAND / "controller/state.json").read_text())[
                    "phase"
                ]
            except (OSError, ValueError, KeyError):
                r["phase"] = None
            if java_procs:
                r["apps"] = {
                    app: {"pid": pid, "rss_mb": (rss_kb(pid) or 0) // 1024}
                    for pid, app in java_procs
                }
                r["rss_mb"] = sum(app["rss_mb"] for app in r["apps"].values())
                if started - last_heap > 5:
                    for name, app in r["apps"].items():
                        app["heap"] = (
                            heap(app["pid"])
                            if registered_pids.get(name) == app["pid"]
                            else None
                        )
                    last_heap = started
            f.write(json.dumps(r) + "\n")
            time.sleep(max(0.0, 0.2 - (time.monotonic() - started)))


def check(path):
    records = [
        json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()
    ]
    violations = journal.check(records)
    java_count = max((r["game_java"] + r["workshop_java"] for r in records), default=0)
    phases = []
    for r in records:
        if not phases or phases[-1] != r.get("phase"):
            phases.append(r.get("phase"))
    print(
        json.dumps(
            {
                "samples": len(records),
                "span_s": round(records[-1]["t"] - records[0]["t"]) if records else 0,
                "max_stand_java": java_count,
                "max_host_fabric_java": max(
                    (r.get("host_fabric_java", 0) for r in records), default=0
                ),
                "gates": sorted({str(r.get("gate")) for r in records}),
                "violations": len(violations),
                "phase_sequence": phases,
            },
            ensure_ascii=False,
        )
    )
    print("\n".join(violations[:20]))
    return 1 if violations else 0


if __name__ == "__main__":
    if sys.argv[1] == "record":
        record(sys.argv[2], float(sys.argv[3]) if len(sys.argv) > 3 else 600)
    else:
        sys.exit(check(sys.argv[2]))
