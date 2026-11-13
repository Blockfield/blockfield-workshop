#!/usr/bin/env python3
"""Host CLI for the persistent Workshop controller (root: needs the Docker socket).

Backend: --backend docker (host default, needs the Docker socket) or dokploy
(BF_WORKSHOP_BACKEND; approved-tag mechanism, used by the container entry
point and needing no Dokploy token). Container paths come from BF_* env with
host defaults; the deployment/ examples set the container values.

workshopctl request workshop --session <uuid> [--release vX.Y.Z|sha256:<digest>]
workshopctl request game [--release vX.Y.Z|sha256:<digest>] [--recover]
workshopctl target       # last approved release target (kept while Game is off)
workshopctl members [draslId:name[:role[:mapId+mapId...]] ...]
workshopctl step | status | cancel | wake      # step applies the site admin inbox first


Workshop --release is a verified published immutable server release from the
releases catalog (--releases-file, BF_RELEASES_FILE); staging never reads live Game.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
import fcntl
import json
import math
import os
from pathlib import Path
import sys
import time
from uuid import uuid4

# Container runs this file directly under -I (no implicit cwd on sys.path):
# resolve sibling modules from this file's own directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from controller import DEFAULT_MESSAGE, Controller, Goal  # noqa: E402

# Transitional phases worth re-checking right away; stable phases and ERROR wait for the next tick.
FOLLOWED_PHASES = {"REQUESTED", "SAVING", "STARTING"}

BUSY = 75

BACKENDS = ("docker", "dokploy")


def backend_factory_from_name(name: str | None = None, env=os.environ):
    """Backend factory for ``--backend`` / BF_WORKSHOP_BACKEND (default docker).

    The container entrypoint passes dokploy; the host CLI keeps docker unless
    asked. An unknown name fails before any backend is constructed.
    """
    which = name or env.get("BF_WORKSHOP_BACKEND", "docker")
    if which == "dokploy":
        from dokploy_backend import DokployBackend

        return DokployBackend.from_env
    if which == "docker":
        from docker_backend import DockerBackend

        return DockerBackend.from_env
    raise ValueError(f"unknown workshop backend {which!r} (want one of {BACKENDS})")


def build_controller(
    env=os.environ,
    backend_factory=None,
    *,
    state_dir=None,
    gate_file=None,
    public_file=None,
    admin_dir=None,
    inbox_dir=None,
    releases_file=None,
    desired_file=None,
):
    """Shared construction for the CLI and the container entry point."""
    if backend_factory is None:
        backend_factory = backend_factory_from_name(env=env)
    directory = Path(
        state_dir or env.get("BF_STATE_DIR", "/srv/minecraft/workshop/controller")
    )
    gate = Path(
        gate_file or env.get("BF_GATE_FILE", "/srv/minecraft/workshop/gate/gate.json")
    )
    public = Path(
        public_file
        or env.get("BF_PUBLIC_FILE", "/srv/minecraft/workshop/public/public.json")
    )
    for path in (gate, public):
        path.parent.mkdir(parents=True, exist_ok=True)
    backend = backend_factory()
    import releases as release_catalog

    releases_path = (
        Path(releases_file)
        if releases_file
        else Path(env.get("BF_RELEASES_FILE", ""))
        if env.get("BF_RELEASES_FILE", "")
        else directory / "releases.json"
    )
    inbox_retired = env.get("BLOCKFIELD_CP_INGRESS", "").strip() == "1"
    controller = Controller(
        directory,
        backend,
        gate_file=gate,
        public_file=public,
        message=env.get("BF_MAINTENANCE_MESSAGE", DEFAULT_MESSAGE),
        admin_dir=Path(
            admin_dir or env.get("BF_ADMIN_DIR", "/srv/minecraft/workshop/admin")
        ),
        inbox_dir=Path(
            inbox_dir or env.get("BF_INBOX_DIR", "/srv/minecraft/workshop/inbox")
        ),
        start_timeout=float(env.get("BF_START_TIMEOUT_S", 600)),
        idle_sleep_s=float(env.get("BF_WORKSHOP_IDLE_SLEEP_S", 900)),
        maps=_map_store(env, backend),
        workshop_data_dir=Path(
            env.get("BF_WORKSHOP_DATA", "/srv/minecraft/workshop/data")
        ),
        releases=release_catalog.load_catalog(releases_path),
        releases_path=releases_path,
        inbox_retired=inbox_retired,
        desired_file=(
            Path(desired_file)
            if desired_file
            else Path(env.get("BF_DESIRED_FILE", ""))
            if env.get("BF_DESIRED_FILE", "")
            else None
        ),
    )
    if hasattr(backend, "maps"):
        backend.maps = controller.maps
    # Linearize pre-linear history here, under the controller lock: this
    # shared construction is the entry for both the CLI and the container
    # loop, and every map mutation runs under the same lock. Gated on the
    # refs existing so lock-free reads (status) never take the lock.
    branches = (
        controller.maps.versions.archive / "branches"
        if controller.maps is not None
        else None
    )
    if branches is not None and (
        branches.exists()
        or branches.is_symlink()
        or bool(controller.maps.legacy_pending_candidates())
    ):
        with controller._locked():
            try:
                summary = controller.maps.migrate_branches_to_snapshots()
            except ValueError as error:
                raise ValueError(f"map branch migration failed: {error}") from error
        migrated, at_head = summary["migrated"], summary["atHead"]
        drafts = summary.get("draftSnapshots", [])
        superseded = summary.get("candidatesSuperseded", [])
        if migrated or at_head or drafts or superseded:
            print(
                "workshop: linearized map branches: "
                f"{len(migrated)} snapshots, {len(at_head)} already at head,"
                f" {len(drafts)} stale drafts preserved,"
                f" {len(superseded)} pre-upgrade reviews superseded",
                file=sys.stderr,
            )
    os.chmod(directory, 0o700)
    import cp_client

    cp_config = cp_client.CPConfig.from_env(env)
    if inbox_retired and cp_config is None:
        raise ValueError("BLOCKFIELD_CP_INGRESS=1 needs CP agent credentials")
    controller.cp_client = (
        cp_client.CPClient(cp_config, directory) if cp_config is not None else None
    )
    controller.gate_reporter = cp_client.GateReporter.from_env(
        env, base_url=cp_config.base_url if cp_config is not None else None
    )
    controller.cp_renewer = None
    base = (env.get("BLOCKFIELD_CP_URL") or "").strip().rstrip("/")
    tokens = {key: env.get(key) or "" for key in cp_client.RENEWED_KEYS}
    if base and any(value.strip() for value in tokens.values()):
        renewer = cp_client.CredentialRenewer(
            base, directory / "cp_credentials.json", tokens
        )
        for key, holder in (
            ("BLOCKFIELD_CP_AGENT_TOKEN", controller.cp_client),
            ("BLOCKFIELD_CP_GATE_TOKEN", controller.gate_reporter),
        ):
            transport = getattr(holder, "transport", None)
            if transport is not None and renewer.token(key):
                transport.token = renewer.token(key)
                renewer.listeners[key] = lambda token, t=transport: setattr(
                    t, "token", token
                )
        controller.cp_renewer = renewer
    return controller, backend


def parse_member(text: str) -> dict:
    parts = text.split(":")
    if len(parts) not in (2, 3, 4):
        raise ValueError(
            f"member must be draslId:name[:role[:mapId+mapId...]], got {text!r}"
        )
    result = {
        "draslId": parts[0],
        "name": parts[1],
        "role": parts[2] if len(parts) >= 3 else "builder",
    }
    if len(parts) == 4:
        result["maps"] = parts[3].split("+") if parts[3] else []
    return result


def _map_store(env, backend):
    """Map candidates (candidates.py) with the host paths; saving is held through the backend when it can."""
    from candidates import MapStore

    archive = Path(env.get("BF_MAP_ARCHIVE_DIR", "/mnt/media-hdd/blockfield/maps"))
    store = MapStore(
        Path(env.get("BF_WORKSHOP_DATA", "/srv/minecraft/workshop/data")),
        Path(env.get("BF_TEMPLATES_DIR", "/srv/minecraft/templates/maps")),
        Path(
            env.get(
                "BF_PUBLISHED_MAPS_DIR",
                "/srv/minecraft/lobby/blockfield-published/maps",
            )
        ),
        Path(env.get("BF_CANDIDATES_DIR", str(archive / "candidates"))),
        Path(env.get("BF_MAP_BACKUPS_DIR", str(archive / "backups"))),
        repo_maps=Path(
            env.get("BF_GAME_MAPS_DIR", "/srv/minecraft/lobby/blockfield/maps")
        ),
        archive_root=archive,
        active_root=Path(env.get("BF_MAP_ACTIVE_DIR", "/srv/minecraft/maps-active")),
        legacy_candidates=Path(
            env.get("BF_LEGACY_CANDIDATES_DIR", "/srv/minecraft/workshop/candidates")
        ),
        packages=Path(env.get("BF_MAP_PACKAGES_DIR", str(archive / "packages"))),
        hold_saves=getattr(backend, "workshop_saves_held", None),
    )
    if env.get("BF_PROXY_SERVICE"):
        from limbo import LimboArtifacts

        store.limbo = LimboArtifacts(
            Path(env.get("BF_LIMBO_DERIVED_DIR", str(archive / "limbo"))),
            getattr(backend, "proxy_stopped", lambda: False),
        )
    return store


@contextmanager
def _offline(backend):
    if hasattr(backend, "lock_dir"):
        with ExitStack() as stack:
            for profile in ("game", "workshop"):
                lock = stack.enter_context(
                    (backend.lock_dir / profile / "backend.lock").open("a")
                )
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _require_stopped(backend)
            yield
            _require_stopped(backend)
    else:
        _require_stopped(backend)
        yield
        _require_stopped(backend)


def _require_stopped(backend):
    if hasattr(backend, "inventory"):
        inventory = backend.inventory()
        if inventory["unknown"] or any(
            inventory[profile]["tasks"] or inventory[profile]["java"]
            for profile in ("game", "workshop")
        ):
            raise ValueError(
                "offline map import/application requires both backends at zero replicas and no Java"
            )
    elif backend.observe():
        raise ValueError("offline map import/application requires stopped backends")


def _import_map(args, controller, backend):
    from candidates import MapVersions
    from map_package import _json

    world, config_path = Path(args.world).absolute(), Path(args.config).absolute()
    if (
        config_path.is_symlink()
        or not config_path.is_file()
        or config_path.stat().st_size > 4 << 20
    ):
        raise ValueError("unsafe map metadata file")
    config = _json(config_path.read_bytes())
    MapVersions.validate_config(args.map_id, config)
    if config.get("kind") == "lobby":
        if (
            not hasattr(backend, "apps")
            or world != backend.apps["game"].data.absolute() / "world"
        ):
            raise ValueError("lobby import must use the stopped game overworld")
        environment = backend.container_env(backend.container_spec("game"))
        actual_spawn = [
            float(value)
            for value in environment.get(
                "BLOCKFIELD_LOBBY_SPAWN", "-277.5 -40 604.5"
            ).split()
        ]
        spawn = config["mode_data"].get("lobby", {}).get("spawn")
        if (
            len(actual_spawn) != 3
            or not all(math.isfinite(value) for value in actual_spawn)
            or not isinstance(spawn, dict)
            or [spawn.get(key) for key in ("x", "y", "z")] != actual_spawn
        ):
            raise ValueError("lobby metadata spawn must match the real Hub spawn")
        bounds = config["bounds"]
        if (
            not bounds["minX"] <= spawn["x"] <= bounds["maxX"] + 1
            or not bounds["minZ"] <= spawn["z"] <= bounds["maxZ"] + 1
        ):
            raise ValueError("lobby spawn must lie within supplied map bounds")
    with controller._locked(), _offline(backend):
        head = controller.maps.versions.head(args.map_id)
        version = controller.maps.versions.import_template(
            args.map_id, world, config_path
        )
        controller.export_maps()
    return {
        "mapId": args.map_id,
        "kind": config.get("kind", "arena"),
        "versionDigest": version,
        "expectedHead": head["versionDigest"] if head else None,
        "active": False,
        "source": str(world),
    }


def main(argv=None, backend_factory=None) -> int:
    env = os.environ
    parser = argparse.ArgumentParser(
        prog="workshopctl",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--state-dir",
        default=env.get("BF_STATE_DIR", "/srv/minecraft/workshop/controller"),
    )
    parser.add_argument(
        "--gate-file",
        default=env.get("BF_GATE_FILE", "/srv/minecraft/workshop/gate/gate.json"),
    )
    parser.add_argument(
        "--public-file",
        default=env.get("BF_PUBLIC_FILE", "/srv/minecraft/workshop/public/public.json"),
    )
    parser.add_argument(
        "--admin-dir", default=env.get("BF_ADMIN_DIR", "/srv/minecraft/workshop/admin")
    )
    parser.add_argument(
        "--inbox-dir", default=env.get("BF_INBOX_DIR", "/srv/minecraft/workshop/inbox")
    )
    parser.add_argument(
        "--releases-file",
        default=env.get("BF_RELEASES_FILE"),
        help="verified published server releases catalog (JSON); "
        "defaults to <state-dir>/releases.json; missing catalog blocks new selections",
    )
    parser.add_argument(
        "--backend",
        choices=BACKENDS,
        default=env.get("BF_WORKSHOP_BACKEND", "docker"),
        help="lifecycle backend: docker (host default) or dokploy "
        "(approved-tag mechanism, used by the container entry point)",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    request = commands.add_parser("request").add_subparsers(
        dest="profile", required=True
    )
    workshop = request.add_parser("workshop")
    workshop.add_argument("--session", required=True)
    workshop.add_argument("--release")
    workshop.add_argument(
        "--maps",
        nargs="+",
        help="selected canonical map ids; omitted = all on new, previous selection on resume",
    )
    game = request.add_parser("game")
    game.add_argument("--release")
    game.add_argument(
        "--recover",
        action="store_true",
        help="leave Workshop ERROR: fence, save, stop, resume the editor",
    )
    commands.add_parser("step")
    commands.add_parser("status")
    commands.add_parser("target", help="show the last approved Workshop release target")
    commands.add_parser("desired", help="show the controller-owned desired state")
    commands.add_parser(
        "wake", help="start a sleeping Workshop editor again (idempotent)"
    )
    commands.add_parser(
        "cancel", help="unprivileged rollback of a stuck Workshop STARTING transition"
    )
    members = commands.add_parser(
        "members", help="replace the workshop ACL; no arguments clears it"
    )
    members.add_argument("member", nargs="*")
    importer = commands.add_parser(
        "import-map",
        help="offline: record existing Anvil geometry and explicit map metadata in CAS; no activation",
    )
    importer.add_argument("--map-id", required=True)
    importer.add_argument("--world", required=True)
    importer.add_argument("--config", required=True)
    activate = commands.add_parser(
        "activate-map",
        help="activate one imported version with an explicit expected current head",
    )
    activate.add_argument("--map-id", required=True)
    activate.add_argument("--version-digest", required=True)
    activate.add_argument(
        "--expected-head",
        required=True,
        help="exact current SHA256, or none for initial activation",
    )
    limbo_export = commands.add_parser(
        "export-limbo",
        help="export an accepted immutable version through the ready workshop's trusted WorldEdit adapter",
    )
    limbo_export.add_argument("--version-digest", required=True)
    commit = commands.add_parser(
        "commit", help="save the selected editor copy as a linear map commit"
    )
    commit.add_argument("--map-id", required=True)
    commit.add_argument("--working-copy-id", required=True)
    commit.add_argument("--expected-head", required=True)
    commit.add_argument("--message", required=True)
    compare = commands.add_parser("compare", help="compare two immutable map versions")
    compare.add_argument("--map-id", required=True)
    compare.add_argument("--before", required=True)
    compare.add_argument("--after", required=True)
    snap_create = commands.add_parser(
        "snapshot-create", help="pin a named snapshot at an immutable map version"
    )
    snap_create.add_argument("--map-id", required=True)
    snap_create.add_argument("--name", required=True)
    snap_create.add_argument("--version-digest", required=True)
    snap_delete = commands.add_parser(
        "snapshot-delete", help="remove a named map snapshot (history is kept)"
    )
    snap_delete.add_argument("--map-id", required=True)
    snap_delete.add_argument("--name", required=True)
    snap_list = commands.add_parser("snapshots", help="list named snapshots of a map")
    snap_list.add_argument("--map-id", required=True)
    args = parser.parse_args(argv)

    try:
        if backend_factory is None:
            backend_factory = backend_factory_from_name(args.backend, env)
        controller, backend = build_controller(
            env,
            backend_factory,
            state_dir=args.state_dir,
            gate_file=args.gate_file,
            public_file=args.public_file,
            admin_dir=args.admin_dir,
            inbox_dir=args.inbox_dir,
            releases_file=args.releases_file,
        )
        if args.command == "status":
            result = {"state": controller.status(), "members": controller.members()}
        elif args.command == "target":
            result = {"target": controller.approved_target()}
        elif args.command == "desired":
            result = {"desired": controller.desired_state()}
        elif args.command == "members":
            result = {
                "members": controller.set_members(
                    [parse_member(m) for m in args.member]
                )
            }
        elif args.command == "cancel":
            result = controller.cancel()
        elif args.command == "wake":
            result = controller.wake(str(uuid4()))
            if result.get("phase") == "REQUESTED":
                result = {"wake": result, "state": controller.step()}
                deadline = time.monotonic() + float(env.get("BF_STEP_FOLLOW_S", 12))
                while (
                    result["state"].get("phase") in FOLLOWED_PHASES
                    and time.monotonic() < deadline
                ):
                    time.sleep(float(env.get("BF_STEP_POLL_S", 2)))
                    result["state"] = controller.step()
        elif args.command == "import-map":
            result = _import_map(args, controller, backend)
        elif args.command == "export-limbo":
            result = controller.export_limbo(args.version_digest)
        elif args.command == "commit":
            result = controller._snapshot(
                args.map_id,
                {"draslId": "host", "name": "host"},
                args.message,
                working_copy_id=args.working_copy_id,
                expected_head=args.expected_head,
                commit=True,
            )
        elif args.command == "compare":
            with controller._locked():
                result = controller.maps.versions.compare(
                    args.map_id, args.before, args.after
                )
        elif args.command == "snapshot-create":
            with controller._locked():
                result = controller.maps.versions.create_snapshot(
                    args.map_id,
                    args.name,
                    args.version_digest,
                    {"draslId": "host", "name": "host"},
                )
                controller.export_maps()
        elif args.command == "snapshot-delete":
            with controller._locked():
                controller.maps.versions.delete_snapshot(args.map_id, args.name)
                controller.export_maps()
                result = {"mapId": args.map_id, "deleted": args.name}
        elif args.command == "snapshots":
            with controller._locked():
                result = {
                    "mapId": args.map_id,
                    "snapshots": controller.maps.versions.snapshots(args.map_id),
                }
        elif args.command == "activate-map":
            expected = None if args.expected_head == "none" else args.expected_head
            with controller._locked():
                manifest = controller.maps.versions.read(
                    args.map_id, args.version_digest
                )
                if manifest["kind"] in {"lobby", "limbo"}:
                    with _offline(backend):
                        result = controller.maps.activate(
                            args.map_id,
                            args.version_digest,
                            {"draslId": "host", "name": "host"},
                            expected,
                        )
                else:
                    result = controller.maps.activate(
                        args.map_id,
                        args.version_digest,
                        {"draslId": "host", "name": "host"},
                        expected,
                    )
                controller.export_maps()
        elif args.command == "step":
            try:
                controller.process_inbox()
                controller.export_maps()
            except Exception as error:  # the site inbox must never block reconciliation
                print(f"workshopctl: inbox pass failed: {error!r}", file=sys.stderr)
            try:
                cp = getattr(controller, "cp_client", None)
                if cp is not None:
                    cp.sync(controller)
            except Exception as error:  # cp must never block reconciliation
                print(f"workshopctl: cp pass failed: {error!r}", file=sys.stderr)
            result = controller.step()
            # A switch in flight is followed every few seconds inside this run instead of once per 15 s timer tick:
            # the next phase (stopped, ready) is acted on almost at once. Bounded, so the timer stays the clock.
            deadline = time.monotonic() + float(env.get("BF_STEP_FOLLOW_S", 12))
            while (
                result.get("phase") in FOLLOWED_PHASES and time.monotonic() < deadline
            ):
                time.sleep(float(env.get("BF_STEP_POLL_S", 2)))
                result = controller.step()
        else:
            state = controller.status()
            release = args.release or (state.get("desired") or {}).get("release")
            if not release:
                raise ValueError("--release is required for the first request")
            goal = (
                Goal("workshop", release, args.session)
                if args.profile == "workshop"
                else Goal("game", release)
            )
            recover = getattr(args, "recover", False)
            session, archived = None, None
            if args.profile == "workshop":
                previous = controller.session()
                archived = controller.maps.session(goal.session)
                session = (
                    previous
                    if previous and previous.get("id") == goal.session
                    else archived
                    if archived is not None
                    else {
                        "schema": 1,
                        "id": goal.session,
                        "maps": None,
                        "startedAt": int(time.time() * 1000),
                        "startedBy": None,
                    }
                )
                if args.maps is not None:
                    session = {**session, "maps": args.maps}
                session.pop("branches", None)
            result = controller.request(
                str(uuid4()),
                goal,
                state["revision"],
                recover=recover,
                session=session,
                resume_only=archived is not None,
            )
            if recover:
                result = controller.step(recover=True)
    except BlockingIOError:
        print(
            "workshopctl: another controller operation holds the lock", file=sys.stderr
        )
        return BUSY
    except (ValueError, RuntimeError, OSError) as error:
        print(f"workshopctl: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    phase = result.get("phase", (result.get("state") or {}).get("phase"))
    return 2 if phase == "ERROR" else 0


if __name__ == "__main__":
    sys.exit(main())
