"""Docker Swarm adapter for the persistent Workshop beside the independently deployed Game.

Only a fixed command allowlist is ever executed (see allowed()). The containers themselves
serialize each app through its own flock and profile in <lock_dir>/<app>/;
this adapter mutates only Workshop, follows Game's immutable image and proves readiness.
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
import map_package

# itzg runs `java ... -jar ./fabric-server-mc.1.21.1-loader.X-launcher.Y.jar`.
JAVA_PATTERN = r"fabric-server-mc\..*-launcher\..*\.jar"
RCON_COMMANDS = {
    ("save-all", "flush"),
    ("save-off",),
    ("save-on",),
}
READY_FILE = ".blockfield-ready.json"
UNKNOWN_SESSION = "00000000-0000-0000-0000-000000000000"
SERVICE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}")
CONTAINER = re.compile(r"[0-9a-f]{64}")
LABEL = "label=com.docker.swarm.service.name="


@dataclass(frozen=True)
class App:
    service: str
    data: Path


def _has_symlink(path: Path) -> bool:
    curr = path
    while True:
        if curr.is_symlink():
            return True
        if curr.parent == curr:
            break
        curr = curr.parent
    return False


def _check_readonly_view(source: Path, view: Path) -> None:
    source_str = str(source)
    view_str = str(view)
    if source_str == view_str:
        raise ValueError(
            "canonical workshop readonly view mount must be a separate path"
        )
    for p, s in ((source, source_str), (view, view_str)):
        if (
            not p.is_absolute()
            or s != os.path.normpath(s)
            or ".." in p.parts
            or s == "/"
        ):
            raise ValueError(
                "canonical workshop bind mount paths must be explicit and absolute"
            )
    if _has_symlink(source) or _has_symlink(view):
        raise ValueError("canonical workshop bind mounts cannot use symlinks")
    try:
        if not os.path.samefile(source, view):
            raise ValueError(
                "canonical workshop readonly view mount does not match source"
            )
        stat = os.statvfs(view)
        if not (stat.f_flag & os.ST_RDONLY):
            raise ValueError("canonical workshop readonly view mount is not read-only")
    except OSError as err:
        raise ValueError(
            "canonical workshop bind mounts are missing or unsafe"
        ) from err


class DockerBackend:
    def __init__(
        self,
        apps: dict[str, App],
        lock_dir: Path,
        *,
        proc: Path = Path("/proc"),
        stop_timeout: float = 300.0,
        poll: float = 2.0,
        min_free_bytes: int = 2 << 30,
        admin_dir: Path = Path("/srv/minecraft/workshop/admin"),
        proxy_service: str | None = None,
        map_active_readonly_dir: Path | str | None = None,
        admin_readonly_dir: Path | str | None = None,
    ):
        if set(apps) != {"game", "workshop"} or not all(
            SERVICE.fullmatch(a.service) for a in apps.values()
        ):
            raise ValueError("game and workshop apps with valid service names required")
        if apps["game"].service == apps["workshop"].service:
            raise ValueError("profiles must use different apps")
        self.apps = apps
        self.lock_dir = Path(lock_dir)
        self.proc = Path(proc)
        self.stop_timeout = stop_timeout
        self.poll = poll
        self.min_free_bytes = min_free_bytes
        self.admin_dir = Path(admin_dir)
        if proxy_service is not None and (
            not SERVICE.fullmatch(proxy_service)
            or proxy_service in {app.service for app in apps.values()}
        ):
            raise ValueError("separate configured proxy service required")
        self.proxy_service = proxy_service
        self.map_active_readonly_dir = (
            Path(map_active_readonly_dir)
            if map_active_readonly_dir is not None
            else None
        )
        self.admin_readonly_dir = (
            Path(admin_readonly_dir) if admin_readonly_dir is not None else None
        )
        # candidates.MapStore (workshopctl wires it): extracts a candidate for the test profile.
        self.maps = None

    @staticmethod
    def app_of(profile: str) -> str:
        """The playtest (test) runs in the workshop app: same data volume, still one backend."""
        return "workshop" if profile == "test" else profile

    @classmethod
    def from_env(cls, env=os.environ) -> "DockerBackend":
        return cls(
            {
                "game": App(
                    env.get("BF_GAME_SERVICE", ""),
                    Path(env.get("BF_GAME_DATA", "/srv/minecraft/lobby")),
                ),
                "workshop": App(
                    env.get("BF_WORKSHOP_SERVICE", ""),
                    Path(env.get("BF_WORKSHOP_DATA", "/srv/minecraft/workshop/data")),
                ),
            },
            Path(env.get("BF_LOCK_DIR", "/srv/minecraft/backend-lock")),
            min_free_bytes=int(env.get("BF_MIN_FREE_BYTES", 2 << 30)),
            admin_dir=Path(env.get("BF_ADMIN_DIR", "/srv/minecraft/workshop/admin")),
            proxy_service=env.get("BF_PROXY_SERVICE") or None,
            map_active_readonly_dir=(
                Path(env["BF_MAP_ACTIVE_READONLY_DIR"])
                if env.get("BF_MAP_ACTIVE_READONLY_DIR")
                else None
            ),
            admin_readonly_dir=(
                Path(env["BF_ADMIN_READONLY_DIR"])
                if env.get("BF_ADMIN_READONLY_DIR")
                else None
            ),
        )

    # ------------------------------------------------------------------ commands

    def allowed(self, argv: list[str]) -> bool:
        services = {app.service for app in self.apps.values()}
        observed_services = services | (
            {self.proxy_service} if self.proxy_service else set()
        )
        match argv:
            case [
                "docker",
                "service",
                "inspect",
                service,
                "--format",
                "{{json .Spec.TaskTemplate.ContainerSpec}}",
            ]:
                return service in services
            case ["docker", "service", "scale", "--detach", target]:
                service, _, replicas = target.rpartition("=")
                return service == self.apps["workshop"].service and replicas in {
                    "0",
                    "1",
                }
            case ["docker", "service", "update", "--detach", "--image", image, service]:
                return service == self.apps["workshop"].service and bool(
                    re.fullmatch(
                        r"ghcr\.io/blockfield/blockfield-server(?::[A-Za-z0-9_.-]+)?@sha256:[0-9a-f]{64}",
                        image,
                    )
                )
            case [
                "docker",
                "service",
                "inspect",
                service,
                "--format",
                "{{json .Spec.Mode}}",
            ]:
                return self.proxy_service is not None and service == self.proxy_service
            case [
                "docker",
                "service",
                "ps",
                service,
                "--filter",
                "desired-state=running",
                "--format",
                "{{.CurrentState}}",
            ]:
                return service in observed_services
            case [
                "docker",
                "ps",
                "--no-trunc",
                "--filter",
                label,
                "--format",
                "{{.ID}}",
            ]:
                return (
                    label.startswith(LABEL) and label[len(LABEL) :] in observed_services
                )
            case ["docker", "exec", container, "rcon-cli", *command]:
                return bool(CONTAINER.fullmatch(container)) and (
                    tuple(command) in RCON_COMMANDS
                    or len(command) == 4
                    and command[:3] == ["bf", "workshop", "export-limbo"]
                    and re.fullmatch(
                        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                        command[3],
                    )
                )
            case ["pgrep", "-f", pattern]:
                return pattern == JAVA_PATTERN
        return False

    def run(self, argv: list[str], *, ok_codes=(0,)) -> str:
        if not self.allowed(argv):
            raise PermissionError(f"command not allowlisted: {argv[:4]}")
        result = subprocess.run(
            argv, capture_output=True, text=True, timeout=60, check=False
        )
        if result.returncode not in ok_codes:
            raise RuntimeError(
                f"{argv[0]} {argv[1]} failed ({result.returncode}): {result.stderr.strip()[:300]}"
            )
        return result.stdout

    # ------------------------------------------------------------------ inventory

    def proxy_stopped(self) -> bool:
        if self.proxy_service is None:
            raise ValueError("BF_PROXY_SERVICE is required for stopped Limbo selection")
        mode = json.loads(
            self.run(
                [
                    "docker",
                    "service",
                    "inspect",
                    self.proxy_service,
                    "--format",
                    "{{json .Spec.Mode}}",
                ]
            )
        )
        replicated = mode.get("Replicated") if isinstance(mode, dict) else None
        if (
            not isinstance(replicated, dict)
            or type(replicated.get("Replicas")) is not int
            or replicated["Replicas"] != 0
        ):
            return False
        tasks = self.run(
            [
                "docker",
                "service",
                "ps",
                self.proxy_service,
                "--filter",
                "desired-state=running",
                "--format",
                "{{.CurrentState}}",
            ]
        )
        containers = self.run(
            [
                "docker",
                "ps",
                "--no-trunc",
                "--filter",
                LABEL + self.proxy_service,
                "--format",
                "{{.ID}}",
            ]
        )
        return not tasks.strip() and not containers.strip()

    def export_limbo(self, job: str) -> None:
        if str(UUID(job)) != job or self.maps is None or self.maps.limbo is None:
            raise ValueError("configured native Limbo export job required")
        self._check_workshop_mounts()
        spec = self.container_spec("workshop")
        target = self.container_env(spec).get("BLOCKFIELD_WORKSHOP_EXPORT_DIR", "")
        if (
            not Path(target).is_absolute()
            or str(Path(target)) != target
            or ".." in Path(target).parts
        ):
            raise ValueError("absolute native Limbo export directory required")
        mounts = [
            mount for mount in spec.get("Mounts", []) if mount.get("Target") == target
        ]
        if (
            len(mounts) != 1
            or mounts[0].get("Type") != "bind"
            or mounts[0].get("Source") != str(self.maps.limbo.jobs)
            or mounts[0].get("ReadOnly", False) is not False
        ):
            raise ValueError("isolated HDD Limbo export jobs mount required")
        containers = self.containers("workshop")
        selection = self.selection()
        if (
            len(containers) != 1
            or selection is None
            or selection.get("profile") != "workshop"
        ):
            raise ValueError(
                "one selected workshop container required for native export"
            )
        self.run(
            [
                "docker",
                "exec",
                containers[0],
                "rcon-cli",
                "bf",
                "workshop",
                "export-limbo",
                job,
            ]
        )

    def containers(self, profile: str) -> list[str]:
        out = self.run(
            [
                "docker",
                "ps",
                "--no-trunc",
                "--filter",
                LABEL + self.apps[profile].service,
                "--format",
                "{{.ID}}",
            ]
        )
        return [line for line in out.split() if CONTAINER.fullmatch(line)]

    def tasks(self, profile: str) -> int:
        out = self.run(
            [
                "docker",
                "service",
                "ps",
                self.apps[profile].service,
                "--filter",
                "desired-state=running",
                "--format",
                "{{.CurrentState}}",
            ]
        )
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
        owners = {
            cid: profile for profile in self.apps for cid in self.containers(profile)
        }
        result = {
            profile: {"tasks": self.tasks(profile) if tasks else None, "java": 0}
            for profile in self.apps
        }
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

    def selection(self, profile: str = "workshop") -> dict | None:
        """Profile the host selected; None only before the first controller start. Corrupt raises."""
        app = self.app_of(profile)
        path = self.lock_dir / app / "profile.json"
        legacy = not path.exists()
        if legacy:
            ready = self.readiness(app)
            if app == "game" and ready and ready["generation"] is None:
                return None
            path = self.lock_dir / "profile.json"
        if path.is_symlink():
            raise ValueError("invalid profile.json")
        try:
            data = json.loads(path.read_bytes()[:16384])
        except FileNotFoundError:
            return None
        if (
            not isinstance(data, dict)
            or data.get("schema") != 1
            or data.get("profile") not in {*self.apps, "test"}
            or not isinstance(data.get("release"), str)
            or not data["release"]
        ):
            raise ValueError("invalid profile.json")
        UUID(data.get("generation"))
        if data.get("session") is not None:
            UUID(data["session"])
        if self.app_of(data["profile"]) != app:
            if legacy:
                return None
            raise ValueError("profile.json belongs to another backend")
        if data.get("modeId") is not None and (
            data["profile"] != "test"
            or data["modeId"] not in map_package.SUPPORTED_MODES
        ):
            raise ValueError("invalid selected test mode")
        return data

    def readiness(self, profile: str) -> dict | None:
        try:
            data = json.loads(
                (self.apps[profile].data / READY_FILE).read_bytes()[:16384]
            )
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or set(data) != {
            "profile",
            "generation",
            "session",
            "release",
        }:
            return None
        try:
            if (
                data["profile"] not in {profile, "test"}
                or profile == "game"
                and data["profile"] != "game"
            ):
                return None
            if not isinstance(data["release"], str) or not data["release"].strip():
                return None
            if (
                data["generation"] is not None
                and str(UUID(data["generation"])) != data["generation"]
            ):
                return None
            if profile == "game":
                if data["session"] is not None:
                    return None
            elif str(UUID(data["session"])) != data["session"]:
                return None
        except (ValueError, TypeError, AttributeError):
            return None
        return data

    @staticmethod
    def _goal(profile: str, release, session, candidate=None, mode_id=None) -> Goal:
        try:
            return Goal(
                profile,
                release,
                None if profile == "game" else session,
                candidate if profile == "test" else None,
                mode_id if profile == "test" else None,
            )
        except (ValueError, TypeError):
            if profile == "test":
                return Goal("workshop", "unknown", UNKNOWN_SESSION)
            return Goal(
                profile, "unknown", None if profile == "game" else UNKNOWN_SESSION
            )

    # ------------------------------------------------------------------ Backend protocol

    def container_spec(self, profile: str) -> dict:
        value = json.loads(
            self.run(
                [
                    "docker",
                    "service",
                    "inspect",
                    self.apps[self.app_of(profile)].service,
                    "--format",
                    "{{json .Spec.TaskTemplate.ContainerSpec}}",
                ]
            )
        )
        if not isinstance(value, dict):
            raise ValueError("invalid backend container specification")
        return value

    @staticmethod
    def container_env(spec: dict) -> dict:
        result = {}
        for value in spec.get("Env", []):
            if not isinstance(value, str) or "=" not in value:
                raise ValueError("invalid backend environment")
            key, value = value.split("=", 1)
            if key in result:
                raise ValueError("duplicate backend environment key")
            result[key] = value
        return result

    def _check_workshop_mounts(self) -> None:
        spec = self.container_spec("workshop")
        environment = self.container_env(spec)
        active = environment.get("BF_MAP_ACTIVE_DIR", "")
        drafts = environment.get("BLOCKFIELD_WORKSHOP_DRAFTS_DIR", "")
        session = environment.get(
            "BLOCKFIELD_WORKSHOP_SESSION_FILE", "/workshop-admin/session.json"
        )
        lock = environment.get("BACKEND_LOCK_DIR", "/backend-lock")
        for value in (active, drafts, session, lock):
            path = Path(value)
            if (
                not path.is_absolute()
                or str(path) != value
                or ".." in path.parts
                or value == "/"
            ):
                raise ValueError(
                    "canonical workshop paths must be explicit and absolute"
                )
        if environment.get("BLOCKFIELD_PROFILE") != "workshop":
            raise ValueError("workshop container profile is not configured")
        required = [
            (
                self.maps.versions.active,
                active,
                True,
                Path(self.map_active_readonly_dir)
                if self.map_active_readonly_dir is not None
                else None,
            ),
            (self.maps.versions.active / "drafts", drafts, False, None),
            (
                self.admin_dir,
                str(Path(session).parent),
                True,
                Path(self.admin_readonly_dir)
                if self.admin_readonly_dir is not None
                else None,
            ),
            (self.lock_dir, lock, False, None),
        ]
        mounts = spec.get("Mounts", [])
        for source, target, readonly, view in required:
            matches = [
                mount
                for mount in mounts
                if isinstance(mount, dict) and mount.get("Target") == target
            ]
            if len(matches) != 1 or matches[0].get("Type") != "bind":
                raise ValueError("canonical workshop bind mounts are missing or unsafe")
            mount = matches[0]
            mount_source = mount.get("Source")
            if mount_source == str(source) and mount.get("ReadOnly", False) is readonly:
                continue
            if readonly and view is not None and mount_source == str(view):
                _check_readonly_view(Path(source), view)
                continue
            raise ValueError("canonical workshop bind mounts are missing or unsafe")
        if Path(session).name != "session.json":
            raise ValueError(
                "canonical workshop state filenames must match controller exports"
            )
        for mount in mounts:
            if (
                not isinstance(mount, dict)
                or not isinstance(mount.get("Source"), str)
                or not isinstance(mount.get("Target"), str)
            ):
                raise ValueError("invalid workshop bind mount")
            source, target = Path(mount["Source"]), Path(mount["Target"])
            export_jobs = getattr(getattr(self.maps, "limbo", None), "jobs", None)
            export_target = environment.get("BLOCKFIELD_WORKSHOP_EXPORT_DIR")
            if source.is_relative_to(
                self.maps.versions.archive
            ) or self.maps.versions.archive.is_relative_to(source):
                if (
                    source == export_jobs
                    and str(target) == export_target
                    and mount.get("Type") == "bind"
                    and mount.get("ReadOnly", False) is False
                ):
                    continue
                raise ValueError("archive storage must not be mounted in Minecraft")
            if target.is_relative_to(active) and str(target) not in {active, drafts}:
                raise ValueError(
                    "extra workshop mount shadows immutable active storage"
                )

    def preflight(self, goal: Goal) -> None:
        if goal.profile == "game":
            raise ValueError("Game lifecycle belongs to the server deployment")
        if self.app_of(goal.profile) not in self.apps:
            raise ValueError(f"profile {goal.profile} has no app in this stage")
        if goal.profile == "test" and self.maps is None:
            raise ValueError("test needs the map candidates store")
        if goal.profile == "test" and hasattr(self.maps, "validate_test"):
            self.maps.validate_test(goal.candidate, goal.modeId)
        if goal.profile == "test" and not any(
            process.goal.profile == "game"
            and process.ready
            and process.goal.release == goal.release
            for process in self.observe()
        ):
            raise ValueError(
                "test release must match the ready Game backend; resume the editor first"
            )
        if self.lock_dir.is_symlink() or not self.lock_dir.is_dir():
            raise FileNotFoundError("backend lock directory missing")
        selected_dir = self.lock_dir / self.app_of(goal.profile)
        if selected_dir.is_symlink():
            raise ValueError("invalid backend lock directory")
        data = self.apps[self.app_of(goal.profile)].data
        if data.is_symlink() or not data.is_dir():
            raise FileNotFoundError(f"data directory for {goal.profile} missing")
        if shutil.disk_usage(data).free < self.min_free_bytes:
            raise OSError("not enough free space for the backend data")
        self.game_image()
        if (
            goal.profile == "workshop"
            and getattr(self.maps, "canonical_sessions", False) is True
        ):
            self._check_workshop_mounts()

    def observe(self) -> list[Process]:
        inventory = self.inventory()
        if inventory["unknown"]:
            raise RuntimeError("full backend process outside the configured apps")
        processes = []
        for app in ("game", "workshop"):
            counts = inventory[app]
            if counts["java"] > 1:
                raise RuntimeError(f"{app} runs several java processes")
            try:
                selection = self.selection(app)
            except (ValueError, TypeError, AttributeError, OSError):
                processes.append(
                    Process(self._goal(app, "unknown", UNKNOWN_SESSION), False)
                )
                continue
            selected = selection["profile"] if selection else app
            if counts["java"] == 0 and counts["tasks"] == 0:
                continue
            ready = self.readiness(app)
            profile = selected if app == self.app_of(selected) else app
            if selection and selection["profile"] == profile:
                goal = self._goal(
                    profile,
                    selection["release"],
                    selection.get("session"),
                    selection.get("candidate"),
                    selection.get("modeId"),
                )
                expected = {
                    key: selection.get(key)
                    for key in ("profile", "generation", "session", "release")
                }
                if profile == "game" and ready and ready.get("release"):
                    # Game images are deployed as :latest by release tags, outside the controller: follow the build.
                    goal = self._goal(
                        profile, ready["release"], selection.get("session")
                    )
                    expected["release"] = ready["release"]
                elif (
                    profile == "workshop"
                    and ready
                    and any(
                        process.goal.profile == "game"
                        and process.ready
                        and process.goal.release == ready["release"]
                        for process in processes
                    )
                ):
                    # A Game rollout can finish between preflight and Workshop start; adopt only the compatible release.
                    goal = self._goal(
                        profile, ready["release"], selection.get("session")
                    )
                    expected["release"] = ready["release"]
            elif (
                selection is None
                and ready
                and ready.get("profile") == profile
                and ready.get("generation") is None
            ):
                # Adoption of a game started before the controller existed.
                goal = self._goal(profile, ready.get("release"), ready.get("session"))
                expected = ready
            else:
                goal, expected = self._goal(profile, "unknown", UNKNOWN_SESSION), None
            processes.append(
                Process(
                    goal,
                    counts["java"] == 1 and expected is not None and ready == expected,
                )
            )
        return processes

    def _container(self, profile: str) -> str | None:
        ids = self.containers(profile)
        if len(ids) > 1:
            raise RuntimeError(f"several {profile} containers")
        return ids[0] if ids else None

    def _rcon(self, profile: str, *command: str) -> str | None:
        """None when the profile runs no java (nothing to drain or save)."""
        profile = self.app_of(profile)
        if profile == "game":
            raise ValueError("Workshop controller cannot issue Game commands")
        if self.inventory(tasks=False)[profile]["java"] == 0:
            return None
        container = self._container(profile)
        if container is None:
            raise RuntimeError(f"{profile} java without a container")
        return self.run(["docker", "exec", container, "rcon-cli", *command])

    @contextmanager
    def workshop_saves_held(self):
        """The controller must stop the workshop before copying world and markup together."""
        if self.inventory()["workshop"]["java"]:
            raise RuntimeError("stop the workshop before taking a consistent snapshot")
        yield

    def save_and_freeze(self, process: Process) -> None:
        if process.goal.profile == "game":
            raise ValueError("Workshop controller cannot save or stop Game")
        result = self._rcon(process.goal.profile, "save-all", "flush")
        if result is not None and "Saved the game" not in result:
            raise RuntimeError(f"save-all did not confirm success: {result}")

    def _scale(self, profile: str, replicas: int) -> None:
        if profile == "game":
            raise ValueError("Workshop controller cannot scale Game")
        self.run(
            [
                "docker",
                "service",
                "scale",
                "--detach",
                f"{self.apps[self.app_of(profile)].service}={replicas}",
            ]
        )

    def _wait_gone(self, profiles: list[str]) -> None:
        deadline = time.monotonic() + self.stop_timeout
        while True:
            inventory = self.inventory()
            if (
                all(inventory[p]["tasks"] == 0 for p in profiles)
                and all(inventory[p]["java"] == 0 for p in profiles)
                and inventory["unknown"] == 0
            ):
                return
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{profiles} still running")
            time.sleep(self.poll)

    def stop(self, process: Process) -> None:
        self._scale(process.goal.profile, 0)
        self._wait_gone([self.app_of(process.goal.profile)])

    def fence_pending_starts(self) -> None:
        others = ["workshop"]
        for profile in others:
            self._scale(profile, 0)
        deadline = time.monotonic() + self.stop_timeout
        while True:
            inventory = self.inventory()
            if all(
                inventory[p]["tasks"] == 0 and inventory[p]["java"] == 0 for p in others
            ):
                return
            if time.monotonic() >= deadline:
                raise TimeoutError(f"cannot fence {others}")
            time.sleep(self.poll)

    def game_image(self) -> str:
        image = self.container_spec("game").get("Image")
        if not isinstance(image, str) or not re.fullmatch(
            r"ghcr\.io/blockfield/blockfield-server(?::[A-Za-z0-9_.-]+)?@sha256:[0-9a-f]{64}",
            image,
        ):
            raise ValueError("Game image must have an immutable server digest")
        return image

    def start(self, goal: Goal) -> None:
        self.preflight(goal)
        data = self.apps[self.app_of(goal.profile)].data
        if (
            goal.profile == "workshop"
            and getattr(self.maps, "canonical_sessions", False) is True
        ):
            self.preflight(goal)
            session = json.loads((self.admin_dir / "session.json").read_bytes()[:65536])
            if (
                not isinstance(session, dict)
                or type(session.get("schema")) is not int
                or session["schema"] != 1
                or session.get("id") != goal.session
            ):
                raise ValueError("workshop session/profile mismatch before start")
            self.maps.prepare_session(
                goal.session, session.get("maps"), resume_only=True
            )
        if goal.profile == "test":
            # start-container.sh builds the playtest copy from this before java starts.
            self.maps.extract(goal.candidate, data / "test-candidate")
        document = {
            "schema": 1,
            "profile": goal.profile,
            "generation": str(uuid4()),
            "session": goal.session,
            "release": goal.release,
        }
        if goal.candidate:
            document["candidate"] = goal.candidate
        if goal.modeId is not None:
            document["modeId"] = goal.modeId
        selected_dir = self.lock_dir / self.app_of(goal.profile)
        selected_dir.mkdir(exist_ok=True)
        atomic_write(
            selected_dir / "profile.json",
            json.dumps(document, sort_keys=True).encode(),
            0o644,
        )
        (data / READY_FILE).unlink(missing_ok=True)
        image = self.game_image()
        if self.container_spec("workshop").get("Image") != image:
            self.run(
                [
                    "docker",
                    "service",
                    "update",
                    "--detach",
                    "--image",
                    image,
                    self.apps["workshop"].service,
                ]
            )
        self._scale(goal.profile, 1)
