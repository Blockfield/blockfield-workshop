#!/usr/bin/env python3
"""Host CLI for the GAME<->WORKSHOP controller (root: needs the Docker socket).

workshopctl request workshop --session <uuid> [--release vX.Y.Z]
workshopctl request game [--release vX.Y.Z] [--recover]
workshopctl members [draslId:name[:role] ...]
workshopctl step | status      # step applies the site admin inbox first
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from uuid import uuid4

from controller import DEFAULT_MESSAGE, Controller, Goal

BUSY = 75


def parse_member(text: str) -> dict:
    parts = text.split(":")
    if len(parts) not in (2, 3):
        raise ValueError(f"member must be draslId:name[:role], got {text!r}")
    return {"draslId": parts[0], "name": parts[1], "role": parts[2] if len(parts) == 3 else "builder"}


def main(argv=None, backend_factory=None) -> int:
    env = os.environ
    parser = argparse.ArgumentParser(prog="workshopctl", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--state-dir", default=env.get("BF_STATE_DIR", "/srv/minecraft/workshop/controller"))
    parser.add_argument("--gate-file", default=env.get("BF_GATE_FILE", "/srv/minecraft/workshop/gate/gate.json"))
    parser.add_argument("--public-file", default=env.get("BF_PUBLIC_FILE", "/srv/minecraft/workshop/public/public.json"))
    parser.add_argument("--admin-dir", default=env.get("BF_ADMIN_DIR", "/srv/minecraft/workshop/admin"))
    parser.add_argument("--inbox-dir", default=env.get("BF_INBOX_DIR", "/srv/minecraft/workshop/inbox"))
    commands = parser.add_subparsers(dest="command", required=True)
    request = commands.add_parser("request").add_subparsers(dest="profile", required=True)
    workshop = request.add_parser("workshop")
    workshop.add_argument("--session", required=True)
    workshop.add_argument("--release")
    game = request.add_parser("game")
    game.add_argument("--release")
    game.add_argument("--recover", action="store_true", help="leave ERROR toward game: fence, save, stop, start game")
    commands.add_parser("step")
    commands.add_parser("status")
    members = commands.add_parser("members", help="replace the workshop ACL; no arguments clears it")
    members.add_argument("member", nargs="*")
    args = parser.parse_args(argv)

    try:
        if backend_factory is None:
            from docker_backend import DockerBackend
            backend_factory = DockerBackend.from_env
        state_dir = Path(args.state_dir)
        for path in (Path(args.gate_file), Path(args.public_file)):
            path.parent.mkdir(parents=True, exist_ok=True)
        controller = Controller(state_dir, backend_factory(), gate_file=Path(args.gate_file),
                                public_file=Path(args.public_file), message=env.get("BF_MAINTENANCE_MESSAGE", DEFAULT_MESSAGE),
                                admin_dir=Path(args.admin_dir), inbox_dir=Path(args.inbox_dir))
        os.chmod(state_dir, 0o700)
        if args.command == "status":
            result = {"state": controller.status(), "members": controller.members()}
        elif args.command == "members":
            result = {"members": controller.set_members([parse_member(m) for m in args.member])}
        elif args.command == "step":
            try:
                controller.process_inbox()
            except Exception as error:  # the site inbox must never block reconciliation
                print(f"workshopctl: inbox pass failed: {error!r}", file=sys.stderr)
            state = controller.status()
            result = state if state["phase"] == "UNINITIALIZED" else controller.step()
        else:
            state = controller.status()
            release = args.release or (state.get("desired") or {}).get("release")
            if not release:
                raise ValueError("--release is required for the first request")
            goal = Goal("workshop", release, args.session) if args.profile == "workshop" else Goal("game", release)
            recover = getattr(args, "recover", False)
            result = controller.request(str(uuid4()), goal, state["revision"], recover=recover)
            if recover:
                result = controller.step(recover=True)
    except BlockingIOError:
        print("workshopctl: another controller operation holds the lock", file=sys.stderr)
        return BUSY
    except (ValueError, RuntimeError, OSError) as error:
        print(f"workshopctl: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 2 if result.get("phase") == "ERROR" else 0


if __name__ == "__main__":
    sys.exit(main())
