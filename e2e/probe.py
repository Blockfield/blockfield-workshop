"""Send chat commands as a rig client and record the replies: probe.py <client> <out.jsonl> <cmd>..."""

import json
import subprocess
import sys
import time
import os
from paths import RIG, owner

rig = str(RIG / "rig.sh")
env = {**os.environ, "BF_RIG_OWNER": owner(), "BF_RIG_CLIENT": sys.argv[1]}


def bot(*a):
    return subprocess.run(
        [rig, "bot", *a], capture_output=True, text=True, env=env, cwd=str(RIG)
    ).stdout.strip()


with open(sys.argv[2], "a") as out:
    for cmd in sys.argv[3:]:
        if cmd.startswith("sleep "):
            time.sleep(float(cmd[6:]))
            continue
        bot("clearmessages")
        bot("chat", cmd) if not cmd.startswith("@") else bot(*cmd[1:].split())
        time.sleep(2.0)
        try:
            msgs = json.loads(bot("messages"))
        except ValueError:
            msgs = []
        row = {
            "t": round(time.time(), 1),
            "cmd": cmd,
            "reply": [m.replace("§", "")[:220] for m in msgs][-4:],
        }
        out.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(json.dumps(row, ensure_ascii=False))
