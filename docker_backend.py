"""Docker Swarm adapter for Controller: one Dokploy app per profile, one shared /backend-lock.

Only a fixed command allowlist is ever executed (see allowed()). The containers themselves
serialize java through flock on <lock_dir>/backend.lock and start only the profile selected in
<lock_dir>/profile.json; this adapter selects the profile, scales apps and proves readiness.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from contextlib import contextmanager
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from uuid import UUID, uuid4

from controller import Goal, Process, atomic_write

# itzg runs `java ... -jar ./fabric-server-mc.1.21.1-loader.X-launcher.Y.jar`.
JAVA_PATTERN = r"fabric-server-mc\..*-launcher\..*\.jar"
RCON_COMMANDS = {("bf", "maintenance", "drain"), ("bf", "maintenance", "status"), ("save-all", "flush"),
                 ("save-off",), ("save-on",)}
READY_FILE = ".blockfield-ready.json"
UNKNOWN_SESSION = "00000000-0000-0000-0000-000000000000"
SERVICE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}")
CONTAINER = re.compile(r"[0-9a-f]{64}")
LABEL = "label=com.docker.swarm.service.name="


@dataclass(frozen=True)
class App:
    service: str
    data: Path


class DockerBackend:
    def __init__(self, apps: dict[str, App], lock_dir: Path, *, proc: Path = Path("/proc"),
                 stop_timeout: float = 300.0, poll: float = 2.0, min_free_bytes: int = 2 << 30):
        if set(apps) != {"game", "workshop"} or not all(SERVICE.fullmatch(a.service) for a in apps.values()):
            raise ValueError("game and workshop apps with valid service names required")
        if apps["game"].service == apps["workshop"].service:
            raise ValueError("profiles must use different apps")
        self.apps = apps
        self.lock_dir = Path(lock_dir)
        self.proc = Path(proc)
        self.stop_timeout = stop_timeout
        self.poll = poll
        self.min_free_bytes = min_free_bytes

    @classmethod
    def from_env(cls, env=os.environ) -> "DockerBackend":
        return cls({"game": App(env.get("BF_GAME_SERVICE", ""), Path(env.get("BF_GAME_DATA", "/srv/minecraft/lobby"))),
                    "workshop": App(env.get("BF_WORKSHOP_SERVICE", ""),
                                    Path(env.get("BF_WORKSHOP_DATA", "/srv/minecraft/workshop/data")))},
                   Path(env.get("BF_LOCK_DIR", "/srv/minecraft/backend-lock")),
                   min_free_bytes=int(env.get("BF_MIN_FREE_BYTES", 2 << 30)))

    # ------------------------------------------------------------------ commands

    def allowed(self, argv: list[str]) -> bool:
        services = {app.service for app in self.apps.values()}
        match argv:
            case ["docker", "service", "scale", "--detach", target]:
                service, _, replicas = target.rpartition("=")
                return service in services and replicas in {"0", "1"}
            case ["docker", "service", "ps", service, "--filter", "desired-state=running", "--format", "{{.CurrentState}}"]:
                return service in services
            case ["docker", "ps", "--no-trunc", "--filter", label, "--format", "{{.ID}}"]:
                return label.startswith(LABEL) and label[len(LABEL):] in services
            case ["docker", "exec", container, "rcon-cli", *command]:
                return bool(CONTAINER.fullmatch(container)) and tuple(command) in RCON_COMMANDS
            case ["pgrep", "-f", pattern]:
                return pattern == JAVA_PATTERN
        return False

    def run(self, argv: list[str], *, ok_codes=(0,)) -> str:
        if not self.allowed(argv):
            raise PermissionError(f"command not allowlisted: {argv[:4]}")
        result = subprocess.run(argv, capture_output=True, text=True, timeout=60, check=False)
        if result.returncode not in ok_codes:
            raise RuntimeError(f"{argv[0]} {argv[1]} failed ({result.returncode}): {result.stderr.strip()[:300]}")
        return result.stdout

    # ------------------------------------------------------------------ inventory

    def containers(self, profile: str) -> list[str]:
        out = self.run(["docker", "ps", "--no-trunc", "--filter", LABEL + self.apps[profile].service, "--format", "{{.ID}}"])
        return [line for line in out.split() if CONTAINER.fullmatch(line)]

    def tasks(self, profile: str) -> int:
        out = self.run(["docker", "service", "ps", self.apps[profile].service, "--filter", "desired-state=running",
                        "--format", "{{.CurrentState}}"])
        return sum(1 for line in out.splitlines() if line.strip())

    def java_pids(self) -> list[int]:
        # pgrep -f also matches itzg's mc-server-runner, whose arguments carry the whole java command line.
        pids = []
        for pid in self.run(["pgrep", "-f", JAVA_PATTERN], ok_codes=(0, 1)).split():
            try:
                if (self.proc / pid / "comm").read_text().strip() == "java":
                    pids.append(int(pid))
            except OSError:
                continue  # exited between pgrep and /proc read
        return pids

    def container_of(self, pid: int) -> str | None:
        try:
            found = CONTAINER.findall((self.proc / str(pid) / "cgroup").read_text())
        except OSError:
            return None
        return found[-1] if found else ""

    def inventory(self, *, tasks: bool = True) -> dict:
        owners = {cid: profile for profile in self.apps for cid in self.containers(profile)}
        result = {profile: {"tasks": self.tasks(profile) if tasks else None, "java": 0} for profile in self.apps}
        result["unknown"] = 0
        for pid in self.java_pids():
            container = self.container_of(pid)
            if container is None:
                continue  # exited between pgrep and /proc read
            profile = owners.get(container)
            if profile is None:
                result["unknown"] += 1
            else:
                result[profile]["java"] += 1
        return result

    def selection(self) -> dict | None:
        """Profile the host selected; None only before the first controller start. Corrupt raises."""
        path = self.lock_dir / "profile.json"
        try:
            data = json.loads(path.read_bytes()[:16384])
        except FileNotFoundError:
            return None
        if (not isinstance(data, dict) or data.get("schema") != 1 or data.get("profile") not in self.apps
                or not isinstance(data.get("release"), str) or not data["release"]):
            raise ValueError("invalid profile.json")
        UUID(data.get("generation"))
        if data.get("session") is not None:
            UUID(data["session"])
        return data

    def readiness(self, profile: str) -> dict | None:
        try:
            data = json.loads((self.apps[profile].data / READY_FILE).read_bytes()[:16384])
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _goal(profile: str, release, session) -> Goal:
        try:
            return Goal(profile, release, None if profile == "game" else session)
        except (ValueError, TypeError):
            return Goal(profile, "unknown", None if profile == "game" else UNKNOWN_SESSION)

    # ------------------------------------------------------------------ Backend protocol

    def preflight(self, goal: Goal) -> None:
        if goal.profile not in self.apps:
            raise ValueError(f"profile {goal.profile} has no app in this stage")
        if self.lock_dir.is_symlink() or not self.lock_dir.is_dir():
            raise FileNotFoundError("backend lock directory missing")
        data = self.apps[goal.profile].data
        if data.is_symlink() or not data.is_dir():
            raise FileNotFoundError(f"data directory for {goal.profile} missing")
        if shutil.disk_usage(data).free < self.min_free_bytes:
            raise OSError("not enough free space for the backend data")

    def observe(self) -> list[Process]:
        inventory = self.inventory()
        if inventory["unknown"]:
            raise RuntimeError("full backend process outside the configured apps")
        selection = self.selection()
        selected = selection["profile"] if selection else "game"
        processes = []
        for profile in self.apps:
            counts = inventory[profile]
            if counts["java"] > 1:
                raise RuntimeError(f"{profile} runs several java processes")
            # A task of an unselected profile waits in start-container.sh without java.
            if counts["java"] == 0 and not (counts["tasks"] and profile == selected):
                continue
            ready = self.readiness(profile)
            if selection and selection["profile"] == profile:
                goal = self._goal(profile, selection["release"], selection.get("session"))
                expected = {key: selection.get(key) for key in ("profile", "generation", "session", "release")}
                if profile == "game" and ready and ready.get("release"):
                    # Game images are deployed as :latest by release tags, outside the controller: follow the build.
                    goal = self._goal(profile, ready["release"], selection.get("session"))
                    expected["release"] = ready["release"]
            elif selection is None and ready and ready.get("profile") == profile and ready.get("generation") is None:
                # Adoption of a game started before the controller existed.
                goal = self._goal(profile, ready.get("release"), ready.get("session"))
                expected = ready
            else:
                goal, expected = self._goal(profile, "unknown", UNKNOWN_SESSION), None
            processes.append(Process(goal, counts["java"] == 1 and expected is not None and ready == expected))
        return processes

    def _container(self, profile: str) -> str | None:
        ids = self.containers(profile)
        if len(ids) > 1:
            raise RuntimeError(f"several {profile} containers")
        return ids[0] if ids else None

    def _rcon(self, profile: str, *command: str) -> str | None:
        """None when the profile runs no java (nothing to drain or save)."""
        if self.inventory(tasks=False)[profile]["java"] == 0:
            return None
        container = self._container(profile)
        if container is None:
            raise RuntimeError(f"{profile} java without a container")
        return self.run(["docker", "exec", container, "rcon-cli", *command])

    @contextmanager
    def workshop_saves_held(self):
        """A consistent copy of the workshop's region files (map candidates): no autosave writes meanwhile."""
        if self._rcon("workshop", "save-off") is None:
            yield  # not running: the files are already at rest
            return
        try:
            self._rcon("workshop", "save-all", "flush")
            yield
        finally:
            self._rcon("workshop", "save-on")

    def request_drain(self) -> None:
        self._rcon("game", "bf", "maintenance", "drain")

    def drain_progress(self) -> dict | None:
        out = self._rcon("game", "bf", "maintenance", "status")
        if out is None:
            return None
        match = re.search(r"\bdraining=(true|false)\s+rooms=(\d+)\s+ranked=(\d+)\s+pending=(\d+)\s+"
                           r"healthy=(true|false)\b.*\bdrained=(true|false)\b", out)
        if not match:
            raise RuntimeError("unexpected /bf maintenance status output")
        draining, rooms, ranked, pending, healthy, drained = match.groups()
        return {"draining": draining == "true", "rooms": int(rooms), "ranked": int(ranked),
                "pending": int(pending), "healthy": healthy == "true", "drained": drained == "true"}

    def save_and_freeze(self, process: Process) -> None:
        self._rcon(process.goal.profile, "save-all", "flush")

    def _scale(self, profile: str, replicas: int) -> None:
        self.run(["docker", "service", "scale", "--detach", f"{self.apps[profile].service}={replicas}"])

    def _wait_gone(self, profiles: list[str]) -> None:
        deadline = time.monotonic() + self.stop_timeout
        while True:
            inventory = self.inventory()
            if all(inventory[p]["tasks"] == 0 for p in profiles) and \
                    sum(inventory[p]["java"] for p in self.apps) + inventory["unknown"] == 0:
                return
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{profiles} still running")
            time.sleep(self.poll)

    def stop(self, process: Process) -> None:
        self._scale(process.goal.profile, 0)
        self._wait_gone([process.goal.profile])

    def fence_pending_starts(self) -> None:
        selection = self.selection()
        others = [p for p in self.apps if p != (selection["profile"] if selection else "game")]
        for profile in others:
            self._scale(profile, 0)
        deadline = time.monotonic() + self.stop_timeout
        while True:
            inventory = self.inventory()
            if all(inventory[p]["tasks"] == 0 and inventory[p]["java"] == 0 for p in others):
                return
            if time.monotonic() >= deadline:
                raise TimeoutError(f"cannot fence {others}")
            time.sleep(self.poll)

    def start(self, goal: Goal) -> None:
        document = {"schema": 1, "profile": goal.profile, "generation": str(uuid4()),
                    "session": goal.session, "release": goal.release}
        atomic_write(self.lock_dir / "profile.json", json.dumps(document, sort_keys=True).encode(), 0o644)
        (self.apps[goal.profile].data / READY_FILE).unlink(missing_ok=True)
        self._scale(goal.profile, 1)
