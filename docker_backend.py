"""Docker Swarm adapter for the persistent Workshop beside the independently deployed Game.

Only a fixed command allowlist is ever executed (see allowed()). The containers themselves
serialize each app through its own flock and profile in <lock_dir>/<app>/;
this adapter mutates only Workshop, follows Game's immutable image and proves readiness.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from contextlib import contextmanager
from urllib.parse import urlsplit
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import time
from uuid import UUID, uuid4

from controller import AI_PROVIDER_RESERVED_IDS, Goal, Process
from cp_client import (
    DEFAULT_READY_TTL_MS,
    GateReporter,
    StateReporter,
    WorkshopGameClient,
    cp_saves_held,
    parse_game_activity,
    parse_game_desired,
    parse_game_ready,
)
import map_package

# itzg runs `java ... -jar ./fabric-server-mc.1.21.1-loader.X-launcher.Y.jar`.
JAVA_PATTERN = r"fabric-server-mc\..*-launcher\..*\.jar"
# Retained strictly for reading the main Game container until W4 migrates it;
# Workshop readiness/selection use CP state (game-ready / game-desired).
READY_FILE = ".blockfield-ready.json"
UNKNOWN_SESSION = "00000000-0000-0000-0000-000000000000"
SERVICE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}")
CONTAINER = re.compile(r"[0-9a-f]{64}")
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
GAME_REPOSITORY = "ghcr.io/blockfield/blockfield-server"
GAME_DIGEST = re.compile(re.escape(GAME_REPOSITORY) + r"@sha256:[0-9a-f]{64}")
LABEL = "label=com.docker.swarm.service.name="
# Site-managed workshop AI settings (contract: closed schema, regular single-link file).
AI_SITE_SETTINGS_FILE = "workshop-ai-settings.json"
# Eight full registry entries plus settings stay under this bound.
AI_SITE_SETTINGS_MAX_BYTES = 8192
AI_SITE_SETTINGS_REVISION_MAX = 2**53 - 1
# Same shape as the mod and the site: vendor/name with an optional :variant such as :free.
AI_MODEL = re.compile(r"[a-zA-Z0-9._-]{1,64}/[a-zA-Z0-9._/:-]{1,126}")
AI_MODEL_MAX_LEN = 128
# Native ids for custom providers (contract): no vendor prefix required.
AI_NATIVE_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,99}")
AI_NATIVE_MODEL_MAX_LEN = 100
# Controller inbox key files per site provider id (controller `_apply_ai_key`
# writes openrouter.key for "ai-key", gateway.key for "ai-gateway-key").
AI_PROVIDER_KEY_FILES = {
    "openrouter": Path("blockfield-workshop/openrouter.key"),
    "vercel-gateway": Path("blockfield-workshop/gateway.key"),
}
# Provider registry (contract with the site): presets above, custom entries from
# the settings document as {id, label, baseUrl}. Ids exclude dots and slashes,
# so a custom key file name can never escape its directory.
AI_PROVIDER_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
AI_PROVIDERS_MAX = 8
AI_PROVIDER_BASE_URL_MAX_LEN = 200
AI_PROVIDER_MODEL_FIELDS = (
    "generationProvider",
    "transcriptionProvider",
)
# Inbox-managed key: <workshop data>/blockfield-workshop/openrouter.key, 0600 1000:1000.
AI_KEY_PATH = AI_PROVIDER_KEY_FILES["openrouter"]
# Site integer/price limits (workshop-ai-policy.ts); validated only when present
# so pre-provider documents still pass. Unknown keys are ignored, not refused.
AI_SITE_INT_FIELDS = {
    "maxBlocks": (1, 32768),
    "blocksPerTick": (1, 16384),
    "minimumBuildTicks": (40, 1200),
    "applyBudgetMs": (1, 20),
    "hourlyRequestLimit": (1, 1_000_000),
    "hourlyPlayerRequestLimit": (1, 1_000_000),
    "maxHourlyBuilders": (1, 4096),
}
AI_SITE_PRICE_FIELDS = (
    "generationMaxPricePromptUsdPerM",
    "generationMaxPriceCompletionUsdPerM",
)
AI_KEY_MAX_BYTES = 2048
AI_KEY_UID = 1000
AI_KEY_GID = 1000


@dataclass(frozen=True)
class App:
    service: str
    data: Path


def _has_symlink(path: Path) -> bool:
    return any(p.is_symlink() for p in (path, *path.parents))


def _ai_provider_key_file(provider_id: str) -> Path:
    """Inbox key file for any registry id; presets keep their historic names."""
    if provider_id in AI_PROVIDER_KEY_FILES:
        return AI_PROVIDER_KEY_FILES[provider_id]
    return Path("blockfield-workshop") / f"{provider_id}.key"


def _valid_provider_base_url(value) -> bool:
    """Absolute https URL without credentials, query or fragment (contract)."""
    if (
        type(value) is not str
        or not value
        or len(value) > AI_PROVIDER_BASE_URL_MAX_LEN
        or any(char.isspace() for char in value)
    ):
        return False
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    return (
        parts.scheme == "https"
        and bool(parts.hostname)
        and parts.username is None
        and parts.password is None
        and parts.query == ""
        and parts.fragment == ""
    )


def _valid_provider_entry(entry) -> str | None:
    """Custom provider id when the registry entry is valid, else None."""
    if (
        not isinstance(entry, dict)
        or type(entry.get("id")) is not str
        or AI_PROVIDER_ID.fullmatch(entry["id"]) is None
        or entry["id"] in AI_PROVIDER_RESERVED_IDS
        or type(entry.get("label")) is not str
        or not entry.get("label")
        or len(entry["label"]) > AI_PROVIDER_BASE_URL_MAX_LEN
        or not _valid_provider_base_url(entry.get("baseUrl"))
    ):
        return None
    return entry["id"]


def _valid_model(value, custom: bool) -> bool:
    """Presets keep the vendor/name shape; custom providers allow native ids too."""
    if type(value) is not str:
        return False
    if custom:
        return len(value) <= AI_MODEL_MAX_LEN and (
            AI_MODEL.fullmatch(value) is not None
            or (
                len(value) <= AI_NATIVE_MODEL_MAX_LEN
                and AI_NATIVE_MODEL.fullmatch(value) is not None
            )
        )
    return len(value) <= AI_MODEL_MAX_LEN and AI_MODEL.fullmatch(value) is not None


def _ai_referenced_providers(document: dict) -> set[str]:
    """Provider ids needing a key when AI is enabled (cleanup only when set)."""
    default = document.get("provider", "openrouter")
    referenced = set()
    for field in ("generationProvider", "transcriptionProvider"):
        value = document.get(field)
        referenced.add(default if value is None else value)
    if document.get("cleanupModel"):
        cleanup = document.get("cleanupProvider")
        if cleanup is None:
            cleanup = document.get("transcriptionProvider")
        referenced.add(default if cleanup is None else cleanup)
    return referenced


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
        ai_settings_dir: Path | str | None = None,
        ai_settings_readonly_dir: Path | str | None = None,
        ready_ttl_ms: int | None = DEFAULT_READY_TTL_MS,
        now_ms=None,
        state_reporter: StateReporter | None = None,
        game_client: WorkshopGameClient | None = None,
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
        self.ai_settings_dir = (
            Path(ai_settings_dir)
            if ai_settings_dir is not None
            else self.apps["game"].data / "blockfield-admin"
        )
        self.ai_settings_readonly_dir = (
            Path(ai_settings_readonly_dir)
            if ai_settings_readonly_dir is not None
            else None
        )
        # candidates.MapStore (workshopctl wires it): extracts a candidate for the test profile.
        self.maps = None
        self.ready_ttl_ms = ready_ttl_ms
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self.state_reporter = state_reporter
        self.game_client = game_client

    def _require_state_reporter(self) -> StateReporter:
        if self.state_reporter is None:
            raise RuntimeError("CP state reporter is not configured")
        return self.state_reporter

    def _require_game_client(self) -> WorkshopGameClient:
        if self.game_client is None:
            raise RuntimeError("CP Workshop game client is not configured")
        return self.game_client

    def activity(self) -> dict | None:
        if self.state_reporter is None:
            return None
        return parse_game_activity(self.state_reporter.doc("game-activity"))

    @staticmethod
    def app_of(profile: str) -> str:
        """The playtest (test) runs in the workshop app: same data volume, still one backend."""
        return "workshop" if profile == "test" else profile

    @classmethod
    def from_env(cls, env=os.environ) -> "DockerBackend":
        gate = GateReporter.from_env(env)
        state_reporter = (
            StateReporter(gate.base_url, gate.transport) if gate is not None else None
        )
        game_client = (
            WorkshopGameClient.from_env(
                env, base_url=gate.base_url, transport=gate.transport
            )
            if gate is not None
            else None
        )
        ttl_raw = (env.get("BF_WORKSHOP_READY_TTL_S") or "").strip()
        ready_ttl_ms = int(float(ttl_raw) * 1000) if ttl_raw else DEFAULT_READY_TTL_MS
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
            ai_settings_dir=env.get("BF_AI_SETTINGS_DIR") or None,
            ai_settings_readonly_dir=env.get("BF_AI_SETTINGS_READONLY_DIR") or None,
            ready_ttl_ms=ready_ttl_ms,
            state_reporter=state_reporter,
            game_client=game_client,
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
            case ["docker", "inspect", "--format", "{{.Image}}", container]:
                return bool(CONTAINER.fullmatch(container))
            case [
                "docker",
                "image",
                "inspect",
                "--format",
                "{{json .RepoDigests}}",
                image,
            ]:
                return bool(IMAGE_ID.fullmatch(image))
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
        self._require_game_client().command(
            "export-limbo",
            job=job,
            expires_in_s=max(60, min(300, int(self.stop_timeout))),
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
        containers = out.split()
        if any(not CONTAINER.fullmatch(line) for line in containers):
            raise ValueError("invalid container ID in backend inventory")
        return containers

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
        pids = self.java_pids()
        result["unknown"] = 0
        for pid in pids:
            container = self.container_of(pid)
            if container is None:
                continue  # exited between pgrep and /proc read
            profile = owners.get(container)
            if profile is not None:
                result[profile]["java"] += 1
            else:
                result["unknown"] += 1
        return result

    def selection(self, profile: str = "workshop") -> dict | None:
        """Profile the host/CP selected; None before first start. Corrupt raises."""
        app = self.app_of(profile)
        if app == "workshop":
            doc = self._require_state_reporter().doc("game-desired")
            data = parse_game_desired(doc, allowed_profiles={"workshop", "test"})
            if data is None:
                return None
            if data.get("modeId") is not None and (
                data["profile"] != "test"
                or data["modeId"] not in map_package.SUPPORTED_MODES
            ):
                raise ValueError("invalid selected test mode")
            return data
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
        app = self.app_of(profile)
        if app == "workshop":
            doc = self._require_state_reporter().doc("game-ready")
            ready, alive = parse_game_ready(
                doc,
                allowed_profiles={"workshop", "test"},
                now_ms=self._now_ms(),
                ready_ttl_ms=self.ready_ttl_ms,
            )
            return ready if alive else None
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

    def readiness_generation(self, profile: str) -> str | None:
        """Current Workshop generation from CP game-ready; None when absent/stale."""
        ready = self.readiness(profile)
        generation = ready.get("generation") if ready else None
        return generation if isinstance(generation, str) and generation else None

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

    def _check_workshop_mounts(self, spec: dict | None = None) -> None:
        if spec is None:
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

    def _read_ai_site_document(self) -> dict | None:
        """Validated site settings; None when the file is absent. Invalid refuses.

        Known keys are validated (types/shape); unknown keys are ignored so a newer
        site never bricks the gate, and so are the retired guard-model keys
        (guardModel, guardProvider, inputGuardEnabled) an older site still wrote.
        Missing provider/limits fall back to the pre-provider defaults; schema,
        enabled and the generation and transcription model ids are required.
        Never logs values.
        """
        path = self.ai_settings_dir / AI_SITE_SETTINGS_FILE
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return None
        except OSError:
            raise ValueError("unsafe workshop AI site settings") from None
        if (
            _has_symlink(path)
            or not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_size > AI_SITE_SETTINGS_MAX_BYTES
        ):
            raise ValueError("invalid workshop AI site settings")
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        except OSError:
            raise ValueError("unsafe workshop AI site settings") from None
        try:
            metadata = os.fstat(fd)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_size > AI_SITE_SETTINGS_MAX_BYTES
            ):
                raise ValueError("invalid workshop AI site settings")
            contents = os.read(fd, AI_SITE_SETTINGS_MAX_BYTES + 1)
            if len(contents) > AI_SITE_SETTINGS_MAX_BYTES:
                raise ValueError("invalid workshop AI site settings")
            document = json.loads(contents)
        except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
            raise ValueError("invalid workshop AI site settings") from None
        finally:
            os.close(fd)
        if not isinstance(document, dict):
            raise ValueError("invalid workshop AI site settings")
        generation, transcription = (
            document.get("generationModel"),
            document.get("transcriptionModel"),
        )
        providers = document.get("providers", [])
        if type(providers) is not list or len(providers) > AI_PROVIDERS_MAX:
            raise ValueError("invalid workshop AI site settings")
        custom_ids = set()
        for entry in providers:
            provider_id = _valid_provider_entry(entry)
            if provider_id is None or provider_id in custom_ids:
                raise ValueError("invalid workshop AI site settings")
            custom_ids.add(provider_id)
        known_providers = set(AI_PROVIDER_KEY_FILES) | custom_ids
        default = document.get("provider", "openrouter")
        generation_provider = document.get("generationProvider")
        if generation_provider is None:
            generation_provider = default
        transcription_provider = document.get("transcriptionProvider")
        if transcription_provider is None:
            transcription_provider = default
        cleanup_provider = document.get("cleanupProvider")
        if cleanup_provider is None:
            cleanup_provider = transcription_provider
        cleanup = document.get("cleanupModel", "")
        if (
            type(document.get("schema")) is not int
            or document["schema"] != 1
            or type(document.get("revision")) is not int
            or not 0 <= document["revision"] <= AI_SITE_SETTINGS_REVISION_MAX
            or type(document.get("enabled")) is not bool
            or document.get("provider", "openrouter") not in known_providers
            or not _valid_model(generation, generation_provider in custom_ids)
            or not _valid_model(transcription, transcription_provider in custom_ids)
            or any(
                document.get(field) is not None
                and document.get(field) not in known_providers
                for field in AI_PROVIDER_MODEL_FIELDS
            )
            or (
                type(cleanup) is not str
                or (
                    cleanup
                    and not _valid_model(cleanup, cleanup_provider in custom_ids)
                )
            )
            or (
                document.get("cleanupProvider") is not None
                and document.get("cleanupProvider") not in known_providers
            )
        ):
            raise ValueError("invalid workshop AI site settings")
        for field, (low, high) in AI_SITE_INT_FIELDS.items():
            value = document.get(field)
            if value is not None and (
                type(value) is not int or not low <= value <= high
            ):
                raise ValueError("invalid workshop AI site settings")
        for field in AI_SITE_PRICE_FIELDS:
            value = document.get(field)
            if value is not None and (
                type(value) not in (int, float) or not 0 <= value <= 1000
            ):
                raise ValueError("invalid workshop AI site settings")
        return document

    def _ai_site_enabled(self) -> bool:
        """Site settings enabled flag; missing file means host fallback. Invalid refuses."""
        document = self._read_ai_site_document()
        return document is not None and document["enabled"]

    def _has_ai_key_file(self, filename: Path | str = AI_KEY_PATH) -> bool:
        """Inbox-managed key presence; validated by metadata only, content never read."""
        path = self.apps["workshop"].data / filename
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return False
        except OSError:
            raise ValueError("unsafe workshop AI key file") from None
        if (
            _has_symlink(path)
            or not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_size > AI_KEY_MAX_BYTES
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != AI_KEY_UID
            or info.st_gid != AI_KEY_GID
        ):
            raise ValueError("unsafe workshop AI key file")
        return True

    def _check_workshop_ai(self, spec: dict) -> None:
        environment = self.container_env(spec)
        env_key = bool(
            environment.get("BLOCKFIELD_WORKSHOP_AI_OPENROUTER_KEY", "").strip()
        )
        host_enabled = False
        config = self.apps["workshop"].data / "config/blockfield/workshop-ai.json"
        try:
            fd = os.open(config, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        except FileNotFoundError:
            pass
        except OSError:
            raise ValueError("unsafe workshop AI configuration") from None
        else:
            try:
                metadata = os.fstat(fd)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 32768:
                    raise ValueError("unsafe workshop AI configuration")
                contents = os.read(fd, 32769)
                if len(contents) > 32768:
                    raise ValueError("unsafe workshop AI configuration")
                document = json.loads(contents)
                if (
                    not isinstance(document, dict)
                    or type(document.get("enabled", False)) is not bool
                ):
                    raise ValueError("invalid workshop AI configuration")
            except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
                raise ValueError("invalid workshop AI configuration") from None
            finally:
                os.close(fd)
            host_enabled = document.get("enabled", False)
        site_document = self._read_ai_site_document()
        site_enabled = site_document is not None and site_document["enabled"]
        known_ids = set(AI_PROVIDER_KEY_FILES)
        if site_document is not None:
            known_ids |= {entry["id"] for entry in site_document.get("providers", [])}
        referenced = (
            _ai_referenced_providers(site_document)
            if site_document is not None
            else {"openrouter"}
        )
        # Any known key file arms the strict checks; every referenced provider
        # needs its own key (or the matching env credential). All known files
        # are validated independently: a present-but-unsafe key for an
        # unreferenced provider must still fail closed.
        key_present = {
            pid: self._has_ai_key_file(_ai_provider_key_file(pid)) for pid in known_ids
        }
        provider_keys_ready = all(
            key_present[pid] or (env_key and pid == "openrouter") for pid in referenced
        )
        any_key = any(key_present.values())
        if not (host_enabled or site_enabled or any_key or env_key):
            return
        if environment.get("BLOCKFIELD_PROFILE") != "workshop":
            raise ValueError("workshop container profile is not configured")
        if (host_enabled or site_enabled) and not provider_keys_ready:
            raise ValueError("enabled workshop AI needs its own server key")
        if (
            environment.get(
                "BLOCKFIELD_WORKSHOP_AI_LIMITS_FILE", "/blockfield-admin/ai-limits.json"
            )
            != "/blockfield-admin/ai-limits.json"
            or environment.get(
                "BLOCKFIELD_WORKSHOP_AI_USAGE_FILE",
                "",
            )
            != "/data/blockfield-workshop/ai-usage.json"
        ):
            raise ValueError(
                "workshop AI needs the canonical policy and persistent usage paths"
            )
        usage = self.apps["workshop"].data / "blockfield-workshop/ai-usage.json"
        for path in (
            self.ai_settings_dir,
            self.apps["workshop"].data,
            config,
            usage,
            usage.with_suffix(".json.lock"),
        ):
            if (
                not path.is_absolute()
                or str(path) == "/"
                or str(path).startswith("//")
                or str(path) != os.path.normpath(str(path))
                or _has_symlink(path)
            ):
                raise ValueError("unsafe workshop AI host paths")
        if not self.ai_settings_dir.is_dir():
            raise ValueError("workshop AI site settings directory is missing")
        workshop_data, game_data = (
            self.apps[app].data.resolve() for app in ("workshop", "game")
        )
        # Nested data would put the ledger inside Game's writable /data (or the reverse).
        if (
            os.path.samefile(workshop_data, game_data)
            or workshop_data.is_relative_to(game_data)
            or game_data.is_relative_to(workshop_data)
        ):
            raise ValueError("workshop AI usage must have its own data directory")
        mounts = spec.get("Mounts", [])
        required = (
            (self.apps["workshop"].data, "/data", False, None),
            (
                self.ai_settings_dir,
                "/blockfield-admin",
                True,
                self.ai_settings_readonly_dir,
            ),
        )
        for source, target, readonly, view in required:
            matches = [
                mount
                for mount in mounts
                if isinstance(mount, dict) and mount.get("Target") == target
            ]
            if len(matches) != 1 or matches[0].get("Type") != "bind":
                raise ValueError(
                    "workshop AI needs its own persistent data and readonly site settings mounts"
                )
            mount = matches[0]
            if (
                mount.get("Source") == str(source)
                and mount.get("ReadOnly", False) is readonly
            ):
                continue
            if readonly and view is not None and mount.get("Source") == str(view):
                _check_readonly_view(source, view)
                continue
            raise ValueError("unsafe workshop AI data or policy mount")
        protected = (Path("/blockfield-admin"), Path("/data/blockfield-workshop"))
        configurations = (
            Path("/data/config/blockfield/workshop-ai.json"),
            Path("/config/config/blockfield/workshop-ai.json"),
        )
        targets = []
        try:
            policy_sources = {
                (metadata.st_dev, metadata.st_ino)
                for metadata in (
                    path.stat()
                    for path in (self.ai_settings_dir, *self.ai_settings_dir.parents)
                )
            }
            policy = self.ai_settings_dir / "ai-limits.json"
            try:
                metadata = policy.lstat()
            except FileNotFoundError:
                metadata = None
            if metadata is not None:
                # A hardlink in any writable directory bypasses the policy mount's RO flag.
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    raise ValueError(
                        "workshop AI policy must be regular without symlinks or hardlinks"
                    )
                policy_sources.add((metadata.st_dev, metadata.st_ino))
        except OSError:
            raise ValueError("cannot verify workshop AI policy source") from None
        for mount in mounts:
            if not isinstance(mount, dict) or not isinstance(mount.get("Target"), str):
                raise ValueError("invalid workshop AI mount")
            if mount.get("Type") == "bind" and mount.get("ReadOnly", False) is not True:
                if not isinstance(mount.get("Source"), str):
                    raise ValueError("invalid workshop AI writable bind source")
                source = Path(mount["Source"])
                if (
                    not source.is_absolute()
                    or str(source) != mount["Source"]
                    or ".." in source.parts
                    or str(source).startswith("//")
                ):
                    raise ValueError("invalid workshop AI writable bind source")
                try:
                    metadata = source.stat()
                    if (metadata.st_dev, metadata.st_ino) in policy_sources and not (
                        os.statvfs(source).f_flag & os.ST_RDONLY
                    ):
                        raise ValueError("writable workshop bind exposes AI policy")
                except OSError:
                    raise ValueError(
                        "cannot verify workshop AI writable bind source"
                    ) from None
            elif (
                mount.get("Type") == "volume"
                and mount.get("ReadOnly", False) is not True
            ):
                # A local volume with type=none,o=bind,device=<host path> is a writable host bind.
                volume = mount.get("VolumeOptions")
                driver = (
                    volume.get("DriverConfig") if isinstance(volume, dict) else None
                )
                options = driver.get("Options") if isinstance(driver, dict) else None
                flags = (
                    str(options.get("o", "")).split(",")
                    if isinstance(options, dict)
                    else []
                )
                # With bind/rbind the kernel ignores the fs type, and a later rw undoes ro.
                if (
                    isinstance(options, dict)
                    and {"bind", "rbind"} & set(flags)
                    and [f for f in flags if f in ("ro", "rw")][-1:] != ["ro"]
                    and isinstance(options.get("device"), str)
                ):
                    device = Path(options["device"])
                    if (
                        not device.is_absolute()
                        or str(device) != options["device"]
                        or ".." in device.parts
                        or str(device).startswith("//")
                    ):
                        raise ValueError("invalid workshop AI writable bind source")
                    try:
                        metadata = device.stat()
                        if (
                            metadata.st_dev,
                            metadata.st_ino,
                        ) in policy_sources and not (
                            os.statvfs(device).f_flag & os.ST_RDONLY
                        ):
                            raise ValueError("writable workshop bind exposes AI policy")
                    except OSError:
                        raise ValueError(
                            "cannot verify workshop AI writable bind source"
                        ) from None
            if mount["Target"] not in {"/data", "/blockfield-admin"}:
                targets.append(mount["Target"])
        # Swarm configs/secrets can shadow files without appearing in Mounts.
        for entry in (*spec.get("Configs", []), *spec.get("Secrets", [])):
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("File"), dict)
                or not isinstance(entry["File"].get("Name"), str)
            ):
                raise ValueError("invalid workshop AI file mount")
            targets.append("/" + entry["File"]["Name"].lstrip("/"))
        for value in targets:
            target = Path(value)
            if (
                not target.is_absolute()
                or value.startswith("//")
                or str(target) != value
                or ".." in target.parts
                or any(
                    root.is_relative_to(target) or target.is_relative_to(root)
                    for root in protected
                )
                or any(
                    path.is_relative_to(target) or target.is_relative_to(path)
                    for path in configurations
                )
            ):
                raise ValueError(
                    "extra workshop mount shadows AI configuration, policy or usage"
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
        if self.app_of(goal.profile) == "workshop":
            spec = self.container_spec("workshop")
            # The playtest runs the same Workshop service, so the same AI key/settings apply.
            self._check_workshop_ai(spec)
            if (
                goal.profile == "workshop"
                and getattr(self.maps, "canonical_sessions", False) is True
            ):
                self._check_workshop_mounts(spec)

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

    @contextmanager
    def workshop_saves_held(self):
        """Hot snapshot: chunks are flushed and autosave is off via CP agent ops."""
        if self.inventory(tasks=False)["workshop"]["java"] == 0:
            yield
            return
        with cp_saves_held(self._require_game_client()):
            yield

    def _flush(self, profile: str) -> None:
        if self.app_of(profile) == "game":
            raise ValueError("Workshop controller cannot issue Game commands")
        if self.inventory(tasks=False)["workshop"]["java"] == 0:
            return
        self._require_game_client().command("save-flush")

    def save_and_freeze(self, process: Process) -> None:
        if process.goal.profile == "game":
            raise ValueError("Workshop controller cannot save or stop Game")
        self._flush(process.goal.profile)

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
        # Game rolls out as :latest outside the controller, so the running container is the only proof of its build.
        containers = self.containers("game")
        if len(containers) != 1:
            raise RuntimeError("Workshop needs exactly one running Game container")
        image_id = self.run(
            ["docker", "inspect", "--format", "{{.Image}}", containers[0]]
        ).strip()
        if not IMAGE_ID.fullmatch(image_id):
            raise ValueError("invalid Game image ID")
        digests = json.loads(
            self.run(
                [
                    "docker",
                    "image",
                    "inspect",
                    "--format",
                    "{{json .RepoDigests}}",
                    image_id,
                ]
            )
        )
        if not isinstance(digests, list):
            raise ValueError("Game image has no repository digests")
        # Foreign repositories are ignored; anything but one server digest is ambiguous.
        images = [
            digest
            for digest in digests
            if isinstance(digest, str) and GAME_DIGEST.fullmatch(digest)
        ]
        if len(images) != 1:
            raise ValueError("Game must run exactly one immutable server digest")
        return images[0]

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
        self._require_state_reporter().put("game-desired", document)
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
