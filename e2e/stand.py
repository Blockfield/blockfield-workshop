"""Stand backend for the REAL workshopctl/controller (blockfield-releases#8 acceptance, local, no Docker).

Mirrors docker_backend.DockerBackend semantics on native JVMs started through agent-process:
profile.json with a fresh generation, readiness must equal {profile, generation, session, release},
observe() reports EVERY stand java (registered or not). Faults for failure tests come from faults.json.
STAND/game and STAND/workshop must already hold a packed server (fabric-server-launch.jar, mods,
config, maps, server.properties) -- see README.md "Bootstrap" for the one-time setup.

  python3 stand.py step|status|cancel|members ...|request ...   (= workshopctl with this backend)
"""

import json
import os
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from paths import STAND, RIG, WORKSHOP, owner, java

sys.path[:0] = [str(WORKSHOP), str(RIG)]
from controller import Goal, Process, atomic_write  # noqa: E402
import workshopctl  # noqa: E402

OWNER = owner()
JAVA = java()
APPS = {"game": STAND / "game", "workshop": STAND / "workshop"}
LOCK = STAND / "lock"
RELEASE = LOCK / "build.json"
XMX = os.environ.get(
    "STAND_XMX", "1536m"
)  # keep modest: this dev box runs other agents' JVMs too


def faults():
    try:
        return json.loads((STAND / "faults.json").read_text())
    except (OSError, ValueError):
        return {}


def fault(name):
    current = faults()
    value = current.get(name)
    if value == "once":
        current.pop(name)
        (STAND / "faults.json").write_text(json.dumps(current))
    if value:
        (STAND / "evidence").mkdir(parents=True, exist_ok=True)
        with open(STAND / "evidence/faults-fired.log", "a") as log:
            log.write(f"{time.time():.3f} {name}={value}\n")
    return value


def rcon(command, timeout=60):
    from rcon import Rcon

    r = Rcon()
    r.s.settimeout(timeout)
    try:
        return r.cmd(command)
    finally:
        r.s.close()


def stand_java():
    """[(pid, app)] for every java running fabric-server-launch.jar with cwd in a stand app dir."""
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if (entry / "comm").read_text().strip() != "java":
                continue
            if b"fabric-server-launch.jar" not in (entry / "cmdline").read_bytes():
                continue
            cwd = Path(os.readlink(entry / "cwd"))
        except OSError:
            continue
        for app, path in APPS.items():
            if cwd == path:
                found.append((int(entry.name), app))
    return found


def registered():
    out = subprocess.run(
        ["agent-process", "list"], capture_output=True, text=True
    ).stdout
    return [
        line.split()[0]
        for line in out.splitlines()[1:]
        if "bf-stand-" in line and " yes " in f" {line} "
    ]


class StandBackend:
    apps = APPS
    maps = None

    @staticmethod
    def app_of(profile):
        return "workshop" if profile == "test" else profile

    def selection(self):
        try:
            return json.loads((LOCK / "profile.json").read_text())
        except FileNotFoundError:
            return None

    def readiness(self, app):
        try:
            data = json.loads((APPS[app] / ".blockfield-ready.json").read_text())
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def preflight(self, goal):
        if fault("preflight"):
            raise OSError("injected preflight fault")
        data = APPS[self.app_of(goal.profile)]
        if not data.is_dir() or not LOCK.is_dir():
            raise FileNotFoundError(
                "stand data/lock dir missing -- run the bootstrap in README.md first"
            )
        minimum = int(os.environ.get("BF_MIN_FREE_BYTES", 2 << 30))
        probe = Path(os.environ.get("STAND_DISK_PROBE", data))
        if shutil.disk_usage(probe).free < minimum:
            raise OSError("not enough free space for the backend data")

    def observe(self):
        java_procs = stand_java()
        selection = self.selection()
        selected = selection["profile"] if selection else "game"
        processes = []
        for app in APPS:
            count = sum(1 for _, a in java_procs if a == app)
            if count > 1:
                raise RuntimeError(f"{app} runs several java processes")
            if count == 0:
                continue
            profile = selected if app == self.app_of(selected) else app
            ready = self.readiness(app)
            if selection and selection["profile"] == profile:
                goal = Goal(
                    profile,
                    selection["release"],
                    selection.get("session"),
                    selection.get("candidate") if profile == "test" else None,
                )
                expected = {
                    k: selection.get(k)
                    for k in ("profile", "generation", "session", "release")
                }
            else:
                goal = Goal(
                    profile,
                    "unknown",
                    None
                    if profile == "game"
                    else "00000000-0000-0000-0000-000000000000",
                )
                expected = None
            processes.append(Process(goal, expected is not None and ready == expected))
        return processes

    def request_drain(self):
        if stand_java():
            rcon("bf maintenance drain")

    def drain_progress(self):
        if not stand_java():
            return None
        out = rcon("bf maintenance status")
        parts = dict(x.split("=", 1) for x in out.split() if "=" in x)
        return {
            k: (v == "true" if v in ("true", "false") else int(v) if v.isdigit() else v)
            for k, v in parts.items()
        }

    def save_and_freeze(self, process):
        if fault("save"):
            raise RuntimeError("injected: save-all did not confirm success")
        out = rcon("save-all flush", timeout=120)
        if "Saved the game" not in out:
            raise RuntimeError(f"save-all did not confirm success: {out}")

    def stop(self, process):
        if not stand_java():
            return
        if fault("stop"):
            raise TimeoutError("injected: backend did not stop")
        try:
            rcon("stop")
        except Exception:
            pass
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if not stand_java():
                return
            time.sleep(0.5)
        raise TimeoutError("stand JVM did not stop within 120 s")

    def fence_pending_starts(self):
        # agent-process start is synchronous here: no scheduler can start a backend later.
        return None

    @contextmanager
    def workshop_saves_held(self):
        if any(app == "workshop" for _, app in stand_java()):
            raise RuntimeError("stop the workshop before taking a consistent snapshot")
        yield

    def start(self, goal):
        if stand_java():
            raise RuntimeError("refusing to start: a stand java still runs")
        app = self.app_of(goal.profile)
        cwd = APPS[app]
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("BLOCKFIELD_", "BF_"))
            and k not in ("APPIMAGE", "APPDIR")
        }
        env.update(
            _env_overrides(
                "game-env.json" if goal.profile == "game" else "workshop-env.json"
            )
        )
        env.update(
            BLOCKFIELD_PROFILE=goal.profile,
            BLOCKFIELD_BACKEND_PROFILE_FILE=str(LOCK / "profile.json"),
            BLOCKFIELD_BUILD_FILE=str(RELEASE),
        )
        props = cwd / "server.properties"
        level = "world"
        if goal.profile == "test":
            self.maps.extract(goal.candidate, cwd / "test-candidate")
            for n in ("test-world", "test-maps", "test-published"):
                shutil.rmtree(cwd / n, ignore_errors=True)
            shutil.copytree(APPS["game"] / "world", cwd / "test-world")
            shutil.copytree(cwd / "maps", cwd / "test-maps", symlinks=False)
            mid = (cwd / "test-candidate/map-id").read_text().strip()
            shutil.rmtree(cwd / "test-maps" / mid)
            shutil.copytree(cwd / "test-candidate/world", cwd / "test-maps" / mid)
            (cwd / "test-published/maps").mkdir(parents=True)
            shutil.copy2(
                cwd / "test-candidate/map.json",
                cwd / "test-published/maps" / f"{mid}.json",
            )
            env.update(
                BLOCKFIELD_MAPS_DIR=str(cwd / "test-maps"),
                BLOCKFIELD_PUBLISHED_MAPS_DIR=str(cwd / "test-published/maps"),
                BLOCKFIELD_SESSION_PURPOSE="test",
                BLOCKFIELD_STATS_EXPORT_ENABLED="false",
            )
            level = "test-world"
        elif goal.profile == "workshop":
            for n in ("test-world", "test-maps", "test-published", "test-candidate"):
                shutil.rmtree(cwd / n, ignore_errors=True)
        props.write_text(
            "".join(
                f"level-name={level}\n"
                if line.startswith("level-name=")
                else line + "\n"
                for line in props.read_text().splitlines()
            )
        )
        document = {
            "schema": 1,
            "profile": goal.profile,
            "generation": str(uuid4()),
            "session": goal.session,
            "release": goal.release,
        }
        if goal.candidate:
            document["candidate"] = goal.candidate
        atomic_write(
            LOCK / "profile.json", json.dumps(document, sort_keys=True).encode(), 0o644
        )
        (cwd / ".blockfield-ready.json").unlink(missing_ok=True)
        if fault("start"):
            raise RuntimeError("injected: start failed")
        command = [
            "agent-process",
            "start",
            "--owner",
            OWNER,
            "--name",
            f"bf-stand-{goal.profile}",
            "--cwd",
            str(cwd),
            "--log",
            str(cwd / "server.out"),
            "--",
            JAVA,
            "-Xms512m",
            f"-Xmx{XMX}",
            "-XX:+UseG1GC",
            "-XX:NativeMemoryTracking=summary",
            "-jar",
            "fabric-server-launch.jar",
            "nogui",
        ]
        if fault("start_crash"):
            # A real start error: the JVM cannot read its launcher jar and exits.
            command[command.index("fabric-server-launch.jar")] = "missing-launcher.jar"
        started = json.loads(subprocess.check_output(command, env=env, text=True))
        (STAND / "rig-process.json").write_text(
            json.dumps({"id": started["id"], "goal": goal.__dict__})
        )


def _env_overrides(name):
    path = STAND / name
    return json.loads(path.read_text()) if path.exists() else {}


def env():
    return {
        "BF_STATE_DIR": str(STAND / "controller"),
        "BF_GATE_FILE": str(STAND / "controller/gate.json"),
        "BF_PUBLIC_FILE": str(STAND / "controller/public.json"),
        "BF_ADMIN_DIR": str(STAND / "workshop-admin"),
        "BF_INBOX_DIR": str(STAND / "workshop-inbox"),
        "BF_WORKSHOP_DATA": str(APPS["workshop"]),
        "BF_TEMPLATES_DIR": str(APPS["game"] / "maps"),
        "BF_PUBLISHED_MAPS_DIR": str(STAND / "published"),
        "BF_CANDIDATES_DIR": str(STAND / "candidates"),
        "BF_MAP_BACKUPS_DIR": str(STAND / "map-backups"),
        "BF_GAME_MAPS_DIR": str(APPS["game"] / "blockfield/maps"),
        "BF_STEP_FOLLOW_S": os.environ.get("BF_STEP_FOLLOW_S", "12"),
        "BF_STEP_POLL_S": "2",
        "BF_START_TIMEOUT_S": os.environ.get("BF_START_TIMEOUT_S", "420"),
        "BF_DRAIN_TIMEOUT_S": os.environ.get("BF_DRAIN_TIMEOUT_S", "600"),
    }


if __name__ == "__main__":
    for k, v in env().items():
        os.environ.setdefault(k, v)
    for d in (
        "controller",
        "workshop-admin/results",
        "workshop-inbox",
        "published",
        "candidates",
        "map-backups",
        "evidence",
    ):
        (STAND / d).mkdir(parents=True, exist_ok=True)
    sys.exit(workshopctl.main(sys.argv[1:], backend_factory=StandBackend))
