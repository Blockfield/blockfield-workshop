"""Explain the W01 provisioning stop without mistaking it for a scope proof."""

import argparse
import hashlib
import json
import re
from pathlib import Path

EVIDENCE = Path(__file__).with_name("observed-2026-10-29.json")
PENDING = (
    "direct scoped-token observe and real-task immutable digest identity",
    "direct scoped-token deploy, start, stop and restart",
    "same-org canary, foreign-org, controller and unknown application denials",
    "arbitrary registry/image, mutable tag and extra-field denials",
    "generic update/provider, commands/build and service create/delete denials",
    "env/secret, mount/volume and registry read/write denials",
    "terminal/exec, schedules, compose and identity/role denials",
    "server/settings denial on a disposable installation",
    "before/after hashes, audit records and restored dummy state",
    "duplicate/stale operations, changed replay and delayed success",
    "concurrent deploy/stop, stale start, queue fence and manager restart",
    "lifetime writer lock and crash boundaries through READY",
    "API outage with no alternate actuator",
    "separately authorized Workshop/Game state, rollback, data and RO matrix",
)


def permission_collisions(routes):
    """The same permission and app predicate cannot separate these two calls."""
    required = set(routes["application.saveDockerProvider"])
    forbidden = set(routes["application.update"])
    return sorted(required & forbidden)


def verify_sources(root, evidence):
    """Read only explicitly downloaded pinned source; never fetch at import/run."""
    for entry in evidence["source_model"]["files"]:
        content = (root / Path(entry["path"]).name).read_bytes()
        if hashlib.sha256(content).hexdigest() != entry["sha256"]:
            raise ValueError("pinned source checksum mismatch")
    application = (root / "application.ts").read_text(encoding="utf-8")
    for name in ("saveDockerProvider", "update"):
        match = re.search(rf"^\t{name}:.*?(?=^\t\w+:|\Z)", application, re.M | re.S)
        if not match or not re.search(
            r"checkServicePermissionAndAccess\(ctx, input.applicationId,"
            r'\s*\{\s*service: \["create"\]',
            match[0],
        ):
            raise ValueError("pinned route permission assertion failed")


def assess(evidence):
    if evidence.get("schema") != 1 or evidence.get("kind") != "dokploy-scope-preflight":
        raise ValueError("expected W01 preflight evidence")
    observations = evidence["observations"]
    result = observations["custom_role_result"]
    # This response belongs to the provisioning identity, not a test identity.
    denied = result.get("success") is False and "status code 403" in result.get(
        "details", ""
    )
    name = observations["custom_role_request"]["roleName"]
    absent = all(
        role.get("role") != name and role.get("roleName") != name
        for role in observations["custom_roles_after"]
    )
    collisions = permission_collisions(evidence["source_model"]["routes"])
    # No preflight input can admit production. Runtime proof is a separate gate.
    return {
        "state": "BLOCKED",
        "production_admission": False,
        "direct_scope_proven": False,
        "custom_role_provisioning_denied": denied,
        "custom_role_absent_after_attempt": absent,
        "version_endpoints_agree": observations["version"]
        == observations["release_tag"],
        "installed_build_verified": evidence["source_model"]["installed_build_verified"]
        is True,
        "pinned_source_has_permission_collision": bool(collisions),
        "pin_matches_installed_build": False,
        "direct_scope_cases_run": len(evidence["direct_scope_cases"]),
        "pending": list(PENDING),
        "outstanding_resource_count": len(evidence["cleanup"]["outstanding_resources"]),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, default=EVIDENCE)
    parser.add_argument("--source-root", type=Path)
    args = parser.parse_args(argv)
    try:
        evidence = json.loads(args.evidence.read_text(encoding="utf-8"))
        if args.source_root:
            verify_sources(args.source_root, evidence)
        report = assess(evidence)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        # Exception text/inputs can contain credentials; emit only fixed text.
        print(json.dumps({"state": "INVALID", "production_admission": False}))
        return 1
    print(json.dumps(report, indent=2))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
