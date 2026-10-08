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
import re
import shutil
import socket
import subprocess
import sys
import time
import fcntl
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4

from paths import STAND, RIG, WORKSHOP, WORKSPACE, owner, java

sys.path[:0] = [str(WORKSPACE / "scripts/dev"), str(WORKSHOP), str(RIG)]
from controller import Goal, Process, atomic_write  # noqa: E402
import workshopctl  # noqa: E402

OWNER = owner()
JAVA = java()
APPS = {"game": (STAND / "game").resolve(), "workshop": (STAND / "workshop").resolve()}
LOCK = STAND / "lock"
RELEASE = LOCK / "build.json"
XMX = os.environ.get(
    "STAND_XMX", "1536m"
)  # keep modest: this dev box runs other agents' JVMs too


@contextmanager
def app_lock(app):
    directory = LOCK / app
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "start.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


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


def properties(app):
    return {
        key.strip(): value.strip()
        for line in (APPS[app] / "server.properties").read_text().splitlines()
        if not line.lstrip().startswith(("#", "!")) and "=" in line
        for key, value in [line.split("=", 1)]
    }


def rcon(app, command, timeout=60):
    from rcon import Rcon

    process = registered(app)
    if not process or not process[1]["live"]:
        raise RuntimeError("RCON requires a verified owned backend")
    props = properties(app)
    if props.get("enable-rcon") != "true" or not props.get("rcon.password"):
        raise ValueError("stand RCON is not configured")
    listeners = subprocess.check_output(
        ["ss", "-ltnp", f"sport = :{int(props['rcon.port'])}"], text=True, timeout=10
    )
    if set(map(int, re.findall(r"pid=(\d+)", listeners))) != {process[1]["pid"]}:
        raise RuntimeError("RCON listener does not belong to the verified backend")
    r = Rcon(
        "127.0.0.1", int(props["rcon.port"]), props["rcon.password"], timeout=timeout
    )
    r.s.settimeout(timeout)
    try:
        return r.cmd(command)
    finally:
        r.s.close()


def stand_java():
    """[(pid, app)] for every Java process in exactly a managed app directory."""
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if (entry / "comm").read_text().strip() != "java" and Path(
                os.readlink(entry / "exe")
            ).name != "java":
                continue
            cwd = Path(os.readlink(entry / "cwd"))
        except OSError:
            continue
        for app, path in APPS.items():
            if cwd == path:
                found.append((int(entry.name), app))
    return found


def native_identity(pid):
    from linux_process import identity

    return identity(pid)


def native_command(pid):
    return [
        os.fsdecode(argument)
        for argument in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        if argument
    ]


def registered(app):
    """Only a matching app/session/native identity authorizes access or a stop."""
    path = APPS[app] / "rig-process.json"
    legacy = STAND / "rig-process.json"
    source = path if path.exists() else legacy
    if not source.exists():
        return None
    if source.is_symlink():
        raise ValueError("invalid stand process record")
    record = json.loads(source.read_text())
    goal = Goal(**record["goal"])
    if StandBackend.app_of(goal.profile) != app:
        if source == legacy:
            return None
        raise ValueError("stand process record belongs to another backend")
    process_id = record["id"]
    if not isinstance(process_id, str) or not re.fullmatch(
        r"[A-Za-z0-9-]+", process_id
    ):
        raise ValueError("invalid stand process ID")
    status = json.loads(
        subprocess.check_output(
            ["agent-process", "status", process_id], text=True, timeout=10
        )
    )
    command = status.get("command", [])
    if (
        status.get("id") != process_id
        or status.get("owner") != OWNER
        or Path(status.get("cwd", "")).resolve() != APPS[app]
        or not isinstance(command, list)
        or "-jar" not in command
        or command[command.index("-jar") + 1 :]
        not in (
            ["fabric-server-launch.jar", "nogui"],
            ["missing-launcher.jar", "nogui"] if status.get("live") is False else [],
        )
        or type(status.get("live")) is not bool
    ):
        raise RuntimeError("stand process identity/owner does not match this backend")
    if status["live"]:
        current = native_identity(status["pid"])
        if (
            not current
            or str(current["start_ticks"]) != str(status.get("start_ticks"))
            or Path(current["cwd"]).resolve() != APPS[app]
            or Path(current["exe"]).resolve() != Path(command[0]).resolve()
            or native_command(status["pid"]) != command
            or (status["pid"], app) not in stand_java()
        ):
            raise RuntimeError("stand native process identity changed")
    if source == legacy:
        atomic_write(path, json.dumps(record).encode(), 0o600)
        legacy.unlink()
    return record, status


class StandBackend:
    apps = APPS
    maps = None

    @staticmethod
    def app_of(profile):
        return "workshop" if profile == "test" else profile

    def selection(self, profile="workshop"):
        app = self.app_of(profile)
        path = LOCK / app / "profile.json"
        legacy = not path.exists()
        if legacy:
            path = LOCK / "profile.json"
        if path.is_symlink():
            raise ValueError("invalid stand profile file")
        try:
            data = json.loads(path.read_text())
        except FileNotFoundError:
            return None
        if (
            not isinstance(data, dict)
            or type(data.get("schema")) is not int
            or data["schema"] != 1
        ):
            raise ValueError("invalid stand profile file")
        goal = Goal(
            data["profile"], data["release"], data.get("session"), data.get("candidate")
        )
        if self.app_of(goal.profile) != app:
            if legacy:
                return None
            raise ValueError("stand profile belongs to another backend")
        if str(UUID(data["generation"])) != data["generation"]:
            raise ValueError("invalid stand profile generation")
        return data

    def readiness(self, app):
        try:
            data = json.loads((APPS[app] / ".blockfield-ready.json").read_text())
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def preflight(self, goal):
        if goal.profile == "game":
            raise ValueError("Game lifecycle requires explicit stand bootstrap")
        if fault("preflight"):
            raise OSError("injected preflight fault")
        if goal.profile == "test":
            if self.maps is None:
                raise ValueError("test needs the candidate store")
            if not any(
                p.goal.profile == "game" and p.ready and p.goal.release == goal.release
                for p in self.observe()
            ):
                raise ValueError("test release must match the ready Game backend")
            self.test_seed()
        self.check_data(goal)

    def check_data(self, goal):
        data = APPS[self.app_of(goal.profile)]
        if not data.is_dir() or not LOCK.is_dir():
            raise FileNotFoundError(
                "stand data/lock dir missing -- run the bootstrap in README.md first"
            )
        minimum = int(os.environ.get("BF_MIN_FREE_BYTES", 2 << 30))
        probe = Path(os.environ.get("STAND_DISK_PROBE", data))
        if shutil.disk_usage(probe).free < minimum:
            raise OSError("not enough free space for the backend data")

    def test_seed(self):
        seed = Path(os.environ.get("STAND_TEST_SEED", STAND / "seed/world")).resolve()
        if not seed.is_dir() or any(p.is_symlink() for p in seed.rglob("*")):
            raise ValueError(
                "immutable stand lobby seed is missing or contains symlinks"
            )
        for app in APPS.values():
            world = (app / "world").resolve()
            if seed == world or world in seed.parents:
                raise ValueError("playtest seed must not be a live backend world")
        return seed

    def observe(self):
        java_procs = stand_java()
        processes = []
        for app in APPS:
            count = sum(1 for _, a in java_procs if a == app)
            if count > 1:
                raise RuntimeError(f"{app} runs several java processes")
            if count == 0:
                continue
            try:
                selection = self.selection(app)
                process = registered(app)
            except (OSError, ValueError, TypeError, KeyError, RuntimeError):
                processes.append(
                    Process(
                        Goal(
                            app,
                            "unknown",
                            None
                            if app == "game"
                            else "00000000-0000-0000-0000-000000000000",
                        ),
                        False,
                    )
                )
                continue
            selected = selection["profile"] if selection else app
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
            owned = (
                process is not None
                and process[1]["live"]
                and (process[1]["pid"], app) in java_procs
            )
            processes.append(
                Process(goal, owned and expected is not None and ready == expected)
            )
        return processes

    def save_and_freeze(self, process):
        if process.goal.profile == "game":
            raise ValueError("Workshop controller cannot save or stop Game")
        if fault("save"):
            raise RuntimeError("injected: save-all did not confirm success")
        out = rcon("workshop", "save-all flush", timeout=120)
        if "Saved the game" not in out:
            raise RuntimeError(f"save-all did not confirm success: {out}")

    def stop(self, process):
        if process.goal.profile == "game":
            raise ValueError("Workshop controller cannot save or stop Game")
        with app_lock("workshop"):
            self.stop_workshop()

    def stop_workshop(self):
        if not any(app == "workshop" for _, app in stand_java()):
            return
        process = registered("workshop")
        if not process or not process[1]["live"]:
            raise RuntimeError("cannot stop an unregistered Workshop process")
        if fault("stop"):
            raise TimeoutError("injected: backend did not stop")
        subprocess.run(
            [
                "agent-process",
                "stop",
                "--owner",
                OWNER,
                "--grace-seconds",
                "120",
                "--no-escalate",
                process[0]["id"],
            ],
            check=True,
            timeout=130,
        )
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            current = native_identity(process[1]["pid"])
            if current is None or str(current["start_ticks"]) != str(
                process[1]["start_ticks"]
            ):
                if any(app == "workshop" for _, app in stand_java()):
                    raise RuntimeError(
                        "another Workshop process remains after the owned stop"
                    )
                return
            time.sleep(0.5)
        raise TimeoutError("Workshop JVM did not stop within 120 s")

    def fence_pending_starts(self):
        with app_lock("workshop"):
            self.stop_workshop()

    @contextmanager
    def workshop_saves_held(self):
        if any(app == "workshop" for _, app in stand_java()):
            raise RuntimeError("stop the workshop before taking a consistent snapshot")
        yield

    def start(self, goal):
        self.preflight(goal)
        with app_lock("workshop"):
            self._start_app(goal)

    def bootstrap_game(self, release):
        """Stand setup only; the Workshop controller never calls this API."""
        with app_lock("game"):
            self._start_app(Goal("game", release))

    def _start_app(self, goal):
        app = self.app_of(goal.profile)
        self.check_data(goal)
        if any(current == app for _, current in stand_java()):
            raise RuntimeError(f"refusing to start: {app} Java already runs")
        process = registered(app)
        if process and process[1]["live"]:
            raise RuntimeError("refusing to replace a live registered backend")
        cwd = APPS[app]
        props = properties(app)
        ports = (int(props["server-port"]), int(props["rcon.port"]))
        if len(set(ports)) != 2 or any(not 1 <= port <= 65535 for port in ports):
            raise ValueError("stand TCP and RCON ports must be distinct and valid")
        other = "workshop" if app == "game" else "game"
        if (APPS[other] / "server.properties").is_file():
            other_props = properties(other)
            if set(ports) & {
                int(other_props["server-port"]),
                int(other_props["rcon.port"]),
            }:
                raise ValueError("Game and Workshop must use separate TCP/RCON ports")
        for port in ports:
            listeners = subprocess.check_output(
                ["ss", "-ltnp", f"sport = :{port}"], text=True, timeout=10
            )
            with socket.socket() as probe:
                probe.settimeout(0.25)
                answering = probe.connect_ex(("127.0.0.1", port)) == 0
            if len(listeners.splitlines()) > 1 or answering:
                raise RuntimeError(
                    f"refusing to start: configured stand port {port} is occupied"
                )
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
            BLOCKFIELD_BACKEND_PROFILE_FILE=str(LOCK / app / "profile.json"),
            BLOCKFIELD_BUILD_FILE=str(
                cwd / "build.json" if (cwd / "build.json").is_file() else RELEASE
            ),
            BLOCKFIELD_MAP_ACTIVE_DIR=str(STAND / "maps-active"),
        )
        if app == "workshop":
            env.update(
                BLOCKFIELD_STATS_EXPORT_ENABLED="false",
                BLOCKFIELD_WORKSHOP_DRAFTS_DIR=str(STAND / "maps-active/drafts"),
                BLOCKFIELD_WORKSHOP_SESSION_FILE=str(
                    STAND / "workshop-admin/session.json"
                ),
                BLOCKFIELD_MEMBERS_FILE=str(STAND / "workshop-admin/members.json"),
                BLOCKFIELD_WORKSHOP_CANDIDATES_FILE=str(
                    STAND / "workshop-admin/candidates.json"
                ),
            )
        if (
            goal.profile == "workshop"
            and getattr(self.maps, "canonical_sessions", False) is True
        ):
            session = json.loads((STAND / "workshop-admin/session.json").read_text())
            if (
                type(session.get("schema")) is not int
                or session["schema"] != 1
                or session.get("id") != goal.session
            ):
                raise ValueError("Workshop session/profile mismatch before start")
            self.maps.prepare_session(
                goal.session, session.get("maps"), resume_only=True
            )
        props = cwd / "server.properties"
        level = "world"
        if goal.profile == "test":
            self.maps.extract(goal.candidate, cwd / "test-candidate")
            for n in ("test-world", "test-maps", "test-published"):
                shutil.rmtree(cwd / n, ignore_errors=True)
            shutil.copytree(self.test_seed(), cwd / "test-world")
            shutil.copytree(cwd / "maps", cwd / "test-maps", symlinks=False)
            mid = (cwd / "test-candidate/map-id").read_text().strip()
            if not re.fullmatch(r"[a-z0-9_]{1,32}", mid):
                raise ValueError("invalid candidate map ID")
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
        if "level-name" not in properties(app):
            with props.open("a") as handle:
                handle.write("\nlevel-name=world\n")
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
            LOCK / app / "profile.json",
            json.dumps(document, sort_keys=True).encode(),
            0o644,
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
        started = json.loads(
            subprocess.check_output(command, env=env, text=True, timeout=30)
        )
        atomic_write(
            cwd / "rig-process.json",
            json.dumps({"id": started["id"], "goal": goal.__dict__}).encode(),
            0o600,
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
        "BF_MAP_ARCHIVE_DIR": str(STAND / "archive"),
        "BF_MAP_ACTIVE_DIR": str(STAND / "maps-active"),
        "BF_MAP_PACKAGES_DIR": str(STAND / "archive/packages"),
        "BF_LEGACY_CANDIDATES_DIR": str(STAND / "candidates"),
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
    if len(sys.argv) == 3 and sys.argv[1] == "bootstrap-game":
        StandBackend().bootstrap_game(sys.argv[2])
        sys.exit(0)
    sys.exit(workshopctl.main(sys.argv[1:], backend_factory=StandBackend))
