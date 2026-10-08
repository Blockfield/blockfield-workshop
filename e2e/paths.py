"""Shared path/env resolution for the workshop E2E stand harness (blockfield-releases#8, criterion 14).

Portable replacement for the ad-hoc /var/mnt/projects/.bf-accept8/*.py: every machine-specific path
below is an env override instead of being hardcoded to one throwaway worktree.
"""

import os
import shutil
from pathlib import Path

E2E = Path(__file__).resolve().parent
WORKSHOP = E2E.parent
REPOSITORY = WORKSHOP
WORKSPACE = Path(
    os.environ.get("BF_WORKSPACE", REPOSITORY.parent / "blockfield-workspace")
)
MOD = WORKSPACE / "localmods" / "blockfield-mod"
RIG = MOD / "dev" / "rig"  # rcon.py, rig.sh, server.sh, agent-process
STAND = Path(
    os.environ.get(
        "STAND_DIR",
        Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
        / "bf-workshop-stand",
    )
)


def owner() -> str:
    value = next(
        (
            os.environ[key]
            for key in (
                "AI_AGENT_SESSION_ID",
                "CODEX_THREAD_ID",
                "CODEX_SESSION_ID",
                "CLAUDE_SESSION_ID",
                "GEMINI_SESSION_ID",
                "OPENCODE_SESSION_ID",
                "CLAUDE_CODE_SESSION_ID",
            )
            if os.environ.get(key)
        ),
        None,
    )
    requested = os.environ.get("BF_RIG_OWNER")
    if value and requested and requested != value:
        raise SystemExit("BF_RIG_OWNER does not match the current agent session")
    value = value or requested
    if not value:
        raise SystemExit(
            "set a native agent session ID or BF_RIG_OWNER before running the stand"
        )
    return value


def java() -> str:
    value = os.environ.get("JAVA")
    if value:
        return value
    executable = shutil.which(
        "java",
        path=os.pathsep.join(
            [
                str(Path(os.environ["JAVA_HOME"]) / "bin")
                if "JAVA_HOME" in os.environ
                else "",
                os.environ.get("PATH", ""),
            ]
        ),
    )
    if not executable:
        raise SystemExit("Install native JDK 21 and set JAVA_HOME or PATH")
    return executable
