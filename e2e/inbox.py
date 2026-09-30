"""Drop one admin inbox request like the site does: inbox.py <kind> key=value..."""

import json
import os
import sys
import time
import uuid
from paths import STAND

st = json.load(open(STAND / "controller/state.json"))
rid = str(uuid.uuid4())
doc = {
    "schema": 1,
    "id": rid,
    "kind": sys.argv[1],
    "actor": {"draslId": "343a227845793a598a3aaa2690a75202", "name": "Player115"},
    "createdAt": int(time.time() * 1000),
}
for kv in sys.argv[2:]:
    k, v = kv.split("=", 1)
    doc[k] = (
        json.loads(v)
        if v[:1] in "[{" or v in ("true", "false", "null") or v.lstrip("-").isdigit()
        else v
    )
if doc["kind"] == "mode":
    doc.setdefault("expectedRevision", st["revision"])
inbox = STAND / "workshop-inbox"
(inbox / f".{rid}.tmp").write_text(json.dumps(doc))
os.rename(inbox / f".{rid}.tmp", inbox / f"{rid}.json")
print(rid)
