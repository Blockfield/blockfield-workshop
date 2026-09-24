"""Shared path/env resolution for the workshop E2E stand harness (blockfield-releases#8, criterion 14).

Portable replacement for the ad-hoc /var/mnt/projects/.bf-accept8/*.py: every machine-specific path
below is an env override instead of being hardcoded to one throwaway worktree.
"""
import os
import subprocess
from pathlib import Path

E2E = Path(__file__).resolve().parent
WORKSHOP = E2E.parent                          # tools/workshop: controller.py, workshopctl.py, journal.py, candidates.py, map_package.py
MODPACK = WORKSHOP.parents[1]                  # the blockfield-modpack repo (or worktree) root
WORKSPACE = Path(os.environ.get('BF_WORKSPACE', MODPACK.parent))
MOD = WORKSPACE / 'BlockField-Private-Project'
RIG = MOD / 'dev' / 'rig'                      # rcon.py, rig.sh, server.sh, agent-process
STAND = Path(os.environ.get(
    'STAND_DIR', Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache')) / 'bf-workshop-stand'))


def owner() -> str:
    value = os.environ.get('CLAUDE_CODE_SESSION_ID') or os.environ.get('BF_RIG_OWNER')
    if not value:
        raise SystemExit('set CLAUDE_CODE_SESSION_ID or BF_RIG_OWNER before running the stand (agent-process needs an owner)')
    return value


def java() -> str:
    value = os.environ.get('JAVA')
    if value:
        return value
    # dev/rig/mise.toml pins the same Temurin 21 the packed server was built/tested against.
    return subprocess.check_output(['mise', 'which', 'java'], cwd=str(RIG), text=True).strip()
